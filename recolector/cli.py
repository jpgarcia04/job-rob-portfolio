"""
El comando de consola.

    python -m recolector run                      corrida completa
    python -m recolector run --sources adzuna     solo Adzuna (recomendado al principio)
    python -m recolector run --cache-only         sin tocar la red, sobre lo cacheado
    python -m recolector quota                    estado de las cuotas
    python -m recolector check                    valida la configuración y sale

Recomendación de uso, y no es un detalle menor: **las primeras corridas, solo
con Adzuna**. Jooble tiene 500 peticiones de por vida POR KEY, y una corrida
completa se come 13 de cada una. Cuando el pipeline ya no cambie, haces UNA
pasada con las dos fuentes para medir cuánto aporta Jooble en exclusiva.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import (
    AppConfig,
    ConfigError,
    Credentials,
    MissingCredentialsError,
    SearchProfile,
    load_config,
)
from .connectors import AdzunaConnector, JoobleConnector
from .connectors.adzuna_descripcion import (
    DescripcionError,
    TraedorDeDescripciones,
)
from .connectors.base import BaseConnector
from .http_cache import (
    MODE_BYPASS,
    MODE_CACHE_ONLY,
    MODE_NORMAL,
    CacheMissError,
    DiskCache,
)
from .pipeline import Pipeline
from .quota import (
    ApiBudgetExceededError,
    JoobleQuota,
    QuotaExhaustedError,
    WindowedUsage,
)
from .report import build_report
from .storage import Storage

JOOBLE_QUOTA_FILE = "quota_jooble.json"
ADZUNA_USAGE_FILE = "uso_adzuna.json"
REPORTS_DIRNAME = "reportes"


def _prepare_console() -> None:
    """Deja la consola de Windows en condiciones de imprimir acentos.

    La consola de Windows usa cp1252 por defecto y revienta (o escribe basura)
    con caracteres que no estén en esa tabla. `errors="replace"` garantiza que
    nunca se caiga por un carácter raro: como mucho saldrá un interrogante.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                # Algunas terminales no lo permiten. No es motivo para abortar.
                pass


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # `requests` y `urllib3` son muy ruidosos en DEBUG y tapan lo nuestro.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _split_list(value: str | None) -> list[str] | None:
    """Convierte 'adzuna,jooble' en ['adzuna', 'jooble']."""
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


# ---------------------------------------------------------------------------
#  Construcción de los conectores
# ---------------------------------------------------------------------------

def _build_connectors(
    config: AppConfig,
    credentials: Credentials,
    cache: DiskCache,
    source_names: list[str],
    max_days_old: int,
    jooble_quota: JoobleQuota,
    cache_only: bool = False,
) -> dict[str, BaseConnector]:
    connectors: dict[str, BaseConnector] = {}

    # En modo --cache-only no sale ni una petición, así que no hacen falta
    # credenciales. Exigirlas ahí impediría trabajar sobre datos ya
    # descargados en una máquina sin `.env`, que es justo uno de los usos.
    placeholder = "sin-credencial-modo-cache-only"

    if "adzuna" in source_names:
        if cache_only:
            app_id = credentials.adzuna_app_id or placeholder
            app_key = credentials.adzuna_app_key or placeholder
        else:
            app_id, app_key = credentials.require_adzuna()
        connectors["adzuna"] = AdzunaConnector(
            app_id=app_id,
            app_key=app_key,
            cache=cache,
            results_per_page=config.adzuna.results_per_page,
            max_pages=config.adzuna.max_pages,
            max_days_old=max_days_old,
            timeout_seconds=config.adzuna.timeout_seconds,
            sleep_seconds=config.adzuna.sleep_seconds_between_calls,
        )

    if "jooble" in source_names:
        # `or` aquí devuelve el primer valor "verdadero": si la variable de
        # entorno está vacía y estamos en cache-only, cae al placeholder.
        jooble_provider = (
            (lambda env_name: os.getenv(env_name) or placeholder)
            if cache_only
            else credentials.require_jooble
        )
        connectors["jooble"] = JoobleConnector(
            credentials_provider=jooble_provider,
            cache=cache,
            quota=jooble_quota,
            fingerprint_provider=Credentials.fingerprint,
            max_pages=config.jooble.max_pages,
            timeout_seconds=config.jooble.timeout_seconds,
            sleep_seconds=config.jooble.sleep_seconds_between_calls,
        )

    return connectors


# ---------------------------------------------------------------------------
#  Comprobación de cuotas antes de gastar
# ---------------------------------------------------------------------------

def _check_quotas(
    config: AppConfig,
    credentials: Credentials,
    source_names: list[str],
    market_keys: list[str],
    profile: SearchProfile,
    limit_combos: int | None,
    jooble_quota: JoobleQuota,
    adzuna_usage: WindowedUsage,
    cache_only: bool,
    force: bool,
) -> list[str]:
    """Avisa (o se niega) antes de gastar cuota. Devuelve líneas para el reporte.

    Recibe el perfil entero y no un número de combinaciones porque desde el 17
    septiembre 2026 cada mercado puede preguntar una cantidad distinta, y quien
    sabe cuántas es `SearchProfile.planned_queries`.
    """
    lines: list[str] = []

    if cache_only:
        lines.append("Modo --cache-only: no se gasta ni una petición.")
        return lines

    if "adzuna" in source_names:
        planned = profile.planned_queries(market_keys, limit_combos) * config.adzuna.max_pages
        warnings = adzuna_usage.check_before_run(
            planned=planned,
            daily_budget=config.adzuna.daily_call_budget,
            weekly_budget=config.adzuna.weekly_call_budget,
            monthly_budget=config.adzuna.monthly_call_budget,
            warn_daily_above=config.adzuna.warn_daily_above,
            force=force,
        )
        summary = adzuna_usage.summary()
        lines.append(
            f"Adzuna: {planned} peticiones previstas. "
            f"Consumo previo -> hoy {summary['hoy']}/"
            f"{config.adzuna.daily_call_budget or '?'}, "
            f"7 días {summary['esta_semana']}/"
            f"{config.adzuna.weekly_call_budget or '?'}, "
            f"mes {summary['este_mes']}/"
            f"{config.adzuna.monthly_call_budget or '?'}, "
            f"histórico {summary['historico']}."
        )
        lines.append(
            "  La cuota de Adzuna se regenera por ventana de tiempo: pasarse da un "
            "HTTP 429 y se recupera solo."
        )
        for warning in warnings:
            lines.append(f"  AVISO: {warning}")

    if "jooble" in source_names:
        for market_key in market_keys:
            market = config.market(market_key)
            # LA CUOTA DE JOOBLE ES POR KEY Y CADA KEY ATIENDE A SU MERCADO, así
            # que aquí el número de combinaciones es el DE ESE MERCADO y no el
            # del perfil. Con las 10 en español solo en México, la key mexicana
            # gastaría 21 por corrida y la de EE.UU. 13.
            planned_per_key = (
                profile.planned_queries([market_key], limit_combos) * config.jooble.max_pages
            )
            env_name = market.jooble.api_key_env
            try:
                api_key = credentials.require_jooble(env_name)
            except MissingCredentialsError as exc:
                lines.append(f"Jooble/{market.label}: {exc}")
                continue

            fingerprint = Credentials.fingerprint(api_key)
            status, warnings = jooble_quota.check_before_run(
                env_name=env_name,
                fingerprint=fingerprint,
                planned=planned_per_key,
                refuse_below=config.jooble.refuse_when_remaining_below,
                force=force,
            )
            lines.append(
                f"Jooble/{market.label} ({env_name}): {status.used}/{status.budget} "
                f"consumidas DE POR VIDA. Esta corrida gasta {planned_per_key}; "
                f"quedarían {status.remaining_after_run} "
                f"(~{status.runs_left_after:.0f} corridas más)."
            )
            if status.remaining_after_run < config.jooble.warn_when_remaining_below:
                lines.append(
                    f"  AVISO: por debajo del umbral de aviso "
                    f"({config.jooble.warn_when_remaining_below}). No se regeneran."
                )
            for warning in warnings:
                lines.append(f"  AVISO: {warning}")

    return lines


# ---------------------------------------------------------------------------
#  Subcomandos
# ---------------------------------------------------------------------------

def command_check(args: argparse.Namespace) -> int:
    """Valida la configuración y las credenciales sin gastar una sola petición."""
    config = load_config(Path(args.config) if args.config else None)
    credentials = Credentials.load()

    print(f"Configuración: {config.config_path}")
    print(f"  Perfil activo : {config.active_profile_key}")
    for key, profile in config.profiles.items():
        marker = "*" if key == config.active_profile_key else " "
        # CUÁNTAS PREGUNTA CADA MERCADO, no cuántas hay en la lista general.
        # Decir «13 combinaciones» con México dentro se queda corto en 8 y es
        # justo el sitio donde alguien viene a comprobar el número.
        mercados = ", ".join(
            f"{m} ({profile.planned_queries([m])})" for m in profile.market_keys
        )
        print(
            f"  {marker} {key}: {profile.planned_queries()} consultas por corrida, "
            f"mercados = {mercados}"
        )
    print(f"  Mercados      : {', '.join(sorted(config.markets))}")
    print(f"  Base de datos : {config.database_path}")
    print(f"  Caché         : {config.cache.directory} (TTL {config.cache.ttl_hours}h)")

    profile = config.profile(args.profile)
    floor = profile.hard_filters.salary_floor
    if floor:
        print(
            f"  Umbral salarial: {floor.amount:,.0f} {floor.currency}/año "
            f"en modo '{floor.mode}' (tipo de cambio de {floor.fx_updated})"
        )

    print("\nCredenciales:")
    problems = 0
    try:
        credentials.require_adzuna()
        print("  [ok]    Adzuna")
    except MissingCredentialsError as exc:
        problems += 1
        print(f"  [FALTA] Adzuna -> {str(exc).splitlines()[0]}")

    for market_key in sorted(config.markets):
        env_name = config.markets[market_key].jooble.api_key_env
        try:
            credentials.require_jooble(env_name)
            print(f"  [ok]    Jooble/{market_key} ({env_name})")
        except MissingCredentialsError:
            problems += 1
            print(f"  [FALTA] Jooble/{market_key} -> variable {env_name} vacía o ausente")

    if problems:
        print(f"\n{problems} credencial(es) sin configurar. Copia .env.example a .env.")
        print("Puedes correr igualmente con --sources de las fuentes que sí tengas.")
        return 1

    print("\nTodo en orden.")
    return 0


def command_descripciones(args: argparse.Namespace) -> int:
    """Trae la descripción entera de las ofertas que no la tienen.

    NO GASTA TOKENS. Son peticiones a la web de Adzuna, no a su API ni al LLM:
    ni la cuota de 2.500/mes ni el techo de gasto se enteran. Lo que sí hace es
    encarecer la SIGUIENTE clasificación —+1.710 tokens de entrada medidos por
    oferta, ~+$3/mes—, y por eso el comando lo dice en pantalla en vez de
    dejarlo escrito solo en el YAML.
    """
    config = load_config(Path(args.config) if args.config else None)
    ajustes = config.descripciones

    if not ajustes.habilitado and not args.force:
        print("La descripción entera está APAGADA en config/search_profile.yaml.")
        print()
        print("  descripciones:")
        print("    habilitado: false")
        print()
        print("Encenderla hace una petición HTTP por vacante a la WEB de Adzuna")
        print("(no a su API: la cuota de 2.500/mes no se toca) y sube el coste de")
        print("cada clasificación posterior en ~+$3/mes. Ver el bloque del YAML.")
        print()
        print("Para probarla sin encenderla: --force")
        return 0

    modo = MODE_CACHE_ONLY if args.cache_only else MODE_NORMAL
    cache = DiskCache(
        directory=config.cache.directory,
        ttl_hours=args.ttl if args.ttl is not None else config.cache.ttl_hours,
        enabled=config.cache.enabled,
        mode=modo,
    )
    traedor = TraedorDeDescripciones(
        cache=cache,
        timeout_seconds=ajustes.timeout_seconds,
        sleep_seconds=0.0 if args.cache_only else ajustes.sleep_seconds_between_calls,
    )

    limite = args.limite if args.limite is not None else ajustes.max_por_corrida

    with Storage(config.database_path) as almacen:
        pendientes = almacen.ofertas_sin_descripcion_completa(
            limite=limite, solo_sin_intentar=not args.reintentar
        )
        antes = almacen.resumen_descripciones()

        print("DESCRIPCIONES ENTERAS")
        print("=" * 78)
        print(f"Ofertas en la base ....... {antes['total']}")
        print(f"  Ya intentadas .......... {antes['intentadas']}")
        print(f"Vacantes en esta corrida . {len(pendientes)}"
              f"  (tope: {limite})")
        if args.cache_only:
            print("Modo ..................... --cache-only: no se toca la red")
        print()

        if not pendientes:
            print("No queda ninguna vacante por intentar. Nada que hacer.")
            return 0

        if args.dry_run:
            print("--dry-run: no se ha pedido nada. Se pedirían estas:")
            for fila in pendientes[:10]:
                print(f"  {fila['title'][:56]:56} {fila['company'][:24]}")
            if len(pendientes) > 10:
                print(f"  ... y {len(pendientes) - 10} más")
            return 0

        heredadas = 0
        fallos = 0
        for i, fila in enumerate(pendientes, 1):
            etiqueta = fila["title"][:48]
            try:
                d = traedor.traer(fila["canonical_url"])
            except CacheMissError:
                print(f"  [{i}/{len(pendientes)}] {etiqueta:48} sin copia en caché")
                continue
            except DescripcionError as exc:
                fallos += 1
                print(f"  [{i}/{len(pendientes)}] {etiqueta:48} ERROR", file=sys.stderr)
                print(f"      {exc}", file=sys.stderr)
                continue

            # Se apunta HAYA SALIDO O NO: «se intentó y no la tiene» no puede
            # ser indistinguible de «no se ha intentado», o se reintentaría
            # para siempre. Misma regla que el libro de gastos.
            almacen.guardar_descripcion_completa(
                fila["dedupe_key"], d.texto if d.ok else "", d.via
            )
            n = almacen.heredar_descripcion_completa(fila["dedupe_key"])
            heredadas += n
            sufijo = f"  (+{n} hermanos)" if n else ""
            print(f"  [{i}/{len(pendientes)}] {etiqueta:48} "
                  f"{d.via:8} {len(d.texto):>6} car.{sufijo}")

        despues = almacen.resumen_descripciones()

    print()
    print("RESULTADO")
    print("=" * 78)
    print(f"Peticiones hechas ........ {traedor.peticiones}")
    print(f"Heredadas gratis ......... {heredadas}  (mismos anuncios, otra ciudad)")
    if fallos:
        print(f"Fallos ................... {fallos}")
    print()
    print("Por vía, sobre toda la base:")
    print(f"  json-ld (formato estable)  {despues['por_json_ld']}")
    print(f"  html    (clase de CSS)     {despues['por_html']}")
    print(f"  sin suerte                 {despues['sin_suerte']}")
    print()
    print("Vigila el reparto: `json-ld` es un formato que Adzuna publica para")
    print("Google Jobs; `html` es una clase de CSS que cambia con el rediseño.")
    print("Si `html` sube, nos estamos apoyando en lo frágil.")
    print()
    print("Las nuevas descripciones se usan en la SIGUIENTE clasificación.")
    print("Mira antes lo que va a costar:  python -m clasificador --dry-run")
    return 2 if fallos else 0


def command_quota(args: argparse.Namespace) -> int:
    """Estado de las cuotas, sin gastar nada."""
    config = load_config(Path(args.config) if args.config else None)
    data_dir = config.database_path.parent

    jooble_quota = JoobleQuota(data_dir / JOOBLE_QUOTA_FILE, config.jooble.lifetime_request_budget)
    adzuna_usage = WindowedUsage(data_dir / ADZUNA_USAGE_FILE, "adzuna")

    print("ADZUNA  (cuota por ventana de tiempo: se regenera)")
    summary = adzuna_usage.summary()

    # Las TRES ventanas, no dos. La semana no se deduce de las otras y con una
    # corrida diaria es la segunda que se llena. Se enseña cada una contra su
    # tope para que se vea de un vistazo cuál es la que aprieta.
    for etiqueta, usadas, tope in (
        ("Hoy        ", summary["hoy"], config.adzuna.daily_call_budget),
        ("Últimos 7d ", summary["esta_semana"], config.adzuna.weekly_call_budget),
        ("Este mes   ", summary["este_mes"], config.adzuna.monthly_call_budget),
    ):
        if tope:
            porcentaje = usadas / tope * 100
            print(f"  {etiqueta}: {usadas:>5} / {tope:<5} ({porcentaje:.0f}%)")
        else:
            print(f"  {etiqueta}: {usadas:>5} / sin declarar")
    print(f"  Histórico  : {summary['historico']:>5}")

    # El de ritmo no se cuenta, se respeta yendo despacio. Ver config.py.
    if config.adzuna.max_calls_per_minute:
        por_minuto = 60.0 / config.adzuna.sleep_seconds_between_calls
        print(f"  Ritmo      : {por_minuto:.0f}/min como mucho, y el plan admite "
              f"{config.adzuna.max_calls_per_minute}/min")
    print(f"  Archivo    : {summary['archivo']}")

    print("\nJOOBLE  (cuota DE POR VIDA: no se regenera)")
    perfil_quota = config.profile(args.profile)
    for market_key in sorted(config.markets):
        env_name = config.markets[market_key].jooble.api_key_env
        used = jooble_quota.used(env_name)
        remaining = jooble_quota.remaining(env_name)
        # Las de ESE mercado: desde el 17 septiembre 2026 no todos preguntan lo
        # mismo, y aquí se están contando corridas de una key concreta.
        per_run = perfil_quota.planned_queries([market_key]) * config.jooble.max_pages
        runs_left = remaining / per_run if per_run else 0
        print(
            f"  {market_key:<10} ({env_name}): {used}/{config.jooble.lifetime_request_budget} "
            f"usadas, quedan {remaining} -> ~{runs_left:.0f} corridas de {per_run} peticiones"
        )
    print(f"  Archivo   : {jooble_quota.path}")

    print(
        "\nAviso: estos contadores solo ven las peticiones hechas por este programa.\n"
        "Si gastaste cuota desde curl o Postman, no está contabilizada."
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    credentials = Credentials.load()
    profile = config.profile(args.profile)

    market_keys = _split_list(args.markets) or profile.market_keys
    for market_key in market_keys:
        config.market(market_key)  # valida que exista; lanza ConfigError si no

    available_sources = [
        name
        for name, enabled in (("adzuna", config.adzuna.enabled), ("jooble", config.jooble.enabled))
        if enabled
    ]
    source_names = _split_list(args.sources) or available_sources
    unknown = [s for s in source_names if s not in ("adzuna", "jooble")]
    if unknown:
        raise ConfigError(f"Fuente desconocida: {', '.join(unknown)}. Admitidas: adzuna, jooble.")
    if not source_names:
        raise ConfigError("No hay ninguna fuente habilitada. Revisa `sources` en el YAML.")

    cache_mode = MODE_NORMAL
    if args.cache_only:
        cache_mode = MODE_CACHE_ONLY
    elif args.no_cache:
        cache_mode = MODE_BYPASS

    cache = DiskCache(
        directory=config.cache.directory,
        ttl_hours=config.cache.ttl_hours,
        enabled=config.cache.enabled,
        mode=cache_mode,
    )

    data_dir = config.database_path.parent
    jooble_quota = JoobleQuota(data_dir / JOOBLE_QUOTA_FILE, config.jooble.lifetime_request_budget)
    adzuna_usage = WindowedUsage(data_dir / ADZUNA_USAGE_FILE, "adzuna")

    consultas = profile.planned_queries(market_keys, args.limit_combos)
    # EL DESGLOSE SE ENSEÑA SIEMPRE QUE LOS MERCADOS NO PREGUNTEN LO MISMO.
    # «23 combinaciones × 3 mercados» dejó de ser verdad el 17 septiembre 2026,
    # y una multiplicación que no cuadra con el total es peor que no ponerla.
    por_mercado = [
        (config.market(k).label, profile.planned_queries([k], args.limit_combos))
        for k in market_keys
    ]
    iguales = len({n for _, n in por_mercado}) == 1

    print(f"Perfil    : {profile.label}")
    print(f"Mercados  : {', '.join(etiqueta for etiqueta, _ in por_mercado)}")
    print(f"Fuentes   : {', '.join(source_names)}")
    if iguales:
        print(f"Consultas : {por_mercado[0][1]} combinaciones x {len(market_keys)} mercados "
              f"x {len(source_names)} fuentes = {consultas * len(source_names)}")
    else:
        desglose = " + ".join(f"{n} en {etiqueta}" for etiqueta, n in por_mercado)
        print(f"Consultas : {desglose} = {consultas} "
              f"x {len(source_names)} fuente(s) = {consultas * len(source_names)}")
    if cache_mode != MODE_NORMAL:
        print(f"Caché     : modo {cache_mode}")
    print()

    quota_lines = _check_quotas(
        config=config,
        credentials=credentials,
        source_names=source_names,
        market_keys=market_keys,
        profile=profile,
        limit_combos=args.limit_combos,
        jooble_quota=jooble_quota,
        adzuna_usage=adzuna_usage,
        cache_only=args.cache_only,
        force=args.force,
    )
    for line in quota_lines:
        print(line)
    print()

    if args.dry_run:
        print("--dry-run: no se ejecuta nada más.")
        return 0

    connectors = _build_connectors(
        config=config,
        credentials=credentials,
        cache=cache,
        source_names=source_names,
        max_days_old=profile.hard_filters.max_days_old,
        jooble_quota=jooble_quota,
        cache_only=args.cache_only,
    )

    print("Consultando...")
    with Storage(config.database_path) as storage:
        # La red por si el servidor no pudo aplicar el recorte geográfico al
        # abrir: así lo que ya no es del mercado sale ANTES de que el triaje
        # lo mire y lo cobre. Casi siempre no hace nada. Ver `recorte.py`.
        try:
            from .recorte import recortar_si_hace_falta

            for linea in recortar_si_hace_falta(
                config, storage, config.database_path.parent
            ):
                print(f"Recorte geográfico: {linea}")
        except Exception as exc:  # noqa: BLE001 - no puede impedir la búsqueda
            print(f"AVISO: no se pudo aplicar el recorte geográfico: {exc}")

        pipeline = Pipeline(config=config, storage=storage, connectors=connectors)
        stats = pipeline.run(
            profile=profile,
            market_keys=market_keys,
            source_names=source_names,
            limit_combos=args.limit_combos,
        )

        # Las peticiones de Adzuna se anotan al terminar: su cuota se regenera,
        # así que no hace falta la escritura inmediata que sí exige Jooble.
        adzuna_calls = stats.requests_by_source.get("adzuna", 0)
        if adzuna_calls:
            adzuna_usage.record(adzuna_calls)

        storage.finish_run(
            stats.run_id,
            {
                "crudas": stats.raw_total(),
                "conservadas": stats.kept_total(),
                "unicas": stats.unique_total(),
                "nuevas": stats.new_total(),
                "errores": len(stats.errors),
                "peticiones": dict(stats.requests_by_source),
            },
        )

        report = build_report(
            stats=stats,
            config=config,
            profile=profile,
            storage=storage,
            quota_lines=quota_lines,
            sample_size=args.sample,
        )

    print(report)

    # El reporte también se guarda: es el entregable, y quererlo comparar entre
    # días es justo el uso previsto.
    reports_dir = data_dir / REPORTS_DIRNAME
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"reporte_{stats.run_id}.txt"
    report_path.write_text(report, encoding="utf-8")
    print(f"\nReporte guardado en: {report_path}")

    # Código de salida distinto de cero si algo se rompió: así se nota en un
    # script o en una tarea programada, no solo mirando la consola.
    if stats.errors or stats.silent_sources:
        print("\nLa corrida terminó CON ERRORES. Los recuentos están incompletos.")
        return 2
    return 0


# ---------------------------------------------------------------------------
#  argparse
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recolector",
        description="Sonda de viabilidad: mide cuántas ofertas relevantes devuelven "
                    "Adzuna y Jooble en México y en Texas.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ejemplos:\n"
            "  python -m recolector check\n"
            "  python -m recolector run --sources adzuna\n"
            "  python -m recolector run --limit-combos 2 --markets mexico\n"
            "  python -m recolector run --cache-only\n"
            "  python -m recolector quota\n"
        ),
    )
    parser.add_argument("--config", help="Ruta a otro archivo de configuración.")
    parser.add_argument("--verbose", action="store_true", help="Log detallado.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Ejecuta las búsquedas y genera el reporte.")
    run_parser.add_argument("--profile", help="Perfil a usar (por defecto, el activo).")
    run_parser.add_argument("--markets", help="Mercados separados por coma: mexico,texas_us")
    run_parser.add_argument("--sources", help="Fuentes separadas por coma: adzuna,jooble")
    run_parser.add_argument(
        "--limit-combos",
        type=int,
        help="Usa solo las N primeras combinaciones. Útil para probar sin gastar cuota.",
    )
    run_parser.add_argument(
        "--cache-only",
        action="store_true",
        help="No toca la red: trabaja solo sobre respuestas ya cacheadas.",
    )
    run_parser.add_argument(
        "--no-cache", action="store_true", help="Ignora la caché y pide todo de nuevo."
    )
    run_parser.add_argument(
        "--force", action="store_true", help="Ignora los umbrales de cuota. Con cuidado."
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Enseña el plan y el estado de cuotas, y sale sin consultar nada.",
    )
    run_parser.add_argument(
        "--sample", type=int, default=10, help="Tamaño de la muestra del reporte (por defecto 10)."
    )
    run_parser.set_defaults(func=command_run)

    quota_parser = subparsers.add_parser("quota", help="Estado de las cuotas de las APIs.")
    quota_parser.add_argument("--profile", help="Perfil sobre el que calcular el coste por corrida.")
    quota_parser.set_defaults(func=command_quota)

    check_parser = subparsers.add_parser("check", help="Valida configuración y credenciales.")
    check_parser.add_argument("--profile", help="Perfil a validar.")
    check_parser.set_defaults(func=command_check)

    desc_parser = subparsers.add_parser(
        "descripciones",
        help="Trae la descripción entera del anuncio. No gasta tokens ni cuota de API.",
    )
    desc_parser.add_argument(
        "--limite", type=int, help="Cuántas vacantes como mucho (por defecto, la del YAML)."
    )
    desc_parser.add_argument(
        "--dry-run", action="store_true", help="Enseña cuáles se pedirían, y sale."
    )
    desc_parser.add_argument(
        "--cache-only", action="store_true",
        help="No toca la red: solo lo que ya esté en caché.",
    )
    desc_parser.add_argument(
        "--reintentar", action="store_true",
        help="Vuelve a probar las que ya se intentaron y no salieron.",
    )
    desc_parser.add_argument(
        "--force", action="store_true",
        help="Corre aunque esté apagado en el YAML. Para probarlo.",
    )
    desc_parser.add_argument("--ttl", type=float, help="TTL de caché en horas.")
    desc_parser.set_defaults(func=command_descripciones)

    return parser


def main(argv: list[str] | None = None) -> int:
    _prepare_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    try:
        return args.func(args)
    except (ConfigError, MissingCredentialsError) as exc:
        # Errores de configuración: culpa nuestra, no de la API. Se imprimen
        # limpios, sin traza, porque la traza no aporta nada aquí.
        print(f"\nERROR DE CONFIGURACIÓN\n{exc}", file=sys.stderr)
        return 1
    except (QuotaExhaustedError, ApiBudgetExceededError) as exc:
        print(f"\nCUOTA INSUFICIENTE\n{exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
