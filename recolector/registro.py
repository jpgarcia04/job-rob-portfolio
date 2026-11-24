"""
El log. Lo único que el desarrollador va a poder leer desde 9.000 km.

POR QUÉ EXISTE
--------------
Hoy los errores de este proyecto van a una consola. Eso funciona mientras el
que lanza el comando es el que escribió el código; deja de funcionar el día que
la app la abre una ejecutiva senior desde un icono y el desarrollador está en
otro continente.

Sin esto, **«no funciona» es toda la información que va a existir nunca**. Poder
mandar un arreglo (`herramientas/actualizar.ps1`) no sirve de nada si no se
sabe qué arreglar: son las dos mitades del mismo mantenimiento, y esta es la
que dice qué pasó.

QUÉ SE APUNTA, Y QUÉ NO
-----------------------
Se apunta lo que hace falta para reconstruir un fallo por teléfono: la versión
que corría, qué acción se lanzó, qué código de salida dio y qué excepción hubo.

**No se apunta el contenido de las ofertas ni nada del CV.** El log es un
archivo que ella va a acabar mandando por correo, así que tiene que poder
mandarse sin pensárselo. Las ofertas se identifican por su `dedupe_key`, que no
dice nada por sí sola y sirve igual para buscarla en la base.

Tampoco se apuntan las API keys, evidentemente. Como eso es fácil de romper sin
querer —basta con volcar un objeto de configuración— hay una prueba que lo fija.

POR QUÉ UN SOLO ARCHIVO Y NO UNO POR DÍA
----------------------------------------
Porque hay que poder decirle «mándame ESE archivo», y una carpeta con treinta
lo convierte en una conversación. Rota por tamaño y guarda tres vueltas: lo
suficiente para que un fallo de la semana pasada siga estando, y poco para que
quepa en un correo.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .version import version_actual

# 1 MB por archivo y tres de reserva: ~4 MB en total, que cabe en un correo.
# Un log que no se puede mandar es un log que no existe.
TAMANO_MAXIMO_BYTES = 1_000_000
COPIAS_DE_RESERVA = 3

NOMBRE = "job-rob"
_configurado = False


def ruta_del_log(directorio_datos: Path) -> Path:
    """Dónde vive. Se le enseña a la clienta en la propia pantalla.

    Va dentro de `data/` a propósito, que es la carpeta que ya está en
    `.gitignore`: un log con nombres de empresas y claves de ofertas no se
    sube a un repositorio ni por accidente.
    """
    return directorio_datos / "logs" / f"{NOMBRE}.log"


def configurar(directorio_datos: Path, consola: bool = True) -> Path:
    """Enciende el log. Idempotente: llamarlo dos veces no duplica líneas.

    `consola` deja además la salida por pantalla, que es lo que quiere el
    desarrollador cuando trabaja. En la máquina de la clienta no hay consola
    que mirar, pero tampoco estorba.

    Devuelve la ruta del archivo para poder enseñarla.
    """
    global _configurado

    destino = ruta_del_log(directorio_datos)
    if _configurado:
        return destino

    destino.parent.mkdir(parents=True, exist_ok=True)

    raiz = logging.getLogger(NOMBRE)
    raiz.setLevel(logging.INFO)
    raiz.propagate = False

    formato = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    archivo = logging.handlers.RotatingFileHandler(
        destino, maxBytes=TAMANO_MAXIMO_BYTES,
        backupCount=COPIAS_DE_RESERVA, encoding="utf-8",
    )
    archivo.setFormatter(formato)
    raiz.addHandler(archivo)

    if consola:
        pantalla = logging.StreamHandler()
        pantalla.setFormatter(formato)
        # En pantalla solo lo que merece interrumpir: el detalle está en el
        # archivo. Si el log gritara por consola, la salida cuidada de los
        # comandos se volvería ilegible.
        pantalla.setLevel(logging.WARNING)
        raiz.addHandler(pantalla)

    _configurado = True

    # LA PRIMERA LÍNEA ES SIEMPRE LA VERSIÓN, y por eso se escribe aquí y no
    # en cada punto de entrada: es el primer dato que hace falta para entender
    # cualquier cosa que venga debajo, y el que nadie se acuerda de apuntar.
    log("inicio").info("arranca — versión %s", version_actual().para_log())
    return destino


def log(nombre: str) -> logging.Logger:
    """Un logger por área: `log("bandeja")`, `log("clasificador")`.

    Si nadie ha llamado a `configurar`, esto sigue funcionando y no escribe
    nada. Es deliberado: los tests y los comandos de consola no tienen por qué
    montar el log, y un logger que reventara por no estar configurado
    convertiría una línea de diagnóstico en una caída.
    """
    return logging.getLogger(f"{NOMBRE}.{nombre}")
