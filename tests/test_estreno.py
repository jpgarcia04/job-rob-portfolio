"""
La primera búsqueda de un mercado nuevo, lanzada sola al abrir.

QUÉ SE VIGILA, Y POR QUÉ CADA COSA
----------------------------------
  - que se dispare SOLO una vez. Un mercado que de verdad no devuelva nada
    —Tamaulipas es el candidato— lanzaría una búsqueda en cada apertura para
    siempre si esto se decidiera solo por «no tiene ofertas»;
  - que NO se dispare cuando el mercado ya tiene ofertas. La búsqueda normal
    es de ella, con su botón;
  - que un fallo aquí no impida abrir la app. Es lo único de este archivo que
    de verdad no se puede romper: una app que no abre a 9.000 km es mucho peor
    que un estreno que no se hizo;
  - que se apunte ANTES de lanzar, para que una búsqueda que falla no se
    reintente sola en bucle.

Nada de esto abre un proceso ni toca la red: el registro de trabajos recibe su
lanzador por parámetro, igual que `Motor` recibe el cliente del LLM.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from bandeja import estreno
from bandeja.trabajos import Registro


class ProcesoFalso:
    """Un proceso que no existe: devuelve una línea de progreso y sale bien.

    `suelta` lo deja colgado hasta que la prueba quiera, que es lo que permite
    mirar el trabajo MIENTRAS está en curso — el estado que hace que la pantalla
    enseñe la barra.
    """

    def __init__(self, bloquea: threading.Event | None = None) -> None:
        self.stdout = iter(["  [  1/47] adzuna  México  «COO + Logistics»\n"])
        self.matado = False
        self._bloquea = bloquea

    def wait(self) -> int:
        if self._bloquea is not None:
            self._bloquea.wait(timeout=5)
        return 0

    def kill(self) -> None:
        self.matado = True
        if self._bloquea is not None:
            self._bloquea.set()


class AlmacenFalso:
    def __init__(self, por_mercado: dict[str, int]) -> None:
        self._por_mercado = por_mercado

    def counts_by_market(self) -> dict[str, int]:
        return dict(self._por_mercado)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class PerfilFalso:
    def __init__(self, mercados: list[str]) -> None:
        self.market_keys = mercados

    def planned_queries(self, *_a, **_k) -> int:
        return 47


class MercadoFalso:
    def __init__(self, label: str) -> None:
        self.label = label


class ConfigFalsa:
    def __init__(self, tmp_path: Path, mercados: list[str], base_existe: bool = True) -> None:
        self.database_path = tmp_path / "ofertas.db"
        if base_existe:
            self.database_path.write_text("", encoding="utf-8")
        self._perfil = PerfilFalso(mercados)

    def profile(self, *_a, **_k):
        return self._perfil

    def market(self, clave: str):
        return MercadoFalso({"mexico": "México", "texas_us": "Texas, EE.UU."}.get(clave, clave))


def _log(_nombre):
    class _L:
        def warning(self, *a, **k): pass
        def info(self, *a, **k): pass
        def exception(self, *a, **k): pass
    return _L()


COMANDO = {"argv": ["-m", "recolector", "run"], "etiqueta": "Buscar ofertas nuevas"}


def _registro(lanzados: list, bloquea: threading.Event | None = None) -> Registro:
    def lanzador(argv):
        lanzados.append(argv)
        return ProcesoFalso(bloquea)

    return Registro(lanzador=lanzador)


# ---------------------------------------------------------------------------
#  La decisión, en frío
# ---------------------------------------------------------------------------

class TestQueMercadosSeEstrenan:

    def test_uno_configurado_y_sin_ofertas(self):
        assert estreno.mercados_a_estrenar(
            ["alemania", "mexico", "texas_us"],
            {"alemania": 36, "texas_us": 308},
            set(),
        ) == ["mexico"]

    def test_ninguno_si_todos_tienen_ofertas(self):
        assert estreno.mercados_a_estrenar(
            ["alemania", "texas_us"], {"alemania": 36, "texas_us": 308}, set()
        ) == []

    def test_ninguno_si_ya_se_estreno(self):
        assert estreno.mercados_a_estrenar(
            ["mexico"], {}, {"mexico"}
        ) == []

    def test_un_mercado_que_devolvio_cero_no_se_reintenta(self):
        """El caso Tamaulipas: configurado, sin ofertas, y ya estrenado."""
        assert estreno.mercados_a_estrenar(
            ["alemania", "mexico"], {"alemania": 36}, {"mexico"}
        ) == []


# ---------------------------------------------------------------------------
#  El apunte en disco
# ---------------------------------------------------------------------------

class TestElApunte:

    def test_se_escribe_y_se_lee(self, tmp_path):
        assert estreno.ya_estrenados(tmp_path) == set()
        estreno.apuntar(tmp_path, ["mexico"])
        assert estreno.ya_estrenados(tmp_path) == {"mexico"}
        estreno.apuntar(tmp_path, ["brasil"])
        assert estreno.ya_estrenados(tmp_path) == {"mexico", "brasil"}

    def test_un_archivo_roto_se_lee_como_TODOS_y_no_como_ninguno(self, tmp_path):
        """EL LADO SEGURO ES NO HACER NADA.

        Si un archivo ilegible se leyera como «ninguno estrenado», una copia con
        el archivo corrupto lanzaría la búsqueda de todos los mercados en cada
        apertura, a sus espaldas.
        """
        (tmp_path / estreno.ARCHIVO).write_text("{ esto no es json", encoding="utf-8")
        estrenados = estreno.ya_estrenados(tmp_path)
        assert "mexico" in estrenados
        assert "cualquier_cosa" in estrenados
        assert estreno.mercados_a_estrenar(["mexico"], {}, estrenados) == []


# ---------------------------------------------------------------------------
#  El lanzamiento entero
# ---------------------------------------------------------------------------

class TestElEstreno:

    def test_lanza_la_busqueda_una_sola_vez(self, tmp_path):
        config = ConfigFalsa(tmp_path, ["mexico", "texas_us"])
        almacen = lambda: AlmacenFalso({"texas_us": 308})  # noqa: E731
        lanzados: list = []

        nota = estreno.estrenar_si_hace_falta(
            config, almacen, _registro(lanzados), COMANDO, _log
        )
        assert nota is not None
        assert "México" in nota
        assert "47 peticiones" in nota
        assert "sin coste" in nota
        assert lanzados == [["-m", "recolector", "run"]]

        # segunda apertura: ya está apuntado, no se repite
        nota2 = estreno.estrenar_si_hace_falta(
            config, almacen, _registro(lanzados), COMANDO, _log
        )
        assert nota2 is None
        assert len(lanzados) == 1

    def test_no_lanza_nada_si_todos_los_mercados_tienen_ofertas(self, tmp_path):
        config = ConfigFalsa(tmp_path, ["alemania", "texas_us"])
        almacen = lambda: AlmacenFalso({"alemania": 36, "texas_us": 308})  # noqa: E731
        lanzados: list = []

        assert estreno.estrenar_si_hace_falta(
            config, almacen, _registro(lanzados), COMANDO, _log
        ) is None
        assert lanzados == []

    def test_sin_base_de_datos_no_se_adelanta_al_boton_de_la_pantalla_vacia(self, tmp_path):
        """Primera instalación: la pantalla vacía ya tiene su botón grande."""
        config = ConfigFalsa(tmp_path, ["mexico"], base_existe=False)
        lanzados: list = []
        assert estreno.estrenar_si_hace_falta(
            config, lambda: AlmacenFalso({}), _registro(lanzados), COMANDO, _log
        ) is None
        assert lanzados == []

    def test_se_apunta_ANTES_de_lanzar(self, tmp_path):
        """Si la búsqueda falla, no se reintenta sola en cada apertura."""
        config = ConfigFalsa(tmp_path, ["mexico"])

        def lanzador_que_revienta(argv):
            raise RuntimeError("no hay Python")

        registro = Registro(lanzador=lanzador_que_revienta)
        estreno.estrenar_si_hace_falta(
            config, lambda: AlmacenFalso({}), registro, COMANDO, _log
        )
        assert estreno.ya_estrenados(tmp_path) == {"mexico"}

    def test_un_fallo_aqui_no_impide_abrir_la_app(self, tmp_path):
        """Lo único de este archivo que de verdad no se puede romper."""
        class ConfigQueRevienta:
            @property
            def database_path(self):
                raise RuntimeError("configuración ilegible")

        assert estreno.estrenar_si_hace_falta(
            ConfigQueRevienta(), lambda: None, _registro([]), COMANDO, _log
        ) is None

    def test_el_trabajo_queda_en_curso_para_que_la_pantalla_lo_retome(self, tmp_path):
        """Es lo que hace que ella vea la barra sin tocar nada.

        `app.js::iniciar()` pregunta por `/api/trabajos` al cargar y retoma el
        que esté en curso. Si el estreno no dejara trabajo, no habría barra.
        """
        config = ConfigFalsa(tmp_path, ["mexico"])
        colgado = threading.Event()
        registro = _registro([], bloquea=colgado)
        try:
            estreno.estrenar_si_hace_falta(
                config, lambda: AlmacenFalso({}), registro, COMANDO, _log
            )
            en_curso = registro.en_curso()
        finally:
            colgado.set()
        assert en_curso is not None
        assert en_curso.accion == "recolectar"
        assert en_curso.etiqueta == "Buscar ofertas nuevas"
        assert en_curso.coste_estimado_usd == 0.0
