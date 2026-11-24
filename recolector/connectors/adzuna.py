"""
Conector de Adzuna.

Documentación: https://developer.adzuna.com/

Endpoint que se usa:
    GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}

Decisiones no obvias, explicadas:

1. Se usa `what_and` y NO `what`.
   `what` trata los términos como una búsqueda de relevancia (tira a OR):
   "COO Logistics" devuelve todo lo que contenga *cualquiera* de las dos
   palabras, y eso son miles de resultados irrelevantes. `what_and` exige que
   TODAS las palabras aparezcan, que es lo que "COO + Logistics"
   significa de verdad.

   Efecto secundario asumido: `what_and` trabaja por palabras sueltas, así
   que "VP Operations" se convierte en (VP AND Operations), no en la frase
   exacta. Para este caso es preferible: gana recall sin perder precisión
   apreciable.

2. La divisa no viene en la respuesta: se deduce del índice de país.

3. `salary_is_predicted` llega como "1"/"0" (texto, no booleano). Cuando vale
   "1" el salario es una ESTIMACIÓN de Adzuna, no un dato del anuncio. Se
   marca en el modelo para no presentarlo nunca como si fuera real.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ..config import Market
from ..http_cache import DiskCache
from ..models import FetchOutcome, JobOffer, KeywordCombo
from ..textnorm import strip_html
from .base import ApiHttpError, ApiResponseError, BaseConnector, parse_datetime_loose

API_ROOT = "https://api.adzuna.com/v1/api/jobs"

# Adzuna no devuelve la divisa. Se infiere del índice consultado.
#
# `de` FALTABA, y entró el 4 septiembre 2026 al migrar las pruebas a Alemania.
# Sin él, `market.adzuna.country == "de"` daba `None` y las 36 ofertas alemanas
# de la base salían con `salary_currency` a NULL: `salary.py` no puede comparar
# una cifra sin divisa, así que TODAS caían en el cubo «desconocido» y el
# umbral de 170.000 USD de la clienta era ciego sobre medio inventario.
#
# Hoy muerde poco —solo 1 de las 36 trae salario declarado, Adzuna Alemania casi
# nunca lo da— y por eso no había saltado. Muerde entero el día que lo traiga.
#
# La regla al añadir un mercado: si su `country` no está en este diccionario, el
# salario de ese mercado no se evalúa y nadie avisa.
CURRENCY_BY_COUNTRY = {
    "mx": "MXN",
    "us": "USD",
    "gb": "GBP",
    "ca": "CAD",
    "es": "EUR",
    "de": "EUR",
}


def desglosar_area(area: Any) -> tuple[str | None, str | None, str | None, str | None]:
    """(país, región, condado, ciudad) a partir de `location.area`.

    `area` es una jerarquía de lo general a lo concreto, y su profundidad
    varía. Medido sobre los 392 anuncios de Texas de la base (24 sept 2026):

        ["US", "Texas"]                                         5
        ["US", "Texas", "Travis County"]                       25
        ["US", "Texas", "Harris County", "Houston"]           334
        ["US", "Texas", "Harris County", "Houston", "Audubon Place"]  28

    El CONDADO es el tercer nivel, pero solo se da por condado cuando hay algo
    debajo: con tres niveles ese tercero ya es la ciudad («Dallas», o el
    propio «Travis County»), y va en `ciudad` como siempre. Con cinco, la
    ciudad que se guarda es el barrio —así era antes y no se toca— y por eso
    el condado tiene que ir aparte: el texto visible, «Audubon Place,
    Houston», ya no lo lleva.

    Está suelta porque la usa también el recorte de la base, que vuelve a
    leer la respuesta cruda guardada en `offer_sources.raw_json`.
    """
    niveles = [str(a) for a in (area or [])]
    country = niveles[0] if len(niveles) >= 1 else None
    region = niveles[1] if len(niveles) >= 2 else None
    county = niveles[2] if len(niveles) >= 4 else None
    city = niveles[-1] if len(niveles) >= 3 else None
    return country, region, county, city


class AdzunaConnector(BaseConnector):
    def __init__(
        self,
        app_id: str,
        app_key: str,
        cache: DiskCache,
        results_per_page: int = 50,
        max_pages: int = 1,
        max_days_old: int = 30,
        timeout_seconds: int = 30,
        sleep_seconds: float = 1.0,
    ) -> None:
        super().__init__(cache=cache, timeout_seconds=timeout_seconds, sleep_seconds=sleep_seconds)
        self._app_id = app_id
        self._app_key = app_key
        self.results_per_page = results_per_page
        self.max_pages = max_pages
        self.max_days_old = max_days_old

    @property
    def name(self) -> str:
        return "adzuna"

    def planned_requests(self, combos: int, markets: int) -> int:
        return combos * markets * self.max_pages

    # -- consulta ------------------------------------------------------------

    def _public_params(self, combo: KeywordCombo, market: Market) -> dict[str, Any]:
        """Parámetros que SÍ afectan al resultado.

        Se separan de las credenciales a propósito: estos son los que van a la
        firma de caché y a los mensajes de log. Las keys nunca.
        """
        params: dict[str, Any] = {
            "results_per_page": self.results_per_page,
            "what_and": combo.as_query_string,
            "max_days_old": self.max_days_old,
            "sort_by": "date",
        }
        if market.adzuna.where:
            params["where"] = market.adzuna.where
        return params

    def cache_key_for(self, combo: KeywordCombo, market: Market, page: int = 1) -> str:
        """Clave de caché de una consulta concreta.

        Es público para que las pruebas puedan sembrar respuestas sin
        duplicar aquí la lógica de construcción de la clave (que es
        exactamente el tipo de duplicado que se desincroniza y provoca fallos
        imposibles de ver).
        """
        public_params = self._public_params(combo, market)
        return DiskCache.build_key(
            source=self.name,
            market_key=market.key,
            query=combo.label,
            request_signature=(
                f"{market.adzuna.country}|p{page}|{urlencode(sorted(public_params.items()))}"
            ),
        )

    def fetch(self, combo: KeywordCombo, market: Market) -> FetchOutcome:
        outcome = FetchOutcome(source=self.name, market_key=market.key, combo=combo)
        country = market.adzuna.country

        for page in range(1, self.max_pages + 1):
            public_params = self._public_params(combo, market)

            url = f"{API_ROOT}/{country}/search/{page}"
            safe_url = f"{url}?{urlencode(public_params)}"

            # Las credenciales se añaden aparte y NO entran ni en la caché ni
            # en los logs.
            request_params = {"app_id": self._app_id, "app_key": self._app_key, **public_params}

            cache_key = self.cache_key_for(combo, market, page)

            try:
                payload, from_cache, cache_warnings = self.fetch_with_cache(
                    cache_key=cache_key,
                    request_meta={"url": safe_url, "method": "GET"},
                    do_request=lambda: self.request_json(
                        "GET", url, params=request_params, safe_url_for_logs=safe_url
                    ),
                )
            except (ApiHttpError, ApiResponseError) as exc:
                outcome.error = str(exc)
                return outcome

            outcome.warnings.extend(cache_warnings)
            outcome.served_from_cache = outcome.served_from_cache or from_cache
            if not from_cache:
                outcome.requests_made += 1

            results = self.require_list(payload, "results", self.name, safe_url)

            # `count` es cuántas ofertas dice Adzuna que hay EN TOTAL para esa
            # búsqueda, no cuántas cabían en la página. Es un dato de negocio
            # valioso: si count=3, no hay mercado, y eso hay que verlo.
            if outcome.reported_total is None:
                reported = payload.get("count")
                outcome.reported_total = int(reported) if isinstance(reported, (int, float)) else None

            if not results:
                outcome.warnings.append(
                    f"[adzuna/{market.key}] 0 resultados para «{combo.label}» "
                    f"(count={outcome.reported_total}) — URL: {safe_url}"
                )
                break  # sin resultados no tiene sentido pedir la página siguiente

            for index, item in enumerate(results):
                try:
                    outcome.offers.append(self._to_offer(item, combo, market))
                except (KeyError, TypeError, ValueError) as exc:
                    # Un resultado mal formado no debe tumbar la corrida, pero
                    # tampoco desaparecer: se cuenta como aviso.
                    outcome.warnings.append(
                        f"[adzuna/{market.key}] Resultado #{index} ilegible en "
                        f"«{combo.label}»: {type(exc).__name__}: {exc}"
                    )

            if len(results) < self.results_per_page:
                break  # última página

        return outcome

    # -- traducción al esquema común -----------------------------------------

    def _to_offer(self, item: dict[str, Any], combo: KeywordCombo, market: Market) -> JobOffer:
        location = item.get("location") or {}
        country, region, county, city = desglosar_area(location.get("area"))

        company = (item.get("company") or {}).get("display_name") or ""

        # "1"/"0" como texto. `str(...) == "1"` cubre también el caso de que
        # algún día llegue como número o como booleano.
        predicted_raw = item.get("salary_is_predicted")
        is_predicted = str(predicted_raw) in ("1", "True", "true")

        salary_min = item.get("salary_min")
        salary_max = item.get("salary_max")

        return JobOffer(
            source=self.name,
            market_key=market.key,
            query_label=combo.label,
            external_id=str(item.get("id") or ""),
            title=strip_html(item.get("title")),
            company=strip_html(company),
            location_raw=str(location.get("display_name") or ""),
            location_city=city,
            location_region=region,
            location_country=country,
            location_county=county,
            url=str(item.get("redirect_url") or ""),
            description=strip_html(item.get("description")),
            salary_min=float(salary_min) if isinstance(salary_min, (int, float)) else None,
            salary_max=float(salary_max) if isinstance(salary_max, (int, float)) else None,
            salary_currency=CURRENCY_BY_COUNTRY.get(market.adzuna.country),
            salary_raw=None,
            salary_is_predicted=is_predicted,
            posted_at=parse_datetime_loose(item.get("created")),
            raw=item,
        )
