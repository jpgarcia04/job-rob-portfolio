"""
Cada mercado pregunta lo suyo — y cuánto cuesta eso, contado UNA sola vez.

QUÉ PROBLEMA RESUELVE ESTO
--------------------------
Hasta el 17 septiembre 2026 las combinaciones eran del PERFIL y se lanzaban
enteras en todos sus mercados. Con dos mercados que se buscan en inglés daba
igual. Al entrar México dejó de dar igual, porque cada petición sale de una
cuota mensual de 2.500 que además se comparte con la máquina de la clienta, y
está medido que:

  · las 10 combinaciones en español dan CERO en Texas y en Alemania se busca
    en inglés  ->  20 peticiones por corrida tiradas;
  · las 2 que llevan «Mexico» dentro valen en Texas (4 relevantes medidas) y
    en el índice mexicano buscan una palabra que está en todos los anuncios
    del país: 1.068 y 282 resultados para traer 3 anuncios basura.

23 × 3 = 69 por corrida (2.070 al mes, 83% de la cuota). Repartidas, 47
(1.410, 56%). Ver `data/reportes/2026-09-17-mexico/medicion.md`.

LO QUE SE VIGILA AQUÍ
---------------------
  - que la cuenta de «cuántas peticiones» sea UNA y la misma para los cuatro
    que preguntan: la consola, la bandeja, el plan y la corrida. Ese es el
    fallo que no da síntomas — el número que se enseña deja de ser el que se
    cobra y no falla nada;
  - que una errata en el YAML se NIEGUE en vez de aplicarse a nadie. Los tres
    errores posibles cuestan cobertura o cuota y ninguno se vería;
  - que sin `combinaciones_por_mercado` todo siga exactamente como antes.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from recolector.config import ConfigError, load_config
from recolector.models import KeywordCombo

RUTA_REAL = Path("config/search_profile.yaml")

ESPANOL = [
    ["Director de Operaciones", "Manufactura"],
    ["Director de Operaciones", "Logística"],
]
LAS_DOS_DE_MEXICO = [["Managing Director", "Mexico"], ["Supply Chain Director", "Mexico"]]


def _yaml_real() -> dict:
    return yaml.safe_load(RUTA_REAL.read_text(encoding="utf-8"))


def escribir(tmp_path: Path, datos: dict) -> Path:
    ruta = tmp_path / "perfil.yaml"
    ruta.write_text(yaml.safe_dump(datos, allow_unicode=True), encoding="utf-8")
    return ruta


@pytest.fixture
def base(tmp_path):
    """El YAML real, con tres mercados y SIN ningún ajuste de combinaciones.

    Se parte del archivo que se envía y no de uno inventado —lo que hay que
    comprobar es que la pieza funciona sobre la configuración de verdad—, pero
    se le quita `combinaciones_por_mercado` a propósito: estas pruebas ponen
    cada una el suyo, y si heredaran el del archivo real dirían cosas distintas
    cada vez que alguien mueva una combinación. Lo que el archivo real declara
    HOY se vigila en `tests/test_cuota_adzuna.py`, que es su sitio.
    """
    datos = copy.deepcopy(_yaml_real())
    datos["markets"].setdefault("mexico", {
        "label": "México",
        "country_code": "mx",
        "adzuna": {"country": "mx", "where": ""},
        "jooble": {
            "api_key_env": "JOOBLE_API_KEY_MX",
            "host": "mx.jooble.org",
            "location": "México",
        },
        "geo_filter": {"mode": "none"},
    })
    perfil = datos["profiles"]["ejecutivo_operaciones_logistica"]
    perfil["markets"] = ["alemania", "mexico", "texas_us"]
    perfil.pop("combinaciones_por_mercado", None)
    datos["storage"]["database_path"] = str(tmp_path / "ofertas.db")
    datos["cache"]["directory"] = str(tmp_path / "cache")
    return datos


# ---------------------------------------------------------------------------
#  Lo de siempre sigue funcionando igual
# ---------------------------------------------------------------------------

class TestSinAjustesNoCambiaNada:

    def test_el_perfil_real_no_declara_nada_raro(self, tmp_path, base):
        """Sin `combinaciones_por_mercado`, todos los mercados preguntan igual."""
        ruta = escribir(tmp_path, base)
        perfil = load_config(ruta).profile()

        cuantas = {k: len(perfil.combinations_for(k)) for k in perfil.market_keys}
        assert set(cuantas.values()) == {13}
        assert perfil.planned_queries() == 39

    def test_un_mercado_que_no_es_del_perfil_cae_a_la_lista_general(self, tmp_path, base):
        """`--markets` admite claves que el perfil no lista. No debe reventar."""
        ruta = escribir(tmp_path, base)
        perfil = load_config(ruta).profile()
        assert perfil.combinations_for("un_mercado_que_no_existe") == perfil.keyword_combinations


# ---------------------------------------------------------------------------
#  El reparto
# ---------------------------------------------------------------------------

class TestElReparto:

    def test_ademas_y_menos_se_aplican_solo_a_su_mercado(self, tmp_path, base):
        base["profiles"]["ejecutivo_operaciones_logistica"]["combinaciones_por_mercado"] = {
            "mexico": {"ademas": ESPANOL, "menos": LAS_DOS_DE_MEXICO},
        }
        perfil = load_config(escribir(tmp_path, base)).profile()

        assert len(perfil.combinations_for("mexico")) == 13 - 2 + 2
        assert len(perfil.combinations_for("texas_us")) == 13
        assert len(perfil.combinations_for("alemania")) == 13

        mx = perfil.combinations_for("mexico")
        assert KeywordCombo(terms=("Director de Operaciones", "Manufactura")) in mx
        assert KeywordCombo(terms=("Managing Director", "Mexico")) not in mx
        # y en Texas siguen las dos que allí valen
        tx = perfil.combinations_for("texas_us")
        assert KeywordCombo(terms=("Managing Director", "Mexico")) in tx
        assert KeywordCombo(terms=("Director de Operaciones", "Manufactura")) not in tx

    def test_las_peticiones_se_suman_por_mercado_y_no_se_multiplican(self, tmp_path, base):
        """EL FALLO QUE ESTO IMPIDE: `combos × mercados` deja de ser cierto.

        Con 13 generales, +2 y −2 en México, son 13 + 13 + 13 = 39 y no 15 × 3.
        """
        base["profiles"]["ejecutivo_operaciones_logistica"]["combinaciones_por_mercado"] = {
            "mexico": {"ademas": ESPANOL, "menos": LAS_DOS_DE_MEXICO},
        }
        perfil = load_config(escribir(tmp_path, base)).profile()

        assert perfil.planned_queries() == 13 + 13 + 13
        assert perfil.planned_queries(["mexico"]) == 13
        assert perfil.planned_queries(["texas_us", "alemania"]) == 26

    def test_limit_combos_recorta_cada_mercado(self, tmp_path, base):
        base["profiles"]["ejecutivo_operaciones_logistica"]["combinaciones_por_mercado"] = {
            "mexico": {"ademas": ESPANOL},
        }
        perfil = load_config(escribir(tmp_path, base)).profile()
        assert perfil.planned_queries(limit_combos=5) == 15

    def test_el_plan_y_la_corrida_cuentan_lo_mismo(self, tmp_path, base):
        """La promesa de la casa: el número que se enseña es el que se cobra.

        `plan_de_recoleccion` es lo que lee la bandeja para el diálogo de
        confirmar, y `planned_queries` es lo que usa la corrida. Si divergen,
        no falla nada: simplemente se cobra otra cosa.
        """
        from recolector.plan import plan_de_recoleccion

        base["profiles"]["ejecutivo_operaciones_logistica"]["combinaciones_por_mercado"] = {
            "mexico": {"ademas": ESPANOL, "menos": LAS_DOS_DE_MEXICO},
        }
        config = load_config(escribir(tmp_path, base))
        plan = plan_de_recoleccion(config, fuentes=["adzuna"])

        esperado = config.profile().planned_queries() * config.adzuna.max_pages
        assert plan.peticiones == esperado
        assert sum(n for _, n in plan.combinaciones_por_mercado) == esperado


# ---------------------------------------------------------------------------
#  Se niega ante una errata, que es lo que no se vería
# ---------------------------------------------------------------------------

class TestSeNiegaAnteUnaErrata:

    def test_un_mercado_que_el_perfil_no_usa(self, tmp_path, base):
        base["profiles"]["ejecutivo_operaciones_logistica"]["combinaciones_por_mercado"] = {
            "mexicoo": {"ademas": ESPANOL},
        }
        with pytest.raises(ConfigError) as fallo:
            load_config(escribir(tmp_path, base))
        assert "mexicoo" in str(fallo.value)
        assert "markets" in str(fallo.value)

    def test_quitar_algo_que_no_esta_en_la_lista_general(self, tmp_path, base):
        """Quitar algo que no está no hace nada, y parece que sí."""
        base["profiles"]["ejecutivo_operaciones_logistica"]["combinaciones_por_mercado"] = {
            "mexico": {"menos": [["Managing Director", "Méjico"]]},
        }
        with pytest.raises(ConfigError) as fallo:
            load_config(escribir(tmp_path, base))
        assert "Managing Director + Méjico" in str(fallo.value)

    def test_anadir_algo_que_ya_esta_costaria_dos_peticiones(self, tmp_path, base):
        base["profiles"]["ejecutivo_operaciones_logistica"]["combinaciones_por_mercado"] = {
            "mexico": {"ademas": [["COO", "Logistics"]]},
        }
        with pytest.raises(ConfigError) as fallo:
            load_config(escribir(tmp_path, base))
        assert "COO + Logistics" in str(fallo.value)
        assert "dos veces" in str(fallo.value)

    def test_una_clave_inventada(self, tmp_path, base):
        base["profiles"]["ejecutivo_operaciones_logistica"]["combinaciones_por_mercado"] = {
            "mexico": {"anade": ESPANOL},
        }
        with pytest.raises(ConfigError) as fallo:
            load_config(escribir(tmp_path, base))
        assert "anade" in str(fallo.value)

    def test_dejar_un_mercado_sin_ninguna_combinacion(self, tmp_path, base):
        perfil = base["profiles"]["ejecutivo_operaciones_logistica"]
        perfil["combinaciones_por_mercado"] = {
            "mexico": {"menos": [list(c) for c in perfil["keyword_combinations"]]},
        }
        with pytest.raises(ConfigError) as fallo:
            load_config(escribir(tmp_path, base))
        assert "sin ninguna combinación" in str(fallo.value)
