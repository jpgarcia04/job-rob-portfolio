"""
Evaluación del umbral salarial (170.000 USD/año de partida).

Por qué esto es un módulo aparte y no un `if` dentro del filtro:

El dato salarial es el más ausente y el menos fiable de todos los que
devuelven estas APIs. Tratarlo como un filtro más sería un error de medida
grave, porque la ausencia de dato NO es lo mismo que un salario bajo.

Tres resultados posibles:

    CUMPLE       hay dato y llega al umbral
    NO_CUMPLE    hay dato y no llega
    DESCONOCIDO  no hay dato con el que decidir

El tercero va a ser, con diferencia, el más frecuente. Jooble no devuelve
salario estructurado en absoluto (solo texto libre, que aquí NO se parsea a
propósito: un salario mal interpretado es peor que no tenerlo), y el índice
de México de Adzuna lo trae en una fracción pequeña de las ofertas.

Además se distingue el salario ESTIMADO del declarado: Adzuna a veces predice
el salario cuando el anuncio no lo dice (`salary_is_predicted`). Eso no es un
hecho del anuncio y no debe presentarse como tal.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import SalaryFloor
from .models import JobOffer

MEETS = "cumple"
BELOW = "no_cumple"
UNKNOWN = "desconocido"


@dataclass(frozen=True)
class SalaryDecision:
    status: str
    # Importe comparado, ya convertido a USD anuales. None si no había dato.
    annual_usd: float | None
    # True si el importe venía de una ESTIMACIÓN de la API, no del anuncio.
    is_predicted: bool
    reason: str

    @property
    def has_data(self) -> bool:
        return self.status != UNKNOWN


def evaluate(offer: JobOffer, floor: SalaryFloor | None) -> SalaryDecision:
    """Clasifica una oferta frente al umbral. NO decide si se descarta:
    eso lo hace filters.py leyendo `floor.mode`.
    """
    if floor is None:
        return SalaryDecision(UNKNOWN, None, False, "no hay umbral configurado")

    # Jooble solo trae texto libre. Se registra que lo trae, pero no se
    # convierte en número.
    if offer.salary_min is None and offer.salary_max is None:
        if offer.salary_raw:
            return SalaryDecision(
                UNKNOWN,
                None,
                False,
                f"salario solo como texto libre, sin estructurar: «{offer.salary_raw}»",
            )
        return SalaryDecision(UNKNOWN, None, False, "la oferta no trae salario")

    # Techo del rango por defecto: una oferta de 150k-200k cuenta como que
    # cumple, porque a este nivel se negocia y la sonda está para no perder
    # las pocas oportunidades que importan.
    if floor.use_range_top:
        amount = offer.salary_max if offer.salary_max is not None else offer.salary_min
        which = "techo del rango"
    else:
        amount = offer.salary_min if offer.salary_min is not None else offer.salary_max
        which = "suelo del rango"

    assert amount is not None  # garantizado por la comprobación de arriba

    amount_usd = floor.to_usd(amount, offer.salary_currency)
    if amount_usd is None:
        return SalaryDecision(
            UNKNOWN,
            None,
            offer.salary_is_predicted,
            f"divisa desconocida ({offer.salary_currency!r}): no está en fx_to_usd, "
            f"no me invento la conversión",
        )

    floor_usd = floor.to_usd(floor.amount, floor.currency) or floor.amount

    if amount_usd >= floor_usd:
        return SalaryDecision(
            MEETS,
            amount_usd,
            offer.salary_is_predicted,
            f"{which} = {amount:,.0f} {offer.salary_currency} "
            f"(~{amount_usd:,.0f} USD) >= {floor_usd:,.0f} USD",
        )

    return SalaryDecision(
        BELOW,
        amount_usd,
        offer.salary_is_predicted,
        f"{which} = {amount:,.0f} {offer.salary_currency} "
        f"(~{amount_usd:,.0f} USD) < {floor_usd:,.0f} USD",
    )
