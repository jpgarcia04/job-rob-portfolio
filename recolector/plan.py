"""
Lo que va a costar una corrida del recolector, antes de lanzarla.

El gemelo de `clasificador/plan.py`, y existe por lo mismo: la bandeja tiene un
botón que lanza esto, y la regla de la casa es que nada se gasta sin haber
enseñado antes el número.

LO QUE SE GASTA AQUÍ NO SON DÓLARES, SON PETICIONES. Y esa diferencia hay que
decirla en pantalla, porque las dos cuotas se comportan al revés: la de Adzuna
se regenera —pasarse da un HTTP 429 y se recupera solo— mientras que el techo
del LLM protege dinero de la clienta. Enseñar «esto cuesta 46» al lado de
«esto cuesta $0,88» sin aclarar en qué, invita a confundirlas.

Y HAY UNA TERCERA COSA QUE CONTAR: LA CACHÉ. Las respuestas se guardan en disco
con un TTL de 12 horas, así que repetir una corrida dentro de esa ventana no
gasta cuota casi ninguna. Medido el 2 septiembre 2026: un reintento de México
costó 2 peticiones de red y no 23, porque 21 salieron del disco. Un plan que
prometa 46 cuando van a ser 2 no es más seguro, es menos útil.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .quota import WindowedUsage

# Mismo nombre de archivo que usa el CLI. Se declara aquí también porque este
# módulo tiene que poder leer el contador sin importar el comando.
ARCHIVO_USO_ADZUNA = "uso_adzuna.json"


@dataclass(frozen=True)
class PlanDeRecoleccion:
    """Lo que haría `python -m recolector run`, y lo que consumiría."""

    perfil: str
    mercados: tuple[str, ...]
    fuentes: tuple[str, ...]
    combinaciones: int
    peticiones: int             # el máximo, si nada saliera de la caché

    usadas_hoy: int
    usadas_esta_semana: int
    usadas_este_mes: int
    usadas_historico: int

    tope_diario: int | None     # `null` en el YAML = no se conoce
    tope_semanal: int | None
    tope_mensual: int | None

    # Cuánto va a tardar, que con el límite de ritmo ya no es despreciable.
    segundos_estimados: int = 0

    # (etiqueta del mercado, cuántas combinaciones se le preguntan). Va aparte
    # de `combinaciones` porque desde el 17 septiembre 2026 los mercados no
    # preguntan lo mismo, y enseñar solo el total esconde justo el dato que
    # explica por qué la corrida cuesta lo que cuesta.
    combinaciones_por_mercado: tuple[tuple[str, int], ...] = field(default_factory=tuple)

    avisos: tuple[str, ...] = field(default_factory=tuple)
    nota: str = ""

    @property
    def hay_trabajo(self) -> bool:
        return self.peticiones > 0

    @property
    def cabe(self) -> bool:
        """¿Cabe en los topes DECLARADOS?

        Sin topes declarados devuelve True, y no es optimismo: es que no se
        conocen. El proyecto no finge saber el límite de Adzuna —lo mide— y un
        `False` inventado impediría correr por un número que nadie ha
        verificado. Los avisos sí se enseñan.
        """
        if self.tope_diario is not None and self.usadas_hoy + self.peticiones > self.tope_diario:
            return False
        if self.tope_semanal is not None and self.usadas_esta_semana + self.peticiones > self.tope_semanal:
            return False
        if self.tope_mensual is not None and self.usadas_este_mes + self.peticiones > self.tope_mensual:
            return False
        return True


def _desglose(por_mercado: tuple[tuple[str, int], ...]) -> str:
    """«13 combinaciones × 3 mercados», o el desglose cuando no cuadra.

    Se prefiere la multiplicación cuando es cierta porque se lee de un vistazo;
    en cuanto deja de serlo hay que escribir las tres cifras, porque un producto
    que no da el total es peor que una lista larga.
    """
    if not por_mercado:
        return "0 combinaciones"
    cuantas = {n for _, n in por_mercado}
    if len(cuantas) == 1:
        return f"{por_mercado[0][1]} combinaciones × {len(por_mercado)} mercados"
    return " + ".join(f"{n} en {etiqueta}" for etiqueta, n in por_mercado)


def plan_de_recoleccion(
    config: Any,
    perfil_clave: str | None = None,
    mercados: list[str] | None = None,
    fuentes: list[str] | None = None,
) -> PlanDeRecoleccion:
    """Describe la corrida sin lanzarla. No toca la red ni el disco de caché."""
    perfil = config.profile(perfil_clave)
    claves_mercado = mercados or perfil.market_keys

    disponibles = [
        nombre
        for nombre, activa in (
            ("adzuna", config.adzuna.enabled),
            ("jooble", config.jooble.enabled),
        )
        if activa
    ]
    nombres_fuente = fuentes or disponibles

    # CADA MERCADO PREGUNTA LO SUYO desde el 17 septiembre 2026, así que esto
    # ya no es una multiplicación. Lo cuenta `SearchProfile.planned_queries`,
    # que es la misma función que usan la consola y la corrida: si este número
    # y el que se cobra se separan, la promesa de «no gasto sin enseñar antes
    # el número» se rompe sin que falle nada.
    por_mercado = tuple(
        (config.market(k).label, len(perfil.combinations_for(k))) for k in claves_mercado
    )
    consultas = perfil.planned_queries(claves_mercado)
    combinaciones = len({c for k in claves_mercado for c in perfil.combinations_for(k)})
    peticiones = consultas * len(nombres_fuente)

    uso = WindowedUsage(config.database_path.parent / ARCHIVO_USO_ADZUNA, "adzuna")
    resumen = uso.summary()

    segundos = int(peticiones * config.adzuna.sleep_seconds_between_calls) + 20

    avisos: list[str] = []
    if not nombres_fuente:
        avisos.append(
            "No hay ninguna fuente habilitada. Revisa `sources` en "
            "config/search_profile.yaml."
        )
    if config.adzuna.daily_call_budget is None:
        avisos.append(
            "El tope diario de Adzuna está sin declarar (`null`), así que se "
            "cuenta y se avisa pero nunca se planta. El número real se saca de "
            "developer.adzuna.com."
        )
    elif resumen["hoy"] + peticiones > config.adzuna.warn_daily_above:
        avisos.append(
            f"Llevas {resumen['hoy']} peticiones hoy y esta corrida suma "
            f"{peticiones}. El aviso está en {config.adzuna.warn_daily_above} "
            "porque una corrida son 46: si salta, es que algo se corrió dos veces."
        )
    for ventana, usadas, tope in (
        ("hoy", resumen["hoy"], config.adzuna.daily_call_budget),
        ("en los últimos 7 días", resumen["esta_semana"], config.adzuna.weekly_call_budget),
        ("este mes", resumen["este_mes"], config.adzuna.monthly_call_budget),
    ):
        if tope is not None and usadas + peticiones > tope:
            avisos.append(
                f"No cabe en el tope de Adzuna {ventana}: llevas {usadas} de "
                f"{tope} y esta corrida suma {peticiones}. Su cuota se regenera "
                "sola, así que no se pierde nada esperando."
            )

    return PlanDeRecoleccion(
        perfil=perfil.label,
        mercados=tuple(config.market(k).label for k in claves_mercado),
        fuentes=tuple(nombres_fuente),
        combinaciones=combinaciones,
        peticiones=peticiones,
        usadas_hoy=resumen["hoy"],
        usadas_esta_semana=resumen["esta_semana"],
        usadas_este_mes=resumen["este_mes"],
        usadas_historico=resumen["historico"],
        tope_diario=config.adzuna.daily_call_budget,
        tope_semanal=config.adzuna.weekly_call_budget,
        tope_mensual=config.adzuna.monthly_call_budget,
        segundos_estimados=segundos,
        combinaciones_por_mercado=por_mercado,
        avisos=tuple(avisos),
        nota=(
            f"{_desglose(por_mercado)} × "
            f"{len(nombres_fuente)} fuente(s). Son peticiones a los portales, no "
            "dólares: la cuota de Adzuna se regenera sola. Y es el máximo — lo "
            f"que se haya buscado en las últimas {config.cache.ttl_hours:.0f} "
            "horas sale de la caché en disco y no gasta cuota. Tarda unos "
            f"{segundos // 60} minutos: el plan admite "
            f"{config.adzuna.max_calls_per_minute or '?'} peticiones por minuto "
            "y hay que ir a ese ritmo."
        ),
    )
