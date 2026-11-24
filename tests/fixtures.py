"""
Respuestas de ejemplo de Adzuna y Jooble.

Se generan por código en vez de guardarse como archivos JSON fijos por una
razón concreta: las fechas. Un JSON con fechas escritas a mano caduca solo, y
dentro de dos meses el filtro de antigüedad descartaría todos los ejemplos y
las pruebas empezarían a fallar sin que nada se hubiera roto.

Los datos imitan a propósito los problemas reales de estas fuentes:

  - la misma oferta con la empresa escrita de dos formas
  - la misma oferta con la ubicación a distinta profundidad
    ("Cuauhtémoc, Ciudad de México" vs "Ciudad de México")
  - empresas como "Confidencial"
  - ofertas del índice de EE.UU. que no están en Texas
  - ofertas remotas sin estado identificable
  - salario ausente en la mayoría, y estimado en alguna
  - el formato de fecha de Jooble, con 7 decimales de segundo
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def _adzuna_date(days_ago: int) -> str:
    """Formato de Adzuna: ISO con 'Z' al final."""
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _jooble_date(days_ago: int) -> str:
    """Formato de Jooble: ISO con SIETE decimales de segundo.

    Es un formato que `datetime.fromisoformat` no acepta tal cual, así que
    estos ejemplos también prueban el parser tolerante de connectors/base.py.
    """
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + ".0000000"


# ---------------------------------------------------------------------------
#  ADZUNA
# ---------------------------------------------------------------------------

def adzuna_alemania_coo_logistics() -> dict[str, Any]:
    """Anuncios alemanes, y la mayoria REDACTADOS EN ALEMAN.

    Sustituyeron a los mexicanos el 4 septiembre 2026, cuando Mexico se borro
    de la base y del perfil. Se conserva la FORMA exacta -cinco resultados, los
    mismos dos que tienen que caer por titulo y por antiguedad, el mismo par que
    se fusiona con Jooble- para que las cifras esperadas de
    `test_pipeline_offline.py` sigan siendo las mismas y el cambio de mercado no
    se lea como un cambio de comportamiento.

    Lo unico que se movio a proposito son los importes: la divisa la pone el
    pais (`CURRENCY_BY_COUNTRY`), y en `de` es EUR. 3.200.000 MXN eran ~176.000
    USD y quedaban por encima del umbral de 170.000; 163.000 EUR son ~176.040
    USD y quedan igual. Los estimados eran ~88.000/99.000 USD y siguen ahi.

    Y EL TEXTO VA EN ALEMAN a proposito: el 11% del inventario aleman real lo
    esta (4 de 36, medido el 4 septiembre 2026), y hasta hoy ni una prueba del
    proyecto veia una sola palabra de aleman.
    """
    return {
        "count": 37,
        "mean": 78000.0,
        "results": [
            {
                "id": "4901233412",
                "created": _adzuna_date(2),
                "title": "Chief Operating Officer (COO) &ndash; Logistics",
                "company": {"display_name": "Norddeutsche Energie Infrastruktur GmbH"},
                "location": {
                    "display_name": "Mitte, Berlin",
                    "area": ["Germany", "Berlin", "Mitte"],
                },
                "salary_min": 163000.0,
                "salary_max": 194000.0,
                "salary_is_predicted": "0",
                "redirect_url": "https://www.adzuna.de/land/ad/4901233412",
                "description": "Führendes Unternehmen der Logistikinfrastruktur sucht "
                               "einen COO mit Erfahrung in Betrieb und Transformation.",
                "contract_type": "permanent",
            },
            {
                "id": "4901233413",
                "created": _adzuna_date(5),
                "title": "Leiter Betrieb - Logistics",
                "company": {"display_name": "Stadtwerke Rheinland AG"},
                "location": {
                    "display_name": "Köln, Nordrhein-Westfalen",
                    "area": ["Germany", "Nordrhein-Westfalen", "Köln"],
                },
                "salary_is_predicted": "0",
                "redirect_url": "https://www.adzuna.de/land/ad/4901233413",
                "description": "Verantwortung für den Betrieb der Lagerlogistik.",
            },
            {
                "id": "4901233414",
                "created": _adzuna_date(9),
                "title": "Chief Operating Officer",
                "company": {"display_name": "Vertraulich"},
                "location": {
                    "display_name": "Berlin",
                    "area": ["Germany", "Berlin"],
                },
                # Salario ESTIMADO por Adzuna, no declarado en el anuncio.
                "salary_min": 82000.0,
                "salary_max": 92000.0,
                "salary_is_predicted": "1",
                "redirect_url": "https://www.adzuna.de/land/ad/4901233414",
                "description": "Bedeutende deutsche Logistikgruppe.",
            },
            {
                # Debe caer por el filtro de titulo (perfil senior).
                "id": "4901233415",
                "created": _adzuna_date(1),
                "title": "Praktikant Operations - Logistics",
                "company": {"display_name": "Beratung Alpha GmbH"},
                "location": {
                    "display_name": "München, Bayern",
                    "area": ["Germany", "Bayern", "München"],
                },
                "salary_is_predicted": "0",
                "redirect_url": "https://www.adzuna.de/land/ad/4901233415",
                "description": "Pflichtpraktikum.",
            },
            {
                # Debe caer por antiguedad.
                "id": "4901233416",
                "created": _adzuna_date(75),
                "title": "COO Logistics Projects",
                "company": {"display_name": "Hansebau Nord GmbH"},
                "location": {
                    "display_name": "Hamburg",
                    "area": ["Germany", "Hamburg"],
                },
                "salary_is_predicted": "0",
                "redirect_url": "https://www.adzuna.de/land/ad/4901233416",
                "description": "Veraltete Anzeige.",
            },
        ],
    }


def adzuna_alemania_coo_real_estate() -> dict[str, Any]:
    return {
        "count": 4,
        "results": [
            {
                "id": "4901299001",
                "created": _adzuna_date(11),
                "title": "COO - Supply Chain Development",
                "company": {"display_name": "Frankfurter Projektentwicklung GmbH & Co. KG"},
                "location": {
                    "display_name": "Frankfurt am Main, Hessen",
                    "area": ["Germany", "Hessen", "Frankfurt am Main"],
                },
                "salary_is_predicted": "0",
                "redirect_url": "https://www.adzuna.de/land/ad/4901299001",
                "description": "Betriebsleitung für ein Immobilienportfolio.",
            },
        ],
    }


def adzuna_texas_coo_logistics() -> dict[str, Any]:
    return {
        "count": 128,
        "results": [
            {
                "id": "5100000001",
                "created": _adzuna_date(3),
                "title": "Chief Operating Officer - Logistics",
                "company": {"display_name": "Lone Star Logistics Partners"},
                "location": {
                    "display_name": "Houston, Texas",
                    "area": ["US", "Texas", "Houston"],
                },
                "salary_min": 240000.0,
                "salary_max": 280000.0,
                "salary_is_predicted": "0",
                "redirect_url": "https://www.adzuna.com/land/ad/5100000001",
                "description": "Lead operations for a large logistics portfolio.",
            },
            {
                # Fuera de Texas: debe caer por el filtro geográfico.
                "id": "5100000002",
                "created": _adzuna_date(4),
                "title": "COO, Logistics Services",
                "company": {"display_name": "Midwest Distribution Group Inc"},
                "location": {
                    "display_name": "Chicago, Illinois",
                    "area": ["US", "Illinois", "Chicago"],
                },
                "salary_is_predicted": "0",
                "redirect_url": "https://www.adzuna.com/land/ad/5100000002",
                "description": "Operations leadership for utilities.",
            },
            {
                # Remoto sin estado: ubicación indeterminada.
                "id": "5100000003",
                "created": _adzuna_date(6),
                "title": "VP Operations, Logistics (Remote)",
                "company": {"display_name": "Nationwide Grid Services LLC"},
                "location": {"display_name": "United States", "area": ["US"]},
                "salary_min": 210000.0,
                "salary_max": 250000.0,
                "salary_is_predicted": "0",
                "redirect_url": "https://www.adzuna.com/land/ad/5100000003",
                "description": "Remote role covering national logistics operations.",
            },
            {
                "id": "5100000004",
                "created": _adzuna_date(8),
                "title": "Logistics Director",
                "company": {"display_name": "Austin Manufacturing Partners"},
                "location": {
                    "display_name": "Austin, Texas",
                    "area": ["US", "Texas", "Austin"],
                },
                "salary_is_predicted": "0",
                "redirect_url": "https://www.adzuna.com/land/ad/5100000004",
                "description": "Director of logistics for a growing energy company.",
            },
        ],
    }


def adzuna_texas_coo_real_estate() -> dict[str, Any]:
    """Consulta legítima con cero resultados. NO es un error: pasa y hay que
    poder distinguirlo de una fuente caída.
    """
    return {"count": 0, "results": []}


# ---------------------------------------------------------------------------
#  JOOBLE
# ---------------------------------------------------------------------------

def jooble_alemania_coo_logistics() -> dict[str, Any]:
    return {
        "totalCount": 62,
        "jobs": [
            {
                # MISMA oferta que Adzuna id 4901233414 (Vertraulich / Berlin).
                # Empresa, titulo y ubicacion coinciden tras normalizar:
                # la deduplicacion entre fuentes DEBE fusionarlas.
                "id": 771002,
                "title": "Chief Operating Officer",
                "location": "Berlin",
                "company": "Vertraulich",
                "snippet": "Bedeutende deutsche Logistikgruppe sucht COO.",
                "salary": "",
                "source": "stepstone.de",
                "type": "Vollzeit",
                "link": "https://de.jooble.org/jdp/771002",
                "updated": _jooble_date(9),
            },
            {
                # MISMA oferta que Adzuna id 4901233412, pero con la empresa sin
                # forma juridica y la ubicacion a otra profundidad
                # ("Berlin" en vez de "Mitte, Berlin").
                # La clave estricta NO las va a fusionar; la laxa si.
                # Es el caso que mide la sub-fusion en el reporte.
                "id": 771003,
                "title": "Chief Operating Officer (COO) - Logistics",
                "location": "Berlin",
                "company": "Norddeutsche Energie Infrastruktur",
                "snippet": "Führendes Unternehmen der Logistikinfrastruktur sucht COO.",
                "salary": "163.000 - 194.000 EUR pro Jahr",
                "source": "linkedin.com",
                "type": "Vollzeit",
                "link": "https://de.jooble.org/jdp/771003",
                "updated": _jooble_date(2),
            },
            {
                "id": 771004,
                "title": "Head of Operations - Hafeninfrastruktur",
                "location": "Bremerhaven",
                "company": "Nordsee Terminal Betrieb",
                "snippet": "Verantwortlich für Hafenbetrieb und Infrastruktur.",
                "salary": "",
                "source": "indeed.de",
                "type": "Vollzeit",
                "link": "https://de.jooble.org/jdp/771004",
                "updated": _jooble_date(14),
            },
        ],
    }


def jooble_alemania_coo_real_estate() -> dict[str, Any]:
    return {"totalCount": 0, "jobs": []}


def jooble_texas_coo_logistics() -> dict[str, Any]:
    return {
        "totalCount": 210,
        "jobs": [
            {
                # MISMA oferta que Adzuna id 5100000001: debe fusionarse.
                "id": 990001,
                "title": "Chief Operating Officer - Logistics",
                "location": "Houston, TX",
                "company": "Lone Star Logistics Partners",
                "snippet": "Lead operations for a large logistics portfolio.",
                "salary": "$240,000 - $280,000 per year",
                "source": "indeed.com",
                "type": "Full-time",
                "link": "https://jooble.org/jdp/990001",
                "updated": _jooble_date(3),
            },
            {
                "id": 990002,
                "title": "COO, Logistics & Capital Projects",
                "location": "Dallas, TX",
                "company": "Trinity Capital Logistics",
                "snippet": "Operations executive for capital logistics projects.",
                "salary": "",
                "source": "indeed.com",
                "type": "Full-time",
                "link": "https://jooble.org/jdp/990002",
                "updated": _jooble_date(7),
            },
            {
                # Fuera de Texas: el índice de EE.UU. devuelve el país entero.
                "id": 990003,
                "title": "Chief Operating Officer, Logistics",
                "location": "Oklahoma City, OK",
                "company": "Sooner Distribution",
                "snippet": "Operations leadership role.",
                "salary": "",
                "source": "glassdoor.com",
                "type": "Full-time",
                "link": "https://jooble.org/jdp/990003",
                "updated": _jooble_date(10),
            },
        ],
    }


def jooble_texas_coo_real_estate() -> dict[str, Any]:
    return {"totalCount": 0, "jobs": []}


# Mapa (fuente, mercado, etiqueta de combinación) -> función que da el payload.
# Lo usan el sembrado de caché de las pruebas y la demo offline.
PAYLOADS = {
    ("adzuna", "alemania", "COO + Logistics"): adzuna_alemania_coo_logistics,
    ("adzuna", "alemania", "COO + Supply Chain"): adzuna_alemania_coo_real_estate,
    ("adzuna", "texas_us", "COO + Logistics"): adzuna_texas_coo_logistics,
    ("adzuna", "texas_us", "COO + Supply Chain"): adzuna_texas_coo_real_estate,
    ("jooble", "alemania", "COO + Logistics"): jooble_alemania_coo_logistics,
    ("jooble", "alemania", "COO + Supply Chain"): jooble_alemania_coo_real_estate,
    ("jooble", "texas_us", "COO + Logistics"): jooble_texas_coo_logistics,
    ("jooble", "texas_us", "COO + Supply Chain"): jooble_texas_coo_real_estate,
}
