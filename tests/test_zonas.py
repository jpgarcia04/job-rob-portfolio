"""
Las zonas de Texas y el recorte de lo que ya estaba guardado (24 sept 2026).

La clienta pidió dejar de ver Texas entero y quedarse con unas zonas concretas
en un mapa: Houston, Austin, San Antonio, y el Valle con el sur. Aquí se prueba
contra el YAML REAL, a propósito: lo que tiene que ser cierto es que la
configuración que se envía acepta lo suyo y rechaza lo demás, y un YAML de
mentira probaría el código sin probar la lista de condados.

Tres cosas, y las tres tienen un fallo concreto detrás:

  · el filtro: región Y zona, condado antes que ciudad. «Big Spring, Howard
    County» entraba en Houston por la palabra «spring» en el primer simulacro.
  · el recorte: borra lo de fuera, pero no lo que ella marcó, y no deja
    vacantes huérfanas cuando el anuncio que se borra era el representante.
  · la bandeja: la fila de zonas cuenta y filtra lo mismo.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from recolector.config import ConfigError, _parse_geo_filter, load_config
from recolector.connectors.adzuna import desglosar_area
from recolector.geo import ACCEPTED, REJECTED, UNRESOLVED, classify_location
from recolector.models import JobOffer
from recolector.recorte import ARCHIVO, planificar, recortar_si_hace_falta
from recolector.storage import Storage


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def texas(config):
    return config.market("texas_us")


def juzgar(market, *, region=None, county=None, city=None, raw=None):
    return classify_location(market, region=region, county=county, city=city, raw=raw)


# ---------------------------------------------------------------------------
#  El filtro
# ---------------------------------------------------------------------------

class TestFiltroPorZona:
    def test_las_cuatro_zonas_que_pidio(self, texas):
        assert [z.etiqueta for z in texas.geo_filter.zonas] == [
            "Houston", "Austin", "San Antonio", "Valle y sur de Texas",
        ]

    @pytest.mark.parametrize("county, city, zona", [
        ("Harris County", "Houston", "houston"),
        ("Travis County", "Austin", "austin"),
        ("Bexar County", "San Antonio", "san_antonio"),
        ("Cameron County", "Brownsville", "sur"),
        ("Hidalgo County", "McAllen", "sur"),
        ("Webb County", "Laredo", "sur"),
        ("Nueces County", "Corpus Christi", "sur"),
    ])
    def test_las_ciudades_que_nombro_caen_en_su_zona(self, texas, county, city, zona):
        d = juzgar(texas, region="Texas", county=county, city=city,
                   raw=f"{city}, {county}")
        assert (d.status, d.zona) == (ACCEPTED, zona)

    def test_el_barrio_de_houston_entra_por_el_condado(self, texas):
        """Con cinco niveles el texto visible ya no lleva el condado: 28 de los
        392 anuncios reales eran así, todos de Houston."""
        d = juzgar(texas, region="Texas", county="Harris County",
                   city="Audubon Place", raw="Audubon Place, Houston")
        assert (d.status, d.zona) == (ACCEPTED, "houston")

    def test_dallas_es_texas_pero_no_su_zona(self, texas):
        d = juzgar(texas, region="Texas", city="Highland Park",
                   raw="Highland Park, Dallas")
        assert d.status == REJECTED
        assert "fuera de las zonas" in d.reason

    def test_big_spring_no_entra_por_la_palabra_spring(self, texas):
        """El fallo que salió en el simulacro: el condado manda, y si está
        fuera, la ciudad ni se mira."""
        d = juzgar(texas, region="Texas", city="Big Spring",
                   raw="Big Spring, Howard County")
        assert d.status == REJECTED

    def test_si_hay_condado_la_ciudad_no_se_mira(self, texas):
        """La REGLA, no la lista. La prueba de arriba pasa también porque
        «spring» ya no está en el YAML; esta pone la palabra a propósito y
        comprueba que, con un condado de fuera, no entra igualmente. Sin la
        regla, cualquier ciudad corta que alguien añada un día reabre el
        agujero sin que nada avise."""
        import dataclasses

        geo = _parse_geo_filter({
            "mode": "allow_regions", "regions": ["texas", "tx"],
            "zonas": [{"clave": "h", "etiqueta": "H",
                       "condados": ["Harris County"], "ciudades": ["spring"]}],
        }, "prueba")
        con_spring = dataclasses.replace(texas, geo_filter=geo)
        assert juzgar(con_spring, region="Texas", city="Big Spring",
                      raw="Big Spring, Howard County").status == REJECTED
        # Y sin condado, la ciudad sí vale: la regla no mata el plan B.
        assert juzgar(con_spring, region="TX", city="Spring",
                      raw="Spring, TX").zona == "h"

    def test_el_condado_de_austin_es_de_houston(self, texas):
        """Bellville, Austin County, está al oeste de Houston. La ciudad de
        Austin es otro sitio (Travis)."""
        d = juzgar(texas, region="Texas", county="Austin County",
                   city="Bellville", raw="Bellville, Austin County")
        assert d.zona == "houston"

    def test_el_condado_como_ciudad_cuenta_como_condado(self, texas):
        """Con tres niveles Adzuna pone el condado en el sitio de la ciudad."""
        d = juzgar(texas, region="Texas", city="Travis County", raw="Travis County")
        assert d.zona == "austin"

    def test_sin_condado_vale_la_ciudad(self, texas):
        """Jooble no trae condado: «Mission, TX» entra por su ciudad."""
        d = juzgar(texas, region="TX", city="Mission", raw="Mission, TX")
        assert (d.status, d.zona) == (ACCEPTED, "sur")

    @pytest.mark.parametrize("region, city", [
        ("Tennessee", "Brownsville"),
        ("Kansas", "Mission"),
        ("Minnesota", "Austin"),
        ("California", "San Benito"),
    ])
    def test_una_ciudad_homonima_de_otro_estado_no_entra(self, texas, region, city):
        d = juzgar(texas, region=region, city=city, raw=f"{city}, {region}")
        assert d.status == REJECTED

    def test_texas_a_secas_es_indeterminado_y_se_descarta(self, texas):
        """Se sabe el estado pero no dónde: puede ser Dallas. Se cuenta aparte
        en el reporte, y con `drop` no entra."""
        d = juzgar(texas, region="Texas", raw="Texas, US")
        assert d.status == UNRESOLVED
        assert texas.geo_filter.unresolved_policy == "drop"

    def test_remoto_nacional_sigue_indeterminado(self, texas):
        assert juzgar(texas, raw="United States").status == UNRESOLVED

    def test_mexico_no_cambia(self, config):
        d = juzgar(config.market("mexico"), region="Nuevo León",
                   city="Monterrey", raw="Monterrey, Nuevo León")
        assert (d.status, d.zona) == (ACCEPTED, None)


class TestDesglosarArea:
    @pytest.mark.parametrize("area, esperado", [
        (["US", "Texas"], ("US", "Texas", None, None)),
        (["US", "Texas", "Travis County"], ("US", "Texas", None, "Travis County")),
        (["US", "Texas", "Harris County", "Houston"],
         ("US", "Texas", "Harris County", "Houston")),
        (["US", "Texas", "Harris County", "Houston", "Audubon Place"],
         ("US", "Texas", "Harris County", "Audubon Place")),
        (None, (None, None, None, None)),
    ])
    def test_niveles(self, area, esperado):
        assert desglosar_area(area) == esperado


class TestValidacionDeZonas:
    def base(self, **extra):
        datos = {
            "mode": "allow_regions",
            "regions": ["texas"],
            "zonas": [
                {"clave": "a", "etiqueta": "A", "condados": ["Harris County"]},
                {"clave": "b", "etiqueta": "B", "condados": ["Travis County"]},
            ],
        }
        datos.update(extra)
        return datos

    def test_una_configuracion_buena_se_lee(self):
        geo = _parse_geo_filter(self.base(), "prueba")
        assert [z.clave for z in geo.zonas] == ["a", "b"]

    def test_un_condado_en_dos_zonas_no_arranca(self):
        datos = self.base()
        datos["zonas"][1]["condados"].append("harris county")
        with pytest.raises(ConfigError, match="ya está en la zona 'a'"):
            _parse_geo_filter(datos, "prueba")

    def test_zonas_sin_region_no_arranca(self):
        with pytest.raises(ConfigError, match="región"):
            _parse_geo_filter(self.base(regions=[]), "prueba")

    def test_zonas_con_ciudades_sueltas_no_arranca(self):
        with pytest.raises(ConfigError, match="DENTRO de cada zona"):
            _parse_geo_filter(self.base(cities=["dallas"]), "prueba")

    @pytest.mark.parametrize("clave", ["todas", "Houston", "sur de texas", ""])
    def test_claves_que_no_valen(self, clave):
        datos = self.base()
        datos["zonas"][0]["clave"] = clave
        with pytest.raises(ConfigError):
            _parse_geo_filter(datos, "prueba")


# ---------------------------------------------------------------------------
#  El recorte de la base
# ---------------------------------------------------------------------------

def _oferta(clave: str, area: list[str], estado: str = "nueva") -> tuple[JobOffer, str]:
    """Una oferta como la guarda el conector de Adzuna, con su texto visible
    construido como lo construye Adzuna: «ciudad, condado», salvo con cinco
    niveles, que es «barrio, ciudad» y ya no lleva el condado."""
    _, region, county, city = desglosar_area(area)
    if len(area) >= 5:
        visible = f"{area[4]}, {area[3]}"
    elif len(area) == 4:
        visible = f"{area[3]}, {area[2]}"
    else:
        visible = area[-1]
    ofe = JobOffer(
        source="adzuna", market_key="texas_us", query_label="COO + Manufacturing",
        external_id=clave, title=f"COO {clave}", company="Acme",
        location_raw=visible,
        location_city=city, location_region=region, location_country="US",
        url=f"https://example.test/{clave}", description="",
        location_county=county,
        raw={"id": clave, "location": {"area": area}},
    )
    ofe.dedupe_key = clave
    ofe.loose_key = clave
    return ofe, estado


@pytest.fixture()
def base(tmp_path):
    """Una base con lo que hay que decidir, sin zona puesta (como la vieja)."""
    almacen = Storage(tmp_path / "ofertas.db")
    run = almacen.start_run("p", ["adzuna"], ["texas_us"])
    for ofe, estado in (
        _oferta("houston", ["US", "Texas", "Harris County", "Houston"]),
        _oferta("barrio", ["US", "Texas", "Harris County", "Houston", "Audubon Place"]),
        _oferta("valle", ["US", "Texas", "Cameron County", "Brownsville"]),
        _oferta("dallas", ["US", "Texas", "Dallas", "Irving"]),
        _oferta("fortworth", ["US", "Texas", "Tarrant County", "Fort Worth"]),
        _oferta("bigspring", ["US", "Texas", "Howard County", "Big Spring"]),
        _oferta("solo_texas", ["US", "Texas"]),
        _oferta("postulada", ["US", "Texas", "Dallas", "Plano"], "postulada"),
        _oferta("interesa", ["US", "Texas", "Collin County", "Frisco"], "interesa"),
    ):
        almacen.save_offer(ofe, run)
        if estado != "nueva":
            almacen.set_status(ofe.dedupe_key, estado)
    con = almacen.connection
    con.execute("UPDATE offers SET zona = NULL")

    # UNA VACANTE PUBLICADA EN DALLAS Y EN HOUSTON: se leyó el anuncio de
    # Dallas y el de Houston copia su veredicto.
    for clave, padre in (("dallas", None), ("houston", "dallas")):
        con.execute(
            "INSERT INTO clasificaciones (dedupe_key, estado, relevante, modelo, "
            "fichas_huella, clasificado_at, heredada_de, tokens_salida) "
            "VALUES (?, 'ok', 1, 'm', 'h', '2026-09-20', ?, ?)",
            (clave, padre, 0 if padre else 120),
        )
    con.execute(
        "INSERT INTO clasificaciones (dedupe_key, estado, relevante, modelo, "
        "fichas_huella, clasificado_at, tokens_salida) "
        "VALUES ('fortworth', 'ok', 1, 'm', 'h', '2026-09-20', 90)"
    )
    # Y un orden que las tenía a las dos.
    con.execute(
        "INSERT INTO rankings (id, creado_at, modelo, lectura, ofertas_disponibles) "
        "VALUES ('r1', '2026-09-21', 'm', 'lectura', 5)"
    )
    for pos, clave in enumerate(("dallas", "fortworth"), 1):
        con.execute(
            "INSERT INTO ranking_items (ranking_id, dedupe_key, posicion, por_que, accion) "
            "VALUES ('r1', ?, ?, 'x', 'y')",
            (clave, pos),
        )
    almacen.commit()
    yield almacen
    almacen.close()


def _claves(almacen: Storage, tabla: str) -> set[str]:
    return {f[0] for f in almacen.connection.execute(f"SELECT dedupe_key FROM {tabla}")}


class TestRecorte:
    def test_el_plan_decide_bien_cada_una(self, base, texas):
        plan = planificar(base, texas)
        assert sorted(plan.borrar) == ["bigspring", "dallas", "fortworth", "solo_texas"]
        assert sorted(plan.protegidas) == ["interesa", "postulada"]
        assert plan.zonas["barrio"] == "houston"
        assert plan.zonas["valle"] == "sur"

    def test_borra_de_todas_las_tablas_y_deja_lo_suyo(self, base, config, tmp_path):
        mensajes = recortar_si_hace_falta(config, base, tmp_path)

        assert _claves(base, "offers") == {
            "houston", "barrio", "valle", "postulada", "interesa",
        }
        for tabla in ("offer_sources", "offer_queries", "clasificaciones",
                      "ranking_items"):
            assert not _claves(base, tabla) & {"dallas", "fortworth", "bigspring"}
        zonas = dict(base.connection.execute("SELECT dedupe_key, zona FROM offers"))
        assert zonas == {
            "houston": "houston", "barrio": "houston", "valle": "sur",
            "postulada": None, "interesa": None,
        }
        assert len(mensajes) == 1
        assert "se borraron 4 de 9" in mensajes[0]
        assert "se quedan 2" in mensajes[0]

    def test_la_vacante_de_houston_no_se_queda_huerfana(self, base, config, tmp_path):
        """Su representante era el anuncio de Dallas. Sin promover al heredero,
        el ranking —que solo mira representantes— la perdería entera."""
        recortar_si_hace_falta(config, base, tmp_path)
        con = base.connection
        assert tuple(con.execute(
            "SELECT heredada_de, relevante FROM clasificaciones WHERE dedupe_key='houston'"
        ).fetchone()) == (None, 1)
        assert "houston" in {f["dedupe_key"] for f in base.ofertas_para_ranking()}
        # La línea del orden pasa al heredero; la de Fort Worth se va, y se
        # descuenta de las disponibles para que no parezca un recorte.
        assert _claves(base, "ranking_items") == {"houston"}
        assert con.execute(
            "SELECT ofertas_disponibles FROM rankings WHERE id='r1'"
        ).fetchone()[0] == 4
        # Y no se cuela como lectura medida: sigue con los tokens a cero.
        assert "houston" not in {f["dedupe_key"] for f in base.lecturas_medidas()}

    def test_hace_copia_antes_y_la_copia_lo_tiene_todo(self, base, config, tmp_path):
        recortar_si_hace_falta(config, base, tmp_path)
        copias = list(tmp_path.glob("ofertas.db.bak-recorte-texas_us-*"))
        assert len(copias) == 1
        with sqlite3.connect(copias[0]) as copia:
            assert copia.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 9

    def test_una_vez_por_cambio(self, base, config, tmp_path):
        recortar_si_hace_falta(config, base, tmp_path)
        huellas = json.loads((tmp_path / ARCHIVO).read_text(encoding="utf-8"))
        assert set(huellas["mercados"]) == set(config.profile().market_keys)

        # Algo que entraría borrado si se volviera a juzgar... no se juzga.
        ofe, _ = _oferta("otra_dallas", ["US", "Texas", "Dallas", "Irving"])
        base.save_offer(ofe, base.start_run("p", ["adzuna"], ["texas_us"]))
        base.commit()
        assert recortar_si_hace_falta(config, base, tmp_path) == []
        assert "otra_dallas" in _claves(base, "offers")
        assert len(list(tmp_path.glob("ofertas.db.bak-recorte-*"))) == 1

    def test_sin_nada_que_borrar_no_hace_copia(self, tmp_path, config):
        almacen = Storage(tmp_path / "ofertas.db")
        try:
            assert recortar_si_hace_falta(config, almacen, tmp_path) == []
            assert not list(tmp_path.glob("*.bak-recorte-*"))
            assert (tmp_path / ARCHIVO).exists()
        finally:
            almacen.close()


class TestGuardarLaZona:
    def test_la_zona_se_guarda_y_no_se_pisa_con_nada(self, tmp_path):
        almacen = Storage(tmp_path / "ofertas.db")
        try:
            ofe, _ = _oferta("h", ["US", "Texas", "Harris County", "Houston"])
            ofe.zona = "houston"
            run = almacen.start_run("p", ["adzuna"], ["texas_us"])
            almacen.save_offer(ofe, run)
            ofe.zona = None       # otra aparición sin zona: no borra la buena
            almacen.save_offer(ofe, run)
            almacen.commit()
            assert almacen.connection.execute(
                "SELECT zona FROM offers WHERE dedupe_key='h'"
            ).fetchone()[0] == "houston"
        finally:
            almacen.close()


# ---------------------------------------------------------------------------
#  La bandeja
# ---------------------------------------------------------------------------

@pytest.fixture()
def servidor(base, tmp_path, monkeypatch, config):
    from bandeja import server

    recortar_si_hace_falta(config, base, tmp_path)
    falsa = SimpleNamespace(
        database_path=Path(tmp_path) / "ofertas.db",
        market=config.market,
    )
    monkeypatch.setattr(server, "load_config", lambda: falsa)
    monkeypatch.setattr(server, "_abrir_almacen", lambda: base)
    monkeypatch.setattr(base, "close", lambda: None)
    return server


class TestBandeja:
    def test_la_fila_de_zonas_sale_con_texas_y_cuenta_vacantes(self, servidor):
        datos = servidor.resumen(estado="nueva", mercado="texas_us")
        cuentas = {z["clave"]: z["total"] for z in datos["zonas"]}
        assert cuentas == {"houston": 2, "austin": 0, "san_antonio": 0, "sur": 1}
        assert datos["zonas_total"] == 3
        assert all(z["descripcion"] for z in datos["zonas"])

    def test_sin_mercado_elegido_no_hay_zonas(self, servidor):
        assert servidor.resumen(estado="nueva", mercado="todos")["zonas"] == []

    def test_filtrar_por_zona(self, servidor):
        datos = servidor.listar_ofertas(estado="todas", mercado="texas_us", zona="sur")
        assert [o["dedupe_key"] for o in datos["ofertas"]] == ["valle"]

    def test_las_pastillas_de_estado_cuentan_dentro_de_la_zona(self, servidor):
        datos = servidor.resumen(estado="nueva", mercado="texas_us", zona="sur")
        assert datos["por_estado"]["nueva"] == 1
        assert datos["por_estado"]["postulada"] == 0
