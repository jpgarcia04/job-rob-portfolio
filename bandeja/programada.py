"""
La tarea del Programador de tareas de Windows. Se pone y se quita sola.

POR QUÉ LA REGISTRA LA APP Y NO EL INSTALADOR
---------------------------------------------
Porque el instalador corre UNA vez, en la máquina del desarrollador, el día
de la entrega — y esta función nació después, con el portátil ya entregado y
a 9.000 km. El único camino que queda hasta esa máquina es el actualizador,
o sea código que se ejecuta al arrancar. Mismo razonamiento que la reparación
del libro de gastos.

Y VA EN EL SERVIDOR, NO EN `abrir.py` (medido el 11 septiembre 2026). La
cáscara ya está cargada en memoria cuando el actualizador cambia los archivos,
así que ese proceso sigue corriendo la versión ANTERIOR y lo que se ponga allí
tarda DOS aperturas en hacer efecto. El servidor arranca después del
`checkout`.

APAGADA DE FÁBRICA
------------------
`Automatica.activa` viene en `false`. Encenderla es gastar dinero de ella sin
que esté delante, y eso lo decide ella desde la pantalla de ajustes con el
importe a la vista — no una actualización que le llega sin avisar.

Cuando está apagada, esta función BORRA la tarea si existía. Dejar corriendo
un reloj que ella apagó sería el peor fallo posible de esta pieza.

POR QUÉ XML Y NO `schtasks /Create /SC DAILY`
---------------------------------------------
Porque tres de los valores que hacen que esto funcione de verdad en un
portátil no se pueden poner con banderas:

    DisallowStartIfOnBatteries  El defecto de Windows es `true`. Un portátil
                                a las siete de la mañana está con mucha
                                probabilidad con la batería: con el defecto,
                                la tarea NO CORRE NUNCA y nadie se entera.
    StartWhenAvailable          Si el portátil estaba apagado a las siete,
                                la corrida se hace en cuanto se encienda, en
                                vez de perderse hasta mañana.
    MultipleInstances           `IgnoreNew`: si una corrida se alargó, la
                                siguiente no se solapa. Dos corridas a la vez
                                se pisarían el libro de gastos.

`InteractiveToken` significa que corre SOLO con ella dentro de su sesión, y
sin guardar ninguna contraseña en ningún sitio. Es lo correcto aquí: la app
usa su clave de Anthropic y su red, y nada de eso tiene sentido con la sesión
cerrada.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# El nombre con el que ella lo va a ver en el Programador de tareas si algún
# día lo abre. En español y diciendo qué es, no `JobRobTask`.
NOMBRE_TAREA = "job-rob - corrida diaria"

SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PLANTILLA = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Busca ofertas nuevas, las lee y las ordena, para que la bandeja este lista al abrirla. La pone y la quita la propia aplicacion desde Ajustes.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{hora}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{usuario}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{comando}</Command>
      <Arguments>{argumentos}</Arguments>
      <WorkingDirectory>{carpeta}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> tuple[int, str]:
    """Devuelve (código, salida). No lanza: esto corre en el arranque."""
    try:
        hecho = subprocess.run(
            ["schtasks", *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", creationflags=SIN_VENTANA,
            timeout=30,
        )
        return hecho.returncode, (hecho.stdout + hecho.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def existe(nombre: str = NOMBRE_TAREA) -> bool:
    return _schtasks("/Query", "/TN", nombre)[0] == 0


def quitar(nombre: str = NOMBRE_TAREA) -> bool:
    """Borra la tarea. `True` si había una y se fue."""
    if not existe(nombre):
        return False
    return _schtasks("/Delete", "/TN", nombre, "/F")[0] == 0


def _python_sin_consola(raiz: Path) -> Path:
    for nombre in ("pythonw.exe", "python.exe", "python"):
        candidato = raiz / ".venv" / "Scripts" / nombre
        if candidato.exists():
            return candidato
    return Path(sys.executable)


def _usuario() -> str:
    dominio = os.environ.get("USERDOMAIN", "")
    usuario = os.environ.get("USERNAME", "")
    return f"{dominio}\\{usuario}" if dominio and usuario else usuario


def poner(hora: str, raiz: Path = RAIZ, nombre: str = NOMBRE_TAREA) -> str | None:
    """Crea o reemplaza la tarea. Devuelve el motivo si no pudo.

    Se reemplaza siempre (`/F`) en vez de mirar si ya estaba y compararla: el
    XML es la descripción entera de lo que tiene que haber, así que escribirlo
    otra vez es la forma barata de que la hora y la ruta no se queden viejas
    cuando ella las cambie.
    """
    guion = raiz / "herramientas" / "corrida_diaria.py"
    if not guion.exists():
        return f"no está {guion}"

    xml = PLANTILLA.format(
        hora=hora,
        usuario=_usuario(),
        comando=_python_sin_consola(raiz),
        argumentos=f'"{guion}"',
        carpeta=raiz,
    )

    # UTF-16 CON BOM: `schtasks /XML` no lee otra cosa. Con UTF-8 contesta un
    # error de formato que no dice nada de la codificación.
    temporal = Path(tempfile.gettempdir()) / "job-rob-tarea.xml"
    try:
        temporal.write_text(xml, encoding="utf-16")
        codigo, salida = _schtasks("/Create", "/TN", nombre, "/XML", str(temporal), "/F")
    except OSError as exc:
        return f"no se pudo escribir el XML: {exc}"
    finally:
        try:
            temporal.unlink()
        except OSError:
            pass

    return None if codigo == 0 else (salida or f"schtasks devolvió {codigo}")


def asegurar(activa: bool, hora: str, raiz: Path = RAIZ,
             nombre: str = NOMBRE_TAREA) -> str | None:
    """Deja el Programador de tareas como digan sus ajustes.

    Devuelve una línea para el log cuando ha cambiado algo, o `None` cuando ya
    estaba como tenía que estar — que es el caso normal en todos los arranques
    menos el primero.
    """
    if sys.platform != "win32":  # pragma: no cover - el proyecto corre en Windows
        return None

    if not activa:
        return f"corrida automática apagada: tarea «{nombre}» quitada" if quitar(nombre) else None

    fallo = poner(hora, raiz, nombre)
    if fallo:
        return f"no se pudo programar la corrida diaria: {fallo}"
    return f"corrida automática programada para las {hora}"
