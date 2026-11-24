"""
El contador de gasto de la cabecera: qué cuenta y de qué mes.

QUÉ CAMBIÓ EL 8 SEPTIEMBRE 2026. La cabecera decía «$4,47 de $12,00 este mes»,
con barra de relleno, ámbar al 75 % y rojo al agotarse. Ese denominador no
frenaba nada: el mes está en modo `avisar`, y la cuenta de Anthropic se recarga
sola al bajar de un umbral, así que tampoco hay un techo detrás. Enseñar un
tope que no corta —y pintarlo de rojo— promete una protección que no existe.

Lo que queda es una cuenta: cuánto lleva gastado, de qué mes, y cuándo vuelve a
cero. Lo que frena de verdad —el tope por acción— se dice antes de cada cobro.

Y con eso aparece una frontera que antes no se miraba: **cuándo se reinicia**.
El libro agrupaba por UTC, así que para ella, en México, el mes se ponía a cero
el último día a las seis de la tarde. Mientras la pantalla no decía nada del
periodo daba igual; en cuanto dice «gastados en septiembre» y «vuelve a cero el
1 de octubre», una frontera a seis horas de lo anunciado son dos números que se
contradicen. Regla 5 de la casa.

Todo offline: se escriben cajones a mano en un libro de usar y tirar.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bandeja import server
from clasificador.presupuesto import LibroDeGastos

from .test_servidor import entorno, sembrar  # noqa: F401

RAIZ = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
#  El mes es SU mes
# ---------------------------------------------------------------------------

class TestElMesEsElDeSuReloj:

    def test_los_cajones_se_escriben_con_la_fecha_local(self, monkeypatch):
        """`datetime.now()` sin huso, no `now(timezone.utc)`.

        Se comprueba interceptando la llamada en vez de comparando fechas: a
        seis husos de distancia, una prueba que compare `_hoy()` con la fecha
        local solo fallaría durante seis horas al día, y una prueba que falla a
        ratos se acaba ignorando.
        """
        vistos = []

        class RelojEspia(datetime):
            @classmethod
            def now(cls, tz=None):
                vistos.append(tz)
                return datetime(2026, 9, 8, 0, 34)

        monkeypatch.setattr("clasificador.presupuesto.datetime", RelojEspia)

        assert LibroDeGastos._hoy() == "2026-09-08"
        assert LibroDeGastos.mes_actual() == "2026-09"
        assert vistos == [None, None], (
            "los cajones del gasto se agrupan por la fecha LOCAL: con UTC, el "
            "mes de una usuaria en México se reinicia el día 30 a las 18:00"
        )

    def test_pero_las_marcas_de_tiempo_siguen_en_utc(self, tmp_path: Path):
        """Un cajón es «de qué mes»; una marca es «cuándo pasó».

        `last_used` es un registro, y un registro sin huso no se puede leer
        desde otro sitio. Se queda en UTC y con su desplazamiento dentro.
        """
        ruta = tmp_path / "uso_llm.json"
        LibroDeGastos(ruta).registrar(0.10)

        datos = json.loads(ruta.read_text(encoding="utf-8"))
        marca = datetime.fromisoformat(datos["last_used"])
        assert marca.tzinfo is not None
        assert marca.utcoffset() == timezone.utc.utcoffset(None)

    def test_solo_suma_los_cajones_del_mes_que_corre(self, tmp_path: Path):
        """Lo que hace que la cifra sea «de este mes» y no «de siempre»."""
        ruta = tmp_path / "uso_llm.json"
        libro = LibroDeGastos(ruta)
        mes = libro.mes_actual()
        anio, numero = int(mes[:4]), int(mes[5:7])
        anterior = f"{anio - 1}-{numero:02d}"

        ruta.write_text(json.dumps({
            "by_day": {
                f"{mes}-01": {"usd": 1.50, "llamadas": 3},
                f"{mes}-02": {"usd": 0.50, "llamadas": 1},
                f"{anterior}-15": {"usd": 99.00, "llamadas": 40},
            },
            "total_usd": 101.0, "total_llamadas": 44,
        }), encoding="utf-8")

        libro = LibroDeGastos(ruta)
        assert libro.gastado_este_mes() == pytest.approx(2.00)
        assert libro.llamadas_este_mes() == 4
        # El acumulado de siempre sigue existiendo, y no es el que se enseña.
        assert libro.gastado_total() == pytest.approx(101.0)


# ---------------------------------------------------------------------------
#  Lo que la pantalla necesita para no mentir
# ---------------------------------------------------------------------------

class TestLaEtiquetaSaleDelMismoRelojQueLaCifra:

    def test_el_endpoint_dice_de_que_mes_es_y_cuando_se_reinicia(self, entorno):
        """Un acumulado sin periodo no se puede leer: el día 1 baja de golpe y
        parece un fallo. Y los dos salen del libro, no del navegador."""
        datos = server.presupuesto()

        assert datos["mes"] == LibroDeGastos.mes_actual()
        assert datos["mes_nombre"] == server._nombre_del_mes(datos["mes"])
        assert datos["reinicio"] == server._cuando_se_reinicia(datos["mes"])
        assert datos["reinicio"].startswith("el 1 de ")

    def test_los_nombres_de_los_meses_no_dependen_del_sistema(self):
        """A mano y no con `locale`: la máquina del desarrollador no tiene por
        qué tener instalado el español, y el nombre saldría en inglés."""
        assert server._nombre_del_mes("2026-01") == "enero"
        assert server._nombre_del_mes("2026-09") == "septiembre"
        assert server._nombre_del_mes("2026-12") == "diciembre"

    def test_diciembre_reinicia_en_enero_del_ano_siguiente(self):
        """El único caso que se escribe mal solo, y una vez al año."""
        assert server._cuando_se_reinicia("2026-11") == "el 1 de diciembre"
        assert server._cuando_se_reinicia("2026-12") == "el 1 de enero de 2027"


# ---------------------------------------------------------------------------
#  Y la barra que ya no está
# ---------------------------------------------------------------------------

class TestNoQuedaUnaBarraSinDenominador:
    """Una barra de relleno necesita un tope contra el que llenarse.

    Al quitar el denominador, dejar la barra habría sido peor que antes: una
    barra que no se mueve nunca, o que se llena contra un número invisible.
    """

    def test_ni_en_la_pantalla_ni_en_el_javascript(self):
        html = (RAIZ / "bandeja" / "static" / "index.html").read_text(encoding="utf-8")
        js = (RAIZ / "bandeja" / "static" / "app.js").read_text(encoding="utf-8")

        assert "presupuesto-relleno" not in html
        assert "presupuesto-relleno" not in js, (
            "el JavaScript escribiría en un elemento que ya no existe"
        )
        assert 'id="presupuesto-texto"' in html, "la cifra sí se queda"

    def test_la_ayuda_no_manda_mirar_una_barra_que_no_existe(self):
        """`AYUDA.md` decía «pasa el ratón por la barra de gasto».

        Es la misma clase de deriva que ya vigila
        `test_el_documento_no_nombra_botones_que_no_existen`: un manual que
        manda mirar algo que se quitó de la pantalla.
        """
        ayuda = (RAIZ / "AYUDA.md").read_text(encoding="utf-8")
        assert "barra de gasto" not in ayuda
        assert "barra del mes" not in ayuda


# ---------------------------------------------------------------------------
#  Anuncios y vacantes: dos unidades, dos palabras
# ---------------------------------------------------------------------------

class TestCadaCifraDiceSuUnidad:
    """La cabecera decía «344 ofertas» contando ANUNCIOS mientras la pastilla
    de al lado decía «Sin revisar 272» contando VACANTES.

    Las dos cifras eran ciertas. Lo que fallaba es que la palabra «ofertas» las
    ponía en la misma unidad, así que a cuatro centímetros una de otra parecía
    que una de las dos estaba mal — la regla 5 de la casa rota justo en lo
    primero que se ve al abrir.
    """

    def test_el_total_de_la_cabecera_son_anuncios(self, entorno):
        """Es `total_offers()`, y esa función cuenta filas de `offers`."""
        claves = sembrar(entorno.almacen, 3)
        datos = server.resumen()

        assert datos["total"] == len(claves)
        assert datos["total"] == entorno.almacen.total_offers()

    def test_la_pantalla_llama_anuncios_al_total_y_no_ofertas(self):
        js = (RAIZ / "bandeja" / "static" / "app.js").read_text(encoding="utf-8")
        pintado = js.split("sub.textContent =", 1)[1].split(";", 1)[0]

        assert "anuncios" in pintado
        assert "ofertas" not in pintado, (
            "«ofertas» no distingue anuncio de vacante, que es justo lo que "
            "hace que las dos cifras de la cabecera parezcan contradecirse"
        )

    def test_los_mercados_siguen_en_esa_linea_aunque_ya_no_por_su_nombre(self):
        """DÓNDE está buscando el sistema, y por qué cambió la forma de decirlo.

        La línea llevaba la lista entera —«477 anuncios · Alemania · México ·
        Texas, EE.UU.»— y con el tercer mercado dejó de caber: medido
        ejecutando a 1360 px, pedía 322 px y tenía 165, así que se cortaba en
        «· Alemania…». El dato que esta prueba protegía llevaba semanas sin
        verse, y con tres mercados escondía dos de los tres.

        Así que el dato no se borra, se muda a donde cabe: el recuento aquí
        —que es lo que cambia cuando entra un mercado nuevo, y se ve sin abrir
        nada— y los nombres en el desplegable de al lado, con sus cifras, más
        el `title` para el ratón. Lo que no puede volver a pasar es que esta
        línea se quede sin ninguna señal de los mercados.
        """
        js = (RAIZ / "bandeja" / "static" / "app.js").read_text(encoding="utf-8")
        pintado = js.split("sub.textContent =", 1)[1].split(";", 1)[0]

        assert "cuantos" in pintado, "la línea tiene que seguir diciendo cuántos mercados hay"
        assert "mercado" in js.split("const cuantos =", 1)[1].split("\n", 1)[0]
        assert "sub.title = nombres.join" in js, (
            "los nombres no pueden perderse: van al title para el ratón"
        )

    def test_la_unidad_de_las_tarjetas_esta_junto_a_las_tarjetas(self):
        """«Una tarjeta por vacante» va sobre la rejilla, no en la cabecera."""
        html = (RAIZ / "bandeja" / "static" / "index.html").read_text(encoding="utf-8")
        antes_de_la_rejilla = html.split('<div id="rejilla"', 1)[0]

        assert "Una tarjeta por vacante" in antes_de_la_rejilla

    def test_el_aviso_del_recorte_usa_la_misma_palabra(self):
        """Ese aviso también cuenta anuncios (`data.anuncios`).

        Decía «publicaciones»: un tercer nombre para lo mismo en la misma
        pantalla. Tres palabras para dos cosas es peor que dos para dos.
        """
        js = (RAIZ / "bandeja" / "static" / "app.js").read_text(encoding="utf-8")
        assert "publicaciones más importantes" not in js
        assert "anuncios más importantes" in js
