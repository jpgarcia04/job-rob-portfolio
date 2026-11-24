"""
Contrato común de los conectores + utilidades de HTTP.

Principio que manda aquí: **que falle ruidosamente**.

Un conector NUNCA devuelve una lista vacía para disimular un problema. Si la
API responde 500, si cambia el formato, si devuelve cero resultados donde
esperábamos algo — se registra con la URL exacta, el código de estado y un
trozo del cuerpo de la respuesta, y eso termina impreso en el reporte final.

Cero resultados y "algo se rompió" se parecen mucho desde fuera. Distinguirlos
es la mitad del valor de esta v0.
"""

from __future__ import annotations

import abc
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from ..config import Market
from ..http_cache import MODE_CACHE_ONLY, CacheMissError, DiskCache
from ..models import FetchOutcome, KeywordCombo

logger = logging.getLogger(__name__)

# Cuánto cuerpo de respuesta se enseña cuando algo va mal. Suficiente para
# reconocer un mensaje de error de la API, corto para no inundar la consola.
ERROR_BODY_SNIPPET = 400


class ConnectorError(Exception):
    """Fallo al hablar con una fuente externa."""


class ApiHttpError(ConnectorError):
    """La API respondió con un código que no es 200."""


class ApiResponseError(ConnectorError):
    """La API respondió 200 pero con algo que no sabemos leer."""


def parse_datetime_loose(value: Any) -> datetime | None:
    """Parsea las fechas que devuelven estas APIs, que no son iguales.

      Adzuna: "2026-08-01T10:12:33Z"
      Jooble: "2026-08-01T10:12:33.0000000"   <- 7 decimales, que rompen
                                                 datetime.fromisoformat

    Devuelve siempre una fecha con zona horaria (UTC si la API no la dice).
    Devuelve None si no hay nada parseable, y quien llama decide qué hacer:
    NO se inventa `datetime.now()`, porque eso haría pasar por reciente una
    oferta de fecha desconocida.
    """
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    # "Z" (Zulu = UTC) no lo entiende fromisoformat en todas las versiones.
    text = text.replace("Z", "+00:00")

    # Recorta los decimales de segundo a 6 dígitos como máximo.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Segundo intento: formatos sin separador ISO.
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            # `for ... else`: el else se ejecuta si el bucle NO hizo break,
            # o sea si ningún formato funcionó.
            logger.warning("No se pudo interpretar la fecha %r", value)
            return None

    if parsed.tzinfo is None:
        # Fecha sin zona: se asume UTC. Un desfase de horas no cambia nada
        # cuando el filtro es de 30 días.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class BaseConnector(abc.ABC):
    """Lo que todo conector debe saber hacer.

    Nota para quien viene de Java: `abc.ABC` + `@abc.abstractmethod` es una
    clase abstracta. Una subclase que no implemente los métodos abstractos
    no se puede instanciar.
    """

    def __init__(self, cache: DiskCache, timeout_seconds: int, sleep_seconds: float) -> None:
        self.cache = cache
        self.timeout_seconds = timeout_seconds
        self.sleep_seconds = sleep_seconds
        # Una única `Session` reutiliza la conexión TCP entre peticiones.
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "recolector-ofertas/0.1 (sonda de viabilidad)"}
        )
        self.requests_made = 0

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Identificador corto de la fuente: 'adzuna', 'jooble'."""

    @abc.abstractmethod
    def fetch(self, combo: KeywordCombo, market: Market) -> FetchOutcome:
        """Ejecuta UNA consulta y devuelve el resultado ya normalizado.

        No debe lanzar excepciones por fallos de la API: los recoge en
        `FetchOutcome.error` para que la corrida siga y el reporte lo cuente.
        """

    @abc.abstractmethod
    def planned_requests(self, combos: int, markets: int) -> int:
        """Cuántas peticiones HTTP costaría una corrida. Se consulta ANTES de
        correr para poder avisar (o negarse) por cuota.
        """

    # -- HTTP ----------------------------------------------------------------

    def _throttle(self) -> None:
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        safe_url_for_logs: str,
    ) -> dict[str, Any]:
        """Hace la petición y devuelve el JSON, o revienta con contexto.

        `safe_url_for_logs` es la URL SIN credenciales. Todo lo que se imprime
        o se guarda en caché usa esa versión: las keys no deben acabar en la
        consola, ni en un log, ni en un archivo de caché.
        """
        self._throttle()
        self.requests_made += 1

        logger.debug("%s %s %s", self.name, method, safe_url_for_logs)

        try:
            response = self.session.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=self.timeout_seconds,
            )
        except requests.Timeout as exc:
            raise ApiHttpError(
                f"[{self.name}] TIMEOUT tras {self.timeout_seconds}s en {safe_url_for_logs}"
            ) from exc
        except requests.RequestException as exc:
            raise ApiHttpError(
                f"[{self.name}] Fallo de red en {safe_url_for_logs}: {exc}"
            ) from exc

        if response.status_code != 200:
            body = response.text[:ERROR_BODY_SNIPPET].replace("\n", " ")
            hint = ""
            if response.status_code in (401, 403):
                hint = "  -> Credenciales inválidas o no autorizadas para este índice."
            elif response.status_code == 429:
                hint = "  -> Límite de peticiones alcanzado. Sube sleep_seconds_between_calls."
            elif response.status_code >= 500:
                hint = "  -> Error del lado de la API. Reintentar más tarde."
            raise ApiHttpError(
                f"[{self.name}] HTTP {response.status_code} en {safe_url_for_logs}\n"
                f"  Cuerpo: {body}\n{hint}".rstrip()
            )

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            body = response.text[:ERROR_BODY_SNIPPET].replace("\n", " ")
            raise ApiResponseError(
                f"[{self.name}] Respuesta 200 que no es JSON en {safe_url_for_logs}\n"
                f"  Cuerpo: {body}"
            ) from exc

        if not isinstance(payload, dict):
            raise ApiResponseError(
                f"[{self.name}] Se esperaba un objeto JSON y llegó "
                f"{type(payload).__name__} en {safe_url_for_logs}"
            )

        return payload

    def fetch_with_cache(
        self,
        cache_key: str,
        request_meta: dict[str, Any],
        do_request,
    ) -> tuple[dict[str, Any], bool, list[str]]:
        """Envoltorio de caché común a los dos conectores.

        Devuelve (payload, vino_de_cache, avisos).

        `do_request` es una función sin argumentos que hace la petición real.
        Pasar una función en vez de ejecutarla es lo que permite NO gastar la
        petición cuando la caché sirve.
        """
        warnings: list[str] = []

        entry = self.cache.get(cache_key)
        if entry is not None:
            if entry.is_fresh:
                return entry.payload, True, warnings
            if self.cache.is_cache_only:
                warnings.append(
                    f"[{self.name}] Sirviendo respuesta CADUCADA de caché "
                    f"({entry.age_hours:.1f}h) porque estás en modo --cache-only."
                )
                return entry.payload, True, warnings

        if self.cache.mode == MODE_CACHE_ONLY:
            raise CacheMissError(
                f"[{self.name}] Modo --cache-only y no hay respuesta en caché para "
                f"{request_meta.get('url', cache_key)}.\n"
                f"  Clave de caché: {cache_key}\n"
                "  Corre una vez sin --cache-only para poblar la caché."
            )

        payload = do_request()
        self.cache.put(cache_key, request_meta, payload)
        return payload, False, warnings

    # -- ayudas para las subclases -------------------------------------------

    @staticmethod
    def require_list(payload: dict[str, Any], key: str, source: str, url: str) -> list[Any]:
        """Extrae una lista del payload o falla explicando qué llegó.

        Este es el punto donde se detecta que la API cambió de formato. Sin
        esto, un cambio de nombre de campo se traduciría en "0 resultados" y
        nos llevaría a la conclusión de negocio equivocada.
        """
        if key not in payload:
            keys_present = ", ".join(sorted(payload.keys())) or "(ninguna)"
            raise ApiResponseError(
                f"[{source}] La respuesta no trae el campo '{key}' en {url}\n"
                f"  Campos presentes: {keys_present}\n"
                "  Probablemente la API cambió de formato."
            )
        value = payload[key]
        if value is None:
            return []
        if not isinstance(value, list):
            raise ApiResponseError(
                f"[{source}] El campo '{key}' debería ser una lista y es "
                f"{type(value).__name__} en {url}"
            )
        return value
