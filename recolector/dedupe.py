"""
Deduplicación.

La misma oferta llega por Adzuna y por Jooble. La clave acordada es un hash
normalizado de empresa + título + ubicación.

Aviso importante, que condiciona cómo hay que leer el reporte de la v0:

    Esta clave SUB-FUSIONA. Es decir, va a contar como dos ofertas distintas
    cosas que son la misma. Causas principales:

      1. La empresa no siempre es la empresa (agencia de reclutamiento,
         "Confidencial", el nombre del portal de origen).
      2. La ubicación viene con granularidades incompatibles: Adzuna da
         jerarquía ("Mexico > Ciudad de México > Cuauhtémoc"), Jooble da
         texto libre ("CDMX").
      3. Los títulos van en idiomas distintos.

    El error va SIEMPRE en la dirección de inflar el recuento de ofertas
    únicas, que es justo el número que esta v0 existe para medir. Por eso se
    calcula además una clave laxa (empresa + título, sin ubicación) que no
    cambia el comportamiento del sistema pero permite CUANTIFICAR cuánto está
    sub-fusionando la clave estricta. Si la diferencia entre ambas es grande,
    el número de "únicas" hay que leerlo como techo, no como dato.

¿Por qué no se usa la clave laxa como operativa? Porque una empresa que
publica el mismo puesto en Monterrey, CDMX y Guadalajara son tres
oportunidades reales para la clienta, no una.
"""

from __future__ import annotations

import hashlib

from .models import JobOffer
from .textnorm import normalize_company, normalize_location, normalize_title


def _sha256(*parts: str) -> str:
    """Hash estable de varias partes unidas por '|'.

    Se usa '|' como separador para que ("ab", "c") y ("a", "bc") no colisionen.
    """
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def location_key_for(offer: JobOffer) -> str:
    """Qué parte de la ubicación entra en el hash.

    Se prefiere la ciudad; si no hay, el estado/región; si tampoco, el texto
    crudo. Devolver "" (sin ubicación conocida) es un caso legítimo y se
    hashea igual: dos ofertas sin ubicación de la misma empresa y título se
    consideran la misma.
    """
    for candidate in (offer.location_city, offer.location_region, offer.location_raw):
        normalized = normalize_location(candidate)
        if normalized:
            return normalized
    return ""


def strict_key(offer: JobOffer) -> str:
    """Clave operativa: empresa + título + ubicación."""
    return _sha256(
        normalize_company(offer.company),
        normalize_title(offer.title),
        location_key_for(offer),
    )


def loose_key(offer: JobOffer) -> str:
    """Clave de medición: empresa + título, sin ubicación.

    NO se usa para decidir nada. Solo para que el reporte pueda decir
    "la clave estricta produjo 40 únicas; ignorando la ubicación serían 31,
    o sea 9 posibles duplicados no detectados".
    """
    return _sha256(
        normalize_company(offer.company),
        normalize_title(offer.title),
    )


def assign_keys(offer: JobOffer) -> JobOffer:
    """Rellena las dos claves de la oferta y la devuelve.

    Se devuelve el mismo objeto (mutado) en lugar de una copia: en Python los
    objetos se pasan por referencia, así que quien la pasó ya ve el cambio.
    Se devuelve igualmente para poder encadenar llamadas.
    """
    offer.dedupe_key = strict_key(offer)
    offer.loose_key = loose_key(offer)
    return offer
