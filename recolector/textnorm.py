"""
Normalización de texto.

Todo lo que se compara entre fuentes distintas (empresa, título, ubicación)
pasa por aquí primero. Adzuna y Jooble escriben lo mismo de formas distintas
y sin esta capa la deduplicación no funciona.

Nada de esto es inteligente a propósito: son reglas explícitas y auditables.
Cuando la dedup falle, quiero poder leer la función y saber por qué falló.
"""

from __future__ import annotations

import html
import re
import unicodedata

# ---------------------------------------------------------------------------
# Formas jurídicas que se eliminan del nombre de empresa antes de comparar.
#
# "Grupo Energético del Norte S.A. de C.V." y "Grupo Energetico del Norte"
# son la misma empresa. Se listan ya normalizadas (minúsculas, sin acentos,
# sin puntuación), porque se aplican DESPUÉS de normalizar.
# ---------------------------------------------------------------------------
_LEGAL_SUFFIX_PHRASES = (
    "s a p i de c v",
    "s de r l de c v",
    "s a de c v",
    "sapi de cv",
    "s de rl de cv",
    "sa de cv",
    "s a b de c v",
    "sab de cv",
)

_LEGAL_SUFFIX_TOKENS = frozenset({
    "sa", "sl", "srl", "sas", "sac", "spa",
    "inc", "llc", "llp", "lp", "ltd", "ltda", "limited",
    "corp", "corporation", "co", "company", "incorporated",
    "gmbh", "ag", "bv", "nv", "plc", "pte", "pty", "oy", "ab",
})

# Marcas de género/idioma que algunos portales pegan al título:
# "Director of Operations (m/f/d)". Se eliminan tras quitar la puntuación.
_GENDER_TOKEN_PATTERNS = (
    re.compile(r"\bm\s*f\s*d\b"),
    re.compile(r"\bm\s*w\s*d\b"),
    re.compile(r"\bh\s*f\s*d\b"),
    re.compile(r"\bm\s*f\b"),
    re.compile(r"\bh\s*m\b"),
)

# Códigos de requisición: "#40912", "REQ-40912", "JR0012345", "ID 88213".
_REQUISITION_PATTERNS = (
    re.compile(r"#\s*\d{3,}"),
    re.compile(r"\b(?:req|requisition|job|jr|r|id)[\s\-_]*\d{4,}\b", re.IGNORECASE),
    re.compile(r"\b\d{6,}\b"),
)

# Contenido entre paréntesis, corchetes o llaves. En los títulos casi siempre
# es ruido ("(Remote)", "(m/f/d)", "(Ref. 8821)").
_BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")

_HTML_TAG = re.compile(r"<[^>]+>")
_NON_ALNUM = re.compile(r"[^0-9a-z\s]")
_WHITESPACE = re.compile(r"\s+")


def strip_html(text: str | None) -> str:
    """Quita etiquetas HTML y decodifica entidades (&amp; -> &).

    Adzuna devuelve descripciones y a veces títulos con <strong> dentro.
    """
    if not text:
        return ""
    without_tags = _HTML_TAG.sub(" ", text)
    unescaped = html.unescape(without_tags)
    return _WHITESPACE.sub(" ", unescaped).strip()


def strip_accents(text: str) -> str:
    """"México" -> "Mexico".

    NFKD descompone cada letra acentuada en (letra base + marca de acento);
    después se descartan las marcas (categoría Unicode "Mn" = Mark, nonspacing).
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def normalize_text(text: str | None) -> str:
    """Forma canónica para comparar: sin HTML, sin acentos, sin puntuación,
    en minúsculas y con los espacios colapsados.

    "  Dirección   General, S.A. " -> "direccion general s a"
    """
    if not text:
        return ""
    result = strip_html(text)
    result = strip_accents(result).lower()
    result = _NON_ALNUM.sub(" ", result)
    return _WHITESPACE.sub(" ", result).strip()


def normalize_company(name: str | None) -> str:
    """Nombre de empresa listo para hashear.

    Además de la normalización general, elimina la forma jurídica del final.
    OJO: esto NO resuelve el problema de fondo — los agregadores devuelven a
    veces la agencia de reclutamiento o "Confidencial" en lugar del empleador
    real. Eso no se puede arreglar aquí; se mide en el reporte comparando la
    clave estricta contra la laxa (ver dedupe.py).
    """
    normalized = normalize_text(name)
    if not normalized:
        return ""

    # Primero las frases de varias palabras ("s a de c v"), ancladas al final.
    for phrase in _LEGAL_SUFFIX_PHRASES:
        if normalized.endswith(" " + phrase):
            normalized = normalized[: -(len(phrase) + 1)]
            break

    # Después, tokens sueltos del final ("inc", "llc"), que pueden apilarse:
    # "acme corp inc" -> "acme".
    tokens = normalized.split()
    while tokens and tokens[-1] in _LEGAL_SUFFIX_TOKENS:
        tokens.pop()

    return " ".join(tokens)


def normalize_title(title: str | None) -> str:
    """Título listo para hashear: sin paréntesis, sin códigos de requisición
    y sin marcas de género.

    Limitación conocida y aceptada: NO traduce. "Director of Operations" y
    "Director de Operaciones" siguen siendo claves distintas. Resolverlo
    requiere un diccionario de sinónimos o un LLM, y ninguna de las dos cosas
    entra en esta v0.
    """
    if not title:
        return ""

    cleaned = strip_html(title)
    cleaned = _BRACKETED.sub(" ", cleaned)
    for pattern in _REQUISITION_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)

    normalized = normalize_text(cleaned)
    for pattern in _GENDER_TOKEN_PATTERNS:
        normalized = pattern.sub(" ", normalized)

    return _WHITESPACE.sub(" ", normalized).strip()


def normalize_location(location: str | None) -> str:
    """Ubicación lista para comparar.

    Se limita a normalizar y a unificar un puñado de alias frecuentes de la
    Ciudad de México, que es donde se concentra el grueso de las ofertas
    ejecutivas del país y donde cada portal escribe una cosa distinta.
    """
    normalized = normalize_text(location)
    if not normalized:
        return ""

    cdmx_aliases = (
        "ciudad de mexico",
        "mexico city",
        "cdmx",
        "distrito federal",
        "d f",
        "mexico df",
    )
    for alias in cdmx_aliases:
        # Coincidencia por token completo, no por substring: "mexico" a secas
        # (el país) no debe convertirse en la ciudad.
        if normalized == alias:
            return "ciudad de mexico"

    return normalized


def tokens(text: str | None) -> frozenset[str]:
    """Conjunto de palabras del texto ya normalizado.

    Sirve para comprobar pertenencia sin falsos positivos por substring:
    "tx" como token no coincide con "txistu".
    """
    return frozenset(normalize_text(text).split())


def contains_any_term(haystack: str | None, terms: list[str]) -> str | None:
    """Devuelve el primer término de `terms` que aparece en `haystack`, o None.

    Ambos lados se normalizan antes de comparar. La comparación es por
    substring sobre el texto normalizado, con espacios alrededor, para que
    "junior" no coincida dentro de "juniors" pero sí como palabra.
    """
    normalized_haystack = f" {normalize_text(haystack)} "
    for term in terms:
        normalized_term = normalize_text(term)
        if not normalized_term:
            continue
        if f" {normalized_term} " in normalized_haystack:
            return term
    return None
