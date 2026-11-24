"""
Filtros duros, aplicados después de recibir los resultados de la API.

Regla de oro de este archivo: **nada se descarta en silencio**. Cada oferta
que cae lo hace con una categoría y una explicación, y el reporte final
imprime cuántas cayeron por cada motivo.

Eso es lo que permite distinguir estas dos frases, que se parecen mucho y
significan cosas opuestas:

    "en México hay 4 ofertas para este perfil"
    "en México llegaron 60 ofertas y mi filtro de antigüedad se comió 56"

La primera replantea el producto. La segunda replantea el filtro.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Market, SearchProfile
from .geo import ACCEPTED, REJECTED, UNRESOLVED, GeoDecision, classify
from .models import JobOffer, KeywordCombo
from .salary import BELOW, UNKNOWN, SalaryDecision, evaluate
from .textnorm import contains_any_term, normalize_company, normalize_text

# Categorías de descarte. Se usan como claves en las estadísticas, así que
# conviene que sean cortas y estables.
DROP_AGE = "antigüedad"
DROP_TITLE = "título excluido"
DROP_TITLE_OFF_TARGET = "título fuera de los puestos objetivo"
DROP_COMPANY = "empresa vetada"
DROP_TERMS = "términos ausentes en el texto"
DROP_GEO = "fuera del mercado"
DROP_GEO_UNRESOLVED = "ubicación indeterminada"
DROP_SALARY = "salario por debajo del umbral"
DROP_SALARY_UNKNOWN = "salario desconocido"
DROP_NO_DATE = "sin fecha de publicación"


@dataclass
class FilterResult:
    offer: JobOffer
    kept: bool
    drop_reason: str | None
    drop_detail: str | None
    geo: GeoDecision
    salary: SalaryDecision

    @property
    def dropped(self) -> bool:
        return not self.kept


def _check_age(offer: JobOffer, max_days_old: int) -> tuple[bool, str | None]:
    """Antigüedad. Una oferta SIN fecha no se descarta.

    Adzuna ya filtra por `max_days_old` en el servidor, así que aquí se
    recomprueba sobre todo por Jooble, que no soporta ese parámetro.

    Decisión: si no hay fecha, se conserva. Tirarla sería asumir que es vieja,
    y con el volumen esperado (decenas al mes) perder una buena oferta es peor
    que revisar una caducada.
    """
    age = offer.age_in_days
    if age is None:
        return True, None
    if age > max_days_old:
        return False, f"publicada hace {age:.0f} días (máximo {max_days_old})"
    return True, None


def _check_terms_present(offer: JobOffer, combo: KeywordCombo) -> tuple[bool, str | None]:
    """Exige que TODOS los términos aparezcan en el título o la descripción.

    Red de seguridad para Jooble, cuya semántica de `keywords` no está
    documentada: no se sabe si combina con AND, con OR o por relevancia.
    """
    haystack = normalize_text(f"{offer.title} {offer.description}")
    missing = [
        term for term in combo.terms if normalize_text(term) not in haystack
    ]
    if missing:
        return False, f"faltan en el texto: {', '.join(missing)}"
    return True, None


def apply_filters(
    offer: JobOffer,
    profile: SearchProfile,
    market: Market,
    combo: KeywordCombo,
) -> FilterResult:
    """Pasa una oferta por todos los filtros duros.

    El orden importa solo para saber qué motivo se reporta cuando una oferta
    incumple varios: se reporta el primero que falla. Se ponen antes los
    filtros baratos y menos discutibles.
    """
    filters = profile.hard_filters

    # La geo y el salario se evalúan SIEMPRE, aunque la oferta caiga antes por
    # otro motivo: el reporte quiere contar cuántas ofertas traían salario,
    # no solo cuántas sobrevivieron.
    geo_decision = classify(offer, market)
    salary_decision = evaluate(offer, filters.salary_floor)

    def drop(reason: str, detail: str) -> FilterResult:
        return FilterResult(offer, False, reason, detail, geo_decision, salary_decision)

    # 1. Antigüedad
    ok, detail = _check_age(offer, filters.max_days_old)
    if not ok:
        return drop(DROP_AGE, detail or "")

    # 2. Título excluido (becas, junior, etc.)
    matched_term = contains_any_term(offer.title, filters.exclude_title_terms)
    if matched_term:
        return drop(DROP_TITLE, f"el título contiene «{matched_term}»")

    # 3. Lista blanca de títulos: ¿esto es siquiera un puesto de dirección?
    #
    #    Va DESPUÉS de la lista negra a propósito. Ante «Junior Operations
    #    Director» las dos listas opinan, y tiene que ganar la que descarta:
    #    un puesto junior no deja de serlo porque el título diga «Director».
    if filters.uses_title_whitelist:
        if contains_any_term(offer.title, filters.include_title_terms) is None:
            return drop(
                DROP_TITLE_OFF_TARGET,
                f"«{offer.title}» no encaja con ningún puesto objetivo",
            )

    # 4. Empresa vetada
    if filters.exclude_companies:
        normalized_company = normalize_company(offer.company)
        for banned in filters.exclude_companies:
            if normalized_company == normalize_company(banned):
                return drop(DROP_COMPANY, f"empresa vetada: «{offer.company}»")

    # 5. Todos los términos presentes en el texto.
    #
    #    ES EL PLAN B, NO EL FILTRO PRINCIPAL. Solo corre si NO hay lista
    #    blanca. Los dos responden a la misma pregunta —«¿esta oferta va de lo
    #    suyo?»— y encadenarlos multiplica su severidad: exigir a la vez el
    #    título correcto Y las dos palabras de la combinación en el texto
    #    dejaba pasar 13 ofertas de 1.294 (medido, 27 agosto 2026). Con solo
    #    la lista blanca pasan 235. La que sobrevive es la que mira el título,
    #    porque la otra descarta por palabras que el anuncio puede no usar
    #    nunca aunque el puesto encaje.
    if filters.require_all_terms_in_text and not filters.uses_title_whitelist:
        ok, detail = _check_terms_present(offer, combo)
        if not ok:
            return drop(DROP_TERMS, detail or "")

    # 6. Geografía
    if geo_decision.status == REJECTED:
        return drop(DROP_GEO, geo_decision.reason)
    if geo_decision.status == UNRESOLVED and market.geo_filter.unresolved_policy == "drop":
        return drop(DROP_GEO_UNRESOLVED, geo_decision.reason)

    # 7. Salario. Solo corta si el umbral está en modo `filter`; en modo
    #    `report` (el de la v0) clasifica pero deja pasar todo.
    floor = filters.salary_floor
    if floor is not None and floor.is_active_filter:
        if salary_decision.status == BELOW:
            return drop(DROP_SALARY, salary_decision.reason)
        if salary_decision.status == UNKNOWN and floor.on_missing_salary == "drop":
            return drop(DROP_SALARY_UNKNOWN, salary_decision.reason)

    return FilterResult(offer, True, None, None, geo_decision, salary_decision)


# Se reexporta para que el pipeline no tenga que importar de geo.py también.
__all__ = [
    "FilterResult",
    "apply_filters",
    "ACCEPTED",
    "REJECTED",
    "UNRESOLVED",
    "DROP_AGE",
    "DROP_TITLE",
    "DROP_TITLE_OFF_TARGET",
    "DROP_COMPANY",
    "DROP_TERMS",
    "DROP_GEO",
    "DROP_GEO_UNRESOLVED",
    "DROP_SALARY",
    "DROP_SALARY_UNKNOWN",
]
