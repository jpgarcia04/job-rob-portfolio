"""Preparación común de las pruebas.

Algunas pruebas cargan las fichas REALES de `config/cvs.yaml`, y la carga
comprueba que cada CV exista en `data/cvs/` (es lo que se adjuntará a la
candidatura). Esa carpeta está fuera de git a propósito, así que en un clon
recién hecho no existe. Aquí se crean CVs vacíos de relleno para la sesión de
pruebas y se borran al terminar; nunca se toca un archivo que ya estuviera.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

RAIZ = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _cvs_de_relleno():
    datos = yaml.safe_load((RAIZ / "config" / "cvs.yaml").read_text(encoding="utf-8"))
    creados: list[Path] = []
    for ficha in datos.get("cvs", []):
        ruta = RAIZ / ficha["archivo"]
        if not ruta.exists():
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_bytes(b"")
            creados.append(ruta)
    yield
    for ruta in creados:
        ruta.unlink(missing_ok=True)
