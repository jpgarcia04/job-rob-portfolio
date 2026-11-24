"""
El pipeline: orquesta la corrida y acumula las estadísticas.

Flujo, por cada mercado x combinación x fuente:

    consultar -> normalizar -> filtrar -> hashear -> guardar -> contar

Lo que hace que esto sea una sonda y no un recolector cualquiera es la última
palabra: **contar**. Se registra el recuento en cada etapa, para poder mirar
el reporte y saber en qué punto exacto se perdieron las ofertas. Sin eso, un
resultado bajo es ambiguo y no responde la pregunta de negocio.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import AppConfig, SearchProfile
from .connectors.base import BaseConnector
from .dedupe import assign_keys
from .filters import FilterResult, apply_filters
from .geo import UNRESOLVED
from .http_cache import CacheMissError
from .models import KeywordCombo
from .salary import BELOW, MEETS
from .storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class QueryStat:
    """Resultado de UNA consulta: una combinación, en un mercado, por una fuente."""

    market_key: str
    combo_label: str
    source: str

    raw: int = 0                       # resultados devueltos por la API
    reported_total: int | None = None  # los que la API dice que existen en total
    kept: int = 0                      # los que pasan los filtros duros
    new_in_db: int = 0                 # los que nunca habíamos visto
    requests: int = 0
    from_cache: bool = False
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class RunStats:
    """Todo lo que el reporte necesita saber de una corrida."""

    run_id: str
    profile_key: str
    started_at: datetime
    market_keys: list[str]
    source_names: list[str]
    combos: list[KeywordCombo]

    queries: list[QueryStat] = field(default_factory=list)

    # Motivos de descarte -> cuántas cayeron por cada uno.
    drop_reasons: Counter = field(default_factory=Counter)
    # Detalle: hasta 3 ejemplos por motivo, para poder verlos en el reporte.
    drop_examples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    # Cubos del umbral salarial.
    salary_buckets: Counter = field(default_factory=Counter)
    salary_predicted: int = 0

    # Ubicaciones que no permitieron decidir el estado, por mercado.
    geo_unresolved: Counter = field(default_factory=Counter)
    geo_unresolved_examples: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )

    # Claves de deduplicación vistas, por fuente y en total.
    keys_by_source: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    keys_by_market: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    all_keys: set[str] = field(default_factory=set)
    all_loose_keys: set[str] = field(default_factory=set)

    requests_by_source: Counter = field(default_factory=Counter)
    cache_hits: int = 0

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Sospechas de fuente caída: (fuente, mercado) que devolvieron cero en TODAS
    # las consultas sin dar un solo error. Ver `_detect_silent_sources`.
    silent_sources: list[tuple[str, str]] = field(default_factory=list)

    finished_at: datetime | None = None

    # -- agregados para el reporte -------------------------------------------

    def raw_total(self) -> int:
        return sum(q.raw for q in self.queries)

    def kept_total(self) -> int:
        return sum(q.kept for q in self.queries)

    def new_total(self) -> int:
        return sum(q.new_in_db for q in self.queries)

    def unique_total(self) -> int:
        return len(self.all_keys)

    def duplicates_collapsed(self) -> int:
        """Cuántas apariciones se fusionaron: guardadas menos únicas."""
        return max(0, self.kept_total() - self.unique_total())

    def under_merge_gap(self) -> int:
        """Duplicados PROBABLES que la clave estricta no detectó.

        Diferencia entre las claves estrictas (empresa+título+ubicación) y las
        laxas (empresa+título). Ver la explicación larga en dedupe.py.
        """
        return max(0, len(self.all_keys) - len(self.all_loose_keys))

    def by_market(self, market_key: str) -> list[QueryStat]:
        return [q for q in self.queries if q.market_key == market_key]

    def by_source(self, source: str) -> list[QueryStat]:
        return [q for q in self.queries if q.source == source]

    def exclusive_keys(self, source: str) -> set[str]:
        """Claves que SOLO alcanzó esta fuente. El número que decide Jooble."""
        others: set[str] = set()
        for name, keys in self.keys_by_source.items():
            if name != source:
                others |= keys
        return self.keys_by_source.get(source, set()) - others

    def shared_keys(self) -> set[str]:
        """Claves alcanzadas por más de una fuente."""
        counts: Counter = Counter()
        for keys in self.keys_by_source.values():
            counts.update(keys)
        return {key for key, count in counts.items() if count > 1}


class Pipeline:
    def __init__(
        self,
        config: AppConfig,
        storage: Storage,
        connectors: dict[str, BaseConnector],
    ) -> None:
        self.config = config
        self.storage = storage
        self.connectors = connectors

    def run(
        self,
        profile: SearchProfile,
        market_keys: list[str],
        source_names: list[str],
        limit_combos: int | None = None,
    ) -> RunStats:
        # CADA MERCADO PREGUNTA LO SUYO (17 septiembre 2026). Ver
        # `SearchProfile.combinations_for`: las 10 combinaciones en español solo
        # tienen sentido en México, y las dos que llevan «Mexico» dentro solo en
        # Texas. Cuando nadie declara nada, los tres mercados reciben la misma
        # lista y esto es idéntico al bucle de antes.
        combos_por_mercado = {
            market_key: (
                profile.combinations_for(market_key)[:limit_combos]
                if limit_combos is not None
                else profile.combinations_for(market_key)
            )
            for market_key in market_keys
        }

        run_id = self.storage.start_run(profile.key, source_names, market_keys)

        # Las combinaciones del reporte son la UNIÓN de las de todos los
        # mercados, sin repetir y en orden estable: el reporte cuenta por
        # combinación y una que solo corre en un mercado también tiene que salir.
        union: list[KeywordCombo] = []
        for market_key in market_keys:
            for combo in combos_por_mercado[market_key]:
                if combo not in union:
                    union.append(combo)

        stats = RunStats(
            run_id=run_id,
            profile_key=profile.key,
            started_at=datetime.now(timezone.utc),
            market_keys=market_keys,
            source_names=source_names,
            combos=union,
        )

        total_queries = sum(
            len(combos_por_mercado[k]) for k in market_keys
        ) * len(source_names)
        done = 0

        for market_key in market_keys:
            market = self.config.market(market_key)
            for combo in combos_por_mercado[market_key]:
                for source_name in source_names:
                    connector = self.connectors[source_name]
                    done += 1
                    # Progreso en vivo: una corrida completa tarda minutos por
                    # las pausas entre peticiones, y una consola muda parece
                    # colgada.
                    print(
                        f"  [{done:3d}/{total_queries}] {source_name:<7} "
                        f"{market.label:<14} «{combo.label}»",
                        flush=True,
                    )
                    stat = self._run_one(
                        connector, combo, market, profile, run_id, stats
                    )
                    stats.queries.append(stat)

        self.storage.commit()
        self._detect_silent_sources(stats)
        stats.finished_at = datetime.now(timezone.utc)
        return stats

    # -- una consulta --------------------------------------------------------

    def _run_one(
        self,
        connector: BaseConnector,
        combo: KeywordCombo,
        market,
        profile: SearchProfile,
        run_id: str,
        stats: RunStats,
    ) -> QueryStat:
        stat = QueryStat(
            market_key=market.key, combo_label=combo.label, source=connector.name
        )

        try:
            outcome = connector.fetch(combo, market)
        except CacheMissError as exc:
            # Modo --cache-only sin datos previos: es un error de uso, no de
            # la API, y conviene que se vea distinto.
            stat.error = str(exc)
            stats.errors.append(str(exc))
            return stat
        except Exception as exc:  # noqa: BLE001 - queremos ver CUALQUIER fallo
            # Se captura todo a propósito: una consulta rota no debe tumbar la
            # corrida entera, pero tampoco puede pasar desapercibida.
            message = f"[{connector.name}/{market.key}] «{combo.label}» {type(exc).__name__}: {exc}"
            stat.error = message
            stats.errors.append(message)
            logger.exception("Fallo inesperado en %s", message)
            return stat

        stat.raw = len(outcome.offers)
        stat.reported_total = outcome.reported_total
        stat.requests = outcome.requests_made
        stat.from_cache = outcome.served_from_cache
        stat.warnings = list(outcome.warnings)

        stats.requests_by_source[connector.name] += outcome.requests_made
        if outcome.served_from_cache:
            stats.cache_hits += 1
        stats.warnings.extend(outcome.warnings)

        if outcome.failed:
            stat.error = outcome.error
            stats.errors.append(outcome.error or "error sin detalle")
            return stat

        for offer in outcome.offers:
            result = apply_filters(offer, profile, market, combo)
            self._record_result(result, stats)

            if result.dropped:
                continue

            assign_keys(offer)
            offer.zona = result.geo.zona
            is_new = self.storage.save_offer(offer, run_id)

            stat.kept += 1
            if is_new:
                stat.new_in_db += 1

            stats.keys_by_source[connector.name].add(offer.dedupe_key)
            stats.keys_by_market[market.key].add(offer.dedupe_key)
            stats.all_keys.add(offer.dedupe_key)
            stats.all_loose_keys.add(offer.loose_key)

        return stat

    @staticmethod
    def _record_result(result: FilterResult, stats: RunStats) -> None:
        """Contabiliza una oferta, se haya quedado o no.

        El salario y la geografía se cuentan SIEMPRE, incluso en las
        descartadas: queremos saber cuántas ofertas del mercado traían
        salario, no solo cuántas sobrevivieron a los filtros.
        """
        stats.salary_buckets[result.salary.status] += 1
        if result.salary.is_predicted and result.salary.status in (MEETS, BELOW):
            stats.salary_predicted += 1

        if result.geo.status == UNRESOLVED:
            market = result.offer.market_key
            stats.geo_unresolved[market] += 1
            examples = stats.geo_unresolved_examples[market]
            if len(examples) < 3 and result.offer.location_raw:
                examples.append(result.offer.location_raw)

        if result.dropped and result.drop_reason:
            stats.drop_reasons[result.drop_reason] += 1
            examples = stats.drop_examples[result.drop_reason]
            if len(examples) < 3:
                examples.append(
                    f"«{result.offer.title}» — {result.drop_detail or ''}".strip(" —")
                )

    # -- guardas de fallo silencioso -----------------------------------------

    @staticmethod
    def _detect_silent_sources(stats: RunStats) -> None:
        """Detecta fuentes que devolvieron cero en TODO un mercado sin error.

        Este es el fallo que más daño haría a la conclusión de negocio: una
        API sin cuota o con la key caducada puede responder HTTP 200 con cero
        resultados, y desde fuera eso es idéntico a "no hay ofertas".

        Cero resultados en una consulta suelta es normal y esperable. Cero en
        las trece, en el mismo mercado, sin un solo error, no lo es.
        """
        grouped: dict[tuple[str, str], list[QueryStat]] = defaultdict(list)
        for query in stats.queries:
            grouped[(query.source, query.market_key)].append(query)

        for (source, market_key), queries in grouped.items():
            if len(queries) < 3:
                # Con una o dos consultas no hay evidencia suficiente para
                # acusar a la fuente.
                continue
            if any(q.error for q in queries):
                continue  # ya falló ruidosamente por su cuenta
            if all(q.raw == 0 for q in queries):
                stats.silent_sources.append((source, market_key))
