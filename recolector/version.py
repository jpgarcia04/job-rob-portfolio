"""
Qué versión está corriendo, y de dónde salió.

POR QUÉ EXISTE
--------------
El desarrollador se vuelve a España después de la entrega. A partir de ahí,
toda conversación sobre un fallo empieza con la misma pregunta —*«¿qué versión
tienes?»*— y hoy no hay forma de contestarla. Sin esto, «ya lo arreglé» y «ya
te llegó el arreglo» son indistinguibles desde 9.000 km.

Es la mitad barata del mantenimiento en remoto. La otra mitad es el log
(`recolector/registro.py`): uno dice qué código corre y el otro qué hizo.

DE DÓNDE SALE EL NÚMERO
-----------------------
De **git**, no de una constante escrita a mano, y eso es deliberado: la
instalación de la clienta ES un clon de git —tiene que serlo, o el actualizador
no funciona—, así que git es la única fuente que no puede desincronizarse de lo
que de verdad se está ejecutando. Una constante en un archivo se olvida de
subir; una etiqueta de git no, porque es lo mismo que el actualizador usa para
decidir qué traer.

El orden de preferencia, y cada escalón dice menos que el anterior:

    v1.2.0                 etiqueta exacta: una versión publicada
    v1.2.0-3-gabc1234      tres commits POR DELANTE de la última etiqueta
    v1.2.0-3-gabc1234-sucio  además con cambios sin guardar
    abc1234                sin etiquetas todavía: solo el commit
    0.1.0 (sin git)        ni git ni repositorio: lo último que se puede decir

Los dos del medio importan más de lo que parece: si la máquina de la clienta
dice «por delante» o «sucio», el actualizador NO está mandando y eso es un
fallo de instalación, no de código.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import __version__ as VERSION_DE_PAQUETE

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# El sufijo que git añade cuando hay cambios sin guardar. Se traduce, porque
# esta cadena se le puede acabar leyendo a la clienta por teléfono.
SUFIJO_SUCIO = "-sucio"


@dataclass(frozen=True)
class Version:
    """Lo que corre ahora mismo."""

    etiqueta: str          # lo que se enseña: "v1.2.0" o "abc1234"
    commit: str            # el hash corto, siempre que haya git
    limpia: bool           # ¿sin cambios locales sin guardar?
    publicada: bool        # ¿es exactamente una etiqueta, sin commits encima?
    desde_git: bool        # False = no se pudo preguntar a git

    @property
    def es_de_fiar(self) -> bool:
        """¿Corresponde a algo que se publicó a propósito?

        Solo entonces «tengo la v1.2.0» significa lo mismo en las dos puntas
        del teléfono. Si es False, lo que hay en esa máquina no está en
        ninguna parte del repositorio y el actualizador no la está gobernando.
        """
        return self.desde_git and self.limpia and self.publicada

    def para_pantalla(self) -> str:
        return self.etiqueta + ("" if self.limpia else SUFIJO_SUCIO)

    def para_log(self) -> str:
        if not self.desde_git:
            return f"{self.etiqueta} (sin git)"
        estado = "publicada" if self.publicada else "por delante de la etiqueta"
        if not self.limpia:
            estado += ", con cambios sin guardar"
        return f"{self.para_pantalla()} [{self.commit}] ({estado})"


def _git(*argumentos: str) -> str | None:
    """Ejecuta git en la raíz del proyecto. `None` si no se puede.

    Nunca lanza: una máquina sin git tiene que poder abrir la app igual. Que
    no se sepa la versión es una molestia; que no arranque, un desastre.
    """
    try:
        salida = subprocess.run(
            ["git", *argumentos],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            # En Windows, sin esto se abre una consola negra un instante cada
            # vez que se pregunta la versión. La app tiene que parecer una app.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if salida.returncode != 0:
        return None
    return salida.stdout.strip() or None


@lru_cache(maxsize=1)
def version_actual() -> Version:
    """La versión de este árbol. Se cachea: no cambia durante una ejecución.

    Y no cambia a propósito. El actualizador corre ANTES de arrancar la app,
    así que si el número cambiara a media sesión sería que alguien tocó los
    archivos por debajo — y entonces lo que está en memoria ya no es lo que
    está en disco, que es peor que un número viejo.
    """
    descripcion = _git("describe", "--tags", "--always", "--dirty=+")
    if descripcion is None:
        return Version(
            etiqueta=VERSION_DE_PAQUETE, commit="", limpia=True,
            publicada=False, desde_git=False,
        )

    limpia = not descripcion.endswith("+")
    descripcion = descripcion.rstrip("+")

    commit = _git("rev-parse", "--short", "HEAD") or ""
    # `git describe` sin etiquetas devuelve solo el hash. Con etiquetas
    # devuelve `v1.2.0` si estás justo encima, o `v1.2.0-3-gabc1234` si no.
    publicada = bool(_git("describe", "--tags", "--exact-match", "HEAD"))

    return Version(
        etiqueta=descripcion, commit=commit, limpia=limpia,
        publicada=publicada, desde_git=True,
    )
