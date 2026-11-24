"""
Que `.env` llegue al entorno del proceso.

POR QUÉ EXISTE ESTE ARCHIVO
---------------------------
El 2 septiembre 2026, la primera vez que se intentó clasificar de verdad, el
clasificador dijo «Falta ANTHROPIC_API_KEY» con la key escrita en `.env`.
Cargar el archivo era responsabilidad de `Credentials.load()` —la clase de las
keys de Adzuna y Jooble—, así que `.env` solo se leía si alguien pedía
credenciales de una FUENTE. El recolector las pedía; el clasificador no, y por
eso era inejecutable salvo exportando la variable a mano.

El fallo no tenía ningún síntoma hasta que costaba una sesión entera, porque
el mensaje de error apuntaba al sitio equivocado: decía que faltaba la key,
no que nadie hubiera abierto el archivo donde estaba.

Estas pruebas fijan el arreglo: cargar la configuración carga el entorno.
Ninguna toca la red ni necesita una key de verdad — la key es inventada.
"""

from __future__ import annotations

import os

import pytest

from recolector.config import cargar_env, load_config

# Un nombre que no existe en ningún `.env` real, para no depender de la
# máquina donde corran las pruebas ni pisar nada del desarrollador.
VARIABLE = "JOB_ROB_VARIABLE_DE_PRUEBA"


@pytest.fixture
def env_temporal(tmp_path, monkeypatch):
    """Un `.env` de mentira, y el entorno limpio de esa variable."""
    monkeypatch.delenv(VARIABLE, raising=False)
    archivo = tmp_path / ".env"
    archivo.write_text(f"{VARIABLE}=valor_del_archivo\n", encoding="utf-8")
    return archivo


class TestCargarEnv:
    def test_vuelca_el_archivo_en_el_entorno(self, env_temporal):
        cargar_env(env_temporal)
        assert os.environ[VARIABLE] == "valor_del_archivo"

    def test_no_pisa_lo_que_ya_estaba_en_el_entorno(self, env_temporal, monkeypatch):
        """Permite sobreescribir una key desde la consola sin editar `.env`."""
        monkeypatch.setenv(VARIABLE, "valor_de_la_consola")
        cargar_env(env_temporal)
        assert os.environ[VARIABLE] == "valor_de_la_consola"

    def test_sin_archivo_no_revienta(self, tmp_path):
        """Una máquina recién clonada no tiene `.env` todavía."""
        cargar_env(tmp_path / "no_existe.env")


class TestLoadConfigCargaElEntorno:
    """La prueba que habría cazado el bug.

    Es sobre `load_config` y no sobre `Credentials` a propósito: es el camino
    por el que pasan LOS DOS comandos, y el que garantiza que un punto de
    entrada nuevo no pueda volver a olvidarse de cargar el archivo.
    """

    def test_load_config_carga_el_env_del_proyecto(self, monkeypatch, tmp_path):
        llamadas: list[object] = []
        monkeypatch.setattr(
            "recolector.config.cargar_env",
            lambda ruta=None: llamadas.append(ruta),
        )
        load_config()
        assert llamadas, (
            "load_config() no cargó el entorno. El clasificador vuelve a no "
            "encontrar su API key aunque esté escrita en .env."
        )
