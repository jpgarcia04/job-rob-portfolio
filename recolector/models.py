"""
El esquema común.

Adzuna y Jooble devuelven formatos distintos. Cada conector traduce lo suyo a
`JobOffer` y a partir de ahí el resto del sistema no sabe (ni debe saber) de
dónde vino cada oferta.

Nota para quien viene de Java: `@dataclass` es el equivalente a un record /
POJO con constructor, equals y toString generados. Los tipos con `| None`
son lo mismo que `Optional<T>`: el valor puede faltar legítimamente.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class KeywordCombo:
    """Una de las 13 combinaciones. `frozen=True` la hace inmutable y
    hasheable, para poder usarla como clave de diccionario en las estadísticas.
    """

    terms: tuple[str, ...]

    @property
    def label(self) -> str:
        """Etiqueta legible para el reporte: 'COO + Logistics'."""
        return " + ".join(self.terms)

    @property
    def as_query_string(self) -> str:
        """Los términos unidos por espacio, como los espera una caja de búsqueda."""
        return " ".join(self.terms)


@dataclass
class JobOffer:
    """Una oferta ya normalizada, venga de donde venga."""

    # --- Procedencia -------------------------------------------------------
    source: str            # "adzuna" | "jooble"
    market_key: str        # "mexico" | "texas_us"
    query_label: str       # "COO + Logistics"
    external_id: str       # id de la oferta EN SU PORTAL (no es único global)

    # --- Contenido ---------------------------------------------------------
    title: str
    company: str
    location_raw: str              # tal cual lo devolvió la API
    location_city: str | None
    location_region: str | None    # estado / provincia
    location_country: str | None
    url: str                       # URL de postulación
    description: str

    # --- Salario (casi siempre ausente en el índice de México) -------------
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_raw: str | None = None          # texto libre, cuando la API no da números
    salary_is_predicted: bool = False      # Adzuna a veces ESTIMA el salario

    # --- Ubicación fina (24 septiembre 2026) --------------------------------
    # El nivel entre el estado y la ciudad, cuando la fuente lo da: en EE.UU.,
    # Adzuna pone ahí el CONDADO («Harris County»). Hace falta aparte porque en
    # los anuncios de barrio —«Audubon Place, Houston»— el texto visible ya no
    # lo lleva, y el condado es lo que decide la zona. Ver `geo.py`.
    location_county: str | None = None
    # La zona del mercado en la que cayó (solo mercados con `zonas`). La pone
    # el filtro geográfico, no el conector.
    zona: str | None = None

    # --- Fechas ------------------------------------------------------------
    posted_at: datetime | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # --- Claves de deduplicación (las rellena dedupe.py) -------------------
    dedupe_key: str = ""
    loose_key: str = ""

    # --- Respuesta cruda, para poder depurar sin volver a pedirla ----------
    raw: dict = field(default_factory=dict)

    @property
    def location_display(self) -> str:
        """Lo que se enseña en el reporte."""
        parts = [p for p in (self.location_city, self.location_region) if p]
        if parts:
            return ", ".join(parts)
        return self.location_raw or "(sin ubicación)"

    @property
    def age_in_days(self) -> float | None:
        """Antigüedad de la oferta. None si la API no dio fecha."""
        if self.posted_at is None:
            return None
        delta = datetime.now(timezone.utc) - self.posted_at
        return delta.total_seconds() / 86400.0


@dataclass
class FetchOutcome:
    """Lo que devuelve un conector para UNA consulta (combinación x mercado).

    Se devuelven los avisos y el error en lugar de lanzarlos hacia arriba sin
    más porque una corrida no debe abortar entera porque una consulta falle:
    lo que hay que evitar es que el fallo pase DESAPERCIBIDO. Por eso todo
    esto termina impreso en el reporte.
    """

    source: str
    market_key: str
    combo: KeywordCombo
    offers: list[JobOffer] = field(default_factory=list)

    # Cuántos resultados dijo la API que había en total (no cuántos devolvió).
    reported_total: int | None = None
    # Cuántas peticiones HTTP costó (importa para la cuota de Jooble).
    requests_made: int = 0
    served_from_cache: bool = False

    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None
