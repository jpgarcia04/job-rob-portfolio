"""
El recorte geográfico de lo que YA está guardado.

EL PROBLEMA QUE RESUELVE
------------------------
El filtro geográfico (`geo.py`) decide qué entra en cada corrida. Pero la base
guarda lo que entró con el filtro de su día, y el 24 septiembre 2026 ese filtro
cambió: la clienta pidió quedarse solo con una zona de Texas —de Houston y
Austin hasta Brownsville— y dejar de ver el resto. Medido ese día sobre la base
del desarrollador: 392 anuncios de Texas, de los que 179 caen fuera (casi todo
Dallas–Fort Worth, y el oeste y el norte del estado).

Cambiar el YAML solo frena lo que llega a partir de mañana. Los 179 seguirían
en su bandeja, y en la cola del triaje gastando su dinero. El desarrollador lo
dijo sin rodeos: lo que queda fuera, a la basura.

CUÁNDO CORRE, Y POR QUÉ NO SIEMPRE
---------------------------------
Una vez por cada cambio de la geografía de un mercado. Se guarda una HUELLA de
su `geo_filter` en `data/recorte_geo.json` y solo se vuelve a juzgar la base
cuando la huella cambia. Así abrir la app no vuelve a leer y reescribir la base
cada vez, y un mercado que no se ha tocado (Alemania, México) no se mira.

Lo llaman el servidor al arrancar —el primer código nuevo que corre después de
que el actualizador cambie la versión, igual que `estreno.py`— y el recolector
antes de cada búsqueda, como red por si lo primero falló: así el triaje nunca
llega a pagar por anuncios que ya no son del mercado.

LO QUE SE BORRA, Y LO QUE NO
----------------------------
Se juzga cada oferta con `geo.classify_location`, la MISMA función que filtra
al recolectar. Si hoy no entraría, se borra de todas las tablas.

Con una excepción: lo que ELLA ya marcó como «interesa» o «postulada» se
queda, aunque caiga fuera. Eso ya no es una oferta de la bandeja, es su
registro de trabajo —una candidatura enviada a Dallas sigue enviada aunque hoy
no buscáramos en Dallas—, y borrarlo sería borrarle algo suyo sin avisar. Se
quedan sin zona y se cuentan aparte en el mensaje del log.

ANTES DE BORRAR, UNA COPIA
--------------------------
`data/ofertas.db.bak-recorte-<mercado>-<fecha>`, con la API de copia de
SQLite (no copiando el archivo, que puede tener escrituras a medias). Si la
copia falla, no se borra nada. Y todo el borrado va en UNA transacción: o se
hace entero o no se hace.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig, Market
from .connectors.adzuna import desglosar_area
from .geo import ACCEPTED, UNRESOLVED, classify_location
from .storage import Storage

ARCHIVO = "recorte_geo.json"

# Sube si cambia la forma de JUZGAR (geo.py) sin que cambie el YAML: así el
# recorte vuelve a correr sobre las bases que ya se recortaron con la regla vieja.
VERSION_DEL_JUICIO = 1

# Lo que ella ya decidió y no se le borra. Ver la cabecera.
ESTADOS_QUE_SE_QUEDAN = frozenset({"interesa", "postulada"})


def huella(market: Market) -> str:
    """Resumen estable de la geografía de un mercado. Si cambia, se recorta."""
    geo = market.geo_filter
    datos = {
        "juicio": VERSION_DEL_JUICIO,
        "mode": geo.mode,
        "regions": geo.regions,
        "cities": geo.cities,
        "unresolved_policy": geo.unresolved_policy,
        "zonas": [
            {"clave": z.clave, "condados": z.condados, "ciudades": z.ciudades}
            for z in geo.zonas
        ],
    }
    texto = json.dumps(datos, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:16]


@dataclass
class Plan:
    """Lo que el recorte haría en un mercado. Sin tocar nada."""

    market_key: str
    total: int = 0
    zonas: dict[str, str | None] = field(default_factory=dict)
    borrar: list[str] = field(default_factory=list)
    # Fuera del mercado pero marcadas por ella: se quedan.
    protegidas: list[str] = field(default_factory=list)
    por_zona: Counter = field(default_factory=Counter)
    # Ejemplos de por qué se va cada una, para el log.
    motivos: Counter = field(default_factory=Counter)


def planificar(almacen: Storage, market: Market) -> Plan:
    """Juzga cada oferta guardada del mercado con la geografía de HOY."""
    plan = Plan(market_key=market.key)
    politica = market.geo_filter.unresolved_policy

    for fila in almacen.ubicaciones_guardadas(market.key):
        plan.total += 1
        county = None
        if fila["raw_json"]:
            try:
                area = (json.loads(fila["raw_json"]).get("location") or {}).get("area")
                _, _, county, _ = desglosar_area(area)
            except (ValueError, AttributeError):
                county = None

        decision = classify_location(
            market,
            region=fila["location_region"],
            county=county,
            city=fila["location_city"],
            raw=fila["location_raw"],
        )
        se_queda = decision.status == ACCEPTED or (
            decision.status == UNRESOLVED and politica == "keep"
        )
        clave = fila["dedupe_key"]
        if se_queda:
            plan.zonas[clave] = decision.zona
            plan.por_zona[decision.zona or "(sin zona)"] += 1
        elif fila["status"] in ESTADOS_QUE_SE_QUEDAN:
            plan.zonas[clave] = None
            plan.protegidas.append(clave)
        else:
            plan.borrar.append(clave)
            plan.motivos[decision.reason] += 1
    return plan


def _leer_huellas(directorio_datos: Path) -> dict[str, str]:
    ruta = directorio_datos / ARCHIVO
    if not ruta.exists():
        return {}
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in (datos.get("mercados") or {}).items()}
    except (OSError, ValueError, AttributeError):
        # Ilegible = como si no hubiera: se vuelve a juzgar todo. Aquí ese es
        # el lado seguro, al revés que en `estreno.py`: juzgar de nuevo con la
        # misma regla no borra nada que la regla no borrara ya.
        return {}


def _escribir_huellas(directorio_datos: Path, huellas: dict[str, str]) -> None:
    ruta = directorio_datos / ARCHIVO
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"mercados": huellas}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, ruta)


def _copia_de_seguridad(almacen: Storage, market_key: str, ahora: datetime) -> Path:
    destino = almacen.database_path.with_name(
        f"{almacen.database_path.name}.bak-recorte-{market_key}-"
        f"{ahora.strftime('%Y%m%d-%H%M%S')}"
    )
    copia = sqlite3.connect(destino)
    try:
        almacen.connection.backup(copia)
    finally:
        copia.close()
    return destino


def recortar_si_hace_falta(
    config: AppConfig,
    almacen: Storage,
    directorio_datos: Path,
    *,
    ahora: datetime | None = None,
) -> list[str]:
    """Aplica la geografía de hoy a la base, en los mercados que cambiaron.

    Devuelve una línea por mercado recortado, para la consola y el log; una
    lista vacía si no había nada que hacer. Un mercado que falla no se apunta
    como hecho, así que se reintenta en la siguiente apertura.
    """
    ahora = ahora or datetime.now()
    hechas = _leer_huellas(directorio_datos)
    nuevas = dict(hechas)
    mensajes: list[str] = []

    for market_key in config.profile().market_keys:
        market = config.market(market_key)
        h = huella(market)
        if hechas.get(market_key) == h:
            continue

        plan = planificar(almacen, market)
        copia: Path | None = None
        try:
            if plan.borrar:
                copia = _copia_de_seguridad(almacen, market_key, ahora)
            almacen.fijar_zonas(plan.zonas)
            borrado = almacen.borrar_ofertas(plan.borrar)
            almacen.commit()
        except Exception:
            almacen.connection.rollback()
            raise

        nuevas[market_key] = h
        if plan.borrar or plan.protegidas:
            partes = [
                f"{market.label}: se borraron {borrado['ofertas']} de "
                f"{plan.total} anuncios que ya no caen en su geografía"
            ]
            if plan.protegidas:
                partes.append(
                    f"se quedan {len(plan.protegidas)} fuera de ella porque "
                    "están marcadas como «interesa» o «postulada»"
                )
            if borrado["herederos"]:
                partes.append(
                    f"{borrado['herederos']} vacante(s) cambiaron de anuncio "
                    "representante"
                )
            if copia:
                partes.append(f"copia previa en {copia.name}")
            mensajes.append("; ".join(partes) + ".")

    if nuevas != hechas:
        _escribir_huellas(directorio_datos, nuevas)
    return mensajes


def resumen_de_plan(plan: Plan, market: Market) -> dict[str, Any]:
    """El plan en cifras, para mirarlo antes de dejar que borre nada."""
    etiquetas = {z.clave: z.etiqueta for z in market.geo_filter.zonas}
    return {
        "mercado": market.label,
        "anuncios": plan.total,
        "se_quedan": plan.total - len(plan.borrar),
        "por_zona": {
            etiquetas.get(k, k): n for k, n in plan.por_zona.most_common()
        },
        "se_borran": len(plan.borrar),
        "protegidas": len(plan.protegidas),
        "motivos": dict(plan.motivos.most_common(8)),
    }
