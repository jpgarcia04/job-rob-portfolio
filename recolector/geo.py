"""
Filtro geográfico posterior a la consulta.

El índice de EE.UU. de ambas APIs devuelve el país entero. La clienta solo
quiere Texas. Filtrar aquí, y no confiar en el parámetro `where`/`location`
de la API, es deliberado: esos parámetros son de "relevancia", no de
exclusión, y dejan pasar cosas de otros estados.

Tres resultados posibles, no dos:

  accepted   -> la ubicación pertenece al mercado
  rejected   -> la ubicación pertenece a otro sitio
  unresolved -> la API no dio información suficiente para decidir
                ("Remote", "United States", vacío)

El tercer caso es el importante. Un "Director of Operations, Remote (US)" es
perfectamente postulable para la clienta, y tirarlo en silencio sería mentir
sobre el volumen disponible. La política se decide en configuración
(`unresolved_policy`) y en cualquier caso se cuenta y se reporta aparte.

LAS ZONAS (24 septiembre 2026)
------------------------------
Texas entero traía demasiado, y la clienta acotó la parte que le
interesa: de Houston y Austin hasta Brownsville. Un mercado con `zonas` exige
DOS cosas, y la primera es la que no se puede saltar:

  1. que la región sea la suya (Texas), y
  2. que el condado —o, si la fuente no lo trae, la ciudad— esté en una zona.

Sin la 1, las ciudades de la lista aceptarían a sus homónimas de otros
estados, y hay muchas: Brownsville (Tennessee, Pensilvania, Brooklyn),
Mission (Kansas), Austin (Minnesota), Houston (Misisipi), San Benito
(California). El índice de EE.UU. devuelve el país entero, así que llegan.

Y hay un tercer resultado que aquí también existe: «Texas» a secas. Se sabe el
estado pero no dónde, así que es `unresolved`, no `rejected`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import GeoFilter, Market, Zona
from .models import JobOffer
from .textnorm import normalize_text

ACCEPTED = "accepted"
REJECTED = "rejected"
UNRESOLVED = "unresolved"

# Ubicaciones que no permiten decidir un estado. Ya normalizadas.
_UNRESOLVABLE_LOCATIONS = frozenset({
    "",
    "remote",
    "remoto",
    "teletrabajo",
    "work from home",
    "home based",
    "anywhere",
    "nationwide",
    "united states",
    "united states of america",
    "usa",
    "us",
    "estados unidos",
    "mexico",
    "mexico mexico",
})


# Palabras que acompañan al nombre de un estado sin decir nada más de dónde
# está la oferta: «Texas, US», «Texas, United States». Ya normalizadas.
_PALABRAS_DE_PAIS = frozenset({
    "us", "usa", "united", "states", "of", "america", "estados", "unidos", "eeuu",
})


@dataclass(frozen=True)
class GeoDecision:
    status: str
    reason: str
    # La clave de la zona, solo si el mercado tiene zonas y la oferta cae en
    # una. Es lo que se guarda en `offers.zona` y lo que filtra la bandeja.
    zona: str | None = None


def _matches_region(normalized_candidates: list[str], geo: GeoFilter) -> str | None:
    """¿Alguno de los textos de ubicación identifica una región permitida?

    La comparación de regiones es por token completo para que el alias corto
    ("tx") no coincida por accidente dentro de otra palabra.
    """
    allowed_regions = {normalize_text(r) for r in geo.regions if normalize_text(r)}
    for candidate in normalized_candidates:
        if not candidate:
            continue
        if candidate in allowed_regions:
            return candidate
        candidate_tokens = set(candidate.split())
        for region in allowed_regions:
            region_tokens = region.split()
            # Región de una sola palabra ("texas", "tx"): basta con que el
            # token esté presente. De varias palabras: substring delimitado.
            if len(region_tokens) == 1:
                if region in candidate_tokens:
                    return region
            elif f" {region} " in f" {candidate} ":
                return region
    return None


def _matches_city(normalized_candidates: list[str], geo: GeoFilter) -> str | None:
    """¿Alguno de los textos de ubicación nombra una ciudad permitida?

    Muchas ciudades son de varias palabras ("san antonio", "fort worth"), así
    que aquí sí se busca por substring, delimitado por espacios para no
    aceptar coincidencias parciales.
    """
    allowed_cities = {normalize_text(c) for c in geo.cities if normalize_text(c)}
    for candidate in normalized_candidates:
        if not candidate:
            continue
        padded = f" {candidate} "
        for city in allowed_cities:
            if f" {city} " in padded:
                return city
    return None


def _zona_por_condado(normalized_candidates: list[str], geo: GeoFilter) -> Zona | None:
    """La zona cuyo condado aparece en la ubicación. Manda sobre la ciudad.

    Delimitado por espacios, como las ciudades: «harris county» no casa
    dentro de otra palabra.
    """
    for candidate in normalized_candidates:
        if not candidate:
            continue
        padded = f" {candidate} "
        for zona in geo.zonas:
            for condado in zona.condados:
                if f" {normalize_text(condado)} " in padded:
                    return zona
    return None


def _zona_por_ciudad(normalized_candidates: list[str], geo: GeoFilter) -> Zona | None:
    """El plan B: la zona de la ciudad, para cuando no hay condado.

    NO CUENTA «Austin» DENTRO DE «Austin County». Son sitios distintos —el
    condado de Austin está al oeste de Houston, y la ciudad de Austin en el
    condado de Travis— y sin esta guarda un anuncio de Bellville, Austin
    County, acabaría en la zona equivocada si un día su condado dejara de
    estar en la lista.
    """
    for candidate in normalized_candidates:
        if not candidate:
            continue
        padded = f" {candidate} "
        for zona in geo.zonas:
            for ciudad in zona.ciudades:
                nombre = normalize_text(ciudad)
                if f" {nombre} " in padded and f" {nombre} county " not in padded:
                    return zona
    return None


def _trae_condado(normalized_candidates: list[str]) -> bool:
    """¿La ubicación dice su condado? Adzuna EE.UU. los escribe «X County»."""
    return any("county" in c.split() for c in normalized_candidates if c)


def _solo_dice_la_region(candidate: str, geo: GeoFilter) -> bool:
    """¿Este texto nombra el estado y nada más? («texas», «texas us», «tx»)."""
    region_tokens = {t for r in geo.regions for t in normalize_text(r).split()}
    return all(t in region_tokens or t in _PALABRAS_DE_PAIS for t in candidate.split())


def classify(offer: JobOffer, market: Market) -> GeoDecision:
    """Decide si una oferta pertenece geográficamente al mercado."""
    return classify_location(
        market,
        region=offer.location_region,
        county=offer.location_county,
        city=offer.location_city,
        raw=offer.location_raw,
    )


def classify_location(
    market: Market,
    *,
    region: str | None,
    county: str | None,
    city: str | None,
    raw: str | None,
) -> GeoDecision:
    """Lo mismo que `classify`, a partir de los campos sueltos.

    Existe para poder volver a juzgar lo que YA está guardado sin fabricar un
    `JobOffer` de mentira: la base guarda la ubicación, no la oferta entera.
    Lo usa `recolector/recorte.py`, y que sea la MISMA función que filtra al
    recolectar es lo que garantiza que el recorte de la base y el filtro de
    cada corrida no puedan decir cosas distintas.
    """
    geo = market.geo_filter

    if geo.mode == "none":
        return GeoDecision(ACCEPTED, "mercado sin restricción geográfica")

    display = ", ".join(p for p in (city, region) if p) or raw or "(sin ubicación)"

    # Se miran todos los campos de ubicación disponibles, no solo uno: Adzuna
    # rellena `region` (y en EE.UU. el condado), Jooble solo trae texto libre.
    candidates = [
        normalize_text(region),
        normalize_text(county),
        normalize_text(city),
        normalize_text(raw),
    ]

    if geo.zonas:
        return _classify_by_zone(candidates, geo, display, raw)

    matched_region = _matches_region(candidates, geo)
    if matched_region:
        return GeoDecision(ACCEPTED, f"región reconocida: '{matched_region}'")

    matched_city = _matches_city(candidates, geo)
    if matched_city:
        return GeoDecision(ACCEPTED, f"ciudad reconocida: '{matched_city}'")

    # Sin coincidencia. ¿Es que está en otro sitio, o es que no lo sabemos?
    informative = [c for c in candidates if c and c not in _UNRESOLVABLE_LOCATIONS]
    if not informative:
        return GeoDecision(
            UNRESOLVED,
            f"ubicación no permite decidir estado: '{raw or '(vacía)'}'",
        )

    return GeoDecision(
        REJECTED,
        f"fuera del mercado: '{display}'",
    )


def _classify_by_zone(
    candidates: list[str], geo: GeoFilter, display: str, raw: str | None
) -> GeoDecision:
    """Región Y zona. Ver la cabecera del módulo."""
    informative = [c for c in candidates if c and c not in _UNRESOLVABLE_LOCATIONS]

    if not _matches_region(candidates, geo):
        if not informative:
            return GeoDecision(
                UNRESOLVED,
                f"ubicación no permite decidir estado: '{raw or '(vacía)'}'",
            )
        return GeoDecision(REJECTED, f"fuera del mercado: '{display}'")

    # SI HAY CONDADO, MANDA EL CONDADO, y la ciudad ni se mira. Medido en el
    # simulacro del 24 septiembre 2026: «Big Spring, Howard County» —oeste de
    # Texas, a 700 km de Houston— entraba en Houston porque la ciudad
    # «spring» casaba dentro de «big spring». Un condado que no está en
    # ninguna zona es una respuesta, no una falta de datos.
    zona = _zona_por_condado(candidates, geo)
    if zona is None and not _trae_condado(candidates):
        zona = _zona_por_ciudad(candidates, geo)
    if zona:
        return GeoDecision(ACCEPTED, f"zona «{zona.etiqueta}»", zona=zona.clave)

    # Es del estado, pero ¿de dónde? Si lo único que dice es «Texas», no se
    # puede saber si cae dentro: eso es indeterminado, no un rechazo.
    if all(_solo_dice_la_region(c, geo) for c in informative):
        return GeoDecision(
            UNRESOLVED,
            f"se sabe el estado pero no el condado ni la ciudad: '{raw or display}'",
        )
    return GeoDecision(REJECTED, f"fuera de las zonas: '{raw or display}'")
