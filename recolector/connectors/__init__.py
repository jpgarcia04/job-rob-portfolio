"""Conectores contra las APIs externas.

Regla de la casa: nada fuera de este paquete sabe que Adzuna o Jooble existen.
El resto del sistema habla de `JobOffer` y de `FetchOutcome`. Añadir una
fuente nueva (LinkedIn en la v1) debe ser un archivo más aquí dentro, no una
refactorización.
"""

from .adzuna import AdzunaConnector
from .base import ApiHttpError, ApiResponseError, BaseConnector, ConnectorError
from .jooble import JoobleConnector

__all__ = [
    "AdzunaConnector",
    "JoobleConnector",
    "BaseConnector",
    "ConnectorError",
    "ApiHttpError",
    "ApiResponseError",
]
