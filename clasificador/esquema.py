"""
La forma EXACTA de lo que puede devolver el modelo.

Esto no es documentación: es el contrato. Se le pasa al SDK como
`output_format=` y la API queda obligada a devolver algo que encaje. Si no
encaja, falla ruidosamente en `cliente_llm.py` en vez de colarse a la base de
datos como una clasificación inventada.

Nota para quien viene de Java: `BaseModel` de Pydantic es el equivalente a un
DTO con validación por anotaciones (tipo Bean Validation), y `Literal[...]` es
un enum cerrado — cualquier otro valor es un error de validación, no un
string más.

POR QUÉ HAY MÁS CAMPOS QUE `cv_id`
----------------------------------
Porque `cv_id`, con dos CVs que solo se diferencian en el idioma, lo resuelve
`idioma.py` gratis. Lo que no resuelve nadie más es:

  - que 15 de las 21 ofertas que pasan el filtro duro sean ruido
    (`relevante`, `motivo_descarte`)
  - que 7 tarjetas distintas sean la MISMA vacante publicada en 5 ciudades y
    bajo dos nombres de empresa (`empresa_normalizada`, `puesto_canonico`)
  - que la "empresa" sea en realidad una reclutadora (`es_reclutadora`)

Todo eso sale de la MISMA llamada. Más campos en una sola pasada, no más
llamadas: sigue siendo "entra texto, sale estructura", como fija el brief.
Comparar ofertas entre sí lo hace Python después, en `agrupador.py`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Niveles de responsabilidad. Cerrado a propósito: el eje sobre el que gira
# todo el perfil de la clienta es "dirige o no dirige", y un campo de texto
# libre aquí acabaría con veinte formas de decir lo mismo.
Seniority = Literal[
    "c_level",           # CEO, COO, Director General, Managing Director
    "direccion",         # Director de área con equipos y presupuesto
    "gerencia",          # Gerente / Manager con equipo pequeño
    "individual",        # Analista, especialista, sin equipo a cargo
    "desconocido",
]

Modalidad = Literal["presencial", "hibrido", "remoto", "desconocido"]

# `de` está aquí desde el 5 septiembre 2026 y NO es porque haya un CV alemán:
# es porque sin él el modelo no podía nombrar lo que estaba leyendo. Un anuncio
# escrito en alemán le obligaba a elegir entre "es" y "en", así que el campo
# devolvía siempre un dato falso y la discrepancia con `idioma.py` —que es lo
# que manda a una oferta a "necesita tu criterio"— no podía saltar nunca.
# Qué CV se adjunta a un anuncio alemán lo decide `CV_SUPLENTE` en `motor.py`.
Idioma = Literal["es", "en", "de"]


class Clasificacion(BaseModel):
    """Lo que el modelo devuelve por CADA oferta."""

    # --- Lo que decide si ella llega a verla ---------------------------------

    relevante: bool = Field(
        description=(
            "true si esta oferta merece que la candidata la mire. false solo si puedes "
            "decir en una frase concreta qué es lo que no encaja: a qué se dedica la "
            "empresa, qué se dirige en el puesto, o qué nivel tiene realmente. "
            "Ante la duda, true con confianza baja: enseñarle una de más le cuesta "
            "treinta segundos, esconderle una buena le cuesta la oportunidad entera."
        )
    )
    motivo_descarte: str | None = Field(
        description=(
            "Si relevante es false, una frase corta diciendo POR QUÉ no encaja. "
            "Si relevante es true, null."
        )
    )

    confianza: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Cómo de seguro estás de tu veredicto de relevancia, de 0 a 1. "
            "Baja de 0.7 cuando la descripción esté cortada, sea genérica, o el puesto "
            "sea ambiguo. Es preferible que dude a que acierte por casualidad."
        ),
    )
    razon: str = Field(
        description=(
            "Dos frases como mucho, dirigidas a la candidata, explicando tu veredicto "
            "citando lo que dice el anuncio. Esta frase se le enseña tal cual: si no "
            "convence a alguien que lee la oferta al lado, no sirve."
        )
    )

    # --- Qué CV le corresponde ----------------------------------------------

    cv_id: str = Field(
        description=(
            "El id del CV que hay que adjuntar, de la lista de CVs disponibles. "
            "Con CVs que solo se distinguen por idioma, el criterio es el idioma del anuncio."
        )
    )
    idioma_anuncio: Idioma = Field(
        description=(
            "Idioma en el que está redactado el anuncio: es, en o de. "
            "Si el anuncio trae el mismo texto en dos idiomas, di el del "
            "encabezado, que es el que la empresa puso primero."
        )
    )

    # --- Lo que permite agrupar ofertas equivalentes -------------------------

    empresa_normalizada: str = Field(
        description=(
            "El nombre del EMPLEADOR REAL, limpio y canónico: sin forma jurídica "
            "(LLC, S.A. de C.V., Inc.), sin ciudad, sin división. Si el anuncio deja claro "
            "que una marca es la misma empresa que otra (por ejemplo 'Northline Asset Services' "
            "es el negocio de 'Harlan Contracting Group'), usa el nombre de la matriz. "
            "Si quien publica es una agencia de reclutamiento y no se sabe el empleador final, "
            "pon el nombre de la agencia."
        )
    )
    puesto_canonico: str = Field(
        description=(
            "El título del puesto reducido a su forma estándar, sin ciudad, sin adornos de "
            "marketing y sin coletillas. 'COO, Asset Maintenance - Strategic Growth Leader "
            "(Dallas)' es 'Chief Operating Officer'. Úsalo en el idioma del anuncio."
        )
    )
    es_reclutadora: bool = Field(
        description=(
            "true si quien publica es una agencia de reclutamiento, headhunter o "
            "consultora de selección, y no el empleador final."
        )
    )

    # --- Contexto que cambia si le interesa o no -----------------------------

    seniority_detectado: Seniority = Field(
        description="Nivel de responsabilidad real del puesto según el anuncio."
    )
    modalidad: Modalidad = Field(
        description=(
            "presencial, hibrido o remoto según lo diga el anuncio. "
            "desconocido si no lo menciona: no lo deduzcas."
        )
    )
