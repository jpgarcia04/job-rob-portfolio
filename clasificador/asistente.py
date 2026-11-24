"""
El asistente: qué hacer con la semana.

LA DIFERENCIA CON EL CLASIFICADOR
---------------------------------
El clasificador mira UNA oferta y decide si merece su tiempo. Es un filtro con
explicación, y por bueno que sea sigue siendo un filtro: le entrega una lista
de catorce cosas igual de importantes entre sí.

Esto mira TODAS a la vez y le dice por dónde empezar. Es lo único del sistema
que no se podría hacer con reglas, porque comparar una dirección general en
agua contra una jefatura de operaciones en manufactura no es aplicar un
criterio: es pesar dos criterios que se contradicen.

Y ESO ES LO BARATO
------------------
Una llamada por RANKING, no una por oferta. Sobre veinte ofertas ya
clasificadas son ~2.500 tokens de entrada y ~1.200 de salida: unos $0,05. Las
capas deterministas —las listas de títulos, la agrupación, la caché— ya
hicieron el trabajo sucio gratis, así que lo que queda para el modelo es lo
único que no es mecánico. El filtro cuesta ~$3 al mes; el asistente, veinte
veces menos.

QUÉ NO SE LE MANDA
------------------
Ni las descripciones completas ni el CV. Solo lo que el clasificador YA
destiló de cada oferta: puesto canónico, empresa normalizada, nivel, modalidad,
si publica una agencia, cuántos anuncios tiene y la razón que ya se escribió.
Es más barato, y sigue cumpliendo la regla de la casa: el documento de la
clienta no sale de su máquina.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field

from .cliente_llm import Cabida, cabe_la_respuesta, coste_de, coste_medido
from .fichas import Preferencias

# Qué hacer con una oferta. Cerrado a propósito: el valor de esto es que le
# diga por dónde empezar, y "depende" no es por dónde empezar.
Accion = Literal[
    "postular_ya",      # esto vale su tiempo hoy
    "revisar",          # merece leerla entera antes de decidir
    "descartar",        # el clasificador la dejó pasar, pero vista al lado del
                        # resto no compite
]


class OfertaOrdenada(BaseModel):
    """Una oferta en su sitio dentro del ranking."""

    numero: int = Field(
        description=(
            "El número que tiene la oferta en la lista de arriba. Úsalo tal cual: "
            "es la única forma de saber de cuál estás hablando."
        )
    )
    por_que: str = Field(
        description=(
            "Una frase, dirigida a ella, diciendo por qué va en esa posición. "
            "Compárala con las otras: 'dirige más presupuesto que la de Houston' "
            "sirve; 'buen puesto ejecutivo' no dice nada."
        )
    )
    accion: Accion = Field(description="Qué debería hacer con esta oferta.")


class Ranking(BaseModel):
    """Lo que devuelve el modelo por CADA lote, en una sola llamada."""

    lectura: str = Field(
        description=(
            "Dos o tres frases sobre el lote entero, dirigidas a ella: qué hay esta "
            "vez, qué destaca y qué falta. Es lo primero que va a leer."
        )
    )
    ofertas: list[OfertaOrdenada] = Field(
        description=(
            "TODAS las ofertas de la lista, ordenadas de más a menos interesante. "
            "Ninguna se queda fuera: si algo no vale la pena, va al final con "
            "accion='descartar', no desaparece."
        )
    )
    patrones: list[str] = Field(
        description=(
            "Cero a tres observaciones sobre el conjunto que ella no vería mirando "
            "las ofertas de una en una: que tres son de la misma reclutadora, que "
            "México no ha dado nada este mes, que un sector se está moviendo. "
            "Lista vacía si no hay nada que merezca decirse."
        )
    )


class RankingInvalidoError(Exception):
    """El modelo devolvió números que no corresponden con la lista enviada."""


@dataclass
class ResultadoRanking:
    """Lo que sale de una corrida del asistente."""

    lectura: str = ""
    patrones: list[str] = field(default_factory=list)
    # (dedupe_key, posicion, por_que, accion), ya en orden
    items: list[tuple[str, int, str, str]] = field(default_factory=list)
    consideradas: int = 0
    tokens_entrada: int = 0
    tokens_salida: int = 0
    modelo: str = ""
    error: str | None = None

    # Ver `ResultadoOferta.coste_medido_usd` en motor.py: el desglose de caché
    # solo existe en el instante de la llamada.
    coste_medido_usd: float | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def coste_usd(self) -> float:
        if self.coste_medido_usd is not None:
            return self.coste_medido_usd
        return coste_de(self.tokens_entrada, self.tokens_salida)


# ---------------------------------------------------------------------------
#  El mensaje
# ---------------------------------------------------------------------------

def construir_sistema(preferencias: Preferencias, cuantas: int) -> str:
    """El bloque estable: qué se le pide y con qué criterio ordenar.

    Los criterios entran TAL CUAL los escribió ella. No se reformulan ni se
    traducen a puntuaciones: reducir «el nivel manda sobre el sector» a un
    peso numérico es exactamente el tipo de precisión falsa que hace que el
    resultado deje de parecerse a lo que ella pidió.
    """
    if preferencias.hay_criterios:
        criterios = "\n".join(f"{i}. {c}" for i, c in enumerate(preferencias.criterios, 1))
        bloque_criterios = (
            "Sus criterios, en orden de importancia y con sus palabras:\n\n"
            f"{criterios}\n\n"
            "Cuando dos criterios se contradigan, manda el que esté más arriba."
        )
    else:
        bloque_criterios = (
            "No ha declarado criterios propios. Ordena por encaje con un perfil "
            "ejecutivo de logística, manufactura y operaciones."
        )

    return f"""\
Ayudas a una candidata ejecutiva a decidir por dónde empezar.

Otra parte del sistema ya revisó estas ofertas una por una y descartó el
ruido. Tu trabajo NO es volver a filtrar: es ORDENAR lo que sobrevivió y
decirle qué hacer con su semana.

{bloque_criterios}

CÓMO ORDENAR

- Compara las ofertas ENTRE SÍ, no cada una contra un ideal. La pregunta no es
  "¿es buena?", es "¿es mejor que la siguiente, y por qué?".
- Destaca {cuantas} arriba del todo. Esas son las que va a mirar hoy.
- Devuelve TODAS las ofertas de la lista, ninguna se queda fuera. Lo que no
  compite va al final con accion="descartar", pero sigue estando: esconder una
  oferta no es una decisión que te toque a ti.
- Usa el `numero` de cada oferta tal como aparece en la lista.
- PERO NO CITES NÚMEROS DENTRO DE `por_que` NI DE `lectura`. Ella lee la lista
  ya reordenada por ti, así que «el mismo puesto que la 5» apunta a otra cosa
  para ella que para ti. Si tienes que referirte a otra oferta, nómbrala por
  empresa o por puesto: «el mismo puesto que el de JLL en Alief».

CÓMO ESCRIBIR

Ella lee esto antes de abrir ninguna oferta. Cada `por_que` es una frase que
compara: "más presupuesto a cargo que la de Dallas y reporta a consejo" sirve;
"excelente oportunidad ejecutiva" no le dice nada y le hace perder el tiempo.

Si el lote es flojo, dilo en la `lectura`. Inflar una semana mala para que
parezca productiva es la forma más rápida de que deje de abrir esto."""


def _edad_en_dias(valor: Any) -> str:
    if not valor:
        return "sin fecha"
    try:
        fecha = datetime.fromisoformat(str(valor))
    except ValueError:
        return "sin fecha"
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)
    dias = (datetime.now(timezone.utc) - fecha).days
    return "hoy" if dias <= 0 else f"hace {dias} d"


def construir_lote(filas: Sequence[sqlite3.Row]) -> str:
    """El bloque variable: las ofertas ya clasificadas, numeradas.

    LOS NÚMEROS SON LA CLAVE. Al modelo no se le piden `dedupe_key`, que son
    hashes de 16 caracteres: pedirle que copie veinte hashes sin equivocarse
    es regalar un modo de fallo. Se le dan índices del 1 al N y Python
    traduce de vuelta, comprobando que estén todos y que ninguno se repita.
    """
    lineas: list[str] = []

    for numero, fila in enumerate(filas, 1):
        etiquetas: list[str] = []
        if fila["es_reclutadora"]:
            etiquetas.append("la publica una AGENCIA")
        if fila["necesita_criterio"]:
            etiquetas.append("el clasificador dudó")
        anuncios = fila["anuncios"] if "anuncios" in fila.keys() else 1
        if anuncios and anuncios > 1:
            etiquetas.append(f"publicada en {anuncios} sitios")

        extra = f"  [{'; '.join(etiquetas)}]" if etiquetas else ""

        lineas.append(
            f"{numero}. {fila['puesto_canonico'] or fila['title']}\n"
            f"   empresa: {fila['empresa_normalizada'] or fila['company']}"
            f"  ·  {fila['market_key']}"
            f"  ·  {fila['location_raw'] or 'sin ubicación'}\n"
            f"   nivel: {fila['seniority_detectado'] or '?'}"
            f"  ·  modalidad: {fila['modalidad'] or '?'}"
            f"  ·  {_edad_en_dias(fila['posted_at'])}{extra}\n"
            f"   ya se dijo de ella: {fila['razon'] or '(sin razón registrada)'}"
        )

    return (
        f"Estas son las {len(filas)} ofertas que sobrevivieron al filtro. "
        "Ordénalas.\n\n" + "\n\n".join(lineas)
    )


# ---------------------------------------------------------------------------
#  El motor
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  Lo que cuesta ordenar un lote, antes de gastarlo
# ---------------------------------------------------------------------------
#  Todas las cifras medidas el 3 septiembre 2026. El porqué de cada una está
#  en `Asistente.coste_estimado`, que es donde se usan.
TOKENS_FIJOS = 2106
TOKENS_POR_OFERTA = 237

# Esquema + prompt de sistema, sin el lote. Por encima del mínimo de 512 de
# Opus 5, así que cachea; y como el ranking es UNA llamada, siempre escribe la
# caché (x1,25) y nunca la lee.
TOKENS_PREFIJO_CACHEADO = 2077

# LA SALIDA MANDA: son dos tercios de la factura del ranking, y casi todo es
# pensamiento del modelo, que se factura como salida y no se ve en el esquema.
#
# Despejada de los tres costes MEDIDOS el 2 septiembre 2026 sobre las mismas 11
# ofertas, descontándoles la entrada. Que el método vale se comprueba con la
# fila de `high`: da 3.383 tokens y la corrida guardó 3.378.
#
#     high (el defecto de la API)   $0,110  ->  308 por oferta
#     medium (lo que corre hoy)     $0,077  ->  188 por oferta
#     low                           $0,061  ->  129 por oferta
#
# SE MODELA PROPORCIONAL, SIN TÉRMINO FIJO, Y ES DELIBERADO. Los tres puntos
# son del mismo lote de 11, así que no se puede separar lo fijo (la lectura de
# la semana, los patrones) de lo que crece por oferta. Repartir todo entre las
# ofertas es la opción CONSERVADORA para lotes mayores que 11: si de verdad
# hubiera un término fijo, esta fórmula cobra de más, nunca de menos. Sobre 11
# es exacta. Una segunda corrida con otro tamaño de lote despeja las dos cosas.
SALIDA_POR_OFERTA = {
    "low": 129,
    "medium": 188,
    "high": 308,
    # Sin medir; se les pone la cifra de `high` como suelo. Ver el mismo
    # comentario en `triaje.py`.
    "xhigh": 308,
    "max": 308,
}

# Lo que aplica la API si no se manda `output_config.effort`.
ESFUERZO_POR_DEFECTO = "high"


def _salida_por_oferta(esfuerzo: str | None) -> int:
    """Un esfuerzo desconocido cae en el más caro que conocemos."""
    return SALIDA_POR_OFERTA.get(
        esfuerzo or ESFUERZO_POR_DEFECTO, SALIDA_POR_OFERTA["high"]
    )


def cabida(
    cuantas_ofertas: int,
    esfuerzo: str | None = None,
    techo: int | None = None,
) -> Cabida:
    """¿Entra en una llamada la respuesta de ordenar N ofertas?

    MISMO MODO DE FALLO QUE EL TRIAJE, y por eso la aritmética es compartida
    (`cliente_llm.cabe_la_respuesta`). Lo que cambia es cuánto ocupa una línea:
    el ranking escribe 188 tokens por oferta a `medium` contra los ~50 del
    triaje a `low`, porque además de ordenar razona sobre cada una. A `medium`
    el techo está en **~60 ofertas**; a `high`, en 37.

    No es hipotético a plazo largo: al ranking entran las clasificadas que ella
    todavía no ha revisado, y eso crece a 8 al día menos lo que marque. Dos
    semanas sin abrir la bandeja y el lote llega a 60.

    Y aquí un ranking cortado es peor que caro: el modelo devolvería una lista
    incompleta, la validación la rechazaría entera —que es lo correcto, ver
    `_traducir`— y la llamada estaría pagada para nada.
    """
    return cabe_la_respuesta(
        cuantas_ofertas,
        _salida_por_oferta(esfuerzo),
        techo=techo,
        unidad="ofertas",
        consecuencia="y el orden se rechazaría entero",
    )


class Asistente:
    """Ordena un lote ya clasificado. Una llamada, no una por oferta.

    Recibe el cliente por parámetro igual que `Motor`, para poder probar el
    camino entero —construcción del mensaje, validación de los números,
    traducción a claves, persistencia— sin red y sin API key.
    """

    def __init__(
        self,
        cliente: Any,
        preferencias: Preferencias,
        esfuerzo: str | None = None,
    ) -> None:
        self.cliente = cliente
        self.preferencias = preferencias
        self.esfuerzo = esfuerzo
        self.sistema = construir_sistema(preferencias, preferencias.cuantas_destacar)

    def coste_estimado(self, filas: Sequence[sqlite3.Row]) -> tuple[int, int, float]:
        """(entrada, salida, USD) de ordenar el lote. No llama a nada.

        MEDIDO EL 3 SEPTIEMBRE 2026, y antes estaba mal por los dos lados.

        Decía `entrada = estimar_tokens(sistema) + estimar_tokens(lote)` y
        `salida = 400 + 60 * ofertas`. Sobre las 11 ofertas reales de la base
        eso daba $0,0371 donde la corrida del 2 septiembre costó **$0,1097**:
        se quedaba en un TERCIO. Y hacia abajo, que es el lado malo — el
        estimador del triaje al menos se pasaba.

        Las dos causas son las mismas de siempre en este proyecto:

          1. EL ESQUEMA VIAJA Y NO SE VE. `Ranking` ocupa 1.126 tokens que el
             SDK manda en cada llamada (el prompt de sistema son 951 y el
             prefijo completo 2.077). Tercera vez que este error aparece, y la
             primera en la que se queda corto en vez de largo.
          2. `estimar_tokens` cuenta 4 caracteres por token, que es el número
             de manual para inglés. Aquí entran nombres de empresa, puestos
             canónicos y razones en español.

        La entrada, con `count_tokens` mandando esquema y `thinking` como la
        llamada de verdad:

            ofertas      entrada
            --------------------
                  5        3.289
                 11        4.718
                 22        7.328
                 44       12.548
                 88       22.988

        Ajuste: 2.106 + 237,3 por oferta. (Los lotes de más de 11 repiten las
        filas reales, porque no hay más ofertas clasificadas en la base: la
        pendiente es la longitud media de LAS LÍNEAS DE ESTE LOTE, no una
        constante universal. El corte, 2.106, sí es sólido: cuadra con los
        2.077 del prefijo medido aparte.)

        LA VALIDACIÓN QUE HACE CREÍBLE TODO LO DEMÁS: con esta fórmula y el
        prefijo cacheado, el coste de la corrida real guardada en `rankings`
        —11 ofertas, 4.564 de entrada, 3.378 de salida— se reconstruye en
        $0,1099 contra los $0,1097 apuntados. Un 0,17%.
        """
        entrada = int(TOKENS_FIJOS + TOKENS_POR_OFERTA * len(filas))
        salida = _salida_por_oferta(self.esfuerzo) * len(filas)
        nueva = max(0, entrada - TOKENS_PREFIJO_CACHEADO)
        coste = coste_medido(
            entrada_nueva=nueva,
            cache_leida=0,
            cache_escrita=TOKENS_PREFIJO_CACHEADO,
            tokens_salida=salida,
        )
        return entrada, salida, coste

    def ordenar(self, filas: Sequence[sqlite3.Row]) -> ResultadoRanking:
        resultado = ResultadoRanking(consideradas=len(filas))

        if not filas:
            resultado.error = "No hay ofertas clasificadas que ordenar."
            return resultado

        # ¿CABE LA RESPUESTA? Antes de llamar, igual que en el triaje y por lo
        # mismo: al techo del mes esta llamada le cabe de sobra, así que nadie
        # más va a impedir que se corte a media lista.
        sitio = cabida(len(filas), self.esfuerzo)
        if not sitio.cabe:
            resultado.error = sitio.explicacion(len(filas))
            return resultado

        respuesta = self.cliente.ordenar(
            self.sistema, construir_lote(filas), self.esfuerzo
        )

        resultado.tokens_entrada = respuesta.tokens_entrada
        resultado.tokens_salida = respuesta.tokens_salida
        resultado.modelo = respuesta.modelo
        resultado.coste_medido_usd = respuesta.coste_usd

        if not respuesta.ok or respuesta.ranking is None:
            resultado.error = respuesta.error or "El modelo no devolvió un ranking."
            return resultado

        try:
            resultado.items = self._traducir(respuesta.ranking, filas)
        except RankingInvalidoError as exc:
            # NO se guarda un ranking a medias. Un orden con huecos es peor que
            # no tener orden: parece completo y esconde lo que falta.
            resultado.error = str(exc)
            return resultado

        resultado.lectura = respuesta.ranking.lectura
        resultado.patrones = list(respuesta.ranking.patrones)
        return resultado

    def _traducir(
        self,
        ranking: Ranking,
        filas: Sequence[sqlite3.Row],
    ) -> list[tuple[str, int, str, str]]:
        """Números del modelo -> claves de la base, comprobando por el camino.

        Se valida ruidosamente porque el fallo silencioso aquí es feísimo: si
        el modelo repite un número o se salta uno, el ranking parecería
        completo y una oferta quedaría escondida sin que nadie lo notara.
        """
        total = len(filas)
        vistos: set[int] = set()
        items: list[tuple[str, int, str, str]] = []

        for posicion, item in enumerate(ranking.ofertas, 1):
            if not 1 <= item.numero <= total:
                raise RankingInvalidoError(
                    f"El modelo devolvió el número {item.numero} y solo se le "
                    f"mandaron {total} ofertas."
                )
            if item.numero in vistos:
                raise RankingInvalidoError(
                    f"El modelo colocó la oferta {item.numero} dos veces."
                )
            vistos.add(item.numero)
            items.append(
                (
                    filas[item.numero - 1]["dedupe_key"],
                    posicion,
                    item.por_que,
                    item.accion,
                )
            )

        faltan = sorted(set(range(1, total + 1)) - vistos)
        if faltan:
            raise RankingInvalidoError(
                f"El modelo se dejó fuera {len(faltan)} oferta(s) (números: "
                f"{', '.join(map(str, faltan[:10]))}). Un ranking incompleto "
                "escondería esas ofertas sin decirlo."
            )

        return items


def patrones_a_json(patrones: Sequence[str]) -> str:
    return json.dumps(list(patrones), ensure_ascii=False)


def patrones_desde_json(texto: str | None) -> list[str]:
    if not texto:
        return []
    try:
        datos = json.loads(texto)
    except json.JSONDecodeError:
        return []
    return [str(p) for p in datos] if isinstance(datos, list) else []
