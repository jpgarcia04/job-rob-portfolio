"""
La corrida de la mañana. Lo que la tarea programada ejecuta.

    .venv\\Scripts\\pythonw.exe herramientas\\corrida_diaria.py

QUÉ RESUELVE
------------
Hasta hoy la app era inerte: no existía hasta que ella hacía doble clic, y
entonces le tocaba pulsar «Ponme al día» y mirar una barra tres minutos. Esto
hace ese trabajo a las siete de la mañana para que a las nueve la bandeja ya
esté ordenada. Es la diferencia entre una herramienta que se opera y un
asistente que te tenía la mañana lista.

LOS TRES PASOS, Y POR QUÉ ESE ORDEN
-----------------------------------
El mismo que «Ponme al día»: buscar trae material nuevo, leer solo puede
trabajar sobre lo que ya está, y ordenar solo vale si incluye lo que se acaba
de leer.

SIN `--manual`, Y ESA PALABRA ES TODO EL CONTROL DE GASTO
---------------------------------------------------------
`--manual` marca las corridas que pide ELLA, y esas se rigen por el tope por
acción ($0,60). Estas no las pide nadie: las decide un reloj, con ella
dormida. Así que van sin la marca y las gobierna la RACIÓN MENSUAL de las
automáticas (`llm.racion_automatica_mensual_usd`, $10 hoy), que sí frena en
seco al agotarse. Ver `presupuesto.py`.

Es deliberado que sean dos cajones distintos: así una corrida automática no
puede dejarle la caja vacía a las siete de la mañana para cuando ella pulse
un botón a las nueve.

DEJA CONSTANCIA, Y NO ES UN ADORNO
----------------------------------
Gastar sin que ella mire solo es aceptable si al llegar ve qué se hizo y qué
costó. Por eso escribe `data/ultima_corrida.json`, que es lo que la pantalla
enseña: «lo hice yo a las 7:04 · 12 ofertas nuevas · 6 leídas · $0,31».

Las cifras salen de CONTAR ANTES Y DESPUÉS en la base y en el libro, no de
leer lo que imprimieron los comandos: un resumen que parsea texto de consola
empieza a mentir el día que alguien cambia una línea.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Nombre del archivo que lee la pantalla. En `data/`, con los contadores.
ARCHIVO_RESUMEN = "ultima_corrida.json"

PASOS = (
    ("Buscar ofertas nuevas", ["-m", "recolector", "run"]),
    ("Que el asistente lea lo nuevo", ["-m", "clasificador"]),
    ("Ordenar por dónde empezar", ["-m", "clasificador", "ranking"]),
)

# Lo que se espera como mucho a cada paso. El recolector hace 26 peticiones
# con pausas y el clasificador ocho llamadas de ~15 s: veinte minutos es
# holgado para los dos y corta en seco un cuelgue, que es lo que no puede
# quedarse ocupando la máquina hasta que ella llegue.
ESPERA_MAXIMA_S = 20 * 60


def _python() -> Path:
    for nombre in ("pythonw.exe", "python.exe", "python"):
        candidato = RAIZ / ".venv" / "Scripts" / nombre
        if candidato.exists():
            return candidato
    return Path(sys.executable)


def _log():
    import logging
    import logging.handlers

    from recolector.registro import COPIAS_DE_RESERVA, TAMANO_MAXIMO_BYTES

    registro = logging.getLogger("job-rob-corrida-diaria")
    if not registro.handlers:
        destino = RAIZ / "data" / "logs" / "corrida_diaria.log"
        destino.parent.mkdir(parents=True, exist_ok=True)
        manejador = logging.handlers.RotatingFileHandler(
            destino, maxBytes=TAMANO_MAXIMO_BYTES,
            backupCount=COPIAS_DE_RESERVA, encoding="utf-8",
        )
        manejador.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
        ))
        registro.addHandler(manejador)
        registro.setLevel(logging.INFO)
        registro.propagate = False
    return registro


def _foto() -> tuple[int, int, float]:
    """(anuncios, clasificados, gastado) ahora mismo. Para restar después."""
    from clasificador.presupuesto import LibroDeGastos, ruta_del_libro
    from recolector.config import load_config
    from recolector.storage import Storage

    config = load_config()
    try:
        libro = LibroDeGastos(ruta_del_libro(config))
        gastado = libro.gastado_total()
    except Exception:  # noqa: BLE001 - un libro ilegible no para la corrida
        gastado = 0.0

    almacen = Storage(config.database_path)
    try:
        anuncios = almacen.connection.execute(
            "SELECT COUNT(*) FROM offers"
        ).fetchone()[0]
        clasificados = almacen.connection.execute(
            "SELECT COUNT(*) FROM clasificaciones"
        ).fetchone()[0]
    finally:
        almacen.close()
    return anuncios, clasificados, gastado


def correr(raiz: Path = RAIZ) -> int:
    registro = _log()
    registro.info("--- corrida automática ---")

    empezo = time.monotonic()
    cuando = datetime.now().isoformat(timespec="seconds")
    try:
        anuncios_antes, leidas_antes, gastado_antes = _foto()
    except Exception as exc:  # noqa: BLE001
        registro.error("no se pudo mirar la base antes de empezar: %s", exc)
        anuncios_antes = leidas_antes = 0
        gastado_antes = 0.0

    entorno = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    pasos: list[dict] = []

    for etiqueta, argv in PASOS:
        registro.info("paso: %s", etiqueta)
        try:
            hecho = subprocess.run(
                [str(_python()), *argv], cwd=raiz, env=entorno,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=ESPERA_MAXIMA_S, creationflags=SIN_VENTANA,
            )
            codigo = hecho.returncode
        except subprocess.TimeoutExpired:
            registro.error("«%s» tardó más de %d minutos: se corta",
                           etiqueta, ESPERA_MAXIMA_S // 60)
            pasos.append({"paso": etiqueta, "codigo": None, "nota": "tardó demasiado"})
            break
        except Exception as exc:  # noqa: BLE001
            registro.error("no se pudo lanzar «%s»: %s", etiqueta, exc)
            pasos.append({"paso": etiqueta, "codigo": None, "nota": str(exc)})
            break

        registro.info("  -> código %s", codigo)
        pasos.append({"paso": etiqueta, "codigo": codigo})

        # SE PARA, PERO SIN GRITAR, CUANDO NO QUEDA RACIÓN. El código 3 es
        # «sin saldo», y de madrugada eso no es una avería: es el freno
        # haciendo lo suyo. Lo que no tiene sentido es seguir con los
        # siguientes pasos, que también gastan.
        if codigo == 3:
            registro.info("se paró por presupuesto: el resto queda para mañana")
            break
        if codigo not in (0, 1):
            registro.warning("«%s» terminó mal: no se siguen los pasos siguientes",
                             etiqueta)
            break

    try:
        anuncios, leidas, gastado = _foto()
    except Exception:  # noqa: BLE001
        anuncios, leidas, gastado = anuncios_antes, leidas_antes, gastado_antes

    resumen = {
        "cuando": cuando,
        "segundos": round(time.monotonic() - empezo, 1),
        "ofertas_nuevas": max(0, anuncios - anuncios_antes),
        "leidas": max(0, leidas - leidas_antes),
        "coste_usd": round(max(0.0, gastado - gastado_antes), 6),
        "pasos": pasos,
        "se_paro_por_presupuesto": any(p.get("codigo") == 3 for p in pasos),
    }

    destino = raiz / "data" / ARCHIVO_RESUMEN
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        tmp = destino.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(resumen, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, destino)
    except OSError as exc:
        registro.error("no se pudo escribir el resumen: %s", exc)

    registro.info(
        "hecho en %.0fs: %d ofertas nuevas, %d leídas, $%.4f",
        resumen["segundos"], resumen["ofertas_nuevas"],
        resumen["leidas"], resumen["coste_usd"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(correr())
