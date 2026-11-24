"""
Lo que pasa DESPUÉS de que una lectura falle.

Que un fallo de la API acabe en `sin_clasificar` ya está probado en
`test_clasificador.py`. Lo que no estaba probado es lo siguiente, que es lo
que ella vive: qué le ofrece la aplicación cuando vuelve a mirar esa oferta.

EL FALLO QUE ESTAS PRUEBAS FIJAN (medido el 7 septiembre 2026). Una fila
`sin_clasificar` contaba como «clasificada» en los dos sitios que deciden si
el botón «que la lea el asistente» está vivo —`vacante_pendiente_por_clave` y
`plan_de_una_lectura`—, así que un intento fallido:

  - sacaba la oferta de su propio botón, para siempre;
  - y la despedía con «Ya está clasificada: su veredicto está en la bandeja»,
    que era falso: no había veredicto, había un error.

Solo `--reintentar`, que es una bandera de consola sin botón, la devolvía. Es
decir: la regla 3 de la casa —nada se descarta en silencio— rota justo en el
camino que existe para que aplazar no signifique descartar.

NO ES UN CASO DE LABORATORIO: la cuenta de la clienta todavía no tiene saldo
(7 sept 2026), así que hoy CUALQUIER lectura suelta que pulse termina en ese
estado. El mensaje real de la API en esa situación entra aquí literal.

Todo offline: el cliente del motor es falso y no se gasta un céntimo.
"""

from __future__ import annotations

import pytest

from clasificador.cliente_llm import RespuestaLLM, mensaje_de_peticion_rechazada
from clasificador.motor import Motor
from clasificador.plan import plan_de_una_lectura
from recolector.storage import Storage

from .test_clasificador import (
    ClienteFalso,
    fichas_de_prueba,
    oferta_en_base,
    respuesta_ok,
)
from .test_servidor import entorno, sembrar  # noqa: F401

# El texto EXACTO de la API cuando la cuenta no tiene crédito. Va literal a
# propósito: es el estado de la cuenta de la clienta esta semana, y es lo que
# tiene que reconocer `mensaje_de_peticion_rechazada`.
CRUDO_DE_LA_API = (
    "Your credit balance is too low to access the Anthropic API. Please go "
    "to Plans & Billing to upgrade or purchase credits."
)

# Y lo que el cliente guarda de verdad en `clasificaciones.error` después de
# traducirlo. Es lo que la bandeja pinta, así que es lo que ella lee.
SIN_SALDO = mensaje_de_peticion_rechazada(CRUDO_DE_LA_API)


@pytest.fixture()
def almacen(tmp_path):
    with Storage(tmp_path / "prueba.db") as db:
        yield db


def _intento_fallido(almacen: Storage, clave: str, error: str = SIN_SALDO) -> None:
    """Gasta el turno de esa oferta contra una API que dice que no."""
    fallo = RespuestaLLM(
        clasificacion=None, tokens_entrada=0, tokens_salida=0,
        modelo="modelo-de-prueba", error=error,
    )
    motor = Motor(almacen, fichas_de_prueba(), ClienteFalso(fallo))
    motor.clasificar(almacen.vacante_pendiente_por_clave(clave))


# ---------------------------------------------------------------------------
#  El botón de la oferta
# ---------------------------------------------------------------------------

class TestElBotonDeLaOfertaSobreviveAlFallo:

    def test_sigue_siendo_pendiente_para_su_boton(self, almacen: Storage):
        """Un intento que no llegó a leerla no le quita el turno."""
        run = almacen.start_run("prueba", ["adzuna"], ["alemania"])
        clave = oferta_en_base(almacen, run)

        _intento_fallido(almacen, clave)

        assert almacen.obtener_oferta_con_clasificacion(clave)["clas_estado"] == (
            "sin_clasificar"
        )
        assert len(almacen.vacante_pendiente_por_clave(clave)) == 1

    def test_el_plan_no_la_da_por_clasificada(self, almacen: Storage):
        """`ya_clasificada` es lo que apaga el botón y escribe la frase.

        Con un error dentro, esa frase decía que el veredicto estaba en la
        bandeja. No estaba: eso es enseñarle un número —el de ofertas
        resueltas— que no se corresponde con nada.
        """
        run = almacen.start_run("prueba", ["adzuna"], ["alemania"])
        clave = oferta_en_base(almacen, run)
        _intento_fallido(almacen, clave)

        plan = plan_de_una_lectura(almacen, fichas_de_prueba(), clave)

        assert plan.existe
        assert not plan.ya_clasificada
        assert plan.se_puede
        assert plan.coste_usd > 0
        # Y el motivo viaja, porque volver a intentarlo a ciegas contra una
        # cuenta sin saldo es pulsar dos veces para el mismo error.
        assert plan.fallo_anterior is not None
        assert "sin saldo" in plan.fallo_anterior

    def test_un_veredicto_de_verdad_si_apaga_el_boton(self, almacen: Storage):
        """La caché no se salta desde un botón: eso no cambia.

        Es la mitad que hay que no romper al arreglar la otra. Una oferta con
        veredicto no se vuelve a pagar.
        """
        run = almacen.start_run("prueba", ["adzuna"], ["alemania"])
        clave = oferta_en_base(almacen, run)
        Motor(almacen, fichas_de_prueba(), ClienteFalso(respuesta_ok())).clasificar(
            almacen.vacante_pendiente_por_clave(clave)
        )

        plan = plan_de_una_lectura(almacen, fichas_de_prueba(), clave)

        assert plan.ya_clasificada
        assert not plan.se_puede
        assert plan.fallo_anterior is None
        assert almacen.vacante_pendiente_por_clave(clave) == []

    def test_el_reintento_pisa_la_fila_fallida(self, almacen: Storage):
        """Reintentar deja UN veredicto, no un veredicto y un error."""
        run = almacen.start_run("prueba", ["adzuna"], ["alemania"])
        clave = oferta_en_base(almacen, run)
        _intento_fallido(almacen, clave)

        cliente = ClienteFalso(respuesta_ok())
        Motor(almacen, fichas_de_prueba(), cliente).clasificar(
            almacen.vacante_pendiente_por_clave(clave)
        )

        assert cliente.llamadas == 1
        fila = almacen.obtener_oferta_con_clasificacion(clave)
        assert fila["clas_estado"] == "ok"
        assert fila["clas_error"] is None
        assert almacen.connection.execute(
            "SELECT COUNT(*) FROM clasificaciones WHERE dedupe_key = ?", (clave,)
        ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
#  Y la corrida en bloque, que NO debe cambiar
# ---------------------------------------------------------------------------

class TestLaCorridaEnBloqueNoSeContagia:
    """Devolverle el turno a UNA oferta que ella señala es una cosa.

    Que «Que el asistente lea lo nuevo» reintente solo las fallidas es otra
    muy distinta: son llamadas que ella no ha pedido una por una, y contra una
    API caída se convertirían en una factura repetida cada vez que pulsa. Por
    eso el reintento en bloque sigue siendo `--reintentar`, opt-in.
    """

    def test_la_fallida_no_vuelve_a_la_cola_por_su_cuenta(self, almacen: Storage):
        run = almacen.start_run("prueba", ["adzuna"], ["alemania"])
        clave = oferta_en_base(almacen, run)
        _intento_fallido(almacen, clave)

        huella = fichas_de_prueba().huella
        assert almacen.ofertas_pendientes(fichas_huella=huella) == []
        assert len(
            almacen.ofertas_pendientes(fichas_huella=huella, incluir_fallidas=True)
        ) == 1


# ---------------------------------------------------------------------------
#  El mensaje, que ella lee
# ---------------------------------------------------------------------------

class TestElMensajeDeLaApiLlegaEnCastellano:
    """`clasificaciones.error` no es un log: la bandeja lo pinta.

    `bandeja/static/app.js` vuelca las líneas del trabajo en `#trabajo-salida`
    y toma la última como titular. O sea que el texto que devuelve la API se
    le enseña tal cual a una persona que no tiene por qué leer inglés técnico
    ni saber qué es un «credit balance».
    """

    def test_sin_saldo_se_dice_en_castellano_y_sin_alarma(self):
        traducido = mensaje_de_peticion_rechazada(CRUDO_DE_LA_API)

        assert "credit balance" not in traducido.lower()
        assert "sin saldo" in traducido
        # Las tres preguntas que se hace quien lee un error a mitad de una
        # bandeja: ¿he roto algo?, ¿he perdido ofertas?, ¿me han cobrado?
        assert "no se ha cobrado nada" in traducido
        assert "conserva su turno" in traducido

    def test_reconoce_el_mensaje_aunque_cambien_la_puntuacion(self):
        """Se buscan dos trozos, no la frase entera."""
        variante = "Your credit balance is TOO LOW; purchase credits to continue"
        assert "sin saldo" in mensaje_de_peticion_rechazada(variante)

    def test_un_400_que_no_es_de_saldo_se_ensena_tal_cual(self):
        """No se traduce lo que no se ha reconocido.

        Inventar una frase amable para un error de parámetros escondería el
        único dato con el que se arregla: el que manda la API.
        """
        crudo = "max_tokens: must be less than or equal to 64000"
        salida = mensaje_de_peticion_rechazada(crudo)

        assert crudo in salida
        assert "sin saldo" not in salida


# ---------------------------------------------------------------------------
#  Lo que dice la pantalla
# ---------------------------------------------------------------------------

class TestLaFraseDeLaPantalla:

    def test_no_le_dice_que_hay_un_veredicto_que_no_existe(self, entorno):
        """El diálogo del botón, tal y como lo arma el servidor."""
        from bandeja import server

        clave = sembrar(entorno.almacen, 1)[0]
        _intento_fallido(entorno.almacen, clave)

        datos = server.plan_de_leer_una(clave)

        assert datos["hay_trabajo"]
        assert not datos["ya_clasificada"]
        assert datos["coste_usd"] > 0
        assert "veredicto está en la bandeja" not in datos["nota"]
        # Y se le cuenta que el intento anterior falló, sin el inglés de la API
        # crudo: eso va al log, no a su pantalla.
        assert datos["avisos"]
        assert "no se pudo leer" in " ".join(datos["avisos"]).lower()
