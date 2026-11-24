"""
Los trabajos en segundo plano de la bandeja.

Ninguna de estas pruebas abre un proceso: el lanzador entra por parámetro,
igual que `Motor` recibe el cliente del LLM, y aquí se le pasa uno falso que
devuelve las líneas y el código de salida que la prueba quiera.

Lo que se vigila es lo que puede costar dinero o esconder algo:

  - que NO se puedan lanzar dos corridas a la vez, porque el libro de gastos
    se escribe entero sin cerrojo y la segunda borraría los apuntes de la
    primera — o sea, dinero gastado que desaparece de la cuenta del mes;
  - que el código 3 llegue a la pantalla como «no había saldo» y no como un
    fallo, que es la misma distinción que hace el resto del proyecto;
  - que el progreso no retroceda, porque una barra que baja parece un fallo.
"""

from __future__ import annotations

import threading
import time

import pytest

from bandeja import trabajos as modulo_trabajos
from bandeja.trabajos import Registro, TrabajoEnCursoError


class ProcesoFalso:
    """Escupe las líneas que le digan y termina con el código que le digan.

    `bloquear` es un evento que la prueba abre cuando quiere: sirve para tener
    un trabajo «corriendo» de verdad mientras se comprueba que el segundo se
    rechaza.
    """

    def __init__(
        self,
        lineas: list[str],
        codigo: int = 0,
        bloquear: threading.Event | None = None,
    ) -> None:
        self._lineas = lineas
        self._codigo = codigo
        self._bloquear = bloquear
        self.matado = False

    @property
    def stdout(self):
        def generador():
            for linea in self._lineas:
                yield linea + "\n"
            if self._bloquear is not None:
                self._bloquear.wait(timeout=5)
        return generador()

    def wait(self) -> int:
        return self._codigo

    def kill(self) -> None:
        self.matado = True
        if self._bloquear is not None:
            self._bloquear.set()


def lanzador_que_devuelve(proceso: ProcesoFalso):
    argv_vistos: list[list[str]] = []

    def lanzador(argv: list[str]):
        argv_vistos.append(argv)
        return proceso

    lanzador.argv_vistos = argv_vistos  # type: ignore[attr-defined]
    return lanzador


def esperar(trabajo, registro: Registro, segundos: float = 3.0):
    """Espera a que el hilo del trabajo termine. Devuelve el trabajo al día."""
    limite = time.time() + segundos
    while time.time() < limite:
        actual = registro.obtener(trabajo.id)
        if actual is not None and actual.terminado:
            return actual
        time.sleep(0.01)
    raise AssertionError("el trabajo no terminó a tiempo")


# ---------------------------------------------------------------------------
#  Uno cada vez
# ---------------------------------------------------------------------------

class TestUnoCadaVez:
    """La prueba que protege el dinero.

    `data/uso_llm.json` se lee, se modifica y se escribe entero, sin cerrojo.
    Dos corridas simultáneas se pisan los apuntes y lo gastado por una
    desaparece de la cuenta del mes, que es la única que impide pasarse del
    techo. Con un comando de consola no pasaba; con un botón, un doble clic
    basta.
    """

    def test_el_segundo_trabajo_se_rechaza(self):
        puerta = threading.Event()
        registro = Registro(
            lanzador=lanzador_que_devuelve(ProcesoFalso(["arrancando"], bloquear=puerta))
        )

        primero = registro.lanzar("clasificar", ["-m", "clasificador"], "Clasificar")

        with pytest.raises(TrabajoEnCursoError) as fallo:
            registro.lanzar("recolectar", ["-m", "recolector", "run"], "Buscar")

        assert fallo.value.en_curso.id == primero.id
        puerta.set()
        esperar(primero, registro)

    def test_tras_terminar_uno_se_puede_lanzar_otro(self):
        registro = Registro(lanzador=lanzador_que_devuelve(ProcesoFalso(["listo"])))

        primero = registro.lanzar("clasificar", ["-m", "clasificador"], "Clasificar")
        esperar(primero, registro)

        segundo = registro.lanzar("ranking", ["-m", "clasificador", "ranking"], "Ordenar")
        assert segundo.id != primero.id
        esperar(segundo, registro)

    def test_el_rechazo_vale_tambien_entre_acciones_distintas(self):
        """El recolector escribe en la misma base que el clasificador lee."""
        puerta = threading.Event()
        registro = Registro(
            lanzador=lanzador_que_devuelve(ProcesoFalso([], bloquear=puerta))
        )
        primero = registro.lanzar("recolectar", ["-m", "recolector", "run"], "Buscar")

        with pytest.raises(TrabajoEnCursoError):
            registro.lanzar("leer_una", ["-m", "clasificador", "--solo", "x"], "Leer")

        puerta.set()
        esperar(primero, registro)


# ---------------------------------------------------------------------------
#  Los códigos de salida significan cosas distintas
# ---------------------------------------------------------------------------

class TestEstadosFinales:
    @pytest.mark.parametrize(
        "codigo,estado",
        [(0, "terminado"), (2, "con_errores"), (3, "sin_saldo"), (1, "fallido")],
    )
    def test_cada_codigo_tiene_su_estado(self, codigo, estado):
        registro = Registro(
            lanzador=lanzador_que_devuelve(ProcesoFalso(["algo"], codigo=codigo))
        )
        trabajo = registro.lanzar("clasificar", ["-m", "clasificador"], "Clasificar")

        assert esperar(trabajo, registro).estado == estado

    def test_sin_saldo_no_es_un_fallo(self):
        """La regla nº 5 de la casa, llevada a la pantalla.

        Lo que no se hizo por falta de dinero sigue PENDIENTE y se hará solo
        cuando haya saldo. Enseñarlo como un error haría que ella creyera que
        algo se rompió, y peor: que esas ofertas se perdieron.
        """
        registro = Registro(
            lanzador=lanzador_que_devuelve(ProcesoFalso([], codigo=3))
        )
        trabajo = esperar(
            registro.lanzar("clasificar", ["-m", "clasificador"], "Clasificar"),
            registro,
        )

        assert trabajo.estado == "sin_saldo"
        assert not trabajo.fue_bien
        assert trabajo.estado != "fallido"

    def test_con_errores_no_borra_lo_que_si_se_hizo(self):
        """El código 2 es «algunas ofertas quedaron sin clasificar»."""
        registro = Registro(
            lanzador=lanzador_que_devuelve(ProcesoFalso([], codigo=2))
        )
        trabajo = esperar(
            registro.lanzar("clasificar", ["-m", "clasificador"], "Clasificar"),
            registro,
        )

        assert trabajo.estado == "con_errores"
        assert trabajo.fue_bien, "hubo trabajo hecho; no es una corrida perdida"

    def test_si_el_comando_no_arranca_el_trabajo_queda_fallido(self):
        def lanzador_roto(argv):
            raise OSError("no existe el intérprete")

        registro = Registro(lanzador=lanzador_roto)
        trabajo = registro.lanzar("clasificar", ["-m", "clasificador"], "Clasificar")

        assert trabajo.estado == "fallido"
        assert registro.en_curso() is None, "un trabajo que no arrancó no bloquea"


# ---------------------------------------------------------------------------
#  El progreso
# ---------------------------------------------------------------------------

class TestProgreso:
    def test_lee_el_contador_del_recolector(self):
        registro = Registro(
            lanzador=lanzador_que_devuelve(
                ProcesoFalso([
                    "Consultando...",
                    "  [  1/46] adzuna  México         «COO + Manufactura»",
                    "  [ 12/46] adzuna  México         «Director + Logística»",
                ])
            )
        )
        trabajo = esperar(
            registro.lanzar("recolectar", ["-m", "recolector", "run"], "Buscar"),
            registro,
        )

        assert trabajo.total == 46
        assert trabajo.hechos == 46, "al terminar bien, la barra se completa"

    def test_lee_el_contador_del_clasificador(self):
        puerta = threading.Event()
        registro = Registro(
            lanzador=lanzador_que_devuelve(
                ProcesoFalso(
                    ["  [1/8] Director de Operaciones", "  [3/8] Gerente corporativo"],
                    bloquear=puerta,
                )
            )
        )
        trabajo = registro.lanzar("clasificar", ["-m", "clasificador"], "Clasificar")

        limite = time.time() + 3
        while time.time() < limite and registro.obtener(trabajo.id).hechos < 3:
            time.sleep(0.01)

        vivo = registro.obtener(trabajo.id)
        assert (vivo.hechos, vivo.total) == (3, 8)
        puerta.set()
        esperar(trabajo, registro)

    def test_la_barra_no_retrocede(self):
        """El reporte final trae otros números entre corchetes.

        Una barra que baja parece un fallo, y aquí no lo es: es que el mismo
        patrón aparece en texto que no es progreso.
        """
        registro = Registro(
            lanzador=lanzador_que_devuelve(
                ProcesoFalso([
                    "  [ 40/46] adzuna",
                    "Reporte: [2/9] fuentes con resultados",
                ])
            )
        )
        trabajo = esperar(
            registro.lanzar("recolectar", ["-m", "recolector", "run"], "Buscar"),
            registro,
        )
        assert trabajo.total == 46

    def test_sin_contador_no_se_inventa_progreso(self):
        registro = Registro(
            lanzador=lanzador_que_devuelve(ProcesoFalso(["ordenando el lote"]))
        )
        trabajo = esperar(
            registro.lanzar("ranking", ["-m", "clasificador", "ranking"], "Ordenar"),
            registro,
        )
        assert trabajo.total == 0


# ---------------------------------------------------------------------------
#  Lo que se enseñó antes de gastar
# ---------------------------------------------------------------------------

class TestElNumeroQueSeEnseno:
    def test_el_coste_confirmado_viaja_con_el_trabajo(self):
        """Para poder comparar después lo prometido con lo gastado.

        La regla no es solo enseñar un número: es que ese número se parezca a
        la factura, y sin guardarlo no hay forma de comprobarlo.
        """
        registro = Registro(lanzador=lanzador_que_devuelve(ProcesoFalso([])))
        trabajo = registro.lanzar(
            "clasificar", ["-m", "clasificador"], "Clasificar",
            coste_estimado_usd=1.0266,
        )

        assert trabajo.a_json()["coste_estimado_usd"] == 1.0266
        esperar(trabajo, registro)


# ---------------------------------------------------------------------------
#  Cancelar
# ---------------------------------------------------------------------------

class TestCancelar:
    def test_matar_el_proceso_deja_el_trabajo_cancelado(self):
        puerta = threading.Event()
        proceso = ProcesoFalso(["arrancando"], codigo=1, bloquear=puerta)
        registro = Registro(lanzador=lanzador_que_devuelve(proceso))

        trabajo = registro.lanzar("clasificar", ["-m", "clasificador"], "Clasificar")
        assert registro.cancelar(trabajo.id)

        final = esperar(trabajo, registro)
        assert proceso.matado
        assert final.estado == "cancelado", (
            "el código de salida de un proceso muerto no dice nada: cancelar "
            "no es lo mismo que fallar"
        )

    def test_cancelar_uno_ya_terminado_no_hace_nada(self):
        registro = Registro(lanzador=lanzador_que_devuelve(ProcesoFalso([])))
        trabajo = esperar(
            registro.lanzar("ranking", ["-m", "clasificador", "ranking"], "Ordenar"),
            registro,
        )
        assert not registro.cancelar(trabajo.id)

    def test_tras_cancelar_se_puede_lanzar_otro(self):
        puerta = threading.Event()
        registro = Registro(
            lanzador=lanzador_que_devuelve(ProcesoFalso([], bloquear=puerta))
        )
        primero = registro.lanzar("clasificar", ["-m", "clasificador"], "Clasificar")
        registro.cancelar(primero.id)
        esperar(primero, registro)

        assert registro.en_curso() is None


# ---------------------------------------------------------------------------
#  El historial
# ---------------------------------------------------------------------------

class TestHistorial:
    def test_se_guarda_la_salida_del_comando(self):
        registro = Registro(
            lanzador=lanzador_que_devuelve(ProcesoFalso(["primera", "segunda"]))
        )
        trabajo = esperar(
            registro.lanzar("ranking", ["-m", "clasificador", "ranking"], "Ordenar"),
            registro,
        )
        assert trabajo.a_json()["lineas"] == ["primera", "segunda"]

    def test_el_historico_no_crece_sin_fin(self):
        registro = Registro(
            lanzador=lanzador_que_devuelve(ProcesoFalso([])), maximo_historico=3
        )
        for _ in range(6):
            esperar(registro.lanzar("ranking", ["-m", "x"], "Ordenar"), registro)

        assert len(registro.ultimos(50)) == 3

    def test_los_argumentos_llegan_tal_cual_al_lanzador(self):
        lanzador = lanzador_que_devuelve(ProcesoFalso([]))
        registro = Registro(lanzador=lanzador)
        esperar(
            registro.lanzar(
                "clasificar", ["-m", "clasificador", "--manual"], "Clasificar"
            ),
            registro,
        )

        assert lanzador.argv_vistos == [["-m", "clasificador", "--manual"]]


# ---------------------------------------------------------------------------
#  Cómo se arranca el proceso de verdad
# ---------------------------------------------------------------------------

class TestElLanzadorReal:
    """Lo único que no se puede probar con un lanzador falso.

    No se abre ningún proceso: se intercepta `Popen` y se mira con qué lo
    habrían llamado. Sigue siendo una prueba offline y no gasta nada.
    """

    def _capturar(self, monkeypatch):
        visto = {}

        class PopenFalso:
            def __init__(self, argv, **kwargs):
                visto["argv"] = argv
                visto.update(kwargs)

        monkeypatch.setattr(modulo_trabajos.subprocess, "Popen", PopenFalso)
        modulo_trabajos.lanzador_de_procesos(["-m", "clasificador"])
        return visto

    def test_fuerza_utf8_en_el_hijo(self, monkeypatch):
        """Sin esto, los acentos vuelven rotos y ella lee «energ?a».

        En Windows un proceso Python escribe en la página de códigos de la
        consola (cp1252) salvo que se le diga otra cosa. Los comandos del
        proyecto la reconfiguran al arrancar, pero eso deja fuera lo que
        imprime el propio intérprete antes de esa línea — los `Traceback`,
        justo el texto que hay que poder leer entero cuando algo falla.

        Medido con un subproceso real: sin la variable llega «energ�a».
        """
        visto = self._capturar(monkeypatch)

        assert visto["env"]["PYTHONIOENCODING"] == "utf-8"
        assert visto["encoding"] == "utf-8"

    def test_usa_el_interprete_del_entorno_virtual(self, monkeypatch):
        """`sys.executable`, no un `python` cualquiera del PATH.

        El del PATH puede no tener instalado ni el SDK ni FastAPI.
        """
        import sys

        visto = self._capturar(monkeypatch)
        assert visto["argv"][0] == sys.executable

    def test_la_salida_de_error_va_junto_a_la_normal(self, monkeypatch):
        """Para la pantalla de ella son lo mismo: lo que pasó."""
        visto = self._capturar(monkeypatch)
        assert visto["stderr"] is modulo_trabajos.subprocess.STDOUT
