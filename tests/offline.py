"""
Entorno de ejecución offline.

Siembra la caché con las respuestas de `fixtures.py` y monta el pipeline en
modo `cache_only`, de forma que la corrida entera se ejecuta:

  - sin red
  - sin API keys
  - sin gastar una sola petición de la cuota vital de Jooble

Sirve para dos cosas: las pruebas automáticas, y `tools/demo_offline.py`, que
imprime un reporte completo de ejemplo. Es la forma de verificar el pipeline
sin quemar cuota, que era una de las condiciones del encargo.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from recolector.config import AppConfig, CacheConfig, SearchProfile, load_config
from recolector.connectors import AdzunaConnector, JoobleConnector
from recolector.connectors.base import BaseConnector
from recolector.http_cache import MODE_CACHE_ONLY, DiskCache
from recolector.pipeline import Pipeline, RunStats
from recolector.quota import JoobleQuota
from recolector.storage import Storage

from . import fixtures

DUMMY_ADZUNA_ID = "offline-app-id"
DUMMY_ADZUNA_KEY = "offline-app-key"
DUMMY_JOOBLE_KEY = "offline-jooble-key"

DEFAULT_SOURCES = ("adzuna", "jooble")
DEFAULT_MARKETS = ("alemania", "texas_us")
# Las combinaciones para las que hay respuestas de ejemplo.
COMBOS_WITH_FIXTURES = 2


def build_config(tmp_dir: Path) -> AppConfig:
    """Configuración real del proyecto, con las rutas apuntando a un temporal.

    `dataclasses.replace` devuelve una copia con algunos campos cambiados.
    Hace falta porque AppConfig es inmutable (frozen=True), que es justo lo
    que queremos: la configuración no debe poder mutarse por accidente a
    mitad de una corrida.

    IMPORTANTE: los DOS filtros de pertinencia —`require_all_terms_in_text` e
    `include_title_terms`— se fijan explícitamente aquí en vez de heredar lo
    que diga el YAML de producción. Las cifras esperadas en
    test_pipeline_offline.py se calcularon a mano contra esos valores. Si las
    pruebas leyeran el YAML real, ajustar la configuración del proyecto (como
    se hizo al descubrir que el `what_and` de Adzuna no filtra de forma
    fiable, o al cambiar a lista blanca de títulos) rompería pruebas que no
    tienen nada que ver con ese cambio. Las pruebas deben ser deterministas
    con independencia de cómo esté afinado el proyecto en cada momento.

    Quien SÍ prueba el afinado real es tests/test_lista_blanca.py, y lo hace
    contra el YAML que se envía, a propósito.
    """
    config = load_config()
    active_profile = config.profiles[config.active_profile_key]
    fixed_profile = dataclasses.replace(
        active_profile,
        hard_filters=dataclasses.replace(
            active_profile.hard_filters,
            require_all_terms_in_text=False,
            include_title_terms=[],
        ),
    )
    profiles = {**config.profiles, config.active_profile_key: fixed_profile}

    return dataclasses.replace(
        config,
        profiles=profiles,
        database_path=tmp_dir / "ofertas.db",
        cache=CacheConfig(
            enabled=True,
            directory=tmp_dir / "cache",
            # TTL enorme: las respuestas sembradas nunca deben considerarse
            # caducadas durante la prueba.
            ttl_hours=24 * 365 * 10,
        ),
    )


def build_connectors(
    config: AppConfig,
    cache: DiskCache,
    quota: JoobleQuota,
    profile: SearchProfile,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
) -> dict[str, BaseConnector]:
    connectors: dict[str, BaseConnector] = {}

    if "adzuna" in sources:
        connectors["adzuna"] = AdzunaConnector(
            app_id=DUMMY_ADZUNA_ID,
            app_key=DUMMY_ADZUNA_KEY,
            cache=cache,
            results_per_page=config.adzuna.results_per_page,
            max_pages=config.adzuna.max_pages,
            max_days_old=profile.hard_filters.max_days_old,
            timeout_seconds=config.adzuna.timeout_seconds,
            # Sin pausas: no hay red que cuidar.
            sleep_seconds=0.0,
        )

    if "jooble" in sources:
        connectors["jooble"] = JoobleConnector(
            credentials_provider=lambda _env_name: DUMMY_JOOBLE_KEY,
            cache=cache,
            quota=quota,
            fingerprint_provider=lambda _key: "offline",
            max_pages=config.jooble.max_pages,
            timeout_seconds=config.jooble.timeout_seconds,
            sleep_seconds=0.0,
        )

    return connectors


def seed_cache(
    config: AppConfig,
    cache: DiskCache,
    connectors: dict[str, BaseConnector],
    profile: SearchProfile,
    market_keys: tuple[str, ...],
    combo_count: int,
) -> int:
    """Mete las respuestas de ejemplo en la caché.

    La clave se pide al propio conector (`cache_key_for`) en lugar de
    recalcularla aquí. Duplicar esa lógica sería garantizar que un día se
    desincronice y las pruebas empiecen a sembrar en un sitio y a leer en otro.
    """
    seeded = 0
    for market_key in market_keys:
        market = config.market(market_key)
        for combo in profile.keyword_combinations[:combo_count]:
            for source_name, connector in connectors.items():
                payload_factory = fixtures.PAYLOADS.get(
                    (source_name, market_key, combo.label)
                )
                if payload_factory is None:
                    continue
                key = connector.cache_key_for(combo, market)  # type: ignore[attr-defined]
                cache.put(
                    key,
                    {"url": f"offline://{source_name}/{market_key}", "method": "OFFLINE"},
                    payload_factory(),
                )
                seeded += 1
    return seeded


def run_offline(
    tmp_dir: Path,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    combo_count: int = COMBOS_WITH_FIXTURES,
) -> tuple[RunStats, AppConfig, SearchProfile, Storage]:
    """Ejecuta una corrida completa contra las respuestas de ejemplo.

    Devuelve el Storage ABIERTO: el reporte necesita consultarlo. Quien llame
    es responsable de cerrarlo.
    """
    config = build_config(tmp_dir)
    profile = config.profile()

    cache = DiskCache(
        directory=config.cache.directory,
        ttl_hours=config.cache.ttl_hours,
        enabled=True,
        mode=MODE_CACHE_ONLY,
    )
    quota = JoobleQuota(tmp_dir / "quota_jooble.json", config.jooble.lifetime_request_budget)

    connectors = build_connectors(config, cache, quota, profile, sources)
    seed_cache(config, cache, connectors, profile, markets, combo_count)

    storage = Storage(config.database_path)
    pipeline = Pipeline(config=config, storage=storage, connectors=connectors)
    stats = pipeline.run(
        profile=profile,
        market_keys=list(markets),
        source_names=list(sources),
        limit_combos=combo_count,
    )
    return stats, config, profile, storage
