"""
La pregunta del encargo: ¿puede una secuencia de clics gastar más de lo que
se le enseñó?

No es una pregunta de opinión ni de lectura del código: hay que atacarlo. Aquí
están los ataques, todos offline y con dobles —ni una llamada a la API, ni un
céntimo—, y todos escritos desde el gesto que ella puede hacer de verdad:
pulsar dos veces, dejar un diálogo abierto mientras se toma un café, tener dos
pestañas, o parar a mitad.

LA RESPUESTA, MEDIDA (8 septiembre 2026), en dos mitades:

  1. **Por acumulación, no.** El guardia comprueba el tope contra lo que la
     acción lleva GASTADO de verdad, no contra la estimación con la que
     arrancó, así que una corrida de cuarenta lecturas se para sola aunque
     cada llamada cueste el cuádruple de lo previsto. Medido abajo.

  2. **Por la última llamada, sí, y como mucho por ella.** El guardia deja
     pasar una llamada cuando `gastado + estimado <= tope`; si esa llamada
     cuesta más que su estimación, el exceso sobre el tope es exactamente esa
     diferencia. No se acumula: la siguiente ya no pasa. Con el estimador
     equivocándose por DIEZ veces, el exceso medido fueron $0,0695 sobre
     $0,60.

Esa segunda mitad es una limitación conocida, no un fallo escondido, y estas
pruebas la fijan con un número para que nadie la empeore sin enterarse.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi import HTTPException

from bandeja import server
from clasificador.cliente_llm import RespuestaLLM
from clasificador.motor import Motor
from clasificador.presupuesto import GuardiaPresupuesto, LibroDeGastos
from recolector.config import LlmBudgetConfig
from recolector.storage import Storage

from .test_clasificador import (  # noqa: F401
    clasificacion,
    fichas_de_prueba,
    oferta_en_base,
)
from .test_servidor import config_falsa, entorno, sembrar  # noqa: F401
from .test_trabajos import ProcesoFalso, lanzador_que_devuelve

TOPE = 0.60


def _esperar_a_que_termine(registro, trabajo_id: str, segundos: float = 5.0) -> None:
    """Deja que el hilo del trabajo acabe antes de que pytest desmonte todo."""
    import time

    limite = time.time() + segundos
    while time.time() < limite:
        actual = registro.obtener(trabajo_id)
        if actual is not None and actual.terminado:
            return
        time.sleep(0.01)
    raise AssertionError("el trabajo no terminó a tiempo")


def _cada_peticion_su_conexion(entorno, monkeypatch) -> None:
    """`_abrir_almacen` abre una conexión NUEVA, como en producción.

    La fixture del servidor devuelve siempre la misma para poder mirar la base
    desde la prueba, y eso vale para todo menos para esto: SQLite prohíbe usar
    una conexión desde otro hilo, así que con la compartida el ataque de las
    dos pestañas moriría por el andamio y no por el fallo que busca. Aquí se
    hace lo que hace el servidor de verdad: una por petición.
    """
    ruta = entorno.config.database_path
    monkeypatch.setattr(server, "_abrir_almacen", lambda: Storage(ruta))


# ---------------------------------------------------------------------------
#  Andamiaje
# ---------------------------------------------------------------------------

class ClienteQueCuestaDeMas:
    """Devuelve un veredicto válido y cobra MÁS de lo que se estimó.

    Es el doble que hace falta para atacar el tope: un cliente que cuesta lo
    previsto no puede rebasarlo nunca, así que no prueba nada. Apunta en el
    mismo libro que el guardia —un solo libro por proceso— porque si abriera
    el suyo, el guardia leería un saldo congelado y el freno no frenaría.
    """

    modelo = "modelo-de-prueba"

    def __init__(self, coste_real: float, libro: LibroDeGastos) -> None:
        self.coste_real = coste_real
        self._libro = libro
        self.llamadas = 0

    def clasificar(self, sistema: str, oferta: str) -> RespuestaLLM:
        self.llamadas += 1
        self._libro.registrar(self.coste_real)
        return RespuestaLLM(
            clasificacion=clasificacion(), tokens_entrada=900, tokens_salida=400,
            modelo=self.modelo, coste_medido_usd=self.coste_real,
        )


def _corrida_con_estimador_corto(tmp_path: Path, factor: float, cuantas: int = 40):
    """Una corrida de `cuantas` ofertas donde cada llamada cuesta `factor` veces
    lo estimado. Devuelve (gastado, estimado_por_llamada, cliente)."""
    with Storage(tmp_path / "p.db") as db:
        run = db.start_run("prueba", ["adzuna"], ["texas_us"])
        for i in range(cuantas):
            oferta_en_base(db, run, external_id=str(i), title=f"Director {i}")

        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        guardia = GuardiaPresupuesto(
            libro,
            LlmBudgetConfig(
                presupuesto_mensual_usd=100.0, al_agotarse="avisar",
                racion_automatica_mensual_usd=100.0, avisar_al_superar_pct=75,
                tope_por_accion_usd=TOPE,
            ),
            es_manual=True,
        )
        fichas = fichas_de_prueba()
        filas = db.ofertas_pendientes(fichas_huella=fichas.huella)

        # Lo que el guardia va a estimar por llamada, calculado con su misma
        # fórmula: si la prueba usara otra, no estaría midiendo el freno.
        estimado = Motor(db, fichas, None)._coste_de_una(filas[0])

        cliente = ClienteQueCuestaDeMas(estimado * factor, libro)
        Motor(db, fichas, cliente, presupuesto=guardia).clasificar(filas)
        return libro.gastado_total(), estimado, cliente


# ---------------------------------------------------------------------------
#  1. Por acumulación no se puede pasar
# ---------------------------------------------------------------------------

class TestElTopeAguantaAunqueLaEstimacionFalle:

    @pytest.mark.parametrize("factor", [1.0, 2.0, 4.0])
    def test_no_se_rebasa_por_acumulacion(self, tmp_path: Path, factor: float):
        """Cuarenta lecturas, cada una costando hasta el cuádruple.

        Lo que lo salva es que `permite()` mira lo GASTADO de verdad y no la
        estimación con la que arrancó la acción. Si alguien cambiara eso por
        una resta contra el plan inicial, esta prueba se cae.
        """
        gastado, _, cliente = _corrida_con_estimador_corto(tmp_path, factor)

        assert gastado <= TOPE, (
            f"con el estimador corto x{factor} la corrida gastó ${gastado:.4f}"
        )
        assert cliente.llamadas > 0, "la corrida tiene que haber hecho algo"

    def test_y_se_para_sola_en_vez_de_terminar(self, tmp_path: Path):
        """Con el coste x4, no le da para las 40: se para y deja pendientes."""
        _, _, cliente = _corrida_con_estimador_corto(tmp_path, 4.0, cuantas=40)
        assert cliente.llamadas < 40


class TestLoQueSiPuedeRebasarlo:
    """La limitación conocida, con su número.

    El guardia deja pasar una llamada cuando `gastado + estimado <= tope`. Si
    esa llamada cuesta más que su estimación, el tope se rebasa por la
    diferencia. Es de UNA llamada y no se acumula: la siguiente ya no pasa.
    """

    def test_el_exceso_es_como_mucho_el_error_de_una_llamada(self, tmp_path: Path):
        gastado, estimado, cliente = _corrida_con_estimador_corto(tmp_path, 10.0)
        exceso = gastado - TOPE
        error_de_una = cliente.coste_real - estimado

        assert exceso > 0, (
            "si esto deja de pasarse, el freno ha cambiado y hay que reescribir "
            "lo que se documenta como limitación"
        )
        assert exceso <= error_de_una + 1e-9, (
            f"el exceso (${exceso:.4f}) supera el error de UNA llamada "
            f"(${error_de_una:.4f}): entonces sí se acumula, y eso es otro fallo"
        )
        assert exceso < cliente.coste_real

    def test_y_no_crece_con_el_numero_de_ofertas(self, tmp_path: Path):
        """El mismo exceso con 40 ofertas que con 200.

        Es lo que distingue «se pasa por una llamada» de «se pasa por corrida
        larga». Si el exceso creciera con la cola, el tope no sería un tope
        sino una sugerencia, y una corrida de 300 se lo comería.

        No se comparan los dos importes al céntimo: la estimación depende de lo
        que mida cada anuncio y los títulos no son idénticos. Lo que se fija es
        la COTA, que es la afirmación que se documenta.
        """
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        pocas, est_p, cli_p = _corrida_con_estimador_corto(tmp_path / "a", 10.0, 40)
        muchas, est_m, cli_m = _corrida_con_estimador_corto(tmp_path / "b", 10.0, 200)

        assert pocas - TOPE <= cli_p.coste_real - est_p + 1e-9
        assert muchas - TOPE <= cli_m.coste_real - est_m + 1e-9
        assert muchas - TOPE == pytest.approx(pocas - TOPE, abs=0.01), (
            "el exceso ha crecido al alargar la cola: entonces se acumula"
        )


# ---------------------------------------------------------------------------
#  2. Los clics: dos veces, dos pestañas, un diálogo viejo
# ---------------------------------------------------------------------------

class TestDosPulsaciones:

    def test_la_segunda_se_rechaza_con_409_y_no_lanza_nada(self, entorno):
        """Pulsar otra vez porque «no parece que pase nada».

        `AYUDA.md` ya le pide que no lo haga; esto comprueba que si lo hace no
        pasa nada malo. 409 y no 500: el servidor tiene que contestar algo que
        la pantalla pueda pintar.
        """
        sembrar(entorno.almacen, 30)
        bloqueo = threading.Event()
        lanzador = lanzador_que_devuelve(ProcesoFalso(["va"], bloquear=bloqueo))
        entorno.registro._lanzador = lanzador

        primero = server.lanzar_trabajo(server.LanzarTrabajo(accion="clasificar"))

        with pytest.raises(HTTPException) as fallo:
            server.lanzar_trabajo(server.LanzarTrabajo(accion="clasificar"))

        assert fallo.value.status_code == 409
        assert len(lanzador.argv_vistos) == 1, "se lanzó un segundo proceso"
        assert entorno.registro.en_curso().id == primero["id"]

        bloqueo.set()
        _esperar_a_que_termine(entorno.registro, primero["id"])

    def test_dos_pestanas_a_la_vez_solo_arrancan_una(self, entorno, monkeypatch):
        """El ataque de verdad: las dos peticiones a la vez, no seguidas.

        Entre comprobar el presupuesto y arrancar el proceso hay una ventana, y
        si el registro no cerrara con llave, las dos peticiones pasarían el
        control y se pisarían el libro de gastos. Se lanzan desde dos hilos
        porque secuencialmente esa ventana no existe.
        """
        sembrar(entorno.almacen, 30)
        _cada_peticion_su_conexion(entorno, monkeypatch)
        bloqueo = threading.Event()
        lanzador = lanzador_que_devuelve(ProcesoFalso(["va"], bloquear=bloqueo))
        entorno.registro._lanzador = lanzador

        salidas: list[object] = []
        listos = threading.Barrier(2)

        def pulsar():
            listos.wait()
            try:
                salidas.append(server.lanzar_trabajo(
                    server.LanzarTrabajo(accion="clasificar")))
            except HTTPException as exc:
                salidas.append(exc)

        hilos = [threading.Thread(target=pulsar) for _ in range(2)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(timeout=10)

        arrancados = [s for s in salidas if isinstance(s, dict)]
        rechazados = [s for s in salidas if isinstance(s, HTTPException)]

        assert len(arrancados) == 1, "arrancaron dos corridas a la vez"
        assert len(rechazados) == 1
        assert rechazados[0].status_code == 409
        assert len(lanzador.argv_vistos) == 1

        bloqueo.set()
        _esperar_a_que_termine(entorno.registro, arrancados[0]["id"])


class TestElDialogoQueLlevaDiezMinutosAbierto:
    """`MARGEN_DE_CONFIRMACION` = 1,25. El margen existe porque el coste se
    mueve un poco solo (una oferta más, una descripción más larga), y rechazar
    por dos céntimos sería inservible. Lo que no puede es dejar pasar un salto.
    """

    def test_un_salto_por_encima_del_margen_no_lanza(self, entorno):
        sembrar(entorno.almacen, 200)
        with pytest.raises(HTTPException) as fallo:
            server.lanzar_trabajo(server.LanzarTrabajo(
                accion="clasificar", coste_confirmado_usd=0.01))

        assert fallo.value.status_code == 409
        assert "ha subido" in fallo.value.detail
        assert entorno.registro.en_curso() is None

    def test_justo_por_debajo_del_margen_si_lanza(self, entorno):
        """El margen es a propósito: comprobar el borde por los dos lados.

        Si solo se probara el rechazo, bajar `MARGEN_DE_CONFIRMACION` a 1,0
        pasaría inadvertido y la app rechazaría corridas legítimas cada vez que
        entrara una oferta nueva entre el diálogo y el clic.
        """
        sembrar(entorno.almacen, 30)
        ahora = server.plan("clasificar")["coste_usd"]
        # Confirmó algo un 20% más barato: dentro del 25% de margen.
        confirmado = ahora / 1.20

        trabajo = server.lanzar_trabajo(server.LanzarTrabajo(
            accion="clasificar", coste_confirmado_usd=confirmado))
        assert trabajo["estado"] in ("corriendo", "terminado")

    def test_el_tope_por_accion_gana_al_margen(self, entorno, monkeypatch):
        """Y este es el orden que importa.

        Si el margen se comprobara ANTES que el tope, una confirmación barata
        haría que un coste actual por encima del tope se rechazara con «el
        coste ha subido» —un 409, que la pantalla pinta como «vuelve a
        pedirlo»— en vez de con el motivo real. Ella volvería a pulsar y se
        encontraría lo mismo sin saber por qué.
        """
        sembrar(entorno.almacen, 30)
        monkeypatch.setattr(
            server, "load_config",
            lambda *a, **k: config_falsa(entorno.tmp_path, tope_accion=0.001),
        )

        with pytest.raises(HTTPException) as fallo:
            server.lanzar_trabajo(server.LanzarTrabajo(
                accion="clasificar", coste_confirmado_usd=0.0001))

        assert fallo.value.status_code == 402
        assert "tope por acción" in fallo.value.detail
        assert entorno.registro.en_curso() is None


# ---------------------------------------------------------------------------
#  3. Parar a mitad
# ---------------------------------------------------------------------------

class TestPararAMitad:
    """«Parar» no devuelve lo gastado, y no tiene por qué: lo hecho está hecho.

    Lo que sí tiene que cumplirse es que el libro cuadre —ni un céntimo de más
    ni de menos— y que lo que no se hizo siga pendiente. Es la misma promesa
    que `AYUDA.md` le hace por escrito.
    """

    def test_el_libro_tiene_lo_hecho_y_ni_un_centimo_mas(self, tmp_path: Path):
        with Storage(tmp_path / "p.db") as db:
            run = db.start_run("prueba", ["adzuna"], ["texas_us"])
            for i in range(10):
                oferta_en_base(db, run, external_id=str(i), title=f"Director {i}")

            libro = LibroDeGastos(tmp_path / "uso_llm.json")
            guardia = GuardiaPresupuesto(
                libro,
                LlmBudgetConfig(
                    presupuesto_mensual_usd=100.0, al_agotarse="avisar",
                    racion_automatica_mensual_usd=100.0, avisar_al_superar_pct=75,
                    tope_por_accion_usd=0.05,   # da para tres y se para
                ),
                es_manual=True,
            )
            fichas = fichas_de_prueba()
            filas = db.ofertas_pendientes(fichas_huella=fichas.huella)
            cliente = ClienteQueCuestaDeMas(0.01, libro)
            Motor(db, fichas, cliente, presupuesto=guardia).clasificar(filas)

            apuntado = libro.gastado_total()
            guardadas = db.connection.execute(
                "SELECT COUNT(*) FROM clasificaciones WHERE estado = 'ok'"
            ).fetchone()[0]

            assert cliente.llamadas > 0
            assert guardadas == cliente.llamadas, (
                "se cobró una llamada cuyo veredicto no se guardó, o al revés"
            )
            assert apuntado == pytest.approx(cliente.llamadas * 0.01)

    def test_lo_que_no_se_hizo_sigue_pendiente(self, tmp_path: Path):
        """Ninguna oferta se pierde al pararse: es literalmente lo que dice la
        hoja de ayuda, y lo que hace que volver a pulsar siga donde iba."""
        with Storage(tmp_path / "p.db") as db:
            run = db.start_run("prueba", ["adzuna"], ["texas_us"])
            for i in range(10):
                oferta_en_base(db, run, external_id=str(i), title=f"Director {i}")

            libro = LibroDeGastos(tmp_path / "uso_llm.json")
            guardia = GuardiaPresupuesto(
                libro,
                LlmBudgetConfig(
                    presupuesto_mensual_usd=100.0, al_agotarse="avisar",
                    racion_automatica_mensual_usd=100.0, avisar_al_superar_pct=75,
                    tope_por_accion_usd=0.05,
                ),
                es_manual=True,
            )
            fichas = fichas_de_prueba()
            cliente = ClienteQueCuestaDeMas(0.01, libro)
            resumen = Motor(db, fichas, cliente, presupuesto=guardia).clasificar(
                db.ofertas_pendientes(fichas_huella=fichas.huella))

            pendientes = db.ofertas_pendientes(fichas_huella=fichas.huella)

            assert resumen.detenidas_por_presupuesto > 0
            assert len(pendientes) == 10 - cliente.llamadas
            assert resumen.avisos, "pararse en seco sin decirlo es descartar en silencio"
