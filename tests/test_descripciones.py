"""
La descripción entera del anuncio.

CONTRA PÁGINAS REALES, NO INVENTADAS. Las cuatro de `tests/paginas/` se
descargaron de Adzuna el 3 septiembre 2026 y son los cuatro casos que existen
de verdad:

    texas_bloqueada_json_ld.html   Texas visto desde fuera de EE.UU.: HTTP 200
                                   con el cartel de región, y la descripción
                                   entera en el JSON-LD, debajo del cartel.
    texas_404_solo_html.html       HTTP 404 con la página entera y SIN JSON-LD
                                   de oferta. El texto está en el HTML visible.
    mexico_normal_json_ld.html     El caso fácil: 200, sin cartel, con JSON-LD.
    mexico_puente_sin_nada.html    `/land/ad/`: una página-puente de 3 KB que
                                   solo salta a click.appcast.io. No hay texto.

Solo se les quitó lo que el extractor no mira nunca —scripts que no son
`ld+json`, estilos, `<svg>` y comentarios— para que no ocupen 80 KB cada una.
Todo lo que sí mira está intacto, incluidos los OTROS bloques `ld+json` de la
página, que son los que hacen que la prueba valga: si el extractor cogiera el
primero que encuentra en vez del `JobPosting`, aquí se vería.

Probar esto contra HTML escrito a mano no serviría de nada: el riesgo no es
que la expresión regular esté mal, es que la página no sea como creemos.

Ninguna prueba toca la red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recolector.connectors.adzuna_descripcion import (
    MINIMO_CARACTERES,
    Descripcion,
    DescripcionError,
    TraedorDeDescripciones,
    a_texto,
    extraer,
    normalizar_url,
)
from recolector.http_cache import MODE_CACHE_ONLY, CacheMissError, DiskCache
from recolector.storage import Storage

from .test_clasificador import almacen, oferta_en_base  # noqa: F401

PAGINAS = Path(__file__).parent / "paginas"


def pagina(nombre: str) -> str:
    return (PAGINAS / nombre).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
#  Los cuatro casos reales
# ---------------------------------------------------------------------------

class TestLasCuatroPaginasReales:
    def test_texas_bloqueada_por_region_trae_la_descripcion_entera(self):
        """EL HALLAZGO QUE HIZO POSIBLE TODO ESTO.

        El cartel de «Sorry, this job is not available in your region» afecta
        al 68% de la base y parecía dejar la página inservible. Lo que
        sustituye es el BOTÓN DE POSTULAR, no el texto: la descripción sigue
        entera debajo. Si esta prueba se cae, la función pierde dos tercios de
        su alcance y hay que enterarse por aquí, no por la bandeja.
        """
        html = pagina("texas_bloqueada_json_ld.html")
        assert "not available in your region" in html, (
            "la página de prueba tiene que seguir siendo la bloqueada"
        )

        d = extraer(html)
        assert d.via == "json-ld"
        assert d.ok
        assert len(d.texto) > 3_000
        assert "Program Director" in d.texto

    def test_texas_404_cae_al_html_visible(self):
        """UN 404 NO SIGNIFICA QUE NO HAYA PÁGINA.

        Adzuna sirve el 404 con el anuncio entero dentro. Un
        `raise_for_status()` aquí tiraría el 21% de las ofertas de Texas por un
        código que la propia respuesta desmiente. El estado no decide: decide
        si sale texto.
        """
        d = extraer(pagina("texas_404_solo_html.html"), http=404)
        assert d.http == 404
        assert d.via == "html"
        assert d.ok
        assert len(d.texto) > 3_000

    def test_mexico_normal_sale_por_json_ld(self):
        d = extraer(pagina("mexico_normal_json_ld.html"))
        assert d.via == "json-ld"
        assert d.ok

    def test_la_pagina_puente_no_inventa_nada(self):
        """`/land/ad/` sin reescribir es un salto a click.appcast.io.

        Lo importante es que devuelva `via='nada'` y texto vacío, no que
        devuelva los 178 caracteres de «Estás siendo redireccionado a...».
        Guardar eso como descripción sería peor que no guardar nada: el modelo
        lo leería como si fuera el anuncio.
        """
        d = extraer(pagina("mexico_puente_sin_nada.html"))
        assert d.via == "nada"
        assert d.texto == ""
        assert not d.ok

    def test_ninguna_devuelve_el_json_ld_equivocado(self):
        """Las páginas traen varios `ld+json`; solo uno es el `JobPosting`.

        El primero suele ser un `BreadcrumbList`. Coger ese daría una
        descripción vacía sin que fallara nada, que es el modo de fallo
        silencioso que este proyecto persigue.
        """
        for nombre in ("texas_bloqueada_json_ld.html", "mexico_normal_json_ld.html"):
            d = extraer(pagina(nombre))
            assert d.via == "json-ld", nombre
            assert "Jobs" != d.texto.strip()[:4], nombre


# ---------------------------------------------------------------------------
#  La URL, que es donde está la trampa
# ---------------------------------------------------------------------------

class TestLaUrl:
    def test_land_ad_se_reescribe_a_details(self):
        """Sin esto, 110 de las 381 ofertas (29%) no traen descripción."""
        u = normalizar_url(
            "https://www.adzuna.com/land/ad/5867721285"
            "?se=EHAO4Tun&utm_medium=api&utm_source=2c5bc4df"
        )
        assert u.startswith("https://www.adzuna.com/details/5867721285")

    def test_los_utm_sobreviven_a_la_reescritura(self):
        """SIN ELLOS, HTTP 403 CON 919 BYTES.

        Medido: la misma URL sin `utm_medium`/`utm_source` no devuelve la
        página, devuelve un bloqueo. Es la clase de detalle que se pierde al
        «limpiar» una URL y cuyo síntoma no dice nada de la causa.
        """
        u = normalizar_url(
            "https://www.adzuna.com/land/ad/123?se=X&utm_medium=api&utm_source=abc"
        )
        assert "utm_medium=api" in u
        assert "utm_source=abc" in u

    def test_se_tira_el_token_de_sesion(self):
        """`se=` caduca y ensuciaría la clave de caché sin aportar nada."""
        u = normalizar_url(
            "https://www.adzuna.com/land/ad/123?se=CADUCA&utm_medium=api"
        )
        assert "se=" not in u

    def test_details_se_deja_como_esta(self):
        original = "https://www.adzuna.com.mx/details/999?utm_medium=api"
        assert normalizar_url(original) == original


# ---------------------------------------------------------------------------
#  El texto que sale
# ---------------------------------------------------------------------------

class TestElTexto:
    def test_conserva_la_estructura_de_parrafos(self):
        """Un anuncio sin saltos de línea es un muro y se lee peor."""
        t = a_texto("<p>Uno</p><p>Dos</p><ul><li>A</li><li>B</li></ul>")
        assert "\n" in t
        assert "- A" in t

    def test_no_deja_etiquetas_ni_entidades(self):
        t = a_texto("<p>Direcci&#243;n &amp; Operaci&oacute;n<br/>M&#233;xico</p>")
        assert "<" not in t and ">" not in t
        assert "&#" not in t
        assert "Dirección" in t
        assert "México" in t

    def test_un_texto_corto_no_cuenta_como_recuperado(self):
        """El umbral existe porque los recortes de la API son 500 caracteres.

        Si el mínimo fuera más bajo, una página de error maquetada podría
        pasar por descripción y acabaría en el prompt del clasificador.
        """
        d = Descripcion(texto="x" * (MINIMO_CARACTERES - 1), via="html", http=200, url="")
        assert not d.ok
        assert Descripcion(texto="x" * MINIMO_CARACTERES, via="html", http=200, url="").ok


# ---------------------------------------------------------------------------
#  El traedor, sin red
# ---------------------------------------------------------------------------

class RespuestaFalsa:
    def __init__(self, texto: str, status_code: int = 200) -> None:
        self.text = texto
        self.status_code = status_code


class SesionFalsa:
    """Devuelve lo que le diga la prueba y cuenta las peticiones."""

    def __init__(self, respuesta: RespuestaFalsa) -> None:
        self.respuesta = respuesta
        self.peticiones: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url, timeout=None):
        self.peticiones.append(url)
        return self.respuesta


@pytest.fixture()
def cache(tmp_path: Path) -> DiskCache:
    return DiskCache(directory=tmp_path / "cache", ttl_hours=12, enabled=True)


class TestElTraedor:
    def test_la_segunda_vez_sale_de_la_cache(self, cache):
        """Depurar el extractor no puede costar una petición por intento."""
        sesion = SesionFalsa(RespuestaFalsa(pagina("mexico_normal_json_ld.html")))
        t = TraedorDeDescripciones(cache, sleep_seconds=0, sesion=sesion)
        url = "https://www.adzuna.com.mx/details/1?utm_medium=api"

        primera = t.traer(url)
        segunda = t.traer(url)

        assert primera.texto == segunda.texto
        assert len(sesion.peticiones) == 1, "la segunda tenía que salir de la caché"
        assert t.peticiones == 1

    def test_un_404_se_procesa_igual(self, cache):
        sesion = SesionFalsa(
            RespuestaFalsa(pagina("texas_404_solo_html.html"), status_code=404)
        )
        t = TraedorDeDescripciones(cache, sleep_seconds=0, sesion=sesion)
        d = t.traer("https://www.adzuna.com/details/1?utm_medium=api")
        assert d.ok, "un 404 de Adzuna trae la página entera; no es un fallo"

    def test_un_403_si_es_un_error_y_dice_por_que(self, cache):
        """El 403 tiene una causa concreta y el mensaje tiene que nombrarla.

        «HTTP 403» a secas mandaría a buscar credenciales, y aquí no hay
        ninguna: lo que falta son los `utm_*` de la URL.
        """
        sesion = SesionFalsa(RespuestaFalsa("bloqueado", status_code=403))
        t = TraedorDeDescripciones(cache, sleep_seconds=0, sesion=sesion)
        with pytest.raises(DescripcionError, match="utm_"):
            t.traer("https://www.adzuna.com/details/1")

    def test_cache_only_no_toca_la_red(self, tmp_path):
        cache = DiskCache(
            directory=tmp_path / "c", ttl_hours=12, enabled=True, mode=MODE_CACHE_ONLY
        )
        sesion = SesionFalsa(RespuestaFalsa("no debería pedirse"))
        t = TraedorDeDescripciones(cache, sleep_seconds=0, sesion=sesion)

        with pytest.raises(CacheMissError):
            t.traer("https://www.adzuna.com/details/1?utm_medium=api")
        assert sesion.peticiones == []


# ---------------------------------------------------------------------------
#  Lo que se guarda, y a quién se le hereda
# ---------------------------------------------------------------------------

def vacante_en_tres_ciudades(almacen: Storage) -> list[str]:
    return [
        oferta_en_base(
            almacen, "run-1", external_id=f"c-{c}", title="Director de Planta",
            company="Hermanos SA", location_raw=c, location_city=c,
        )
        for c in ("Monterrey", "Guadalajara", "Puebla")
    ]


class TestLaPersistencia:
    def test_los_hermanos_heredan_gratis(self, almacen):
        """Cinco anuncios de la misma vacante tienen el mismo texto.

        Pedirlo cinco veces son cuatro peticiones regaladas a Adzuna y cuatro
        oportunidades de que nos corten. Es la misma idea que ya usan el
        clasificador y el triaje.
        """
        claves = vacante_en_tres_ciudades(almacen)
        almacen.guardar_descripcion_completa(claves[0], "texto entero " * 60, "json-ld")

        heredados = almacen.heredar_descripcion_completa(claves[0])

        assert heredados == 2
        for clave in claves[1:]:
            fila = almacen.connection.execute(
                "SELECT description_full, description_full_via FROM offers "
                "WHERE dedupe_key = ?", (clave,)
            ).fetchone()
            assert fila["description_full"].startswith("texto entero")
            assert fila["description_full_via"] == "json-ld"

    def test_solo_se_pide_una_por_vacante(self, almacen):
        vacante_en_tres_ciudades(almacen)
        oferta_en_base(almacen, "run-1", external_id="otra", title="Otro puesto")

        pendientes = almacen.ofertas_sin_descripcion_completa()

        assert len(pendientes) == 2, (
            "las tres publicaciones del mismo puesto son UNA petición, no tres"
        )

    def test_un_fallo_se_apunta_y_no_se_reintenta(self, almacen):
        """«Se intentó y no la tiene» no puede leerse como «no se ha mirado».

        Si no se apuntara, esa página se pediría en cada corrida para siempre.
        Es la misma regla del libro de gastos: un registro que solo guarda los
        éxitos miente justo el día que algo va mal.
        """
        clave = oferta_en_base(almacen, "run-1", external_id="sin-suerte")
        almacen.guardar_descripcion_completa(clave, "", "nada")

        pendientes = almacen.ofertas_sin_descripcion_completa()
        assert clave not in [f["dedupe_key"] for f in pendientes]

        con_reintento = almacen.ofertas_sin_descripcion_completa(
            solo_sin_intentar=False
        )
        assert clave in [f["dedupe_key"] for f in con_reintento]

    def test_el_resumen_separa_las_dos_vias(self, almacen):
        """`json-ld` es un formato con dueño; `html` es una clase de CSS.

        Si un día `html` sube, nos estamos apoyando en lo frágil y hay que
        enterarse antes de que se rompa del todo.
        """
        a = oferta_en_base(almacen, "run-1", external_id="a", title="A")
        b = oferta_en_base(almacen, "run-1", external_id="b", title="B")
        c = oferta_en_base(almacen, "run-1", external_id="c", title="C")
        almacen.guardar_descripcion_completa(a, "x" * 900, "json-ld")
        almacen.guardar_descripcion_completa(b, "x" * 900, "html")
        almacen.guardar_descripcion_completa(c, "", "nada")

        r = almacen.resumen_descripciones()
        assert (r["por_json_ld"], r["por_html"], r["sin_suerte"]) == (1, 1, 1)
        assert r["intentadas"] == 3


# ---------------------------------------------------------------------------
#  Y que el clasificador la use
# ---------------------------------------------------------------------------

class TestElClasificadorLaUsa:
    def test_prefiere_la_entera_cuando_existe(self, almacen):
        from clasificador.motor import descripcion_de

        clave = oferta_en_base(almacen, "run-1", external_id="1")
        almacen.guardar_descripcion_completa(clave, "LA ENTERA " * 80, "json-ld")

        fila = almacen.connection.execute(
            "SELECT * FROM offers WHERE dedupe_key = ?", (clave,)
        ).fetchone()
        assert descripcion_de(fila).startswith("LA ENTERA")

    def test_sin_ella_devuelve_la_de_la_api(self, almacen):
        """Con la función apagada, el comportamiento es exactamente el de antes."""
        from clasificador.motor import descripcion_de

        clave = oferta_en_base(almacen, "run-1", external_id="1")
        fila = almacen.connection.execute(
            "SELECT * FROM offers WHERE dedupe_key = ?", (clave,)
        ).fetchone()

        assert descripcion_de(fila) == fila["description"].strip()

    def test_el_coste_estimado_crece_con_la_entera(self, almacen):
        """LA ESTIMACIÓN TIENE QUE VER VENIR EL IMPORTE DE VERDAD.

        El coste se calcula sobre la longitud del texto, así que si el
        estimador leyera `description` mientras la llamada manda
        `description_full`, el número que se le enseña a la clienta dejaría de
        ser el que se cobra — sin que fallara nada. Es la regla nº 9.
        """
        from clasificador.fichas import cargar_fichas
        from clasificador.motor import Motor

        clave = oferta_en_base(almacen, "run-1", external_id="1")
        fichas = cargar_fichas(Path("config/cvs.yaml"))
        motor = Motor(almacen, fichas, cliente=None)

        fila = almacen.connection.execute(
            "SELECT * FROM offers WHERE dedupe_key = ?", (clave,)
        ).fetchone()
        corta = motor._coste_de_una(fila)

        almacen.guardar_descripcion_completa(clave, "palabra " * 900, "json-ld")
        fila = almacen.connection.execute(
            "SELECT * FROM offers WHERE dedupe_key = ?", (clave,)
        ).fetchone()
        entera = motor._coste_de_una(fila)

        assert entera > corta * 1.5, (
            "el plan tiene que cobrar por el texto que se va a mandar de verdad"
        )
