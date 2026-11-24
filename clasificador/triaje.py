"""
El triaje: a cuáles se les presta atención completa.

QUÉ PROBLEMA RESUELVE, Y NO ES EL QUE PARECE
--------------------------------------------
Parece un ahorro. Lo importante es otra cosa: sin esto, la factura del mes la
decide **cuántas vacantes publique el mercado**, y eso no lo controla nadie.
Medido el 2 septiembre 2026: entre 20 y 30 ofertas nuevas al día tras los
filtros, ~$9,40 al mes — y meter `manager` a la lista blanca, que es una línea
de YAML que está esperando, serían +195 ofertas de golpe.

Una cifra así no se puede poner en una recarga automática ni prometer en un
presupuesto. Con el triaje, el que manda es `llm.triaje.ofertas_por_corrida`,
que es un número nuestro.

CÓMO ENCAJA
-----------
    1. Filtros duros          gratis    antigüedad, geografía, listas de títulos
    2. Agrupación laxa        gratis    cinco anuncios de lo mismo son uno
    3. TRIAJE                 ~$0,03    UNA llamada sobre TODO lo pendiente
    4. Clasificación          ~$0,011   solo las N que salieron arriba
    5. Asistente              ~$0,05    UNA llamada, ordena lo clasificado

El triaje ve el lote entero de una sentada y solo METADATOS: título, empresa,
ubicación, sueldo si lo hay, y cuántos anuncios tiene la misma vacante. No ve
descripciones — eso es lo que lo hace costar ~70 tokens por oferta en vez de
~5.400.

LO QUE NO HACE: DESCARTAR
-------------------------
Las que no entran en el corte NO se descartan ni se esconden. Se guardan en
`triajes` con su posición y su motivo, siguen enteras en la bandeja, y siguen
PENDIENTES: sin fila en `clasificaciones`, así que el día que haya presupuesto
o que ella pulse el botón, se clasifican sin que nadie tenga que acordarse.

Esa distinción es la razón de ser de este módulo, igual que en
`presupuesto.py`. «El asistente la vio y la dejó para después, en el puesto 34»
y «nadie miró nunca esta oferta» tienen que poder distinguirse. Si acaban
viéndose igual, hemos reconstruido el filtro silencioso con pasos de más.

POR QUÉ EL MODELO ORDENA Y PYTHON CORTA
---------------------------------------
Al modelo se le pide que ordene y que diga cuáles merecen lectura; el corte en
N lo hace Python. Si el corte lo decidiera el modelo, N dejaría de ser una
promesa: un día diría que veinte merecen lectura y la factura se dispararía,
que es justo lo que este módulo viene a impedir.

Y al revés también: N es un TECHO, no una cuota. Si el modelo dice que solo
tres del lote merecen lectura, se leen tres. Rellenar hasta ocho sería gastar
por gastar.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

from pydantic import BaseModel, Field

from .cliente_llm import (
    Cabida,
    ClienteTriador,
    cabe_la_respuesta,
    coste_de,
    coste_medido,
)
from .fichas import Preferencias


class OfertaTriada(BaseModel):
    """Una oferta con su sitio en la cola de atención."""

    numero: int = Field(
        description=(
            "El número que tiene la oferta en la lista de arriba. Úsalo tal cual: "
            "es la única forma de saber de cuál hablas."
        )
    )
    merece_lectura: bool = Field(
        description=(
            "¿Vale la pena gastar una lectura completa del anuncio en esta? "
            "Di que no cuando el título ya basta para saber que no es lo suyo. "
            "No hay ninguna cuota que rellenar: si de veinte solo tres lo "
            "merecen, marca tres."
        )
    )
    por_que: str = Field(
        description=(
            "Media frase, para ella, diciendo qué te hizo ponerla ahí. "
            "'Operadora de agua, y publica en cinco ciudades' sirve; "
            "'parece interesante' no."
        )
    )


class Triaje(BaseModel):
    """Lo que devuelve el modelo por CADA lote, en una sola llamada."""

    ofertas: list[OfertaTriada] = Field(
        description=(
            "TODAS las ofertas de la lista, de más a menos prometedora. Ninguna "
            "se queda fuera: lo que no promete va al final con "
            "merece_lectura=false, pero sigue estando."
        )
    )
    nota: str = Field(
        description=(
            "Una frase sobre el lote: qué tipo de ofertas trajo y si vale poco. "
            "Si el lote es flojo, dilo — inflarlo hace que se lean anuncios "
            "malos con detalle."
        )
    )


class TriajeInvalidoError(Exception):
    """El modelo devolvió números que no corresponden con la lista enviada."""


@dataclass
class ResultadoTriaje:
    """Lo que sale de una corrida del triaje."""

    nota: str = ""
    # (dedupe_key, posicion, merece_lectura, por_que), ya en orden
    items: list[tuple[str, int, bool, str]] = field(default_factory=list)
    consideradas: int = 0
    # Vacantes que el tope de lote dejó fuera de ESTA llamada. No están
    # descartadas ni aplazadas: no se les escribió fila en `triajes`, así que
    # conservan su turno entero y entran en la corrida siguiente. Se cuenta
    # para poder decirlo en pantalla — «no entró esta vez» y «la vio y la dejó
    # para después» son dos estados distintos y no pueden leerse igual.
    fuera_del_tope: int = 0
    tokens_entrada: int = 0
    # Lo que la estimación dijo ANTES de llamar. Se conserva junto a lo medido
    # porque la pregunta útil no es «cuánto costó» sino «cuánto se desvió de lo
    # que se le enseñó a la clienta», y esa no se puede responder después si
    # solo se guardó una de las dos cifras.
    salida_prevista: int = 0
    coste_previsto: float = 0.0
    tokens_salida: int = 0
    modelo: str = ""
    error: str | None = None
    coste_medido_usd: float | None = None

    # POR QUÉ NO SE HIZO, y no solo que no se hizo.
    #
    # Todos los finales sin triaje acababan en el mismo código de salida —el 3,
    # «no había saldo»— y la bandeja los pintaba a todos como «sin saldo».
    # Así, un lote que no cabe en la llamada, una API key ausente y un mes
    # agotado se le presentan a la clienta con la misma frase, y solo una de
    # las tres es verdad. Es el mismo pecado que el módulo entero viene a
    # evitar, cometido en la pantalla en vez de en la base.
    #
    #   None           salió bien
    #   "vacio"        no había nada nuevo que triar. NO es un fallo
    #   "no_cabe"      el lote no entra en `max_tokens`. Ver `cabida()`
    #   "presupuesto"  no queda saldo este mes
    #   "modelo"       la API falló o devolvió algo que no encaja
    causa: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def coste_usd(self) -> float:
        if self.coste_medido_usd is not None:
            return self.coste_medido_usd
        return coste_de(self.tokens_entrada, self.tokens_salida)

    def claves_con_hermanas(self, cuantas: int, grupos: Any) -> list[str]:
        """Las claves a clasificar: las elegidas MÁS sus anuncios hermanos.

        Un hueco del triaje compra la vacante entera, no un anuncio suelto.
        Los hermanos no cuestan nada —heredan el veredicto del representante—
        y dejarlos fuera haría que la misma vacante volviera a competir mañana
        desde otra ciudad.
        """
        elegidas = set(self.seleccionadas(cuantas))
        claves: list[str] = []
        for representante, companeras in grupos:
            if representante["dedupe_key"] in elegidas:
                claves.append(representante["dedupe_key"])
                claves.extend(c["dedupe_key"] for c in companeras)
        return claves

    def seleccionadas(self, cuantas: int) -> list[str]:
        """Las `dedupe_key` que se van a leer enteras.

        El corte se hace AQUÍ y no en el modelo. Y se cruzan las dos
        condiciones: estar arriba del todo Y que el modelo dijera que merece
        lectura. Un lote flojo gasta menos, no gasta lo mismo peor.
        """
        merecen = [clave for clave, _, merece, _ in self.items if merece]
        return merecen[:cuantas]


# ---------------------------------------------------------------------------
#  El mensaje
# ---------------------------------------------------------------------------

def construir_sistema(preferencias: Preferencias, cuantas: int) -> str:
    """El bloque estable: qué se decide aquí y con qué criterio.

    Los criterios de ella entran TAL CUAL, igual que en el asistente: son las
    mismas prioridades, aplicadas antes y con menos información.
    """
    if preferencias.hay_criterios:
        criterios = "\n".join(f"{i}. {c}" for i, c in enumerate(preferencias.criterios, 1))
        bloque = (
            "Sus criterios, con sus palabras:\n\n"
            f"{criterios}\n\n"
            "Cuando dos se contradigan, manda el que esté más arriba."
        )
    else:
        bloque = (
            "No ha declarado criterios propios. Prioriza el encaje con un perfil "
            "ejecutivo de logística, manufactura y operaciones."
        )

    return f"""\
Ayudas a una candidata ejecutiva a repartir la atención de su asistente.

Abajo tienes las ofertas que entraron hoy. Otra parte del sistema leerá A
FONDO unas {cuantas}, con la descripción entera, y escribirá un veredicto
razonado de cada una. Tu trabajo es decidir CUÁLES.

{bloque}

## Lo primero, porque cambia todo lo demás

NO estás descartando nada. Las que no elijas siguen enteras en su bandeja, con
su título, su empresa y su sueldo, y a un clic de que las lea el asistente si
a ella le llaman la atención. Estás decidiendo el ORDEN de la atención, no qué
existe.

Eso te quita el miedo a dejar una fuera, pero no te da derecho a ser
descuidado: lo que mandes al final es lo que ella probablemente no mire.

## Solo ves metadatos, y el título miente

No tienes la descripción. Tienes título, empresa, ubicación, sueldo si lo hay
y cuántos anuncios tiene la misma vacante. Con eso basta para ordenar, pero
tienes que saber cómo falla, porque en este proyecto ya falló las dos veces:

- «Vice President, Logistics and Security», sueldo de 200-250 mil. Título
  inmejorable. Al leerla, era ciberseguridad en una fintech: servidores, no
  logística física. **Esta MERECÍA lectura** — solo leyéndola se sabía.

- «Gerente corporativo». Un título que no dice nada y que no está en ninguna
  de sus listas. Al leerla, dirigía varias áreas y respondía de la continuidad
  operativa del grupo. Era su puesto. **Esta también merecía lectura**, y una
  lista de títulos la habría enterrado.

La regla que sale de ahí: **prioriza donde leer cambie la respuesta.** Un
título ambiguo, raro o demasiado bueno vale más lectura que uno mediocre y
claro. Lo que no merece lectura es lo que ya se sabe por el título: una
dirección de otra función, otro sector, o un nivel que no es el suyo.

Y un título modesto NO es motivo para mandarla al final. Su mejor hallazgo
hasta hoy se llamaba «Gerente corporativo».

## Señales que sí tienes

- **Cuántos anuncios.** La misma vacante publicada en cinco ciudades a la vez
  se está buscando de verdad. Es una señal para subirla.
- **El sueldo**, cuando viene. Ojo: muchas cifras son estimaciones del portal,
  no del anuncio. Vale como indicio de nivel, no como dato.
- **La empresa.** A qué se dedica es la mitad de la decisión, y a veces el
  nombre ya lo dice.

## Cómo eliges cuántas

Devuelve TODAS ordenadas, y marca `merece_lectura` con honestidad. No hay
cuota que rellenar: si de treinta ofertas solo cuatro merecen que se lea la
descripción, marca cuatro. Leer con detalle un anuncio que ya sabías que no
servía no le aporta nada a ella y gasta su presupuesto.

Si el lote entero es flojo, dilo en `nota`.
"""


def _sueldo(fila: Any) -> str:
    """El sueldo, si lo hay, en una forma corta.

    Se dice cuándo es una ESTIMACIÓN del portal: en la corrida real, 474 de
    las 670 cifras lo eran, y presentarlas como dato del anuncio haría que el
    modelo ordenara por un número inventado.
    """
    try:
        minimo = fila["salary_min"]
    except (IndexError, KeyError):
        return ""
    if not minimo:
        return ""
    maximo = fila["salary_max"] or minimo
    divisa = fila["salary_currency"] or ""
    try:
        estimado = " est." if fila["salary_is_predicted"] else ""
    except (IndexError, KeyError):
        estimado = ""
    return f"  ·  {int(minimo):,}-{int(maximo):,} {divisa}{estimado}"


def construir_lote(filas: Sequence[sqlite3.Row], anuncios: dict[str, int]) -> str:
    """El bloque variable: los metadatos, numerados del 1 al N.

    NÚMEROS, NO HASHES. Igual que en el asistente: pedirle que copie sesenta
    claves de 16 caracteres sin equivocarse es regalar un modo de fallo.
    Python traduce de vuelta y valida que estén todas.
    """
    lineas: list[str] = []
    for numero, fila in enumerate(filas, 1):
        repetida = anuncios.get(fila["dedupe_key"], 1)
        extra = f"  [publicada en {repetida} sitios]" if repetida > 1 else ""
        lineas.append(
            f"{numero}. {fila['title']}\n"
            f"   {fila['company']}  ·  {fila['market_key']}"
            f"  ·  {fila['location_raw'] or 'sin ubicación'}"
            f"{_sueldo(fila)}{extra}"
        )
    return "\n".join(lineas) + "\n\nOrdena estas ofertas por dónde merece la pena leer."


# ---------------------------------------------------------------------------
#  El orquestador
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  QUÉ LOTE SE MANDA — la agrupación y el tope, en un solo sitio
# ---------------------------------------------------------------------------
#
#  SE TRÍAN VACANTES, NO ANUNCIOS. Los tres anuncios de «Site Superintendent»
#  de SmartTech en tres ciudades son un solo puesto: si entran por separado, el
#  modelo puede gastar dos de los ocho huecos en el mismo trabajo. Medido: pasó
#  en la primera corrida real. Se reusa el agrupador del motor, que ya sabe
#  hacer esto y es gratis; después el motor vuelve a agrupar y los anuncios
#  hermanos heredan el veredicto sin coste, así que un hueco compra la vacante
#  entera.
#
#  Y HAY UN TOPE, QUE ES LO QUE HACE QUE EL CASO MALO NO LLEGUE A DARSE.
#  `cabida()` se niega a mandar un lote que no entra en `max_tokens`, y eso
#  está bien —negarse cuesta cero— pero deja la app parada: el 3 septiembre
#  2026 había 273 vacantes sin triar, no cabían en una llamada, y el botón de
#  la bandeja devolvía un 409 corrida tras corrida sin forma de salir. La
#  guarda es la mitad de la solución; esta función es la otra.
#
#  DÓNDE SE APLICA EL TOPE, QUE ES TODO EL DISEÑO. Aquí: DESPUÉS de que quien
#  llama haya quitado las que ya tuvieron su turno (`claves_ya_triadas`) y
#  DESPUÉS de agrupar. Por eso la cola drena de forma monótona — cada corrida
#  se lleva N vacantes que no vuelven a entrar.
#
#  No se puede hacer con `--limite N`, y esa fue la conclusión de una
#  simulación de 25 corridas contra la base real, sin red. `ofertas_pendientes`
#  ordena por fecha DESCENDENTE y aplica el `LIMIT` sobre ANUNCIOS pendientes,
#  antes de quitar las ya triadas y antes de agrupar. Con `--limite 80`:
#
#      corrida 1: de los 80 anuncios más recientes, 29 ya estaban triados y 7
#                 son hermanos -> 44 vacantes nuevas, no las ~75 que parecían;
#      corrida 2: 12;
#      corrida 3 en adelante: entre 4 y 10, y casi siempre 8.
#
#  Ese 8 es el mecanismo entero. La ventana de 80 anuncios se satura de ya
#  triados, así que lo único que abre hueco es que se lea una oferta y salga de
#  pendientes — y se leen 8. `--limite` acaba entregándole al triaje justo lo
#  que el clasificador consume, y con 8 vacantes y 8 huecos el triaje ni se
#  dispara: la corrida dice «caben todas». Mientras, entran ~25 al día MÁS
#  RECIENTES que se ponen delante. La cola crece ~17 diarias y el atraso viejo
#  no lo mira nadie: tras 25 corridas simuladas, y sin contar una sola llegada
#  nueva, seguían 82 vacantes sin triar.
#
#  (Aquí ponía «de la corrida 3 en adelante, 0» y «se vara en 162 pendientes».
#  Misma conclusión, peor medida: ni cae a cero ni son 162. Se deja escrito
#  porque el mecanismo importa más que el titular — lo que mata a `--limite` no
#  es que se pare, es que SE SINCRONIZA con el ritmo de lectura.)
#
#  LO QUE NO ENTRA NO SE DESCARTA, Y ESTA VEZ NI SIQUIERA SE APLAZA. No se
#  escribe nada en `triajes` —eso solo pasa con lo que el modelo llegó a ver—,
#  así que las que se quedan fuera conservan su turno ENTERO y entran en la
#  corrida siguiente como si nada hubiera pasado. Siguen en el cajón «nadie las
#  ha mirado», que es la verdad. Es la regla nº 1 de la casa en el sitio donde
#  más fácil sería romperla: aquí es donde aparece la tentación de decir «y las
#  que no entraron las descartamos».
def preparar_lote(
    filas: Sequence[sqlite3.Row],
    tope: int | None = None,
) -> tuple[list[tuple[Any, list]], int]:
    """Los grupos que se mandan en ESTA llamada, y cuántas vacantes se quedan.

    No llama a nada y no toca la base. Es pura a propósito: la consola la usa
    para enseñar el número antes de gastar, `plan.py` para describir la corrida
    y `Triador.triar` para hacerla. Tres sitios, un solo criterio de qué entra
    — que es la regla nº 9 de la casa aplicada a la composición del lote y no
    solo a su precio.
    """
    # Se importa aquí y no arriba porque `agrupar_para_ahorrar` vive en
    # `motor.py` —es donde se usó primero— y no hace falta atar los dos módulos
    # en tiempo de importación por una función pura.
    from .motor import agrupar_para_ahorrar

    grupos = agrupar_para_ahorrar(filas)
    if tope is not None and len(grupos) > tope:
        return grupos[:tope], len(grupos) - tope
    return grupos, 0


def rescatar_sin_leer(
    filas: Sequence[sqlite3.Row],
    elegidas_sin_leer: set[str],
    cuantas: int,
) -> tuple[list[tuple[Any, list]], int]:
    """Las que el triaje eligió y no llegó a leer, y los huecos que sobran.

    LO QUE ARREGLA. El triaje apunta en `triajes` TODAS las vacantes del lote
    antes de que se lea ninguna. Si la corrida se corta a mitad —«Parar», el
    tope por acción, la ventana cerrada— las elegidas que no se leyeron
    quedan con su fila puesta, y `claves_ya_triadas` las esconde de todas las
    corridas siguientes. Medido el 9 septiembre 2026: seis vacantes muertas
    ahí, con el plan diciendo «hay_trabajo: False» y la corrida habiéndole
    prometido por escrito que la próxima las recogería.

    ENTRAN PRIMERO Y NO VUELVEN AL TRIAJE. Su turno se gastó y su selección
    se pagó; lo que faltó fue la lectura. Devolverlas al sorteo sería poder
    perderlo otra vez, y encima haría que el coste del triaje dejara de ser
    fijo. Ver `Storage.claves_elegidas_sin_leer`.

    OCUPAN HUECO, NO SE SUMAN. Una corrida lee hasta `cuantas` vacantes y
    todo el modelo de coste cuelga de eso. Si los rescates se sumaran a las
    del triaje, una corrida detrás de una interrumpida costaría el doble de
    lo que su botón anuncia. Así que se sirven primero y lo que quede de
    `cuantas` es lo que el triaje puede llenar.

    No llama a nada y no toca la base: la usan igual el plan y la corrida,
    que es la regla nº 9 de la casa.
    """
    if not elegidas_sin_leer:
        return [], cuantas
    candidatas = [f for f in filas if f["dedupe_key"] in elegidas_sin_leer]
    if not candidatas:
        return [], cuantas
    # El tope es `cuantas` VACANTES, no anuncios: agrupar primero es lo que
    # hace que una vacante de diez anuncios ocupe un hueco y no diez.
    grupos, _ = preparar_lote(candidatas, cuantas)
    return grupos, max(0, cuantas - len(grupos))


def claves_de(grupos: Sequence[tuple[Any, list]]) -> set[str]:
    """Todas las `dedupe_key` de unos grupos: representantes y hermanas."""
    claves: set[str] = set()
    for representante, otras in grupos:
        claves.add(representante["dedupe_key"])
        claves.update(o["dedupe_key"] for o in otras)
    return claves


class Triador:
    """Una llamada, todo el lote. Recibe el cliente por parámetro.

    Igual que `Motor` y `Asistente`: los tests le pasan un cliente falso y
    prueban el camino entero —construcción del mensaje, validación, corte en
    N y persistencia— sin red, sin API key y sin gastar un céntimo.
    """

    def __init__(
        self,
        almacen: Any,
        preferencias: Preferencias,
        cliente: ClienteTriador | None,
        cuantas: int,
        presupuesto: Any | None = None,
        esfuerzo: str | None = None,
        tope_lote: int | None = None,
    ) -> None:
        self.almacen = almacen
        self.cliente = cliente
        self.cuantas = cuantas
        self.presupuesto = presupuesto
        self.esfuerzo = esfuerzo
        # Cuántas vacantes como mucho entran en una llamada. `None` = sin tope,
        # que es lo que había antes y lo que dejaba la app bloqueada en cuanto
        # el atraso pasaba de ~228. Ver `preparar_lote`.
        self.tope_lote = tope_lote
        self.sistema = construir_sistema(preferencias, cuantas)
        # Los grupos de la última llamada: (representante, [hermanos]). Lo
        # rellena `triar` y lo lee `_guardar` para marcar la vacante entera.
        # Se inicializa aquí para que el atributo exista siempre.
        self._grupos: list[tuple[Any, list]] = []

    def triar(self, filas: Sequence[sqlite3.Row]) -> ResultadoTriaje:
        self._grupos, fuera = preparar_lote(filas, self.tope_lote)
        representantes = [rep for rep, _ in self._grupos]

        resultado = ResultadoTriaje(
            consideradas=len(representantes), fuera_del_tope=fuera
        )

        if not representantes:
            resultado.error = "No hay ofertas pendientes que triar."
            resultado.causa = "vacio"
            return resultado

        filas = representantes

        # Si caben todas, el triaje solo gastaría por gastar.
        if len(filas) <= self.cuantas:
            resultado.nota = (
                f"{len(filas)} oferta(s) pendientes y se leen {self.cuantas}: "
                "caben todas, no hace falta triar."
            )
            resultado.items = [
                (fila["dedupe_key"], i, True, "Cabe sin triaje.")
                for i, fila in enumerate(filas, 1)
            ]
            return resultado

        # El conteo sale de los GRUPOS, no de la lista de representantes: en
        # esa lista cada vacante aparece una sola vez, así que contar ahí daría
        # siempre 1 y se perdería la señal. Una vacante publicada en cinco
        # ciudades a la vez se está buscando de verdad.
        anuncios = {
            rep["dedupe_key"]: 1 + len(companeras)
            for rep, companeras in self._grupos
        }
        lote = construir_lote(filas, anuncios)

        # SE PIDE PERMISO POR LO QUE VA A COSTAR, NO POR UNA CIFRA FIJA.
        #
        # Aquí había un 0,05 escrito a mano. Es la cifra de un lote pequeño, y
        # el triaje no tiene un tamaño fijo: cuesta ~$0,09 sobre 21 vacantes y
        # ~$0,88 sobre 228, que es el atraso que había en la base el 2
        # septiembre 2026. Pedirle permiso al techo por $0,05 y gastar $0,88 es
        # dieciocho veces el número que se enseñó, y deja de ser inofensivo en
        # cuanto la corrida la lanza ella desde la bandeja.
        #
        # ¿CABE LA RESPUESTA? Antes que el presupuesto, y a propósito: al techo
        # del mes esta llamada le cabría de sobra —son céntimos—, así que
        # preguntarle primero daría permiso para una llamada que se va a cortar
        # a media lista y se va a pagar entera. El orden es el que hace que la
        # comprobación sirva de algo.
        sitio = cabida(len(filas), self.esfuerzo)
        if not sitio.cabe:
            resultado.error = sitio.explicacion(len(filas))
            resultado.causa = "no_cabe"
            return resultado

        # `coste_estimado` crece con el lote igual que crece la factura, y ahora
        # sabe del esfuerzo: a `low` la salida son ~50 tokens por vacante y no
        # los 140 de `high`, que es lo que se cobraba de más.
        _, salida_prevista, coste_previsto = coste_estimado(len(filas), self.esfuerzo)
        resultado.salida_prevista = salida_prevista
        resultado.coste_previsto = coste_previsto
        if self.presupuesto is not None and not self.presupuesto.permite(coste_previsto):
            resultado.error = (
                f"No hay presupuesto para la llamada del triaje "
                f"(~${coste_previsto:.4f} sobre {len(filas)} vacantes). "
                "Las ofertas siguen pendientes."
            )
            resultado.causa = "presupuesto"
            return resultado

        assert self.cliente is not None
        respuesta = self.cliente.triar(self.sistema, lote, self.esfuerzo)

        resultado.tokens_entrada = respuesta.tokens_entrada
        resultado.tokens_salida = respuesta.tokens_salida
        resultado.modelo = respuesta.modelo
        resultado.coste_medido_usd = respuesta.coste_usd

        # EL GASTO LO APUNTA `ClienteAnthropic`, en cuanto la llamada vuelve y
        # sin mirar si la respuesta sirve — que es la regla que aquí estaba
        # escrita a mano. Ver `cliente_llm.py`: eran tres capas repitiéndola y
        # un script de medición que no la repitió.

        # Los TOKENS sí se apuntan aquí, y no es lo mismo: el libro de gastos
        # lleva dólares y esta tabla lleva lo previsto al lado de lo medido.
        # Van aquí y no en `_guardar` a propósito: `_guardar` solo corre si el
        # triaje sale bien, y una llamada que se pagó y devolvió basura es
        # justo la que hay que poder mirar después. Ver el comentario de la
        # tabla `triaje_corridas` en `storage.py`.
        if respuesta.tokens_entrada:
            self._apuntar_corrida(resultado)

        if not respuesta.ok or respuesta.ranking is None:
            resultado.error = respuesta.error or "El modelo no devolvió un triaje."
            resultado.causa = "modelo"
            return resultado

        try:
            resultado.items = self._traducir(respuesta.ranking, filas)
        except TriajeInvalidoError as exc:
            resultado.error = str(exc)
            resultado.causa = "modelo"
            return resultado

        resultado.nota = respuesta.ranking.nota
        self._guardar(resultado)
        return resultado

    # -- ayudas --------------------------------------------------------------

    def _traducir(
        self,
        triaje: Triaje,
        filas: Sequence[sqlite3.Row],
    ) -> list[tuple[str, int, bool, str]]:
        """Números del modelo -> claves de la base, comprobando por el camino.

        Mismo rigor que en el asistente, y por el mismo motivo: si el modelo
        repite un número o se salta uno, el triaje parecería completo y una
        oferta desaparecería de la cola sin que nadie lo notara. Aquí es peor
        todavía, porque lo que desaparece no llega ni a clasificarse.
        """
        total = len(filas)
        vistos: set[int] = set()
        items: list[tuple[str, int, bool, str]] = []

        for posicion, item in enumerate(triaje.ofertas, 1):
            if not 1 <= item.numero <= total:
                raise TriajeInvalidoError(
                    f"El modelo devolvió el número {item.numero} y solo se le "
                    f"mandaron {total} ofertas."
                )
            if item.numero in vistos:
                raise TriajeInvalidoError(
                    f"El modelo colocó la oferta {item.numero} dos veces."
                )
            vistos.add(item.numero)
            items.append(
                (
                    filas[item.numero - 1]["dedupe_key"],
                    posicion,
                    bool(item.merece_lectura),
                    item.por_que,
                )
            )

        faltan = sorted(set(range(1, total + 1)) - vistos)
        if faltan:
            raise TriajeInvalidoError(
                f"El modelo se dejó fuera {len(faltan)} oferta(s) (números: "
                f"{', '.join(map(str, faltan[:10]))}). Un triaje incompleto "
                "sacaría esas ofertas de la cola sin decirlo."
            )
        return items

    def _apuntar_corrida(self, resultado: ResultadoTriaje) -> None:
        """Lo que costó la llamada, para que la constante deje de desviarse a ciegas."""
        self.almacen.guardar_corrida_triaje(
            {
                "vacantes": resultado.consideradas,
                "fuera_del_tope": resultado.fuera_del_tope,
                "tokens_entrada": resultado.tokens_entrada,
                "tokens_salida": resultado.tokens_salida,
                "coste_usd": resultado.coste_usd,
                "salida_prevista": resultado.salida_prevista,
                "coste_previsto": resultado.coste_previsto,
                "modelo": resultado.modelo or "desconocido",
                "esfuerzo": self.esfuerzo,
            }
        )

    def _guardar(self, resultado: ResultadoTriaje) -> None:
        """Marca la vacante ENTERA: el representante y sus anuncios hermanos.

        MARCAR SOLO AL REPRESENTANTE ERA UN AGUJERO, y uno que se realimentaba.
        `claves_ya_triadas` no veía a los hermanos, así que volvían a entrar;
        pero el representante sí estaba excluido, de modo que la corrida
        siguiente ascendía a hermano a representante y mandaba **la misma
        vacante** otra vez. Medido sobre la base el 3 septiembre 2026: 29
        vacantes con más de una fila, 73 filas, 44 huecos de lote gastados en
        repetir (12,4%), y un caso que llegó a seis corridas seguidas.

        No se leyó nada dos veces —de eso protege `clasificaciones.heredada_de`,
        y hay cero vacantes con dos clasificaciones propias—, así que no costó
        lecturas. Lo que costaba era el sitio: cada repetición ocupaba un hueco
        del lote de 60 que le tocaba a una vacante que nadie había mirado.

        EL HERMANO NO SE MARCA COMO SI LO HUBIERA ORDENADO EL MODELO. Lleva
        `heredada_de` con la clave del que sí se mandó, que es lo que permite a
        la bandeja seguir distinguiendo los tres estados. Y nunca va
        `seleccionada`: la que se lee entera es la representante, y el hermano
        recibe su veredicto por herencia sin gastar una llamada.
        """
        elegidas = set(resultado.seleccionadas(self.cuantas))
        modelo = resultado.modelo or "desconocido"

        # `self._grupos` lo dejó puesto `triar` con lo que devolvió
        # `preparar_lote`: (representante, [anuncios hermanos]). Se indexa por
        # la clave del representante porque `resultado.items` viene en el orden
        # que decidió el modelo, no en el de los grupos.
        hermanos: dict[str, list[str]] = {
            rep["dedupe_key"]: [c["dedupe_key"] for c in companeras]
            for rep, companeras in self._grupos
        }

        filas: list[dict[str, Any]] = []
        for clave, posicion, merece, por_que in resultado.items:
            filas.append(
                {
                    "dedupe_key": clave,
                    "posicion": posicion,
                    "merece_lectura": int(merece),
                    "seleccionada": int(clave in elegidas),
                    "por_que": por_que,
                    "modelo": modelo,
                    "heredada_de": None,
                }
            )
            for hermana in hermanos.get(clave, []):
                filas.append(
                    {
                        "dedupe_key": hermana,
                        "posicion": posicion,
                        "merece_lectura": int(merece),
                        "seleccionada": 0,
                        "por_que": por_que,
                        "modelo": modelo,
                        "heredada_de": clave,
                    }
                )

        self.almacen.guardar_triaje(filas)


# ---------------------------------------------------------------------------
#  Lo que cuesta, antes de gastarlo
# ---------------------------------------------------------------------------
#
#  LA ENTRADA. MEDIDA el 3 septiembre 2026 con `count_tokens` —que no cobra
#  nada— mandando el esquema de salida y `thinking` exactamente como los manda
#  la llamada de verdad:
#
#      vacantes   tokens de entrada
#      ---------------------------
#             1               2.451
#            10               2.993
#            21               3.705
#            40               4.937
#            75               7.099
#           150              11.716
#           273              19.150
#
#  Ajuste por mínimos cuadrados sobre esos puntos: 2.447 + 61,46 por vacante,
#  con residuos de ±75 tokens hasta n=273 (±0,4% en el extremo). De ahí los dos
#  números de abajo.
#
#  LA CIFRA ANTERIOR ERA 800 + 70n, Y SE QUEDABA CORTA POR 1.581 TOKENS EN UN
#  LOTE PEQUEÑO. Dos causas, y las dos ya conocidas en este proyecto:
#
#    1. EL ESQUEMA VIAJA Y NO SE VE. `Triaje` ocupa 957 tokens que el SDK
#       serializa y manda en cada llamada, y que no aparecen en ningún string
#       de este repositorio. Es el MISMO error que documenta `cliente_llm.py`
#       sobre el clasificador, cometido por segunda vez en otro archivo.
#    2. EL PROMPT DE SISTEMA CRECIÓ. Los criterios de `preferencias` entran
#       tal cual, y al añadir las empresas objetivo el bloque fijo pasó de
#       ~870 a 1.419 tokens. Nadie volvió a medir.
#
#  Comprobación de que el método vale: el mismo `count_tokens` mide el prefijo
#  del clasificador en 5.115 tokens contra los 5.110 anotados en
#  `cliente_llm.py`, que se midieron contra el `usage` de una llamada real.
#  Cinco tokens de diferencia sobre cinco mil.
TOKENS_FIJOS = 2450
TOKENS_POR_OFERTA = 61.5

# El bloque que se sirve de caché: esquema + prompt de sistema, sin el lote.
# Medido igual, el mismo día: 2.376 tokens. Supera de largo el mínimo de 512
# tokens de Opus 5, así que SÍ cachea — y como el triaje es UNA llamada por
# corrida, siempre la escribe (x1,25) y nunca la lee. Está contado abajo
# porque es gasto real, aunque sean tres milésimas.
TOKENS_PREFIJO_CACHEADO = 2376


# ---------------------------------------------------------------------------
#  LA SALIDA, QUE ES EL 80% DEL COSTE Y DEPENDE DEL ESFUERZO
# ---------------------------------------------------------------------------
#  Aquí no vale `count_tokens`: mide la entrada. La salida solo se sabe después
#  de llamar, y la domina el PENSAMIENTO adaptativo, que se factura como salida
#  y no aparece en el esquema.
#
#  El estimador no sabía del esfuerzo y lo cobraba todo a 140 tokens por
#  vacante, que es la cifra de `high`. El triaje corre en `low`. Sobre 21
#  vacantes eso decía $0,0878 donde lo medido fueron $0,045: casi el doble. El
#  error caía del lado seguro, pero el número que se le enseña a la clienta
#  tiene que parecerse al que se le cobra.
#
#  De dónde sale cada fila, porque no todas valen lo mismo:
#
#    high (= el que aplica la API POR DEFECTO, verificado 3 sept 2026)
#        MEDIDO EN TOKENS. La corrida real del 2 septiembre 2026 devolvió
#        4.208 tokens de salida sobre 30 vacantes.
#    medium
#        DERIVADO de un coste medido ($0,092 sobre 25 vacantes, 2 sept 2026)
#        descontándole la entrada. Sujeto al redondeo de esa cifra.
#    low
#        DERIVADO igual, de DOS costes medidos el mismo día: $0,054 sobre 25
#        vacantes y $0,045 sobre 21. Dan 49,7 y 44,4 tokens por vacante — el
#        acuerdo entre dos lotes distintos es lo que da confianza en el ~50.
#
#  Que dos de las tres filas sean derivadas y no medidas es la razón de que
#  `cabida()` lleve su propio margen, y de que la corrida imprima ahora los
#  tokens reales: la primera corrida que se lance corrige esta tabla con datos.
#
#  El término fijo se midió indistinguible de cero (el ajuste de `low` da un
#  corte negativo, que es ruido), así que no se modela. Da igual en la
#  práctica: con menos vacantes que `ofertas_por_corrida` el triaje ni se lanza.
SALIDA_POR_OFERTA = {
    "low": 50,        # derivado de dos costes medidos
    "medium": 111,    # derivado de un coste medido
    "high": 140,      # MEDIDO en tokens. Es lo que aplica la API por defecto
    # `xhigh` y `max` existen en Opus 5 y este proyecto no los usa en ningún
    # sitio. Sin medir: se les pone la cifra de `high` como SUELO, sabiendo que
    # se quedará corta. Si alguien los pone en el YAML, el margen de `cabida()`
    # es lo único que separa de una respuesta cortada.
    "xhigh": 140,
    "max": 140,
}

# Lo que aplica la API si no se manda `output_config.effort`. Verificado el 3
# septiembre 2026: el defecto es `high`, no un punto medio.
ESFUERZO_POR_DEFECTO = "high"


def _salida_por_oferta(esfuerzo: str | None) -> int:
    """Tokens de salida por vacante al esfuerzo dado.

    Un esfuerzo desconocido cae en el más caro que conocemos, no en el más
    barato: equivocarse por arriba deja saldo de sobra, y equivocarse por abajo
    corta la respuesta a mitad del lote.
    """
    return SALIDA_POR_OFERTA.get(
        esfuerzo or ESFUERZO_POR_DEFECTO, SALIDA_POR_OFERTA["high"]
    )


def coste_estimado(
    cuantas_ofertas: int,
    esfuerzo: str | None = None,
) -> tuple[int, int, float]:
    """(entrada, salida, USD) de triar un lote de N vacantes.

    `esfuerzo` es el de `llm.triaje.esfuerzo`. Omitirlo asume el defecto de la
    API —`high`, el más caro—, que es el lado seguro para quien llame sin
    saberlo.

    El coste sale por `coste_medido` y no por la fórmula plana porque el
    prefijo SÍ cachea en Opus 5, y en una llamada suelta eso es un recargo del
    25% sobre 2.376 tokens, no un descuento. Modelarlo plano se quedaría corto
    — al revés de lo que suele pasar con las estimaciones.
    """
    entrada = int(TOKENS_FIJOS + TOKENS_POR_OFERTA * cuantas_ofertas)
    salida = _salida_por_oferta(esfuerzo) * cuantas_ofertas
    nueva = max(0, entrada - TOKENS_PREFIJO_CACHEADO)
    coste = coste_medido(
        entrada_nueva=nueva,
        cache_leida=0,
        cache_escrita=TOKENS_PREFIJO_CACHEADO,
        tokens_salida=salida,
    )
    return entrada, salida, coste


# ---------------------------------------------------------------------------
#  ¿CABE LA RESPUESTA? — el techo que nadie había elegido
# ---------------------------------------------------------------------------
#  El triaje devuelve una línea por vacante y piensa por el camino, y las dos
#  cosas se facturan como salida y cuentan dentro de `max_tokens`. Si el lote
#  es grande, la respuesta se corta: `stop_reason == "max_tokens"`, la llamada
#  se paga ENTERA, y no se tría ni una sola oferta.
#
#  Eso no era teórico. El 3 septiembre 2026 había 273 vacantes sin triar y el
#  plan las mandaba en una sola llamada, anunciando 38.340 tokens de salida
#  contra un `max_tokens` de 8.000 —cuatro veces y media el techo— y rematando
#  con «El plan CABE en lo que queda del mes». Cabía en el mes; no cabía en la
#  llamada.
#
#  `max_tokens` no es un control de gasto: se paga lo que el modelo escribe, y
#  lo que escribe lo decide el TAMAÑO DEL LOTE. Así que el límite se comprueba
#  aquí, antes de llamar, y lo que lo controla de verdad es cuántas vacantes se
#  mandan.
#
#  La cuenta y el margen viven en `cliente_llm.cabe_la_respuesta`, que es donde
#  está el techo: el ranking tiene exactamente el mismo modo de fallo y sería
#  absurdo escribir la misma aritmética dos veces. Aquí solo se aportan la
#  tabla de tokens por vacante y las dos palabras que cambian — qué se cuenta
#  y qué se pierde.
def cabida(
    cuantas_ofertas: int,
    esfuerzo: str | None = None,
    techo: int | None = None,
) -> Cabida:
    """¿Entra en una llamada la respuesta de triar N vacantes?

    No llama a nada. `techo` es `max_tokens`; por defecto, el que usa el
    cliente de verdad.
    """
    return cabe_la_respuesta(
        cuantas_ofertas,
        _salida_por_oferta(esfuerzo),
        techo=techo,
        unidad='vacantes',
        consecuencia='y no se triaría ninguna oferta',
    )
