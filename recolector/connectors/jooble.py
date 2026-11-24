"""
Conector de Jooble.

Documentación: https://jooble.org/api/about

Endpoint que se usa:
    POST https://{host}/api/{API_KEY}
    Cuerpo: {"keywords": "...", "location": "...", "page": "1"}

Dos particularidades que condicionan todo el diseño de este archivo:

1. **Una key por país/dominio.** La key de México (mx.jooble.org) no sirve
   contra el índice de EE.UU. (jooble.org) ni al revés. Por eso la key llega
   como parámetro del método, tomada del mercado, y no se guarda en el objeto.

2. **500 peticiones DE POR VIDA por key**, no mensuales. Cada llamada a
   `fetch()` que no venga de caché consume presupuesto irrecuperable. Por eso:
     - el contador de cuota se actualiza aquí mismo, petición a petición;
     - `max_pages` vale 1 por defecto (cada página es otra petición);
     - la caché en disco es parte del diseño, no una optimización.

Incertidumbre documentada: Jooble no publica la semántica de `keywords`. No
está claro si varios términos se combinan con AND, con OR o por relevancia.
Se mandan unidos por espacio, tal como se escribirían en su buscador, y el
reporte compara `totalCount` con lo que realmente llega para poder detectar
si la búsqueda está siendo demasiado laxa. Si hace falta apretar, está el
filtro local `require_all_terms_in_text` en la configuración.
"""

from __future__ import annotations

from typing import Any, Callable

from ..config import Market
from ..http_cache import DiskCache
from ..models import FetchOutcome, JobOffer, KeywordCombo
from ..quota import JoobleQuota
from ..textnorm import strip_html
from .base import ApiHttpError, ApiResponseError, BaseConnector, parse_datetime_loose

# Abreviaturas de estado de EE.UU. Solo se usan para separar ciudad de estado
# en el texto libre que devuelve Jooble ("Houston, TX").
_US_STATE_ABBREVIATIONS = frozenset({
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
})


class JoobleConnector(BaseConnector):
    def __init__(
        self,
        credentials_provider: Callable[[str], str],
        cache: DiskCache,
        quota: JoobleQuota,
        fingerprint_provider: Callable[[str], str],
        max_pages: int = 1,
        timeout_seconds: int = 30,
        sleep_seconds: float = 1.0,
    ) -> None:
        super().__init__(cache=cache, timeout_seconds=timeout_seconds, sleep_seconds=sleep_seconds)
        # Se recibe una FUNCIÓN que sabe dar la key a partir del nombre de la
        # variable de entorno, en vez de la key misma. Así el conector nunca
        # guarda credenciales como estado, y soporta una key por mercado sin
        # saber cuántos mercados hay.
        self._credentials_provider = credentials_provider
        self._fingerprint_provider = fingerprint_provider
        self.quota = quota
        self.max_pages = max_pages

    @property
    def name(self) -> str:
        return "jooble"

    def planned_requests(self, combos: int, markets: int) -> int:
        return combos * markets * self.max_pages

    # -- consulta ------------------------------------------------------------

    @staticmethod
    def request_body(combo: KeywordCombo, market: Market, page: int = 1) -> dict[str, str]:
        """Cuerpo de la petición.

        `keywords` va como una sola cadena, tal como se escribiría en su
        buscador. Jooble no documenta si combina los términos con AND, con OR
        o por relevancia; el filtro local `require_all_terms_in_text` de la
        configuración es la red de seguridad para cuando haga falta apretar.
        """
        return {
            "keywords": combo.as_query_string,
            "location": market.jooble.location,
            "page": str(page),
        }

    def cache_key_for(self, combo: KeywordCombo, market: Market, page: int = 1) -> str:
        """Clave de caché de una consulta concreta. Público para las pruebas."""
        body = self.request_body(combo, market, page)
        return DiskCache.build_key(
            source=self.name,
            market_key=market.key,
            query=combo.label,
            request_signature=(
                f"{market.jooble.host}|p{page}|{body['keywords']}|{body['location']}"
            ),
        )

    def fetch(self, combo: KeywordCombo, market: Market) -> FetchOutcome:
        outcome = FetchOutcome(source=self.name, market_key=market.key, combo=combo)

        env_name = market.jooble.api_key_env
        try:
            api_key = self._credentials_provider(env_name)
        except Exception as exc:
            outcome.error = str(exc)
            return outcome

        fingerprint = self._fingerprint_provider(api_key)
        host = market.jooble.host

        for page in range(1, self.max_pages + 1):
            body = self.request_body(combo, market, page)
            url = f"https://{host}/api/{api_key}"
            # La key va en la RUTA de la URL de Jooble, así que hay que
            # enmascararla explícitamente antes de imprimir o cachear nada.
            safe_url = f"https://{host}/api/***"

            cache_key = self.cache_key_for(combo, market, page)

            def do_request() -> dict[str, Any]:
                payload = self.request_json(
                    "POST", url, json_body=body, safe_url_for_logs=safe_url
                )
                # La cuota se anota INMEDIATAMENTE después de que la petición
                # salga, no al final de la corrida: si el proceso muere a
                # mitad, el gasto ya está registrado.
                self.quota.record(env_name, fingerprint, count=1)
                return payload

            try:
                payload, from_cache, cache_warnings = self.fetch_with_cache(
                    cache_key=cache_key,
                    request_meta={"url": safe_url, "method": "POST", "body": body},
                    do_request=do_request,
                )
            except (ApiHttpError, ApiResponseError) as exc:
                outcome.error = str(exc)
                return outcome

            outcome.warnings.extend(cache_warnings)
            outcome.served_from_cache = outcome.served_from_cache or from_cache
            if not from_cache:
                outcome.requests_made += 1

            jobs = self.require_list(payload, "jobs", self.name, safe_url)

            if outcome.reported_total is None:
                reported = payload.get("totalCount")
                outcome.reported_total = int(reported) if isinstance(reported, (int, float)) else None

            if not jobs:
                outcome.warnings.append(
                    f"[jooble/{market.key}] 0 resultados para «{combo.label}» "
                    f"(totalCount={outcome.reported_total}) — "
                    f"host={host} location='{market.jooble.location}'"
                )
                break

            for index, item in enumerate(jobs):
                try:
                    outcome.offers.append(self._to_offer(item, combo, market))
                except (KeyError, TypeError, ValueError) as exc:
                    outcome.warnings.append(
                        f"[jooble/{market.key}] Resultado #{index} ilegible en "
                        f"«{combo.label}»: {type(exc).__name__}: {exc}"
                    )

        return outcome

    # -- traducción al esquema común -----------------------------------------

    @staticmethod
    def _split_location(text: str) -> tuple[str | None, str | None]:
        """Separa "Houston, TX" en (ciudad, estado).

        Heurística deliberadamente simple, con sus límites a la vista:
          - "Houston, TX"            -> ("Houston", "TX")
          - "Ciudad de México"       -> ("Ciudad de México", None)
          - "Austin, Texas, US"      -> ("Austin", "Texas")
        Jooble devuelve texto libre sin estructura, así que no hay forma
        correcta al 100%. Lo que no se puede separar se deja como ciudad y el
        filtro geográfico se apoya igualmente en `location_raw`.
        """
        if not text:
            return None, None

        parts = [p.strip() for p in text.split(",") if p.strip()]
        if not parts:
            return None, None
        if len(parts) == 1:
            return parts[0], None

        city = parts[0]
        second = parts[1]
        # Un token de dos letras que es abreviatura de estado -> es el estado.
        if len(second) == 2 and second.lower() in _US_STATE_ABBREVIATIONS:
            return city, second.upper()
        return city, second

    def _to_offer(self, item: dict[str, Any], combo: KeywordCombo, market: Market) -> JobOffer:
        location_raw = str(item.get("location") or "")
        city, region = self._split_location(location_raw)

        # Jooble no da salario estructurado: es texto libre ("$90,000 per year")
        # y muchas veces cadena vacía. No se intenta parsear a número: un
        # salario mal parseado es peor que no tenerlo.
        salary_raw = str(item.get("salary") or "").strip() or None

        return JobOffer(
            source=self.name,
            market_key=market.key,
            query_label=combo.label,
            external_id=str(item.get("id") or ""),
            title=strip_html(item.get("title")),
            company=strip_html(item.get("company")),
            location_raw=location_raw,
            location_city=city,
            location_region=region,
            location_country=market.country_code.upper(),
            url=str(item.get("link") or ""),
            description=strip_html(item.get("snippet")),
            salary_min=None,
            salary_max=None,
            salary_currency=None,
            salary_raw=salary_raw,
            salary_is_predicted=False,
            posted_at=parse_datetime_loose(item.get("updated")),
            raw=item,
        )
