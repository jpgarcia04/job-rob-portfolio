"""
Lo que hace falta para poder arreglar esto desde otro continente.

El desarrollador entrega en México y se vuelve a España. A partir de ahí toda
conversación sobre un fallo tiene exactamente dos datos disponibles: **qué
versión corre** y **qué dice el log**. Si alguno de los dos miente o no está,
un fallo tonto se vuelve permanente.

Lo que se vigila aquí, por tanto, no es que la app funcione — de eso van las
otras pruebas — sino que **siga siendo diagnosticable cuando no funcione**.

Ninguna toca la red.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from recolector import registro
from recolector.version import Version, version_actual


# ---------------------------------------------------------------------------
#  La versión
# ---------------------------------------------------------------------------

class TestSaberQueVersionCorre:

    def test_sin_git_no_revienta(self, monkeypatch):
        """Una máquina sin git tiene que poder abrir la app.

        Que no se sepa la versión es una molestia; que no arranque, un
        desastre. Y es un caso real: si se entrega como zip en vez de como
        clon, no hay git ni repositorio.
        """
        monkeypatch.setattr("recolector.version._git", lambda *a: None)
        version_actual.cache_clear()

        v = version_actual()

        assert not v.desde_git
        assert v.etiqueta  # dice algo, no una cadena vacía
        assert not v.es_de_fiar
        assert "sin git" in v.para_log()

    def test_git_que_no_esta_instalado_no_lanza(self, monkeypatch):
        """El caso de verdad, no simulado a medias.

        `test_sin_git_no_revienta` reemplaza `_git` entero, asi que prueba el
        camino de respaldo pero NO que `_git` aguante. Si git no esta en el
        PATH, `subprocess.run` lanza FileNotFoundError, y eso tiene que
        quedarse dentro: una app que no arranca porque falta git es peor que
        una app que no sabe su version.
        """
        import subprocess as sp

        from recolector import version as modulo

        def revienta(*args, **kwargs):
            raise FileNotFoundError("git no esta instalado")

        monkeypatch.setattr(modulo.subprocess, "run", revienta)

        assert modulo._git("describe") is None

        version_actual.cache_clear()
        v = version_actual()
        assert not v.desde_git

    def test_git_que_tarda_demasiado_no_cuelga_la_app(self, monkeypatch):
        """Un `git describe` que se queda pensando no puede dejar la ventana
        en blanco. El timeout esta puesto; esto fija que su excepcion tampoco
        se escape."""
        import subprocess as sp

        from recolector import version as modulo

        def tarda(*args, **kwargs):
            raise sp.TimeoutExpired(cmd="git", timeout=5)

        monkeypatch.setattr(modulo.subprocess, "run", tarda)

        assert modulo._git("describe") is None

    def test_un_arbol_sucio_no_es_de_fiar(self, monkeypatch):
        """Cambios locales = ese código no está en ninguna parte del
        repositorio, así que «tengo la v1.2.0» ya no significa lo mismo en las
        dos puntas del teléfono."""
        respuestas = {
            ("describe", "--tags", "--always", "--dirty=+"): "v1.2.0+",
            ("rev-parse", "--short", "HEAD"): "abc1234",
            ("describe", "--tags", "--exact-match", "HEAD"): "v1.2.0",
        }
        monkeypatch.setattr("recolector.version._git", lambda *a: respuestas.get(a))
        version_actual.cache_clear()

        v = version_actual()

        assert not v.limpia
        assert not v.es_de_fiar
        assert v.para_pantalla().endswith("-sucio")

    def test_por_delante_de_la_etiqueta_tampoco(self, monkeypatch):
        """`v1.2.0-3-gabc` significa tres commits por encima de lo publicado.
        En la máquina de la clienta eso es un fallo de instalación: el
        actualizador solo mueve entre etiquetas."""
        respuestas = {
            ("describe", "--tags", "--always", "--dirty=+"): "v1.2.0-3-gabc1234",
            ("rev-parse", "--short", "HEAD"): "abc1234",
            # `--exact-match` falla cuando no estás justo en la etiqueta.
            ("describe", "--tags", "--exact-match", "HEAD"): None,
        }
        monkeypatch.setattr("recolector.version._git", lambda *a: respuestas.get(a))
        version_actual.cache_clear()

        v = version_actual()

        assert v.limpia
        assert not v.publicada
        assert not v.es_de_fiar

    def test_una_version_publicada_si_es_de_fiar(self, monkeypatch):
        respuestas = {
            ("describe", "--tags", "--always", "--dirty=+"): "v1.2.0",
            ("rev-parse", "--short", "HEAD"): "abc1234",
            ("describe", "--tags", "--exact-match", "HEAD"): "v1.2.0",
        }
        monkeypatch.setattr("recolector.version._git", lambda *a: respuestas.get(a))
        version_actual.cache_clear()

        v = version_actual()

        assert v.es_de_fiar
        assert v.para_pantalla() == "v1.2.0"

    def test_el_arbol_de_verdad_responde_algo(self):
        """Sobre este repositorio, sin simular nada: tiene que dar una cadena
        que se le pueda leer a alguien por teléfono."""
        version_actual.cache_clear()
        v = version_actual()
        assert isinstance(v, Version)
        assert v.para_pantalla().strip()


# ---------------------------------------------------------------------------
#  El log
# ---------------------------------------------------------------------------

@pytest.fixture()
def registro_limpio():
    """Deja el logging como estaba. Sin esto, una prueba que configura el log
    se lo deja configurado a las demás y los asertos se contaminan."""
    logger = logging.getLogger(registro.NOMBRE)
    manejadores = list(logger.handlers)
    estaba = registro._configurado
    logger.handlers.clear()
    registro._configurado = False
    yield
    for m in logger.handlers:
        m.close()
    logger.handlers.clear()
    logger.handlers.extend(manejadores)
    registro._configurado = estaba


class TestElLogQueEllaVaAMandar:

    def test_escribe_donde_dice_que_escribe(self, tmp_path: Path, registro_limpio):
        """La ruta se le enseña en la pantalla, así que tiene que ser la de
        verdad: si la app dijera una y escribiera en otra, ella mandaría un
        archivo que no existe."""
        destino = registro.configurar(tmp_path, consola=False)

        assert destino == registro.ruta_del_log(tmp_path)
        assert destino.exists()

    def test_la_primera_linea_es_la_version(self, tmp_path: Path, registro_limpio):
        """Es el primer dato que hace falta para entender cualquier cosa que
        venga debajo, y el que nadie se acuerda de apuntar."""
        destino = registro.configurar(tmp_path, consola=False)

        primera = destino.read_text(encoding="utf-8").splitlines()[0]
        assert "arranca" in primera
        assert version_actual().para_pantalla() in primera

    def test_configurar_dos_veces_no_duplica_lineas(self, tmp_path: Path, registro_limpio):
        """La bandeja y un comando pueden encenderlo los dos. Si duplicara
        manejadores, cada línea saldría dos veces y el archivo se llenaría con
        la mitad de información útil."""
        registro.configurar(tmp_path, consola=False)
        registro.configurar(tmp_path, consola=False)

        registro.log("prueba").info("una sola vez")

        contenido = registro.ruta_del_log(tmp_path).read_text(encoding="utf-8")
        assert contenido.count("una sola vez") == 1

    def test_vive_dentro_de_data(self, tmp_path: Path):
        """`data/` está en .gitignore. Un log con nombres de empresas y claves
        de ofertas no puede acabar en el repositorio ni por accidente."""
        assert registro.ruta_del_log(tmp_path).is_relative_to(tmp_path)

    def test_el_logger_funciona_sin_haberlo_configurado(self, registro_limpio):
        """Los tests y los comandos de consola no montan el log. Un logger que
        reventara por no estar configurado convertiría una línea de
        diagnóstico en una caída — justo en el camino de un error."""
        registro.log("suelto").info("esto no debe lanzar")
        registro.log("suelto").error("esto tampoco")

    def test_una_excepcion_deja_la_traza_entera(self, tmp_path: Path, registro_limpio):
        """Sin traza, «falló» no se puede diagnosticar desde 9.000 km."""
        registro.configurar(tmp_path, consola=False)

        try:
            raise ValueError("el fallo de ejemplo")
        except ValueError:
            registro.log("prueba").exception("algo se rompió")

        contenido = registro.ruta_del_log(tmp_path).read_text(encoding="utf-8")
        assert "algo se rompió" in contenido
        assert "ValueError" in contenido
        assert "el fallo de ejemplo" in contenido


class TestElLogSePuedeMandarSinMiedo:
    """Ella va a adjuntar este archivo a un correo. Tiene que poder hacerlo sin
    pensárselo, o no lo hará — y entonces no hay diagnóstico."""

    def test_no_lleva_la_api_key(self, tmp_path: Path, registro_limpio, monkeypatch):
        """Es fácil de romper sin querer: basta con volcar un objeto de
        configuración en una línea de diagnóstico."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secretisimo-no-debe-salir")
        destino = registro.configurar(tmp_path, consola=False)

        from recolector.config import load_config
        try:
            config = load_config()
        except Exception:
            pytest.skip("no se pudo cargar la configuración real")
        registro.log("prueba").info("configuración cargada: %s", config.database_path)

        contenido = destino.read_text(encoding="utf-8")
        assert "sk-ant" not in contenido
        assert "secretisimo" not in contenido

    def test_rota_para_que_quepa_en_un_correo(self):
        """Un log de 400 MB no se manda, y entonces es como no tenerlo."""
        total = registro.TAMANO_MAXIMO_BYTES * (1 + registro.COPIAS_DE_RESERVA)
        assert total <= 10_000_000, "no cabría en un correo"

    def test_es_un_solo_archivo_y_no_uno_por_dia(self, tmp_path: Path):
        """Hay que poder decirle «mándame ESE archivo». Una carpeta con treinta
        lo convierte en una conversación, y la conversación no va a pasar."""
        assert registro.ruta_del_log(tmp_path).name == f"{registro.NOMBRE}.log"
