"""
La única frontera con el SDK de Anthropic.

Regla de la casa: **nada fuera de este archivo sabe que Anthropic existe.**
Igual que ningún módulo fuera de `recolector/connectors/` sabe que Adzuna
existe. Cambiar de proveedor debe ser escribir otro archivo aquí, no tocar el
motor, la bandeja ni la base de datos.

QUE FALLE RUIDOSAMENTE
----------------------
Este módulo no lanza excepciones hacia arriba salvo por errores de
programación. Devuelve un `RespuestaLLM` que o trae una clasificación válida,
o trae un `error` explicando qué pasó. El motor guarda ese error en la base y
la oferta queda como "sin clasificar", visible en la bandeja y en el resumen
de consola.

Lo que NUNCA hace: devolver una clasificación por defecto, elegir un `cv_id`
a boleo, o tragarse un fallo de la API y seguir como si nada. Una corrida en
la que la API estuvo caída tiene que verse.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic
from pydantic import TypeAdapter, ValidationError

from .esquema import Clasificacion

# ---------------------------------------------------------------------------
#  Modelo y precios
# ---------------------------------------------------------------------------
#  Decisión tomada al arrancar el proyecto y no se reabre: se usa el modelo
#  bueno.
#  A ~90 clasificaciones al mes, elegir mal una oferta le cuesta a la clienta
#  una oportunidad real; ahorrar unos céntimos no compensa eso.
MODELO = "claude-opus-5"

# USD por millón de tokens. Verificado el 27 agosto 2026.
# Están aquí, con fecha, y no ocultos en una fórmula, para que el día que
# cambien se vea qué había que actualizar. Mismo criterio que el tipo de
# cambio explícito de `search_profile.yaml`.
PRECIO_ENTRADA_USD_POR_MILLON = 5.00
PRECIO_SALIDA_USD_POR_MILLON = 25.00
PRECIOS_ACTUALIZADOS = "2026-09-03"   # revisados contra la tarifa vigente; sin cambio

# -- La caché de prompts no es un detalle: es el 90% de la entrada -----------
#
# El bloque `system` va marcado con `cache_control` (ver `_invocar`), así que
# a partir de la segunda llamada de una corrida la API no vuelve a cobrar esos
# tokens a tarifa completa: los sirve de caché a una décima parte. Escribir la
# entrada la primera vez cuesta un 25% de más.
#
# ESTO ESTUVO MAL CONTADO HASTA EL 2 SEPTIEMBRE 2026. La API devuelve la
# entrada partida en tres campos y aquí se sumaban los tres y se cobraban
# todos a $5/millón, que es la tarifa de UNO solo. Medido sobre una llamada
# real: de 5.400 tokens de entrada, 5.110 venían de caché. El coste apuntado
# era 3,25 veces el real.
#
# No es un error de redondeo: quien decide con este número es el techo de
# `presupuesto.py`. Con el gasto inflado, el techo para la corrida por dinero
# que nadie gastó y deja ofertas sin clasificar. Ver `coste_medido`.
MULTIPLICADOR_CACHE_LECTURA = 0.10
MULTIPLICADOR_CACHE_ESCRITURA = 1.25

# Techo de tokens de la respuesta. Con pensamiento adaptativo activado, lo que
# el modelo piensa cuenta dentro de este límite y se factura como salida.
# 4000 da margen de sobra para una clasificación; poner 256 (lo típico para
# clasificar) truncaría el razonamiento a la mitad.
MAX_TOKENS = 4000

# El ranking y el triaje devuelven una línea por oferta, así que su respuesta
# crece con el lote — y el PENSAMIENTO adaptativo cuenta dentro de este límite.
#
# ESTABA EN 8.000 Y ERA UN TECHO INVENTADO. Claude Opus 5 admite hasta 128.000
# tokens de salida (verificado 3 sept 2026). Lo que sí aconseja quedarse por
# debajo de ~16.000 es que estas llamadas NO van en streaming: por encima de
# ahí una respuesta larga puede agotar el tiempo de espera HTTP del SDK. Así
# que 16.000 es el máximo seguro sin reescribir la llamada, y ahí se pone.
#
# Subirlo NO afloja el control de gasto: se paga por los tokens que el modelo
# escribe, no por el techo, y quien decide si una llamada cabe en el mes sigue
# siendo `presupuesto.permite()` con la estimación de `triaje.coste_estimado`.
# Lo que hacía el 8.000 no era acotar la factura: era CORTAR la respuesta a
# mitad de un lote grande, cobrarla entera y no devolver ningún triaje.
#
# El límite de verdad —cuántas vacantes caben en una llamada— se comprueba
# antes de llamar, en `triaje.cabida()`.
MAX_TOKENS_RANKING = 16000



# ---------------------------------------------------------------------------
#  ¿CABE LA RESPUESTA EN UNA LLAMADA?
# ---------------------------------------------------------------------------
#  Vive aquí, y no en `triaje.py` ni en `asistente.py`, porque el techo es
#  `MAX_TOKENS_RANKING` y `MAX_TOKENS` — de este archivo. Las dos capas que
#  mandan un LOTE tienen el mismo modo de fallo: la respuesta crece con el
#  número de ofertas, el pensamiento se factura como salida y cuenta dentro de
#  `max_tokens`, y pasarse no da un error barato — la llamada se paga entera y
#  no devuelve nada aprovechable.
#
#  Cada capa trae su propia tabla de tokens por oferta (son muy distintas: ~50
#  en el triaje a `low`, ~188 en el ranking a `medium`), pero la cuenta y el
#  margen son los mismos y se escriben una vez.
#
#  EL MARGEN NO ES UN ADORNO. Buena parte de esas tablas está DERIVADA de
#  costes redondeados, no medida en tokens, y lo que el modelo piensa depende
#  de lo difícil que sea el lote y no solo de cuántas ofertas trae. Y el error
#  no es simétrico: negarse cuesta cero y no pierde ninguna oferta —siguen
#  pendientes—, mientras que pasarse cuesta la llamada entera a cambio de nada.
#  Se planifica para un lote que piense un 40% más que lo medido.
MARGEN_PENSAMIENTO = 1.4


@dataclass(frozen=True)
class Cabida:
    """Si la respuesta de un lote entra en `max_tokens`, y cuántas entrarían."""

    cabe: bool
    salida_prevista: int
    salida_con_margen: int
    techo: int
    caben: int              # cuántas admitiría el techo, con el margen
    unidad: str = "ofertas"
    consecuencia: str = "y no se devolvería nada"

    def explicacion(self, cuantas: int) -> str:
        """El motivo, con el número accionable dentro.

        Se redacta aquí porque lo dicen la consola, el plan y la bandeja, y
        tres redacciones del mismo número acaban divergiendo. Lo que cada capa
        aporta son sus dos palabras: qué cuenta y qué se pierde.
        """
        return (
            f"{cuantas} {self.unidad} no caben en una sola llamada: la respuesta "
            f"ocuparía ~{self.salida_prevista:,} tokens de salida "
            f"(~{self.salida_con_margen:,} contando margen) y el techo son "
            f"{self.techo:,}. Se cortaría a media lista, se pagaría entera "
            f"{self.consecuencia}. Caben ~{self.caben} {self.unidad} por llamada."
        )


def cabe_la_respuesta(
    cuantas: int,
    salida_por_oferta: int,
    techo: int | None = None,
    unidad: str = "ofertas",
    consecuencia: str = "y no se devolvería nada",
) -> Cabida:
    """La cuenta, sin llamar a nada. `techo` por defecto: `MAX_TOKENS_RANKING`."""
    if techo is None:
        techo = MAX_TOKENS_RANKING

    prevista = salida_por_oferta * cuantas
    con_margen = int(prevista * MARGEN_PENSAMIENTO)
    return Cabida(
        cabe=con_margen <= techo,
        salida_prevista=prevista,
        salida_con_margen=con_margen,
        techo=techo,
        caben=max(1, int(techo / (salida_por_oferta * MARGEN_PENSAMIENTO))),
        unidad=unidad,
        consecuencia=consecuencia,
    )


@dataclass(frozen=True)
class RespuestaLLM:
    """Lo que devuelve una llamada. O clasificación, o error. Nunca las dos."""

    clasificacion: Clasificacion | None
    tokens_entrada: int
    tokens_salida: int
    modelo: str
    error: str | None = None

    # El coste REAL, con el desglose de caché, cuando quien respondió fue la
    # API de verdad. `None` significa "no hay desglose" —un cliente falso de
    # los tests, o una respuesta de error— y entonces se cae a la fórmula
    # plana. Es un campo y no una propiedad porque el desglose solo existe en
    # el instante de la llamada: `tokens_entrada` ya lo perdió al sumarlo.
    coste_medido_usd: float | None = None

    @property
    def ok(self) -> bool:
        return self.clasificacion is not None

    @property
    def coste_usd(self) -> float:
        if self.coste_medido_usd is not None:
            return self.coste_medido_usd
        return coste_de(self.tokens_entrada, self.tokens_salida)


def coste_de(tokens_entrada: int, tokens_salida: int) -> float:
    """Coste en USD SIN desglose de caché. Es la fórmula de las ESTIMACIONES.

    Sigue existiendo, y no se ha corregido, porque responde a otra pregunta:
    `--dry-run` no ha llamado a nadie todavía, así que no sabe cuánto vendrá
    de caché. Ahí se cobra todo a tarifa plena a propósito — una estimación
    que se pasa por arriba deja a la clienta con saldo de sobra, y una que se
    queda corta la deja a media corrida.

    Para lo YA gastado no vale: usa `coste_medido`, que sí tiene el desglose.
    """
    return (
        tokens_entrada / 1_000_000 * PRECIO_ENTRADA_USD_POR_MILLON
        + tokens_salida / 1_000_000 * PRECIO_SALIDA_USD_POR_MILLON
    )


def coste_medido(
    entrada_nueva: int,
    cache_leida: int,
    cache_escrita: int,
    tokens_salida: int,
) -> float:
    """Coste REAL de una llamada que ya se hizo, con los tres precios distintos.

    Los tres campos vienen de `usage` y cada uno se cobra distinto:

        entrada_nueva  tarifa completa   lo que no estaba en caché
        cache_leida    x0,10             el prompt de sistema, ya cacheado
        cache_escrita  x1,25             la primera llamada, que llena la caché

    Sumarlos y cobrarlos todos a tarifa completa —que es lo que se hacía—
    infla el gasto por 3 y hace que el techo del mes se dispare antes de
    tiempo. Este es el número que se apunta en el libro de gastos.
    """
    entrada = (
        entrada_nueva
        + cache_leida * MULTIPLICADOR_CACHE_LECTURA
        + cache_escrita * MULTIPLICADOR_CACHE_ESCRITURA
    )
    return (
        entrada / 1_000_000 * PRECIO_ENTRADA_USD_POR_MILLON
        + tokens_salida / 1_000_000 * PRECIO_SALIDA_USD_POR_MILLON
    )


@dataclass(frozen=True)
class RespuestaRanking:
    """Lo que devuelve la llamada del asistente. O ranking, o error."""

    ranking: Any | None
    tokens_entrada: int
    tokens_salida: int
    modelo: str
    error: str | None = None

    # El coste REAL, con el desglose de caché, cuando quien respondió fue la
    # API de verdad. `None` significa "no hay desglose" —un cliente falso de
    # los tests, o una respuesta de error— y entonces se cae a la fórmula
    # plana. Es un campo y no una propiedad porque el desglose solo existe en
    # el instante de la llamada: `tokens_entrada` ya lo perdió al sumarlo.
    coste_medido_usd: float | None = None

    @property
    def ok(self) -> bool:
        return self.ranking is not None

    @property
    def coste_usd(self) -> float:
        if self.coste_medido_usd is not None:
            return self.coste_medido_usd
        return coste_de(self.tokens_entrada, self.tokens_salida)


class ClienteOrdenador(Protocol):
    """El contrato que ve el asistente. Ver `ClienteClasificador`."""

    def ordenar(
        self, sistema: str, lote: str, esfuerzo: str | None = None
    ) -> RespuestaRanking: ...


class ClienteTriador(Protocol):
    """El contrato que ve el triaje. Ver `ClienteClasificador`."""

    def triar(
        self, sistema: str, lote: str, esfuerzo: str | None = None
    ) -> RespuestaRanking: ...


# Cómo se reconoce «la cuenta no tiene saldo» dentro de un 400.
#
# La API devuelve el mismo tipo de error —`BadRequestError`— para una cuenta
# sin crédito y para un parámetro mal puesto, y lo único que los separa es el
# texto. Se busca en minúsculas y por dos trozos que van juntos en ese mensaje,
# no por la frase entera: si mañana le cambian la puntuación, esto tiene que
# seguir reconociéndolo. Y si deja de reconocerlo, no se rompe nada: se cae al
# camino de abajo, que enseña el mensaje de la API tal cual.
_SENALES_DE_SIN_SALDO = ("credit balance", "too low")


def mensaje_de_peticion_rechazada(mensaje: str) -> str:
    """El texto de un 400 de la API, en castellano cuando se sabe cuál es.

    POR QUÉ ESTO NO ES COSMÉTICA. Este texto no se queda en el log: se guarda
    en `clasificaciones.error` y la bandeja lo pinta tal cual en el panel del
    trabajo, así que es literalmente lo que ella lee cuando algo no sale. El
    mensaje real de una cuenta sin crédito es «Your credit balance is too low
    to access the Anthropic API. Please go to Plans & Billing to upgrade or
    purchase credits», y una ejecutiva mexicana leyendo eso en inglés en mitad
    de su bandeja no sabe si ha roto algo, si ha perdido ofertas o si le acaban
    de cobrar. Las tres respuestas son que no, y caben en dos frases.

    Va aparte de `_invocar` para poder probarla sin fabricar una excepción del
    SDK, que exige montar una respuesta HTTP falsa entera.
    """
    texto = (mensaje or "").lower()
    if all(senal in texto for senal in _SENALES_DE_SIN_SALDO):
        return (
            "La cuenta de Anthropic se ha quedado sin saldo. No se ha leído "
            "nada y no se ha cobrado nada: la oferta conserva su turno y "
            "vuelve a estar disponible en cuanto se recargue la cuenta."
        )
    return f"La API rechazó la petición: {mensaje}"


# ---------------------------------------------------------------------------
#  El esquema que se manda, y la validación de lo que vuelve
# ---------------------------------------------------------------------------
#  LAS DOS MITADES DE LO QUE HACÍA `messages.parse`, SEPARADAS A PROPÓSITO.
#  `parse` mandaba el esquema y validaba la respuesta en la misma llamada, y
#  por eso un fallo de validación se llevaba por delante el apunte del gasto
#  (ver el comentario dentro de `_invocar`). Aquí van sueltas: `_esquema_json`
#  prepara lo que se manda, `_validar` comprueba lo que vuelve, y entre las
#  dos cabe la línea que apunta lo que costó.

try:
    # LA MISMA TRANSFORMACIÓN QUE HACE EL SDK, y no una copia escrita a mano.
    # Convierte el esquema de Pydantic en el dialecto que acepta la API
    # —mete los límites en la descripción, marca los `string` con su formato—
    # y ya estaba corriendo antes de este cambio, dentro de `parse`. Usarla es
    # mandar EXACTAMENTE los mismos bytes que se mandaban ayer.
    #
    # Es un módulo interno del SDK y puede desaparecer en una versión futura.
    # Si desaparece, no se rompe en silencio: se manda el esquema crudo y la
    # API contesta 400, que se ve en la pantalla y deja la oferta pendiente.
    # `test_el_esquema_es_el_que_manda_el_sdk` lo vigila desde las pruebas.
    from anthropic.lib._parse._transform import transform_schema as _transformar
except ImportError:  # pragma: no cover - red de seguridad, no camino normal
    def _transformar(esquema: Any) -> Any:
        return esquema


_ESQUEMAS_EN_CACHE: dict[Any, dict[str, Any]] = {}


def _esquema_json(modelo: Any) -> dict[str, Any]:
    """El esquema que viaja en `output_config.format`, calculado una vez.

    Se cachea por modelo porque son dos —`Clasificacion` y los dos de lote— y
    la transformación recorre el árbol entero: hacerla en cada llamada sería
    trabajo repetido en el bucle que más veces se ejecuta.
    """
    if modelo not in _ESQUEMAS_EN_CACHE:
        _ESQUEMAS_EN_CACHE[modelo] = _transformar(modelo)
    return _ESQUEMAS_EN_CACHE[modelo]


def _validar(respuesta: Any, modelo: Any) -> tuple[Any | None, str | None]:
    """Comprueba la respuesta contra el esquema. Nunca lanza.

    Devuelve `(objeto, None)` o `(None, frase para ella)`. Que no lance es el
    punto entero: esto corre DESPUÉS de haber apuntado el gasto, y una
    excepción aquí volvería a saltarse todo lo que venga detrás.

    La frase dice que la llamada sí se contó. Es la diferencia entre «no ha
    pasado nada» y «esto ha costado dinero y no ha servido», y ella tiene
    derecho a saber cuál de las dos es cuando mire la cuenta del mes.
    """
    texto = "".join(
        bloque.text
        for bloque in (respuesta.content or [])
        if getattr(bloque, "type", None) == "text"
    )
    if not texto.strip():
        return None, (
            "La API contestó sin texto. No se ha guardado nada y la oferta "
            "sigue pendiente; la llamada sí se ha contado."
        )

    try:
        return TypeAdapter(modelo).validate_json(texto), None
    except ValidationError as exc:
        tipos = {error.get("type") for error in exc.errors()}
        detalle = (
            "no era JSON válido"
            if "json_invalid" in tipos
            else f"{exc.error_count()} campo(s) no encajan"
        )
        return None, (
            f"La API respondió algo que no se puede usar: {detalle}. No se ha "
            "guardado nada y la oferta sigue pendiente; la llamada sí se ha "
            "contado."
        )


class ClienteClasificador(Protocol):
    """El contrato que ve el motor.

    Nota de Python: un `Protocol` es lo que en Java sería una interfaz, pero
    sin `implements`: cualquier clase con este método encaja. Existe para que
    los tests puedan pasar un cliente falso sin red ni API key, y para que el
    motor no importe `anthropic` ni de refilón.
    """

    def clasificar(self, sistema: str, oferta: str) -> RespuestaLLM: ...


class ClienteAnthropic:
    """Implementación real contra la API. Y la que APUNTA lo que se gasta.

    QUIÉN APUNTA EL GASTO, Y POR QUÉ SE MOVIÓ AQUÍ (3 septiembre 2026)
    ------------------------------------------------------------------
    Lo apuntaban tres capas: `motor.py` tras cada clasificación, `triaje.py`
    tras su llamada y `cli.py` tras el ranking. Tres copias de la misma regla
    —«apúntalo aunque la respuesta venga mal, que la llamada se pagó»—, cada
    una escrita a mano por quien se acordó.

    La cuarta no se acordó. Un script de medición de esa misma semana
    construyó este cliente directamente para un A/B de tres reclasificaciones,
    llamó a la API sin pasar por `GuardiaPresupuesto`, y sus **$0,1587** no
    entraron en el libro. El libro decía $2,2663 donde el gasto real eran
    $2,4250: un 7% de menos, en el único número que impide pasarse del techo.

    No era un fallo del libro: era que apuntar dependía de acordarse. Así que
    ahora apunta el cliente, en `_invocar`, que es **el único punto del
    programa donde existe el desglose de caché** y por tanto el coste de
    verdad. En cuanto se sumó en `tokens_entrada` ya no se sabe qué parte se
    cobró a un décimo.

    ¿NO ROMPE ESO QUE EL MOTOR APUNTE ANTES DE GUARDAR LA CLASIFICACIÓN?
    No: la aprieta. Esa regla existe para que un guardado que falle no regale
    presupuesto, y el orden sigue siendo el mismo —solo que el apunte ocurre
    aún antes, en cuanto la llamada vuelve, en vez de tres funciones más
    arriba—. La ventana entre «se gastó» y «está contado» pasa de «lo que el
    motor haga por el camino» a nada.

    LO QUE HAY QUE VIGILAR SON DOS COSAS, y las dos tienen prueba:

      1. **Que no se apunte dos veces.** Los tres `apuntar` de las capas se
         quitaron en el mismo cambio. Si alguien vuelve a poner uno, el gasto
         del mes se cuenta doble y el techo se dispara a mitad de mes.

      2. **Que haya UN SOLO libro por proceso.** `LibroDeGastos` guarda el
         archivo entero desde su copia en memoria, así que dos instancias se
         pisan: la segunda en escribir borra lo de la primera, y la del
         guardia se quedaría además leyendo un saldo congelado, con lo que el
         freno dejaría de frenar. Por eso el cliente RECIBE el libro del
         guardia en vez de abrir el suyo. Solo cuando no se le da ninguno
         —el caso del script suelto— abre el de `libro_por_defecto()`.

    `libro=False` desactiva el apunte, y es para las pruebas que quieran
    comprobar el cliente sin tocar disco. No es para producción: llamar a la
    API sin contarlo es el agujero que este cambio cierra.
    """

    def __init__(self, api_key: str | None = None, libro: Any = None) -> None:
        clave = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not clave:
            raise RuntimeError(
                "Falta ANTHROPIC_API_KEY.\n"
                "\n"
                "La API key es de la clienta, no del desarrollador: los costes son suyos\n"
                "y transparentes, y así su CV y sus datos nunca pasan por una cuenta ajena.\n"
                "\n"
                "  1. Crear cuenta en https://console.anthropic.com/\n"
                "  2. Generar una key\n"
                "  3. Añadirla a `.env`:  ANTHROPIC_API_KEY=sk-ant-...\n"
                "\n"
                "`.env` está en .gitignore y no se sube nunca."
            )
        # CON RELOJ, Y NO CON EL DE FÁBRICA. El SDK espera **10 minutos** por
        # llamada y reintenta dos veces solo —consultado en la documentación del
        # SDK, no de memoria—, así que una llamada que se queda colgada puede
        # tener la corrida media hora parada. En una consola eso es una espera;
        # en su pantalla es una barra de progreso que no avanza y ninguna
        # explicación, y el remedio que se le ocurre a cualquiera —cerrar la
        # ventana— es el único gesto que no queremos que aprenda.
        #
        # 240 s es cuatro veces la llamada más larga que este proyecto ha
        # medido (el triaje de 60 vacantes, ~2.750 tokens de salida), así que no
        # corta nada que estuviera funcionando. Con los dos reintentos del SDK
        # el peor caso pasa de ~30 minutos a ~12, y cada intento fallido deja
        # línea en el log en vez de silencio.
        #
        # Los reintentos NO se tocan: el 429 y el 529 son justo lo que hay que
        # reintentar, y el SDK ya lo hace con espera creciente.
        self._cliente = anthropic.Anthropic(api_key=clave, timeout=240.0)

        # `None` -> el libro del proyecto. `False` -> no apuntar (pruebas).
        if libro is None:
            from .presupuesto import libro_por_defecto

            libro = libro_por_defecto()
        self._libro = libro or None

    # -- la llamada, una sola vez -------------------------------------------
    #
    # `clasificar` y `ordenar` mandan cosas distintas y esperan esquemas
    # distintos, pero fallan EXACTAMENTE igual: misma autenticación, mismos
    # límites, mismo crédito agotado, mismo `refusal` con HTTP 200. Duplicar
    # esos diez `except` sería garantizar que un día se arregle un fallo en
    # uno y no en el otro.
    #
    # Devuelve (parsed, tokens_entrada, tokens_salida, coste_usd, error).
    # `parsed` es None si y solo si `error` no lo es.
    #
    # El coste sale de aquí y no del que llama porque este es el único punto
    # del programa donde existe el desglose de caché: en cuanto se suma en
    # `tokens_entrada`, ya no se puede saber qué parte se cobró a un décimo.
    def _invocar(
        self,
        sistema: str,
        usuario: str,
        esquema: Any,
        max_tokens: int,
        esfuerzo: str | None = None,
    ) -> tuple[Any | None, int, int, float, str | None]:
        try:
            # `output_config` solo se manda si hace falta: pasarlo siempre
            # ataría el esfuerzo a un valor y cambiarlo invalida la caché.
            extra: dict[str, Any] = {}
            if esfuerzo:
                extra["output_config"] = {"effort": esfuerzo}

            # EL ESQUEMA VIAJA IGUAL; LA VALIDACIÓN SE HACE AQUÍ, DESPUÉS.
            #
            # Esto era `messages.parse(output_format=…)`, que manda el esquema
            # Y valida la respuesta dentro de la misma llamada. Cómodo, y con
            # un agujero medido el 9 septiembre 2026 haciendo que la API
            # devolviera un 200 con texto que no encajaba:
            #
            #     la llamada se hizo y se pagó
            #     la validación reventó DENTRO de `parse`
            #     la excepción saltó por encima del apunte de más abajo
            #     libro: $0.0000, 0 llamadas   <- dinero gastado y sin contar
            #
            # Es exactamente lo que el comentario de «AQUÍ SE APUNTA» dice que
            # no puede pasar, y pasaba porque el apunte estaba después de una
            # línea que podía lanzar. Ahora se piden los mismos bytes —el
            # esquema va en `output_config.format`, que es lo que `parse`
            # ponía en el cuerpo— pero vuelve el mensaje crudo: se lee `usage`,
            # se apunta, y solo entonces se valida. Ver `_validar`.
            extra.setdefault("output_config", {})["format"] = {
                "type": "json_schema",
                "schema": _esquema_json(esquema),
            }

            respuesta = self._cliente.messages.create(
                model=MODELO,
                max_tokens=max_tokens,
                **extra,
                # Pensamiento adaptativo: el modelo decide solo cuánto razonar.
                # NO se usa `budget_tokens`: está eliminado en Opus 5 y devuelve
                # un 400.
                thinking={"type": "adaptive"},
                # El bloque estable va marcado para la caché de prompts. Con dos
                # fichas puede que no llegue al mínimo de ~1024 tokens que exige
                # la caché, en cuyo caso simplemente no cachea — no falla.
                # El bloque estable va marcado para la caché de prompts.
                #
                # EL MÍNIMO DE OPUS 5 SON 512 TOKENS (verificado 3 sept 2026), y
                # no los ~1.024 que decía el comentario anterior — ese es el de
                # Opus 4.8. Por debajo del mínimo no falla: simplemente no
                # cachea, en silencio. Los dos prompts de este proyecto lo pasan
                # de sobra: 5.115 tokens el del clasificador y 2.376 el del
                # triaje (esquema incluido).
                #
                # Ojo con leer eso como un ahorro en el TRIAJE: es UNA llamada
                # por corrida, así que siempre ESCRIBE la caché (x1,25) y nunca
                # la lee. Cuesta ~$0,003 de más por corrida y no ahorra nada
                # salvo que se lance una segunda corrida antes de que expire.
                # Se deja porque ese día existe y el importe es ruido; está
                # contado en `triaje.coste_estimado`.
                system=[
                    {
                        "type": "text",
                        "text": sistema,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": usuario}],
            )
        except anthropic.AuthenticationError:
            return None, 0, 0, 0.0, "La ANTHROPIC_API_KEY no es válida o fue revocada."
        except anthropic.PermissionDeniedError:
            return None, 0, 0, 0.0, "La API key no tiene permiso para usar este modelo."
        except anthropic.NotFoundError:
            return None, 0, 0, 0.0, (
                f"El modelo '{MODELO}' no existe o no está disponible para esta cuenta."
            )
        except anthropic.RateLimitError as exc:
            espera = exc.response.headers.get("retry-after", "?")
            return None, 0, 0, 0.0, f"Límite de peticiones alcanzado. Reintentar en {espera}s."
        except anthropic.BadRequestError as exc:
            # Aquí caen los errores de crédito agotado y los de parámetros mal
            # formados. Se distingue por el mensaje porque el SDK usa el mismo
            # tipo para los dos. Ver `mensaje_de_peticion_rechazada`.
            return None, 0, 0, 0.0, mensaje_de_peticion_rechazada(exc.message)
        except anthropic.APIStatusError as exc:
            return None, 0, 0, 0.0, f"Error {exc.status_code} de la API: {exc.message}"
        except anthropic.APITimeoutError:
            # ANTES DE `APIConnectionError`, que es su clase padre: si va
            # después, un plantón se lee como «no hay red» y la lleva a
            # revisar el wifi cuando el wifi está bien. Son dos cosas
            # distintas y se arreglan distinto — misma regla que separa «sin
            # saldo» de «se rompió».
            return None, 0, 0, 0.0, (
                "La API tardó demasiado en contestar (más de 4 minutos) y se "
                "dejó de esperar. La oferta sigue pendiente: vuelve a "
                "intentarlo y seguirá donde iba."
            )
        except anthropic.APIConnectionError:
            return None, 0, 0, 0.0, "No se pudo conectar con la API. ¿Hay red?"
        except Exception as exc:  # noqa: BLE001
            # Red de seguridad: cualquier cosa que no hayamos previsto (un
            # fallo de validación del esquema, por ejemplo) se convierte en
            # "sin clasificar" con el motivo a la vista, nunca en una
            # clasificación inventada.
            return None, 0, 0, 0.0, f"{type(exc).__name__}: {exc}"

        uso = respuesta.usage
        entrada_nueva = uso.input_tokens or 0
        cache_leida = uso.cache_read_input_tokens or 0
        cache_escrita = uso.cache_creation_input_tokens or 0

        # `tokens_entrada` sigue siendo el TOTAL, que es lo que se enseña y lo
        # que se guarda: para leer un informe importa cuánto texto entró, no
        # cómo se facturó. El precio distinto de cada trozo se resuelve aquí
        # mismo, en `coste`, y viaja aparte.
        entrada = entrada_nueva + cache_leida + cache_escrita
        salida = uso.output_tokens or 0
        coste = coste_medido(entrada_nueva, cache_leida, cache_escrita, salida)

        # AQUÍ SE APUNTA, Y ANTES DE MIRAR SI LA RESPUESTA SIRVE.
        #
        # El orden es la regla entera: una llamada que devolvió una negativa,
        # un `max_tokens` o basura que no valida se ha pagado exactamente
        # igual. Si el apunte fuera después de los `return` de abajo, el libro
        # solo registraría los éxitos y mentiría justo el día que algo va mal
        # — que es el día en que hace falta. Misma regla que en `triajes` y en
        # `triaje_corridas`.
        if self._libro is not None:
            self._libro.registrar(coste)

        # `refusal` llega con HTTP 200: si no se comprueba, se lee `content`
        # vacío y parece un fallo de esquema incomprensible.
        if respuesta.stop_reason == "refusal":
            detalle = getattr(respuesta.stop_details, "category", None)
            return None, entrada, salida, coste, (
                f"El modelo declinó responder (categoría: {detalle})."
            )

        if respuesta.stop_reason == "max_tokens":
            return None, entrada, salida, coste, (
                f"La respuesta se cortó al llegar a max_tokens ({max_tokens}). "
                "Con un lote grande, pártelo antes que subir el tope."
            )

        # AQUÍ, Y NO ANTES. Todo lo de arriba —leer `usage`, calcular el coste,
        # apuntarlo— tiene que haber ocurrido ya cuando esto se ejecuta,
        # porque esto es lo único que puede fallar sobre una llamada que ya
        # está pagada.
        validada, fallo = _validar(respuesta, esquema)
        if fallo is not None:
            return None, entrada, salida, coste, fallo

        return validada, entrada, salida, coste, None

    # -- los dos usos ------------------------------------------------------

    def clasificar(self, sistema: str, oferta: str) -> RespuestaLLM:
        parsed, entrada, salida, coste, error = self._invocar(
            sistema, oferta, Clasificacion, MAX_TOKENS
        )
        return RespuestaLLM(
            clasificacion=parsed,
            tokens_entrada=entrada,
            tokens_salida=salida,
            modelo=MODELO,
            error=error,
            coste_medido_usd=coste,
        )

    def triar(
        self, sistema: str, lote: str, esfuerzo: str | None = None
    ) -> RespuestaRanking:
        """Una llamada por LOTE sobre METADATOS. Ver clasificador/triaje.py.

        Devuelve un `RespuestaRanking` y no un tipo propio porque la forma es
        exactamente la misma —un lote entra, una lista validada sale— y lo
        único que cambia es el esquema. Un tipo nuevo solo duplicaría el
        cálculo de coste, que es justo donde este proyecto ya se equivocó una
        vez.
        """
        from .triaje import Triaje

        parsed, entrada, salida, coste, error = self._invocar(
            sistema, lote, Triaje, MAX_TOKENS_RANKING, esfuerzo=esfuerzo
        )
        return RespuestaRanking(
            ranking=parsed,
            tokens_entrada=entrada,
            tokens_salida=salida,
            modelo=MODELO,
            error=error,
            coste_medido_usd=coste,
        )

    def ordenar(
        self, sistema: str, lote: str, esfuerzo: str | None = None
    ) -> RespuestaRanking:
        """Una llamada por LOTE, no por oferta. Ver clasificador/asistente.py."""
        from .asistente import Ranking

        parsed, entrada, salida, coste, error = self._invocar(
            sistema, lote, Ranking, MAX_TOKENS_RANKING, esfuerzo=esfuerzo
        )
        return RespuestaRanking(
            ranking=parsed,
            tokens_entrada=entrada,
            tokens_salida=salida,
            modelo=MODELO,
            error=error,
            coste_medido_usd=coste,
        )


# -- Con qué se estima ANTES de llamar --------------------------------------
#
# Tokens del bloque que viaja en TODAS las llamadas y se sirve de caché: el
# prompt de sistema con las fichas MÁS el esquema de salida, que el SDK
# serializa y manda por su cuenta.
#
# ESE ESQUEMA ES INVISIBLE DESDE AQUÍ. No aparece en ningún string de este
# repositorio, así que contar caracteres del prompt de sistema —que es lo que
# hacía `--dry-run`— se dejaba fuera más de la mitad de la entrada real. Por
# eso la estimación se quedaba corta por un factor de 2,4.
#
# MEDIDO el 2 septiembre 2026 contra `usage`: 5.110 tokens. Si las fichas de
# `config/cvs.yaml` crecen mucho, o cambia el esquema de `esquema.py`, hay
# que volver a medirlo. Sale gratis: una llamada e imprimir `usage`.
TOKENS_PREFIJO_CACHEADO = 5110

# Caracteres por token del texto de un anuncio. MEDIDO el mismo día: ~850
# caracteres dieron ~307 tokens. El 4 de `estimar_tokens` es el número de
# manual para inglés; el español con acentos tokeniza bastante peor, y los
# anuncios de México van en español.
CARACTERES_POR_TOKEN = 2.8

# Salida típica de una clasificación, INCLUIDO el pensamiento adaptativo, que
# se factura como salida.
#
# SON DOS NÚMEROS, NO UNO, Y ES LO QUE DICEN LOS DATOS (3 septiembre 2026).
#
# Con el recorte de 500 caracteres de la API se midió entre 245 y 353, y se
# estimaba con 400 a propósito: pasarse por arriba es barato y quedarse corto
# deja a la clienta a media corrida.
#
# Con la descripción ENTERA ese 400 se queda corto en **23 de 28** lecturas
# reales. Medido sobre las 28 reclasificaciones del 3 septiembre 2026:
#
#     n=28   min 263   p25 475   mediana 586   p75 879   p90 1.036   max 1.524
#     media 656, desviación 291
#
# Y AQUÍ HAY UNA CORRECCIÓN AL DIAGNÓSTICO DEL PROYECTO, que importa más que
# el número. El «error nº 5» quedó escrito como «la salida crece cuando crece
# la entrada», deducido de tres pares. Con 28 medidas, la correlación entre
# los caracteres del anuncio y los tokens de salida es **−0,12**: dentro de
# las descripciones enteras, la salida NO sigue a la longitud.
#
# Lo que hay es un ESCALÓN, no una pendiente: con 500 caracteres el modelo no
# tiene material sobre el que razonar y escribe ~300 tokens; con el anuncio
# entero razona de verdad y escribe ~650, mida el anuncio 2.000 caracteres o
# 11.000. Por eso siguen siendo constantes —una constante es el modelo
# correcto— y por eso son dos.
#
# El 700 es la media redondeada hacia arriba, y no el máximo. Con el recorte
# se cogió el máximo porque el rango era estrecho (245-353, un factor 1,4);
# aquí el rango es 263-1.524, un factor 5,8, y coger el máximo significaría
# anunciarle a la clienta más del doble de lo que va a pagar en cada corrida.
# Lo que se estima es siempre un LOTE, y en un lote la varianza se promedia:
# sobre las 28 medidas, 700 se pasa un +6,7%, que es el lado correcto.
#
# REVISADO EL 3 SEPTIEMBRE 2026 CON 60 LECTURAS, Y SE QUEDA EN 700. La media
# de las 60 es 524,8 (desviación 242,5), o sea que 700 se pasa un +33,4% y no
# el +6,7% que decían las 28. Aun así no se baja, y el motivo es que **las 60
# no son una muestra de un día normal**:
#
#     las 28 originales (eran las dudosas) .... media 645,0
#     las 33 releídas   (eran las seguras) .... media 404,7
#
# Los dos grupos se eligieron por criterios opuestos y salen 30 y 30 por
# casualidad, así que su media conjunta no representa nada. Lo que sí se ve con
# 60 puntos es EL MECANISMO, y no era el que este archivo suponía:
#
#     correlación salida <-> caracteres del anuncio .... r = -0,06
#     correlación salida <-> CONFIANZA del veredicto ... r = -0,74
#
# El modelo no escribe más porque el anuncio sea largo: escribe más cuando la
# decisión es difícil. (El -0,06 confirma con 60 medidas el -0,12 que se anotó
# con 28: la salida no sigue a la longitud.)
#
# De ahí que 700 sea el número prudente y no el conservador de más: un día en
# que el triaje elija ocho vacantes genuinamente ambiguas promediaría ~645, y
# 700 lo cubre. Bajarlo a ~525 haría que ese día el importe enseñado fuera
# MENOR que el cobrado, que es el único lado que no se puede permitir.
#
# Se corrige el día que haya lecturas de corridas normales —ni dudosas ni
# releídas—, y para eso está `python -m clasificador lecturas`.
TOKENS_SALIDA_RECORTE = 400
TOKENS_SALIDA_ENTERA = 700

# Por encima de cuántos caracteres se considera que la DESCRIPCIÓN viene
# entera. El plan Trial corta en 500 exactos —367 de las 381 ofertas de la base
# miden exactamente eso—, así que cualquier umbral entre 500 y el mínimo real
# de una página entera vale. 600 deja margen sin confundirse con nada.
CARACTERES_DESCRIPCION_ENTERA = 600

# El mismo umbral, pero sobre el texto ENTERO que se manda (título + empresa +
# ubicación + descripción), para cuando quien pregunta no tiene a mano la
# descripción suelta.
#
# ES UN APAÑO Y SE DICE. El envoltorio del prompt añade ~350 caracteres, así
# que un anuncio recortado llega a la llamada midiendo ~860 y no 500. Medido el
# 3 septiembre 2026 sobre las 295 pendientes, los dos grupos no se tocan:
#
#     con recorte (n=13):  mínimo 728, mediana 835, MÁXIMO 863
#     con entera (n=282):  MÍNIMO 1.222, mediana 6.315, máximo 19.697
#
# El hueco entre 863 y 1.222 es cómodo, pero depende de lo que mida el
# envoltorio: si algún día `prompt.construir_oferta` añade campos, un anuncio
# recortado cruzaría el umbral y se le cobraría un 75% de salida de más sin
# que fallara nada. Por eso el camino bueno es que quien llama pase
# `caracteres_descripcion`, que es lo que hace el motor; esto es solo la red
# para quien no pueda.
CARACTERES_TEXTO_CON_ENTERA = 1000

# Se conserva el nombre viejo apuntando al del recorte: lo importan las pruebas
# y algún comentario, y cambiarlo en silencio escondería que ahora son dos.
TOKENS_SALIDA_TIPICOS = TOKENS_SALIDA_RECORTE


def salida_tipica(
    caracteres_oferta: int, caracteres_descripcion: int | None = None
) -> int:
    """Cuántos tokens de salida esperar de una clasificación así.

    UN SOLO SITIO LO DECIDE, igual que `motor.descripcion_de` decide qué texto
    se manda. El coste que se le enseña, el que frena el tope por acción y el
    que se compara luego con la factura tienen que salir del mismo sitio.

    `caracteres_descripcion` es la respuesta buena y el motor la pasa siempre;
    sin ella se deduce del texto entero, con el umbral y las salvedades de
    `CARACTERES_TEXTO_CON_ENTERA`.
    """
    if caracteres_descripcion is not None:
        entera = caracteres_descripcion > CARACTERES_DESCRIPCION_ENTERA
    else:
        entera = caracteres_oferta > CARACTERES_TEXTO_CON_ENTERA
    return TOKENS_SALIDA_ENTERA if entera else TOKENS_SALIDA_RECORTE


def coste_estimado_de_llamada(
    caracteres_oferta: int,
    primera_de_la_corrida: bool = False,
    caracteres_descripcion: int | None = None,
) -> tuple[int, int, float]:
    """(tokens_entrada, tokens_salida, USD) esperados de UNA clasificación.

    La primera llamada de una corrida ESCRIBE la caché (x1,25) y las demás la
    LEEN (x0,10). Esa diferencia es casi todo el coste, así que una estimación
    que no la modele no sirve para decidir si lanzar: es la que decía $5,77
    donde el gasto real fueron $2,80.

    Y LA SALIDA DEPENDE DE SI EL ANUNCIO VIENE ENTERO O CORTADO. Ver
    `salida_tipica`: con el recorte de 500 caracteres el modelo escribe ~300
    tokens y con el anuncio entero ~650, y usar el mismo número para los dos
    dejaba el plan un 24% por debajo de la factura justo cuando se encendieron
    las descripciones.
    """
    nueva = max(1, int(caracteres_oferta / CARACTERES_POR_TOKEN))
    salida = salida_tipica(caracteres_oferta, caracteres_descripcion)
    if primera_de_la_corrida:
        coste = coste_medido(nueva, 0, TOKENS_PREFIJO_CACHEADO, salida)
    else:
        coste = coste_medido(nueva, TOKENS_PREFIJO_CACHEADO, 0, salida)
    return nueva + TOKENS_PREFIJO_CACHEADO, salida, coste


def estimar_tokens(texto: str) -> int:
    """Estimación local y grosera: ~4 caracteres por token.

    Se usa solo para el `--dry-run`, que tiene que poder decir "esta corrida
    te va a costar X" SIN gastar una llamada. La cifra real se mide después
    con lo que devuelve la API, que es la que se guarda en la base.
    """
    return max(1, len(texto) // 4)
