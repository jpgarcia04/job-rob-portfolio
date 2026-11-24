"""
El reporte. Este es el entregable real de la v0.

Todo lo demás existe para poder imprimir esto con honestidad. El reporte está
ordenado por lo que puede invalidar la conclusión: primero los fallos, luego
el volumen, luego lo que se descartó y por qué, y solo al final la lectura.

Si algo se rompió, se ve antes que cualquier número. Un reporte bonito sobre
datos rotos es peor que no tener reporte.
"""

from __future__ import annotations

from datetime import datetime

from .config import AppConfig, SearchProfile
from .pipeline import RunStats
from .salary import BELOW, MEETS, UNKNOWN
from .storage import Storage

WIDTH = 88


# ---------------------------------------------------------------------------
#  Ayudas de formato
# ---------------------------------------------------------------------------

def _rule(char: str = "-") -> str:
    return char * WIDTH


def _heading(text: str) -> str:
    return f"\n{_rule('=')}\n  {text.upper()}\n{_rule('=')}"


def _section(text: str) -> str:
    return f"\n{text}\n{_rule('-')}"


def _truncate(text: str, limit: int) -> str:
    """Corta un texto largo dejando puntos suspensivos.

    Sin esto, un título de 140 caracteres rompe la alineación de la tabla y el
    reporte deja de poder leerse de un vistazo.
    """
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _bar(value: int, maximum: int, width: int = 18) -> str:
    """Barra de texto proporcional. Ayuda a ver la distribución sin leer cifras."""
    if maximum <= 0:
        return ""
    filled = round((value / maximum) * width)
    return "#" * filled


def _pct(part: int, whole: int) -> str:
    if whole <= 0:
        return "  n/a"
    return f"{(part / whole) * 100:5.1f}%"


# ---------------------------------------------------------------------------
#  Secciones
# ---------------------------------------------------------------------------

def _header(stats: RunStats, config: AppConfig, profile: SearchProfile) -> list[str]:
    duration = ""
    if stats.finished_at:
        seconds = (stats.finished_at - stats.started_at).total_seconds()
        duration = f"  ({seconds:.0f}s)"

    markets = ", ".join(config.market(k).label for k in stats.market_keys)

    requests = ", ".join(
        f"{source}={count}" for source, count in sorted(stats.requests_by_source.items())
    ) or "ninguna (todo de caché o sin respuesta)"

    return [
        _heading("reporte de viabilidad — v0 recolector"),
        f"  Corrida        : {stats.run_id}{duration}",
        f"  Fecha          : {stats.started_at.astimezone().strftime('%Y-%m-%d %H:%M')}",
        f"  Perfil         : {profile.label}",
        f"  Combinaciones  : {len(stats.combos)}",
        f"  Mercados       : {markets}",
        f"  Fuentes        : {', '.join(stats.source_names)}",
        f"  Peticiones     : {requests}"
        + (f"   (respuestas servidas de caché: {stats.cache_hits})" if stats.cache_hits else ""),
    ]


def _failures(stats: RunStats) -> list[str]:
    """Fallos y avisos. Va primero a propósito: si esto no está limpio, los
    números de abajo no valen nada.
    """
    lines: list[str] = []

    if stats.silent_sources:
        lines.append(_heading("!!! fuentes sospechosas de estar caídas"))
        for source, market_key in stats.silent_sources:
            lines.append(
                f"  {source} / {market_key}: devolvió CERO resultados en TODAS las "
                f"consultas, sin dar un solo error."
            )
        lines.append("")
        lines.append("  Cero en una consulta suelta es normal. Cero en todas, no.")
        lines.append("  Causas probables, por orden:")
        lines.append("    1. Cuota agotada (Jooble responde 200 con lista vacía al acabarse).")
        lines.append("    2. API key inválida o de otro país (Jooble emite una key por dominio).")
        lines.append("    3. El parámetro de ubicación no coincide con nada en ese índice.")
        lines.append("  NO leas esto como «no hay ofertas en ese mercado».")

    if stats.errors:
        lines.append(_heading(f"errores ({len(stats.errors)})"))
        # Se muestran todos: son pocos por definición, y esconder uno es
        # justo lo que este proyecto no quiere hacer.
        for error in stats.errors:
            for index, line in enumerate(str(error).splitlines()):
                lines.append(("  * " if index == 0 else "    ") + line)

    if stats.warnings:
        lines.append(_section(f"Avisos ({len(stats.warnings)})"))
        shown = stats.warnings[:15]
        for warning in shown:
            lines.append(f"  - {_truncate(warning, WIDTH - 4)}")
        if len(stats.warnings) > len(shown):
            lines.append(f"  ... y {len(stats.warnings) - len(shown)} avisos más.")

    if not lines:
        lines.append(_section("Fallos y avisos"))
        lines.append("  Ninguno. Todas las consultas respondieron correctamente.")

    return lines


def _by_market(stats: RunStats, config: AppConfig) -> list[str]:
    lines = [_heading("volumen por mercado")]
    lines.append(
        f"  {'Mercado':<18} {'Crudas':>7} {'Tras filtros':>13} {'Únicas':>8} "
        f"{'Nuevas':>8} {'Consultas OK':>13}"
    )
    lines.append("  " + _rule()[:WIDTH - 2])

    for market_key in stats.market_keys:
        queries = stats.by_market(market_key)
        raw = sum(q.raw for q in queries)
        kept = sum(q.kept for q in queries)
        new = sum(q.new_in_db for q in queries)
        unique = len(stats.keys_by_market.get(market_key, set()))
        ok = sum(1 for q in queries if q.error is None)
        label = config.market(market_key).label
        lines.append(
            f"  {_truncate(label, 18):<18} {raw:>7} {kept:>13} {unique:>8} "
            f"{new:>8} {f'{ok}/{len(queries)}':>13}"
        )

    lines.append("")
    lines.append(
        "  «Únicas» son ofertas distintas tras deduplicar; «Nuevas», las que no"
    )
    lines.append("  se habían visto en ninguna corrida anterior.")
    return lines


def _by_combination(stats: RunStats, config: AppConfig) -> list[str]:
    lines = [_heading("volumen por combinación de palabras clave")]

    for market_key in stats.market_keys:
        market_queries = stats.by_market(market_key)
        if not market_queries:
            continue

        lines.append(_section(f"  {config.market(market_key).label}"))
        lines.append(
            f"  {'Combinación':<34} {'Crudas':>7} {'Tras f.':>8} {'API dice':>9}  {'':<18}"
        )

        # Se agrupa por combinación sumando las fuentes.
        totals: dict[str, dict[str, int]] = {}
        for query in market_queries:
            entry = totals.setdefault(
                query.combo_label, {"raw": 0, "kept": 0, "reported": 0, "errors": 0}
            )
            entry["raw"] += query.raw
            entry["kept"] += query.kept
            if query.reported_total:
                entry["reported"] += query.reported_total
            if query.error:
                entry["errors"] += 1

        max_kept = max((v["kept"] for v in totals.values()), default=0)

        # Orden por cosecha descendente: lo que funciona, arriba.
        for label, entry in sorted(totals.items(), key=lambda kv: -kv[1]["kept"]):
            flag = "  <- ERROR" if entry["errors"] else ""
            reported = entry["reported"] if entry["reported"] else "-"
            lines.append(
                f"  {_truncate(label, 34):<34} {entry['raw']:>7} {entry['kept']:>8} "
                f"{str(reported):>9}  {_bar(entry['kept'], max_kept):<18}{flag}"
            )

        dry = [label for label, entry in totals.items() if entry["kept"] == 0]
        if dry:
            lines.append("")
            lines.append(f"  Combinaciones con CERO resultados útiles ({len(dry)} de {len(totals)}):")
            for label in dry:
                lines.append(f"    - {label}")

    lines.append("")
    lines.append("  «API dice» es el total que la API afirma tener para esa búsqueda, no lo")
    lines.append("  que devolvió. Si es mucho mayor que «Crudas», hay volumen sin paginar.")
    return lines


def _by_source(stats: RunStats, storage: Storage) -> list[str]:
    lines = [_heading("volumen por fuente y aporte exclusivo")]
    lines.append(
        f"  {'Fuente':<10} {'Crudas':>7} {'Tras f.':>8} {'Únicas':>8} "
        f"{'Exclusivas':>11} {'Peticiones':>11}"
    )
    lines.append("  " + _rule()[:WIDTH - 2])

    for source in stats.source_names:
        queries = stats.by_source(source)
        raw = sum(q.raw for q in queries)
        kept = sum(q.kept for q in queries)
        unique = len(stats.keys_by_source.get(source, set()))
        exclusive = len(stats.exclusive_keys(source))
        requests = stats.requests_by_source.get(source, 0)
        lines.append(
            f"  {source:<10} {raw:>7} {kept:>8} {unique:>8} {exclusive:>11} {requests:>11}"
        )

    shared = len(stats.shared_keys())
    lines.append("")
    lines.append(f"  Ofertas alcanzadas por MÁS DE UNA fuente: {shared}")

    if len(stats.source_names) > 1:
        total_unique = stats.unique_total()
        lines.append("")
        lines.append("  Aporte exclusivo sobre el total de ofertas únicas:")
        for source in stats.source_names:
            exclusive = len(stats.exclusive_keys(source))
            lines.append(
                f"    {source:<10} {exclusive:>4} de {total_unique:<4} "
                f"({_pct(exclusive, total_unique)})   {_bar(exclusive, total_unique)}"
            )
        lines.append("")
        lines.append("  Esta tabla es la que responde «¿merece la pena Jooble?».")
        lines.append("  Si su columna de exclusivas es baja, no justifica ni la complejidad")
        lines.append("  ni el coste de su plan de pago.")
    else:
        lines.append("")
        lines.append(
            "  Corrida de una sola fuente: no se puede medir solapamiento ni aporte"
        )
        lines.append("  exclusivo. Para eso hace falta correr las dos a la vez.")

    return lines


def _dedupe(stats: RunStats) -> list[str]:
    lines = [_heading("deduplicación")]

    kept = stats.kept_total()
    unique = stats.unique_total()
    collapsed = stats.duplicates_collapsed()
    gap = stats.under_merge_gap()

    lines.append(f"  Ofertas guardadas (apariciones) : {kept}")
    lines.append(f"  Ofertas únicas (clave estricta) : {unique}")
    lines.append(f"  Duplicados fusionados           : {collapsed}  ({_pct(collapsed, kept)})")
    lines.append("")
    lines.append(f"  Claves laxas distintas          : {len(stats.all_loose_keys)}")
    lines.append(f"  Diferencia estricta - laxa      : {gap}")
    lines.append("")

    if gap == 0:
        lines.append("  La clave estricta y la laxa coinciden: no se detectan duplicados")
        lines.append("  ocultos por diferencias de ubicación.")
    else:
        lines.append(
            f"  Hay {gap} pares con la MISMA empresa y el MISMO título pero distinta"
        )
        lines.append("  ubicación. Pueden ser dos cosas distintas:")
        lines.append("    (a) la misma oferta escrita de dos formas -> duplicado no detectado")
        lines.append("    (b) el mismo puesto en dos ciudades      -> dos oportunidades reales")
        lines.append("")
        lines.append(
            f"  En el peor caso, las {unique} «únicas» serían realmente "
            f"{unique - gap}."
        )
        lines.append("  Lee la cifra de volumen como un techo, no como un dato exacto.")

    return lines


def _filters(stats: RunStats, config: AppConfig) -> list[str]:
    lines = [_heading("qué se descartó y por qué")]

    raw = stats.raw_total()
    kept = stats.kept_total()
    dropped = raw - kept

    lines.append(f"  Recibidas de las APIs : {raw}")
    lines.append(f"  Descartadas por filtros: {dropped}  ({_pct(dropped, raw)})")
    lines.append(f"  Conservadas            : {kept}  ({_pct(kept, raw)})")

    if stats.drop_reasons:
        lines.append("")
        lines.append(f"  {'Motivo':<34} {'Nº':>5}  {'':<18}")
        maximum = max(stats.drop_reasons.values())
        for reason, count in stats.drop_reasons.most_common():
            lines.append(
                f"  {_truncate(reason, 34):<34} {count:>5}  {_bar(count, maximum):<18}"
            )
            for example in stats.drop_examples.get(reason, [])[:2]:
                lines.append(f"      p.ej. {_truncate(example, WIDTH - 14)}")

    if stats.geo_unresolved:
        lines.append("")
        lines.append("  Ubicaciones que no permitieron decidir el estado:")
        # QUÉ SE HIZO CON ELLAS, MERCADO A MERCADO. Antes esta sección
        # aconsejaba siempre «cambia `unresolved_policy` a keep», y desde el 17
        # septiembre 2026 México ya lo tiene puesto: el consejo se leía como si
        # se estuvieran tirando cuando en realidad estaban dentro. Un reporte
        # que describe mal lo que hizo es peor que uno que calla.
        politicas = set()
        for market_key, count in stats.geo_unresolved.most_common():
            examples = stats.geo_unresolved_examples.get(market_key, [])
            sample = f"   p.ej. {', '.join(examples)}" if examples else ""
            try:
                politica = config.market(market_key).geo_filter.unresolved_policy
            except Exception:
                politica = "drop"
            politicas.add(politica)
            destino = "SE CONSERVAN" if politica == "keep" else "descartadas"
            lines.append(f"    {market_key:<14} {count:>4}  {destino}{sample}")
        lines.append("")
        lines.append("  Suelen ser remotos nacionales, o anuncios que solo dicen el país.")
        if "drop" in politicas:
            lines.append("  Donde pone «descartadas»: si crees que a ella le valen, cambia")
            lines.append("  `unresolved_policy` a `keep` en la configuración de ese mercado.")
        if "keep" in politicas:
            lines.append("  Donde pone «SE CONSERVAN» sí entran en la bandeja, y se cuentan")
            lines.append("  aquí para que se vea de cuántas no se sabe el estado.")

    if dropped > kept and raw > 0:
        lines.append("")
        lines.append("  AVISO: los filtros están tirando más de lo que conservan. Antes de")
        lines.append("  concluir que no hay mercado, comprueba que el motivo principal de")
        lines.append("  descarte sea el que esperabas.")

    return lines


def _salary(stats: RunStats, profile: SearchProfile) -> list[str]:
    floor = profile.hard_filters.salary_floor
    lines = [_heading("umbral salarial")]

    if floor is None:
        lines.append("  No hay umbral configurado.")
        return lines

    mode_text = (
        "report  (clasifica y cuenta, NO descarta)"
        if floor.mode == "report"
        else "filter  (descarta de verdad)"
    )
    lines.append(f"  Umbral      : {floor.amount:,.0f} {floor.currency} / año")
    lines.append(f"  Modo        : {mode_text}")
    lines.append(
        f"  Referencia  : {'techo' if floor.use_range_top else 'suelo'} del rango salarial"
    )
    lines.append(f"  Tipo cambio : MXN->USD = {floor.fx_to_usd.get('MXN', 0):.4f}"
                 f"  (declarado el {floor.fx_updated})")
    lines.append("")

    total = sum(stats.salary_buckets.values())
    meets = stats.salary_buckets.get(MEETS, 0)
    below = stats.salary_buckets.get(BELOW, 0)
    unknown = stats.salary_buckets.get(UNKNOWN, 0)
    with_data = meets + below

    lines.append(f"  {'Cumple el umbral':<26} {meets:>5}  ({_pct(meets, total)})")
    lines.append(f"  {'No lo cumple':<26} {below:>5}  ({_pct(below, total)})")
    lines.append(f"  {'Sin dato salarial':<26} {unknown:>5}  ({_pct(unknown, total)})")
    lines.append("  " + _rule()[:46])
    lines.append(f"  {'Evaluadas':<26} {total:>5}")

    if stats.salary_predicted:
        lines.append("")
        lines.append(
            f"  OJO: {stats.salary_predicted} de las que traían cifra eran ESTIMACIONES "
            f"de Adzuna,"
        )
        lines.append("  no salarios declarados en el anuncio. No son un hecho.")

    lines.append("")
    if total == 0:
        lines.append("  Sin ofertas que evaluar.")
    elif with_data == 0:
        lines.append("  NINGUNA oferta traía salario estructurado.")
        lines.append("  Conclusión operativa: el umbral de 170k USD no es utilizable como")
        lines.append("  filtro automático con estas fuentes. Tendrá que revisarlo ella, o")
        lines.append("  habrá que sacarlo del texto de la descripción con el LLM en la v1.")
    else:
        lines.append(
            f"  Solo {with_data} de {total} ofertas ({_pct(with_data, total).strip()}) traían "
            f"salario utilizable."
        )
        if with_data / total < 0.25:
            lines.append("  Con esa cobertura, poner el umbral en `filter` descartaría el grueso")
            lines.append("  del mercado por FALTA DE DATO, no por falta de sueldo. No lo hagas")
            lines.append("  todavía.")

    return lines


def _sample(stats: RunStats, storage: Storage, config: AppConfig, limit: int = 10) -> list[str]:
    lines = [_heading(f"muestra de {limit} ofertas")]

    rows = storage.sample_offers(stats.run_id, limit)
    if not rows:
        lines.append("  No hay ofertas que mostrar en esta corrida.")
        return lines

    for index, row in enumerate(rows, start=1):
        sources = ", ".join(storage.sources_for_offer(row["dedupe_key"]))
        market_label = config.market(row["market_key"]).label
        posted = (row["posted_at"] or "")[:10] or "sin fecha"

        lines.append(f"  {index:>2}. {_truncate(row['title'], WIDTH - 6)}")
        lines.append(
            f"      {_truncate(row['company'] or '(sin empresa)', 34):<34} "
            f"{_truncate(row['location_raw'] or row['location_city'] or '-', 28):<28}"
        )
        salary = ""
        if row["salary_min"] or row["salary_max"]:
            low = row["salary_min"] or row["salary_max"]
            high = row["salary_max"] or row["salary_min"]
            predicted = " (estimado)" if row["salary_is_predicted"] else ""
            salary = f"  {low:,.0f}-{high:,.0f} {row['salary_currency'] or ''}{predicted}"
        elif row["salary_raw"]:
            salary = f"  {_truncate(row['salary_raw'], 30)}"
        lines.append(f"      {market_label} · {posted} · vía {sources}{salary}")
        lines.append("")

    return lines


def _quotas(quota_lines: list[str]) -> list[str]:
    if not quota_lines:
        return []
    return [_heading("cuotas")] + [f"  {line}" for line in quota_lines]


def _reading(stats: RunStats, config: AppConfig, profile: SearchProfile) -> list[str]:
    """Lectura automática. Deliberadamente conservadora y etiquetada como tal:
    es una ayuda para leer la tabla, no el veredicto. El veredicto lo firma
    una persona mirando los números de arriba.
    """
    lines = [_heading("lectura automática (orientativa)")]

    total_combos = len(profile.keyword_combinations)
    partial = len(stats.combos) < total_combos

    if stats.silent_sources or stats.errors:
        lines.append("  NO CONCLUYAS NADA DE ESTA CORRIDA.")
        lines.append("  Hubo fuentes caídas o errores: los recuentos de abajo están")
        lines.append("  incompletos por causas técnicas, no de mercado. Arregla eso y repite.")
        lines.append("")

    if partial:
        lines.append(
            f"  CORRIDA PARCIAL: se usaron {len(stats.combos)} de las {total_combos} "
            f"combinaciones."
        )
        lines.append("  Las cifras de volumen NO son comparables con una corrida completa.")
        lines.append("")

    for market_key in stats.market_keys:
        unique = len(stats.keys_by_market.get(market_key, set()))
        label = config.market(market_key).label
        queries = stats.by_market(market_key)
        dry = sum(1 for q in queries if q.raw == 0 and q.error is None)

        lines.append(f"  {label}:")
        lines.append(f"    {unique} ofertas únicas en esta corrida.")
        if dry:
            lines.append(
                f"    {dry} de {len(queries)} consultas volvieron vacías."
            )

        # Umbrales orientativos, pensados para un perfil que espera "decenas al
        # mes" en todo el país.
        if unique == 0:
            lines.append("    -> Sin volumen. Revisa idioma de las búsquedas y credenciales")
            lines.append("       ANTES de descartar la fuente.")
        elif unique < 10:
            lines.append("    -> Volumen muy bajo. Insuficiente por sí solo para sostener")
            lines.append("       el producto: haría falta otra fuente.")
        elif unique < 30:
            lines.append("    -> Volumen bajo pero coherente con lo esperado para este perfil.")
            lines.append("       Viable si la relevancia es alta; revisa la muestra a mano.")
        else:
            lines.append("    -> Volumen suficiente. El cuello de botella pasa a ser la")
            lines.append("       relevancia, no la cantidad.")
        lines.append("")

    lines.append("  Recuerda antes de firmar cualquier conclusión:")
    # EL AVISO DEL IDIOMA, PERO SOLO DONDE TODAVÍA APLICA. Decía siempre «las
    # 13 combinaciones están en inglés, en México puede haber volumen en
    # español que esta corrida no ve»; desde el 17 septiembre 2026 México
    # pregunta en español y ese aviso ahí es falso. Se calcula mercado a
    # mercado en vez de afirmarlo de todos.
    for market_key in stats.market_keys:
        combos = profile.combinations_for(market_key)
        if any(_parece_espanol(c) for c in combos):
            continue
        lines.append(
            f"    - En {config.market(market_key).label} se pregunta solo en INGLÉS "
            f"({len(combos)} combinaciones)."
        )
        lines.append("      Si ahí se publica en otro idioma, esta corrida no lo ve.")
    lines.append("    - Esta es UNA corrida, una foto de un día. El dato de negocio es")
    lines.append("      el flujo mensual: repite unos días y suma las nuevas.")
    return lines


# Palabras que solo aparecen en las combinaciones escritas en español. No hace
# falta detectar idioma de verdad: lo único que se quiere saber es si alguna de
# las combinaciones de ese mercado está en castellano.
_MARCAS_ES = ("director de", "directora de", "gerente", "general +", "manufactura",
              "logística", "agua", "inmobiliario", "asuntos", "relaciones")


def _parece_espanol(combo) -> bool:
    texto = combo.label.lower()
    return any(marca in texto for marca in _MARCAS_ES)


# ---------------------------------------------------------------------------
#  Punto de entrada
# ---------------------------------------------------------------------------

def build_report(
    stats: RunStats,
    config: AppConfig,
    profile: SearchProfile,
    storage: Storage,
    quota_lines: list[str] | None = None,
    sample_size: int = 10,
) -> str:
    """Compone el reporte completo como una sola cadena de texto."""
    blocks: list[list[str]] = [
        _header(stats, config, profile),
        _failures(stats),
        _by_market(stats, config),
        _by_combination(stats, config),
        _by_source(stats, storage),
        _dedupe(stats),
        _filters(stats, config),
        _salary(stats, profile),
        _sample(stats, storage, config, sample_size),
        _quotas(quota_lines or []),
        _reading(stats, config, profile),
    ]

    lines: list[str] = []
    for block in blocks:
        lines.extend(block)

    lines.append("")
    lines.append(_rule("="))
    lines.append(f"  Base de datos: {config.database_path}")
    lines.append(f"  Corrida: {stats.run_id}   ·   Generado: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(_rule("="))

    return "\n".join(lines)
