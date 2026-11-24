"""
Lo que va a costar, antes de gastarlo. UNA sola vez, para todos los que preguntan.

POR QUÉ EXISTE ESTE ARCHIVO
---------------------------
La regla de la casa es que la app nunca gasta un dólar sin haber enseñado antes
el número. Eso deja de ser una promesa en cuanto hay DOS sitios que calculan el
número: la consola con `--dry-run` y la bandeja con su botón de confirmar. Dos
estimadores del mismo gasto acaban divergiendo, y el día que divergen la
promesa se rompe sin que nadie lo note — no falla nada, simplemente el número
que se enseñó no es el que se cobró.

Así que el cálculo vive aquí y los dos lo llaman.

Y HABÍA DIVERGIDO YA. El `--dry-run` estimaba el triaje sobre TODAS las
pendientes, sin quitar las que ya tuvieron su turno (`claves_ya_triadas`) y sin
agrupar los anuncios que son la misma vacante. La corrida real sí hace las dos
cosas. Sobre la base del 2 septiembre 2026 eso eran 302 contra 228: el plan
decía ~$1,17 donde la corrida iba a gastar ~$0,88. El error caía del lado
seguro, pero un plan que no describe la corrida que viene detrás no es un plan.

QUÉ NO HACE
-----------
No mira el presupuesto. Aquí solo se responde «esto cuesta X»; si X cabe o no
en lo que queda del mes lo decide `presupuesto.py`, que es quien sabe de
reservas y de techos. Juntar las dos cosas haría que un plan no se pudiera
calcular sin montar el libro de gastos, y el plan tiene que poder pedirse
siempre — sobre todo cuando NO cabe, que es justo cuando hay que explicarlo.

No llama a nada, no necesita API key y no cuesta un céntimo. Todo lo que usa
son fórmulas medidas: ver `cliente_llm.coste_estimado_de_llamada` y
`triaje.coste_estimado`.
"""

from __future__ import annotations

import sqlite3
import dataclasses
from dataclasses import dataclass, field
from typing import Any, Sequence

from .cliente_llm import MODELO
from .motor import Motor, agrupar_para_ahorrar, descripcion_de
from .triaje import (
    cabida,
    claves_de,
    coste_estimado as coste_estimado_triaje,
    preparar_lote,
    rescatar_sin_leer,
)


class ClienteInerte:
    """Un cliente que no llama a nada.

    Existe para que se pueda construir un `Motor` o un `Asistente` solo para
    preguntarles cuánto costaría algo, sin exigir una API key. Si alguien
    intenta usarlo para trabajar de verdad, revienta ruidosamente: un cliente
    que devolviera respuestas vacías en silencio dejaría ofertas sin clasificar
    con cara de estar clasificadas.
    """

    modelo = MODELO

    def clasificar(self, sistema: str, oferta: str):  # pragma: no cover
        raise RuntimeError("El cliente inerte no clasifica. Es un fallo de programación.")

    def ordenar(self, sistema: str, lote: str, esfuerzo: str | None = None):  # pragma: no cover
        raise RuntimeError("El cliente inerte no ordena. Es un fallo de programación.")

    def triar(self, sistema: str, lote: str, esfuerzo: str | None = None):  # pragma: no cover
        raise RuntimeError("El cliente inerte no tría. Es un fallo de programación.")


# Cuántas vacantes trae un día normal, tras los filtros duros. MEDIDO el 2
# septiembre 2026: entre 20 y 30 ofertas nuevas diarias. Sirve para una sola
# cosa —saber cuándo el lote que se va a triar es un ATRASO y no un día— y así
# poder decirlo en el plan en vez de enseñar una cifra diez veces mayor de la
# cotizada sin explicación.
LOTE_TIPICO = 25


def corridas_para_absorber(
    cola: int, tope: int, llegadas: int = LOTE_TIPICO
) -> int:
    """Cuántas corridas hasta que no quede atraso. `0` = no drena nunca.

    HAY QUE CONTAR LAS LLEGADAS, Y ES EL ERROR FÁCIL. Dividir la cola entre el
    tope da el número de una cola congelada: sobre las 273 vacantes reales del
    3 septiembre 2026 con tope 60 eso decía 5 corridas, y son 8. Cada corrida
    se lleva `tope` pero entran ~25 nuevas, así que lo que drena de verdad es
    `tope - llegadas`, no `tope`.

    Y por eso el tope tiene que ser holgadamente mayor que las llegadas: si las
    igualara, la cola no bajaría nunca y el tope habría dejado de ser una
    medida temporal para convertirse en un filtro permanente — que es
    exactamente lo que este proyecto no quiere que el triaje sea.
    """
    if tope <= 0 or cola <= tope:
        return 1
    if llegadas >= tope:
        return 0
    corridas, restante = 1, cola - tope
    while restante > 0:
        restante += llegadas - tope
        corridas += 1
    return corridas


# ---------------------------------------------------------------------------
#  Clasificar
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanDeClasificacion:
    """Lo que haría `python -m clasificador`, y lo que costaría.

    Los recuentos van separados a propósito. «302 pendientes» no es lo mismo
    que «228 vacantes sin triar» ni que «8 lecturas», y confundirlos es lo que
    hace que un número enseñado no se parezca a la factura.
    """

    pendientes: int          # anuncios sin clasificar en la base
    ya_triadas: int          # de esos, los que ya tuvieron su turno de triaje
    sin_triar: int           # anuncios que no han pasado nunca por el triaje
    vacantes: int            # esos anuncios, agrupados: la misma vacante es una
    # De esas vacantes, las que entran en la llamada de ESTA corrida. Es
    # `vacantes` salvo que el tope de lote recorte. Van separadas porque son
    # dos cosas y confundirlas es exactamente lo que rompe la promesa: «hay 273
    # esperando» y «se van a mirar 60 ahora» tienen que verse las dos, o el
    # número que se enseña no es el que se cobra — o peor, parecería que las
    # otras 213 se han perdido.
    vacantes_esta_corrida: int

    hay_triaje: bool         # ¿se va a gastar la llamada del triaje?
    lecturas: int            # cuántas vacantes se leerían enteras
    llamadas: int            # llamadas a la API, triaje incluido

    coste_triaje_usd: float
    coste_lecturas_usd: float
    tokens_entrada: int
    tokens_salida: int

    # Por qué el plan es este y no otro. Se escribe aquí y no en cada
    # consumidor porque la consola y la bandeja tienen que decir lo mismo.
    nota: str
    # (mercado, título, empresa) de lo que entraría, para poder enseñarlo.
    muestra: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)

    @property
    def coste_usd(self) -> float:
        return self.coste_triaje_usd + self.coste_lecturas_usd

    @property
    def fuera_del_tope(self) -> int:
        """Vacantes que el tope deja para la corrida siguiente.

        NO son descartes ni aplazadas: no llegan a tener fila en `triajes`, así
        que conservan su turno entero. La bandeja lo dice con esas palabras.
        """
        return max(0, self.vacantes - self.vacantes_esta_corrida)

    @property
    def hay_trabajo(self) -> bool:
        """¿Lanzar esto haría algo? Un plan de cero llamadas no se confirma."""
        return self.llamadas > 0


def plan_de_clasificacion(
    almacen: Any,
    config: Any,
    fichas: Any,
    *,
    sin_triaje: bool = False,
    limite: int | None = None,
    reclasificar: bool = False,
    reintentar: bool = False,
) -> PlanDeClasificacion:
    """Describe la corrida que lanzaría `comando_clasificar` con esos mismos
    argumentos, sin lanzarla.

    Sigue el mismo camino que la corrida real, en el mismo orden, porque
    cualquier atajo aquí es una cifra que luego no se cumple.
    """
    filas = almacen.ofertas_pendientes(
        fichas_huella=fichas.huella,
        incluir_obsoletas=reclasificar,
        incluir_fallidas=reintentar,
        limit=limite,
    )

    if not filas:
        return _plan_vacio(
            "Todas las ofertas de la base ya están clasificadas. La caché hace "
            "su trabajo: no se gastaría un token."
        )

    cuantas = config.llm.triaje.ofertas_por_corrida
    esfuerzo = getattr(config.llm.triaje, "esfuerzo", None)
    usa_triaje = bool(config.llm.triaje.activo) and not sin_triaje

    ya_triadas = 0
    grupos_rescate: list = []
    huecos = cuantas
    if usa_triaje:
        # LO QUE SE QUEDÓ A MEDIAS VA PRIMERO, Y SIN VOLVER A TRIARSE.
        # Ver `triaje.rescatar_sin_leer`: son las que el triaje ya eligió y
        # cuya lectura no llegó a hacerse porque la corrida se cortó.
        grupos_rescate, huecos = rescatar_sin_leer(
            filas, almacen.claves_elegidas_sin_leer(), cuantas
        )
        rescatadas = claves_de(grupos_rescate)

        # CADA OFERTA TIENE UN TURNO. Las que ya pasaron por un triaje no
        # vuelven a competir, así que tampoco entran en la cuenta de lo que
        # costaría triar. Ver `storage.claves_ya_triadas`.
        vistas = almacen.claves_ya_triadas()
        nuevas = [f for f in filas if f["dedupe_key"] not in vistas]
        # Las rescatadas ya tienen fila en `triajes`, así que estarían dentro
        # de `vistas`: se descuentan aparte para no contarlas como aplazadas
        # cuando en realidad se van a leer en esta misma corrida.
        ya_triadas = len(filas) - len(nuevas) - len(rescatadas)
    else:
        nuevas = list(filas)

    # EL MISMO RECORTE QUE HARÁ LA CORRIDA, Y POR LA MISMA FUNCIÓN.
    #
    # `preparar_lote` agrupa y aplica el tope de lote; es la que usa
    # `Triador.triar` para hacerlo de verdad. Reimplementar aquí el `[:tope]`
    # sería volver a tener dos criterios de qué entra en la llamada, que es la
    # divergencia que este archivo existe para impedir — ya pasó una vez con la
    # agrupación y las ya triadas, y costó ~$0,29 de diferencia entre lo que el
    # plan decía y lo que la corrida iba a gastar.
    #
    # `grupos` es lo que entra ahora y `fuera` lo que espera a la siguiente:
    # sumados dan la cola entera, así que no hace falta agrupar dos veces.
    tope = (
        getattr(config.llm.triaje, "max_vacantes_por_triaje", None)
        if usa_triaje
        else None
    )
    grupos, fuera_del_tope = preparar_lote(nuevas, tope)
    representantes = [rep for rep, _ in grupos]
    vacantes_en_cola = len(representantes) + fuera_del_tope

    motor = Motor(almacen, fichas, cliente=ClienteInerte())

    if usa_triaje and not nuevas and not grupos_rescate:
        # No es «no hay ofertas»: hay, y ya se miraron. La diferencia es la
        # regla nº 1 de la casa, y aquí es también la diferencia entre un
        # botón que gasta y uno que no haría nada.
        #
        # `not grupos_rescate` es lo que impide que esta rama se trague las
        # lecturas que quedaron a medias: esas SÍ son trabajo, y decir aquí
        # que no lo hay era la forma de perderlas para siempre.
        return _plan_vacio(
            f"Las {len(filas)} pendientes ya tuvieron su turno de triaje y "
            "siguen en la bandeja con su puesto y su motivo. Lanzar la corrida "
            "no las volvería a mirar: su vía es el botón «que la lea el "
            "asistente», oferta por oferta.",
            pendientes=len(filas),
            ya_triadas=ya_triadas,
        )

    # LOS RESCATES SE LEEN SIEMPRE; EL TRIAJE SOLO LLENA LO QUE SOBRE.
    #
    # Con `huecos == 0` —una corrida se cortó habiendo elegido ya las seis—
    # esta corrida no tría nada: lee las seis que faltaban y para. Es lo que
    # hace que un corte no cueste una llamada de triaje de más.
    rescates: list = [rep for rep, _ in grupos_rescate]
    hay_triaje = usa_triaje and huecos > 0 and len(representantes) > huecos

    if hay_triaje:
        # ¿CABE LA RESPUESTA EN UNA LLAMADA? Si no cabe, el plan no puede decir
        # que cabe en el mes y callarse que se va a cortar: la corrida se
        # negaría, y un plan que describe una corrida que no va a ocurrir es
        # justo lo que este archivo existe para impedir. Se devuelve un plan
        # sin llamadas y con el motivo dentro, que es lo que la consola imprime
        # y lo que la bandeja devuelve al negarse (HTTP 409).
        #
        # Con el tope de lote puesto esta rama ya casi no se alcanza: lo que
        # se manda son `max_vacantes_por_triaje` vacantes, no la cola entera.
        # Sigue aquí porque el tope es configurable y nadie impide ponerlo en
        # 300 — y porque una guarda que solo se sostiene en que otro número
        # esté bien puesto no es una guarda. Cuesta cero comprobarlo.
        sitio = cabida(len(representantes), esfuerzo)
        if not sitio.cabe:
            return _plan_vacio(
                sitio.explicacion(len(representantes))
                + (
                    " Baja `llm.triaje.max_vacantes_por_triaje` en "
                    "config/search_profile.yaml: lo que no entre conserva su "
                    "turno y se tría en las corridas siguientes."
                    if tope
                    else ""
                ),
                pendientes=len(filas),
                ya_triadas=ya_triadas,
                sin_triar=len(nuevas),
                vacantes=vacantes_en_cola,
                vacantes_esta_corrida=len(representantes),
            )

        # QUÉ SEIS SE LEEN NO SE SABE TODAVÍA, Y SE ESTIMA POR ARRIBA.
        #
        # Aquí se cobraba por las `huecos` PRIMERAS de la cola, que están
        # ordenadas por fecha. Las que se leen de verdad son las que elija el
        # triaje —que aún no ha corrido— y no tienen por qué ser esas.
        # Medido el 9 septiembre 2026, en la misma corrida:
        #
        #     el diálogo dijo ..... Paystand, Wells Fargo, FloatMe, CNA…
        #     la corrida leyó ..... Honeywell, Fluence, TAK Broadband…
        #     enseñado $0,1742  ->  cobrado $0,2200
        #
        # Los anuncios no miden lo mismo —de 500 a 9.000 caracteres— así que
        # coger seis cualesquiera es una lotería que puede caer por debajo, y
        # por debajo es el único lado que este proyecto no se permite.
        #
        # Se estiman las `huecos` MÁS LARGAS de las candidatas: el triaje no
        # puede elegir nada más caro que eso. Sobra saldo, que es la dirección
        # correcta del error (ver `coste_de`).
        mas_caras = sorted(
            representantes, key=lambda f: len(descripcion_de(f)), reverse=True
        )
        a_clasificar: Sequence[sqlite3.Row] = rescates + mas_caras[:huecos]
        entrada_t, salida_t, coste_triaje = coste_estimado_triaje(
            len(representantes), esfuerzo
        )
        nota = (
            f"Una llamada de triaje mira {len(representantes)} vacante(s) por "
            f"sus metadatos y elige hasta {huecos} para leerlas enteras: cuáles "
            "se sabrá al hacerlo, y el importe de arriba cuenta con las más "
            "largas. Las demás NO se descartan: se quedan en la bandeja, "
            "vistas y con su motivo."
        )
        # EL ATRASO HAY QUE EXPLICARLO O EL NÚMERO ASUSTA CON RAZÓN.
        #
        # Antes esta rama decía «es una sola vez»: sin tope, el atraso entero
        # iba en una llamada de ~$0,88 y esa frase era verdad. Con el tope ya
        # no lo es —van 60 por corrida— y quedaría una promesa falsa en la
        # pantalla, así que lo que se explica ahora es lo otro: cuántas
        # esperan, que NO se pierden, y cuántas corridas va a llevar.
        if fuera_del_tope:
            corridas = corridas_para_absorber(vacantes_en_cola, len(representantes))
            nota += (
                f" Hay {vacantes_en_cola} vacantes esperando: el tope de lote "
                f"manda {len(representantes)} en esta llamada y deja "
                f"{fuera_del_tope} para las siguientes. Esas NO se marcan, así "
                "que conservan su turno entero y entran completas la próxima "
                "vez"
            )
            nota += (
                f", con lo que el atraso se absorbe en unas {corridas} corridas."
                if corridas
                else ". Ojo: con este tope la cola no baja, porque entran más "
                     "vacantes al día de las que se trían."
            )
        elif len(representantes) > LOTE_TIPICO * 2:
            _, _, coste_tipico = coste_estimado_triaje(LOTE_TIPICO, esfuerzo)
            nota += (
                f" Son muchas porque hay atraso acumulado: se están triando de "
                f"una vez todas las que llevaban esperando. Es una sola vez — un "
                f"día normal son ~{LOTE_TIPICO} vacantes y el triaje cuesta "
                f"~${coste_tipico:.2f}."
            )
    else:
        # `--sin-triaje` NO tiene tope de lecturas por corrida: lee todo lo
        # pendiente, que es justo lo que el triaje existe para evitar. Recortar
        # aquí con `huecos` convertiría ese comando en otro sin decirlo.
        a_clasificar = (
            rescates + representantes[:huecos] if usa_triaje else representantes
        )
        coste_triaje = 0.0
        entrada_t = salida_t = 0
        if usa_triaje:
            nota = (
                f"{len(representantes)} vacante(s) sin triar y se leen hasta "
                f"{huecos}: caben todas, no se gasta la llamada del triaje."
            )
        else:
            nota = (
                f"Sin triaje: se leerían enteras las {len(representantes)} "
                "vacantes pendientes. Es lo que el triaje existe para evitar."
            )

    # SE DICE, Y VA DELANTE. Una corrida que empieza leyendo lo que otra dejó
    # a medias tiene que decirlo: si no, el número de lecturas no cuadra con
    # el de vacantes sin triar y parece que la app se ha inventado trabajo.
    if grupos_rescate:
        nota = (
            f"{len(grupos_rescate)} vacante(s) que el asistente ya había "
            "elegido leer se quedaron sin leer —se paró la corrida o se agotó "
            "el tope—. Se leen esas primero, sin volver a triarlas: su turno "
            "ya estaba pagado. "
        ) + (nota if (nuevas or not usa_triaje) else "")

    entrada, salida, coste_lecturas = motor.coste_estimado(a_clasificar)

    return PlanDeClasificacion(
        pendientes=len(filas),
        ya_triadas=ya_triadas,
        sin_triar=len(nuevas),
        vacantes=vacantes_en_cola,
        vacantes_esta_corrida=len(representantes),
        hay_triaje=hay_triaje,
        lecturas=len(a_clasificar),
        llamadas=motor.llamadas_previstas(a_clasificar) + (1 if hay_triaje else 0),
        coste_triaje_usd=coste_triaje,
        coste_lecturas_usd=coste_lecturas,
        tokens_entrada=entrada + entrada_t,
        tokens_salida=salida + salida_t,
        nota=nota,
        # LA LISTA SOLO PUEDE ENSEÑAR LO QUE SE SABE.
        #
        # El diálogo pinta estos títulos debajo del importe, y ella los lee
        # como «esto es lo que va a leer». Con triaje por delante eso era
        # falso: los seis nombres eran los seis primeros de la cola y el
        # triaje elegía otros. Prometer una lista concreta que no se va a
        # cumplir es peor que no prometer ninguna, así que con triaje solo se
        # enseñan los rescates —esos sí están decididos— y la nota explica
        # que del resto elige el triaje.
        muestra=_muestra(rescates if hay_triaje else a_clasificar),
    )


def _plan_vacio(
    nota: str,
    pendientes: int = 0,
    ya_triadas: int = 0,
    sin_triar: int = 0,
    vacantes: int = 0,
    vacantes_esta_corrida: int = 0,
) -> PlanDeClasificacion:
    """Un plan que no lanzaría ninguna llamada, con el motivo escrito.

    Los recuentos se conservan aunque no se vaya a hacer nada: «no cabe en una
    llamada» y «no hay ofertas» no se pueden ver igual en pantalla.
    """
    return PlanDeClasificacion(
        pendientes=pendientes,
        ya_triadas=ya_triadas,
        sin_triar=sin_triar,
        vacantes=vacantes,
        vacantes_esta_corrida=vacantes_esta_corrida,
        hay_triaje=False,
        lecturas=0,
        llamadas=0,
        coste_triaje_usd=0.0,
        coste_lecturas_usd=0.0,
        tokens_entrada=0,
        tokens_salida=0,
        nota=nota,
    )


def _muestra(filas: Sequence[sqlite3.Row], cuantas: int = 20) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (f["market_key"], f["title"], f["company"]) for f in filas[:cuantas]
    )


# ---------------------------------------------------------------------------
#  Leer UNA oferta (el botón de la bandeja)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanDeUnaLectura:
    """Lo que cuesta que el asistente lea una oferta concreta.

    OJO CON EL NÚMERO, QUE PARECE ALTO. Una lectura suelta cuesta ~$0,05 y no
    los ~$0,011 de una dentro de una corrida. No es un recargo: las llamadas
    de una corrida se sirven de una caché de prompt que la PRIMERA escribe, y
    una llamada sola es siempre la primera. Se enseña el número de verdad, que
    es la regla; explicar por qué es alto es trabajo de la interfaz.
    """

    clave: str
    existe: bool
    titulo: str = ""
    empresa: str = ""
    ya_clasificada: bool = False
    coste_usd: float = 0.0
    tokens_entrada: int = 0
    tokens_salida: int = 0
    # POR QUÉ NO BASTA CON `ya_clasificada` (7 septiembre 2026). Son TRES
    # estados y aquí había dos: sin mirar, con veredicto, y «se intentó y
    # falló». El tercero se contaba como el segundo, así que la oferta se
    # quedaba sin botón y con una frase que afirmaba un veredicto inexistente.
    # Ahora el motivo del fallo viaja hasta la pantalla, porque volver a pulsar
    # a ciegas contra una cuenta sin crédito es pagar dos veces el mismo error.
    fallo_anterior: str | None = None

    @property
    def se_puede(self) -> bool:
        return self.existe and not self.ya_clasificada


def plan_de_una_lectura(almacen: Any, fichas: Any, clave: str) -> PlanDeUnaLectura:
    fila = almacen.obtener_oferta_con_clasificacion(clave)
    if fila is None:
        return PlanDeUnaLectura(clave=clave, existe=False)

    # Un veredicto apaga el botón; un intento fallido, no. Ver
    # `Storage.vacante_pendiente_por_clave`, que aplica la misma frontera: si
    # las dos no la aplicaran igual, el diálogo y la corrida dirían cosas
    # distintas sobre la misma oferta.
    #
    # El estado manda, no el mensaje: una fila `sin_clasificar` sin motivo
    # apuntado sigue siendo un fallo, y tratarla como veredicto por no traer
    # texto sería volver al mismo agujero por la puerta de atrás.
    fracaso = fila["clas_estado"] == "sin_clasificar"
    fallo = (fila["clas_error"] or "No se apuntó el motivo.") if fracaso else None

    if fila["clas_estado"] is not None and not fracaso:
        return PlanDeUnaLectura(
            clave=clave,
            existe=True,
            titulo=fila["title"],
            empresa=fila["company"],
            ya_clasificada=True,
        )

    motor = Motor(almacen, fichas, cliente=ClienteInerte())
    entrada, salida, coste = motor.coste_estimado([fila])
    return PlanDeUnaLectura(
        fallo_anterior=fallo,
        clave=clave,
        existe=True,
        titulo=fila["title"],
        empresa=fila["company"],
        coste_usd=coste,
        tokens_entrada=entrada,
        tokens_salida=salida,
    )


# ---------------------------------------------------------------------------
#  Releer lo que se juzgó a ciegas
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanDeRelectura:
    """Lo que cuesta rehacer los veredictos que se emitieron con el recorte.

    NO LLEVA TRIAJE, Y ESA AUSENCIA ES EL DISEÑO. Una oferta que ya se leyó
    entera no necesita que el triaje vuelva a decidir si merece lectura: esa
    decisión se tomó y se pagó. Meterla por el triaje además la haría
    invisible —`claves_ya_triadas` la excluye— y devolverle el turno
    resucitaría vacantes en la cola. Ver `Storage.ofertas_juzgadas_a_ciegas`.
    """

    vacantes: int            # las que se leerían: una llamada cada una
    anuncios: int            # filas de `clasificaciones` que quedarán al día
    esta_accion: int         # de esas vacantes, las que caben en ESTA acción
    coste_usd: float
    tokens_entrada: int
    tokens_salida: int
    nota: str

    # Las `dedupe_key` de TODOS los anuncios que entran en esta acción:
    # representantes y hermanos. El comando lee exactamente estas y no las
    # vuelve a buscar por su cuenta, porque el importe de arriba se calculó
    # sobre ellas — si el comando eligiera otras, el número enseñado y el
    # cobrado dejarían de ser el mismo sin que fallara nada. Regla nº 9.
    claves: tuple[str, ...] = ()

    @property
    def fuera_de_esta_accion(self) -> int:
        """Las que se quedan para la siguiente pasada.

        No son un descarte: su clasificación vieja sigue en la bandeja y
        siguen marcadas como juzgadas a ciegas, así que la próxima corrida
        las vuelve a coger. Es el tope por acción funcionando.
        """
        return max(0, self.vacantes - self.esta_accion)

    @property
    def hay_trabajo(self) -> bool:
        return self.esta_accion > 0


def plan_de_relectura(
    almacen: Any,
    fichas: Any,
    *,
    tope_por_accion_usd: float,
    limite: int | None = None,
) -> PlanDeRelectura:
    """Qué se relee, cuánto cuesta, y cuánto de eso cabe en una acción.

    El troceo se calcula AQUÍ y no en el CLI porque el número que se enseña y
    el trabajo que se hace tienen que salir del mismo sitio. Si el comando
    decidiera por su cuenta dónde cortar, el importe confirmado y el cobrado
    dejarían de ser el mismo sin que fallara nada.

    Y se trocea en vez de subir el tope: releer las 33 de la base no cabe en
    $0,60, y eso es el tope funcionando. Ver la regla de la casa sobre no
    subirlo para que algo quepa.
    """
    todos = almacen.ofertas_juzgadas_a_ciegas()
    anuncios = almacen.contar_juzgadas_a_ciegas()

    if not todos:
        return PlanDeRelectura(
            vacantes=0, anuncios=0, esta_accion=0, coste_usd=0.0,
            tokens_entrada=0, tokens_salida=0,
            nota="No hay ningún veredicto hecho con el recorte que tenga hoy "
                 "el anuncio entero guardado.",
        )

    # `--limite` cuenta VACANTES y no anuncios, porque lo que se paga son
    # vacantes. Recortar la lista de anuncios antes de agrupar es exactamente
    # el fallo que dejó a `--limite N` sin servir para el atraso del triaje:
    # el `LIMIT` caía sobre anuncios y la ventana se llenaba de hermanos.
    grupos = agrupar_para_ahorrar(todos)
    # El TOTAL se cuenta antes del `--limite`: «hay 33 esperando» y «se van a
    # rehacer 4 ahora» son dos cosas y las dos tienen que verse, o parecería
    # que las otras 29 se han perdido. Es la misma distinción que
    # `PlanDeClasificacion.vacantes` contra `vacantes_esta_corrida`.
    total_vacantes = len(grupos)
    if limite is not None:
        grupos = grupos[:limite]

    # Solo los representantes entran en la cuenta: los hermanos heredan y no
    # cuestan una llamada. Se estima sobre ellos y el motor agrupará igual.
    filas = [rep for rep, _ in grupos]

    motor = Motor(almacen, fichas, cliente=ClienteInerte())

    # Se busca el prefijo más largo que cabe. El corte va por DINERO y no por
    # número de ofertas: un anuncio de 15.951 caracteres y uno de 647 no
    # cuestan lo mismo, y un tope en llamadas no es una promesa que se le
    # pueda hacer a nadie.
    #
    # Se estima sobre el PREFIJO ENTERO en cada paso, y no sumando lecturas
    # sueltas, porque la primera llamada de una acción ESCRIBE la caché de
    # prompt (~$0,03 una vez) y las demás la leen. Sumar 33 estimaciones de
    # «una suelta» cobraría ese extra 33 veces. Usar la misma función que el
    # `--dry-run` es además lo que impide que el número enseñado y el cobrado
    # se separen.
    entrada = salida = 0
    coste = 0.0
    cabidas = 0
    for cuantas in range(1, len(filas) + 1):
        e, s, c = motor.coste_estimado(filas[:cuantas])
        if cabidas and c > tope_por_accion_usd:
            break
        entrada, salida, coste = e, s, c
        cabidas = cuantas

    quedan = total_vacantes - cabidas
    if quedan:
        nota = (
            f"{total_vacantes} vacantes se juzgaron con el recorte de 500 caracteres. "
            f"En esta acción caben {cabidas} (~${coste:.4f}); las otras {quedan} "
            f"esperan a la siguiente. El tope por acción es ${tope_por_accion_usd:.2f} "
            "y no se sube para que quepa: se parte."
        )
    else:
        nota = (
            f"{total_vacantes} vacantes se juzgaron con el recorte de 500 caracteres "
            f"y hoy está guardado su anuncio entero. Caben todas en una acción."
        )

    # Representantes que caben, MÁS sus hermanos: los hermanos no cuestan una
    # llamada pero sí hay que reescribirles el veredicto, o quedan diciendo lo
    # contrario que su padre.
    claves: list[str] = []
    for representante, companeras in grupos[:cabidas]:
        claves.append(representante["dedupe_key"])
        claves.extend(c["dedupe_key"] for c in companeras)

    return PlanDeRelectura(
        vacantes=total_vacantes,
        anuncios=anuncios,
        esta_accion=cabidas,
        coste_usd=coste,
        tokens_entrada=entrada,
        tokens_salida=salida,
        nota=nota,
        claves=tuple(claves),
    )


# ---------------------------------------------------------------------------
#  Ordenar (el asistente)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanDeRanking:
    ofertas: int
    coste_usd: float
    tokens_entrada: int
    tokens_salida: int
    nota: str
    # ¿Entra la respuesta en una sola llamada? Ver `asistente.cabida`.
    cabe: bool = True
    # Cuántas había sin revisar ANTES de recortar. Igual a `ofertas` cuando no
    # se recortó nada, que es el caso normal. Es el denominador de «las 45 más
    # recientes de 61»: sin él, un lote recortado se lee como el lote entero.
    disponibles: int = 0
    # El `--limite` que hay que pasarle al comando para que ordene exactamente
    # lo que este plan dice. `None` = sin recorte.
    limite: int | None = None

    @property
    def recortado(self) -> bool:
        return self.limite is not None and self.disponibles > self.ofertas

    @property
    def dejadas_fuera(self) -> int:
        return max(0, self.disponibles - self.ofertas)

    @property
    def hay_trabajo(self) -> bool:
        """Un lote que no cabe no se puede confirmar, aunque haya ofertas.

        Sin esto la bandeja dejaría lanzar una corrida que se va a rechazar a
        sí misma después de pagarla.
        """
        return self.ofertas > 0 and self.cabe


def plan_de_ranking(
    almacen: Any,
    preferencias: Any,
    limite: int | None = None,
    esfuerzo: str | None = None,
    ofertas_extra: int = 0,
) -> PlanDeRanking:
    """Lo que costaría ordenar lo clasificado, sin ordenarlo.

    `esfuerzo` es el de `llm.esfuerzo_ranking`, y NO es un detalle: la salida
    del ranking va de 129 tokens por oferta a `low` a 308 a `high`, o sea que
    sin él el plan estima otra corrida distinta de la que se va a lanzar. Se
    omitía en los dos sitios que llamaban aquí, y eso es exactamente la
    divergencia que este archivo existe para impedir.

    `ofertas_extra` ES PARA «PONME AL DÍA», y sin él la cadena mentiría.
    Ahí el ranking se lanza DESPUÉS de clasificar, así que ordena las de hoy
    MÁS las que la lectura acabe de añadir; estimarlo sobre las de ahora daría
    un número menor que el que se va a cobrar, que es el único lado que este
    proyecto no se permite.

    Se rellena con copias de las filas que ya hay, y eso es exacto y no un
    apaño: el coste del ranking está modelado como `2.106 + 237 por oferta`
    —proporcional, medido con `count_tokens`—, así que N+k copias cuestan lo
    mismo que N+k ofertas distintas. Lo único que se pierde es la variación
    entre ofertas, que este estimador no usa.
    """
    from .asistente import Asistente, cabida as cabida_ranking

    disponibles = almacen.contar_ofertas_para_ranking()
    filas = list(almacen.ofertas_para_ranking(limite=limite))
    if filas and ofertas_extra > 0:
        disponibles += ofertas_extra
        relleno = (filas * (ofertas_extra // len(filas) + 1))[:ofertas_extra]
        filas = filas + relleno
    if not filas:
        return PlanDeRanking(
            ofertas=0,
            coste_usd=0.0,
            tokens_entrada=0,
            tokens_salida=0,
            disponibles=disponibles,
            limite=limite,
            nota=(
                "No hay ofertas clasificadas sin revisar todavía. El asistente "
                "ordena lo que aún no has decidido; si ya lo marcaste todo, no "
                "queda nada que ordenar."
            ),
        )

    sitio = cabida_ranking(len(filas), esfuerzo)
    if not sitio.cabe:
        return PlanDeRanking(
            ofertas=len(filas),
            coste_usd=0.0,
            tokens_entrada=0,
            tokens_salida=0,
            nota=sitio.explicacion(len(filas)),
            cabe=False,
            disponibles=max(disponibles, len(filas)),
            limite=limite,
        )

    asistente = Asistente(
        cliente=ClienteInerte(), preferencias=preferencias, esfuerzo=esfuerzo
    )
    entrada, salida, coste = asistente.coste_estimado(filas)
    disponibles = max(disponibles, len(filas))
    fuera = disponibles - len(filas)
    if fuera > 0:
        nota = (
            f"UNA llamada ordena {len(filas)} de las {disponibles} que tienes "
            f"sin revisar: las {len(filas)} MÁS RECIENTES. Las otras {fuera} no "
            "se pierden —siguen en la bandeja, con sus filtros— pero este orden "
            "no las mira."
        )
    else:
        nota = (
            f"UNA llamada ordena las {len(filas)} de una vez, no una por oferta. "
            "Es la parte barata del sistema y la única que lees entera."
        )
    return PlanDeRanking(
        ofertas=len(filas),
        coste_usd=coste,
        tokens_entrada=entrada,
        tokens_salida=salida,
        nota=nota,
        disponibles=disponibles,
        limite=limite,
    )


def plan_de_ranking_que_quepa(
    almacen: Any,
    preferencias: Any,
    *,
    esfuerzo: str | None = None,
    tope_restante: float | None = None,
    techo_configurado: int | None = None,
    ofertas_extra: int = 0,
) -> PlanDeRanking:
    """El lote de ranking MÁS GRANDE que cabe, en vez de negarse.

    POR QUÉ SE PARTE Y NO SE SUBE EL FRENO
    --------------------------------------
    Es el mismo patrón que `plan_de_relectura`, y por el mismo motivo. Hasta el
    4 septiembre 2026 el ranking tenía dos formas de no hacerse y las dos eran
    todo o nada:

      · el lote pasaba de `asistente.cabida` y `plan_de_ranking` devolvía coste
        $0,00 — que dentro de «Ponme al día» se imprimía como «nada que hacer»;
      · o cabía en la llamada pero no en lo que quedaba del tope por acción, y
        entonces el paso se caía de la cadena entero.

    En los dos casos ella se queda sin lo ÚNICO que lee entero, y en los dos
    casos había un orden perfectamente pagable un poco más corto. Negarse era
    la respuesta segura cuando la alternativa era pasarse del tope; no lo es
    cuando la alternativa es un orden más corto que se anuncia como tal.

    TRES TECHOS, Y GANA EL MÁS BAJO
    -------------------------------
    ==========================  =======================================
    `asistente.cabida`          lo que cabe en UNA llamada (~60 a medium)
    `tope_restante`             lo que queda del tope de la acción
    `techo_configurado`         `llm.max_ofertas_por_ranking`, la decisión
                                de producto del 4 septiembre 2026
    ==========================  =======================================

    Y LO QUE SE RECORTA SE DICE. `PlanDeRanking.disponibles` guarda cuántas
    había, la nota lo redacta, y el ranking lo persiste en
    `rankings.ofertas_disponibles` para que la bandeja pueda seguir diciéndolo
    días después. El recorte es por RECENCIA —`ofertas_para_ranking` ordena por
    fecha descendente— así que lo que se cae son las más viejas. Callarlo sería
    esconder ofertas dentro de la pieza que existe para no esconder ninguna.
    """
    disponibles = almacen.contar_ofertas_para_ranking() + max(0, ofertas_extra)
    if disponibles <= 0:
        return plan_de_ranking(
            almacen, preferencias, esfuerzo=esfuerzo, ofertas_extra=ofertas_extra
        )

    tope = min(disponibles, techo_configurado or disponibles)

    # Se prueba de mayor a menor y se para en el primero que cabe. Son como
    # mucho `disponibles` evaluaciones de una fórmula cerrada: ni una llamada,
    # ni una consulta pesada. Hacerlo por búsqueda binaria ahorraría un
    # parpadeo y costaría tener que demostrar que el coste es monótono.
    for cuantas in range(tope, 0, -1):
        plan = _ranking_de(
            almacen, preferencias, cuantas,
            esfuerzo=esfuerzo, disponibles=disponibles, ofertas_extra=ofertas_extra,
        )
        if not plan.cabe:
            continue
        if tope_restante is not None and plan.coste_usd > tope_restante:
            continue
        return plan

    # Ni una sola oferta cabe en lo que queda del tope. Se devuelve el plan
    # entero para que quien llame pueda enseñar el motivo de verdad, que no es
    # «no hay nada que ordenar».
    return _ranking_de(
        almacen, preferencias, tope,
        esfuerzo=esfuerzo, disponibles=disponibles, ofertas_extra=ofertas_extra,
    )


def _ranking_de(
    almacen: Any,
    preferencias: Any,
    cuantas: int,
    *,
    esfuerzo: str | None,
    disponibles: int,
    ofertas_extra: int = 0,
) -> PlanDeRanking:
    """`plan_de_ranking` sobre `cuantas` ofertas, sabiendo cuántas había.

    El `ofertas_extra` de «Ponme al día» son ofertas que TODAVÍA NO EXISTEN
    —las que el paso 2 va a leer—, así que no se le pueden pedir a la base con
    un `limite`. Se piden las que hay y se rellena; lo que decide el tamaño
    final es `cuantas`.
    """
    if ofertas_extra > 0:
        reales = min(cuantas, almacen.contar_ofertas_para_ranking())
        plan = plan_de_ranking(
            almacen, preferencias,
            limite=reales or None,
            esfuerzo=esfuerzo,
            ofertas_extra=max(0, cuantas - reales),
        )
    else:
        plan = plan_de_ranking(
            almacen, preferencias, limite=cuantas, esfuerzo=esfuerzo
        )
    return dataclasses.replace(
        plan,
        disponibles=max(disponibles, plan.ofertas),
        limite=cuantas if cuantas < disponibles else None,
        nota=_nota_de_ranking(plan, cuantas, disponibles),
    )


def _nota_de_ranking(plan: PlanDeRanking, cuantas: int, disponibles: int) -> str:
    """Una sola redacción del recorte, para que no diverjan tres."""
    if not plan.cabe or cuantas >= disponibles:
        return plan.nota
    fuera = disponibles - cuantas
    return (
        f"UNA llamada ordena {cuantas} de las {disponibles} que tienes sin "
        f"revisar: las {cuantas} MÁS RECIENTES. Las otras {fuera} no se pierden "
        "—siguen en la bandeja, con sus filtros— pero este orden no las mira."
    )


# ---------------------------------------------------------------------------
#  Ponme al día — las tres cosas seguidas, bajo UN tope
# ---------------------------------------------------------------------------
#
# POR QUÉ EXISTE, MEDIDO
# ----------------------
# Para ver algo nuevo en la bandeja hay que pulsar tres botones EN ORDEN y
# esperar entre ellos: «Buscar ofertas nuevas» (~2 min 15 s), «Que el asistente
# lea lo nuevo» (~2 min) y «Ordenar por dónde empezar». Una ejecutiva senior
# hace eso dos veces y deja de abrir la app.
#
# LA PARTE QUE HAY QUE PENSAR NO ES ENCADENAR, ES EL TOPE
# -------------------------------------------------------
# El freno que se le promete es POR ACCIÓN. Una acción que encadena tres
# comandos y deja que cada uno se mida contra los $0,60 no promete $0,60:
# promete $1,80. Es exactamente el error que se corrigió el 3 septiembre 2026
# al mover el freno de por-llamada a por-acción, cometido otra vez un nivel más
# arriba. Así que la cadena enseña UN importe y frena sobre ESE.
#
# Y NO CABE SIEMPRE, QUE ES EL HALLAZGO. Simulado con 2.000 sorteos sobre la
# base real (3 septiembre 2026), con las perillas de hoy —25 vacantes al
# triaje, 8 lecturas, ranking a `medium`—:
#
#     ranking sobre 11 ofertas ..... mediana $0,4043   máx $0,4657   cabe
#     ranking sobre 20 ofertas ..... mediana $0,4573   máx $0,5186   cabe
#     ranking sobre 39 (hoy) ....... mediana $0,5691   máx $0,6304   NO cabe
#     el peor caso que permiten las
#     perillas (triaje de 60 + las 8
#     lecturas más largas + ranking
#     de 60) ....................... $0,8714                         NO cabe
#
# El lote del ranking crece con lo que ella no ha revisado, así que la cadena
# entera cabe mientras esa lista esté por debajo de ~40 ofertas y deja de caber
# por encima. O sea: funciona hasta el primer fin de semana que no abra la app.
#
# QUÉ SE HACE ENTONCES: lo que cabe, y se dice lo que queda. No se sube el tope
# —eso convertiría la promesa en una sugerencia— y no se niega la acción entera
# —eso la dejaría sin poder ponerse al día justo cuando más falta le hace—. Es
# el mismo patrón que `plan_de_relectura`: cuando algo no cabe en una acción,
# se parte; nunca se sube el freno.


# La mediana medida de una lectura DENTRO de una corrida, sobre las 60 lecturas
# reales del 3 septiembre 2026 (ver `lecturas` en el README). Solo se usa cuando
# la cola de pendientes está vacía y por tanto no hay anuncios contra los que
# medir: sin ella, el plan diría que leer ocho ofertas que todavía no existen
# cuesta cero, y ese es justo el caso de «Ponme al día» con la cola al día.
COSTE_MEDIANO_DE_UNA_LECTURA_USD = 0.0287


@dataclass(frozen=True)
class Tramo:
    """Uno de los tres pasos, con lo que cuesta y si entra en el tope."""

    accion: str          # "recolectar" | "clasificar" | "ranking"
    etiqueta: str
    coste_usd: float
    hay_trabajo: bool
    # ¿Entra en lo que queda del tope, contando los tramos de antes?
    entra: bool
    nota: str
    detalle: dict[str, Any] = field(default_factory=dict)
    # POR QUÉ no entra, y son dos cosas que no se arreglan igual:
    #
    #   · "tope"    — el importe se lo comieron los tramos de antes. Se
    #                 resuelve pulsando el botón suelto a continuación.
    #   · "llamada" — el lote no cabe en una sola llamada al modelo, cueste lo
    #                 que cueste (`asistente.cabida`). NO se resuelve pulsando
    #                 nada, y el número accionable es cuántas caben.
    #
    # Hacían falta dos porque el segundo llegaba con coste $0,00, y un tramo de
    # coste cero «entra» en cualquier tope: el diálogo lo imprimía como «nada
    # que hacer» encima de las ofertas que ella no ha ordenado. Ver
    # `tests/test_al_dia.py::TestElRankingQueNoCabeSeDICE`.
    motivo_fuera: str | None = None
    # Cuántas HABÍA cuando el tramo solo hace parte. `None` = las hace todas.
    #
    # Un tramo recortado que no lo diga es peor que un tramo que no entra: el
    # que no entra al menos sale en `quedan_fuera`, mientras que este se pinta
    # como un paso normal con su importe al lado. Es la regla nº 1 de la casa
    # en el sitio donde menos se ve.
    recortado_de: int | None = None


@dataclass(frozen=True)
class PlanDelDia:
    """«Ponme al día»: un importe, un tope, y qué queda fuera."""

    tramos: tuple[Tramo, ...]
    tope_por_accion_usd: float
    segundos_estimados: int

    @property
    def coste_usd(self) -> float:
        """Lo que va a costar la acción tal como se va a lanzar."""
        return sum(t.coste_usd for t in self.tramos if t.entra and t.hay_trabajo)

    @property
    def coste_completo_usd(self) -> float:
        """Lo que costaría hacerlo TODO, quepa o no.

        Se enseña cuando algo queda fuera: esconderlo haría parecer que lo que
        falta no existe, y entonces «Ponme al día» estaría diciendo que ya está
        al día. Es la regla nº 1 de la casa aplicada a un importe.
        """
        return sum(t.coste_usd for t in self.tramos if t.hay_trabajo)

    @property
    def entran(self) -> tuple[Tramo, ...]:
        return tuple(t for t in self.tramos if t.entra and t.hay_trabajo)

    @property
    def quedan_fuera(self) -> tuple[Tramo, ...]:
        return tuple(t for t in self.tramos if t.hay_trabajo and not t.entra)

    @property
    def hay_trabajo(self) -> bool:
        return bool(self.entran)

    @property
    def nota(self) -> str:
        if not self.hay_trabajo:
            ultima = [t.nota for t in self.tramos if not t.hay_trabajo]
            return ("Ya está todo al día. " + (ultima[-1] if ultima else "")).strip()

        partes = [
            f"{len(self.entran)} paso(s) seguidos, un solo importe y un solo "
            f"tope: esta acción no puede pasar de "
            f"${self.tope_por_accion_usd:.2f} pase lo que pase."
        ]
        # LA VERSIÓN CORTA, porque el aviso de la bandeja lleva la larga y las
        # dos salen en el mismo diálogo. La mención tiene que estar igualmente:
        # quien lea solo el plan —la consola, una prueba, el día que se añada
        # otra pantalla— no puede quedarse sin saber que el lote se recortó.
        for tramo in self.entran:
            if tramo.recortado_de:
                partes.append(
                    f"«{tramo.etiqueta}» no las mira todas: ordena "
                    f"{tramo.detalle.get('Ofertas a ordenar')} de "
                    f"{tramo.recortado_de}, las más recientes."
                )
        for tramo in self.quedan_fuera:
            if tramo.motivo_fuera == "llamada":
                partes.append(
                    f"«{tramo.etiqueta}» NO entra, y no es por el importe: "
                    f"{tramo.nota} No se pierde nada, pero pulsar su botón "
                    "suelto tampoco lo arregla."
                )
            else:
                partes.append(
                    f"«{tramo.etiqueta}» NO entra: son ${tramo.coste_usd:.4f} más y "
                    "se saldría del tope. No se pierde nada: queda pendiente y "
                    "tiene su propio botón."
                )
        return " ".join(partes)


def plan_del_dia(
    almacen: Any,
    config: Any,
    fichas: Any,
    preferencias: Any,
    *,
    llegadas_previstas: int = LOTE_TIPICO,
) -> PlanDelDia:
    """Los tres pasos de «Ponme al día», con el tope repartido entre ellos.

    DOS ESTIMACIONES MIRAN AL FUTURO, Y HAY QUE DECIRLO
    ---------------------------------------------------
    El primer paso trae ofertas nuevas y el segundo las clasifica, así que lo
    que cuestan los pasos 2 y 3 depende de lo que hagan los de antes.
    Estimarlos sobre la base de AHORA daría un número menor que el que se va a
    cobrar, y ese es el único lado que este proyecto no se permite — ver el
    estimador del ranking, que se quedaba corto un 66%.

    Así que:

    · el paso 2 se estima sobre la cola de hoy MÁS `llegadas_previstas`
      vacantes —25, medido el 2 septiembre 2026—, acotado por el tope de lote;
    · el paso 3 se estima sobre las candidatas de hoy MÁS las lecturas que el
      paso 2 va a hacer, que es su cota superior: no todas las leídas acaban en
      el ranking, porque las descartadas no entran.

    Las dos caen del lado de arriba, que es el correcto para una estimación. Y
    aun así el importe se vuelve a calcular ANTES DE CADA PASO al ejecutarlo
    (ver `bandeja/server.py::_planificar_el_dia`), porque una estimación no es
    una promesa. La promesa es el tope.
    """
    from recolector.plan import plan_de_recoleccion

    tope = float(getattr(config.llm, "tope_por_accion_usd", 0.0) or 0.0)
    tramos: list[Tramo] = []
    acumulado = 0.0
    segundos = 0

    # -- 1. Buscar ofertas nuevas. No cuesta dinero: gasta cuota de Adzuna, y
    #       esa se regenera sola. Siempre entra.
    recoleccion = plan_de_recoleccion(config)
    hay_recoleccion = recoleccion.hay_trabajo and recoleccion.cabe
    if hay_recoleccion:
        segundos += recoleccion.segundos_estimados
    tramos.append(Tramo(
        accion="recolectar",
        etiqueta="Buscar ofertas nuevas",
        coste_usd=0.0,
        hay_trabajo=hay_recoleccion,
        entra=True,
        nota=recoleccion.nota,
        detalle={
            "Peticiones a los portales": recoleccion.peticiones,
            "Tarda unos": f"{recoleccion.segundos_estimados // 60} min",
        },
    ))

    # -- 2. Leer lo nuevo. Aquí empieza el dinero.
    clasificar = _plan_de_clasificar_despues_de_buscar(
        almacen, config, fichas,
        llegadas_previstas=llegadas_previstas if hay_recoleccion else 0,
    )
    cabe = tope <= 0 or acumulado + clasificar.coste_usd <= tope
    if clasificar.hay_trabajo and cabe:
        acumulado += clasificar.coste_usd
        # ~15 s por lectura más la llamada del triaje, sobre las corridas
        # reales del 3 septiembre 2026.
        segundos += 20 + 15 * clasificar.lecturas
    tramos.append(Tramo(
        accion="clasificar",
        etiqueta="Que el asistente lea lo nuevo",
        coste_usd=clasificar.coste_usd,
        hay_trabajo=clasificar.hay_trabajo,
        entra=cabe,
        nota=clasificar.nota,
        motivo_fuera=None if cabe else "tope",
        detalle={
            "Vacantes que mira el triaje": clasificar.vacantes_esta_corrida,
            "Se leerían enteras": clasificar.lecturas,
            "Llamadas a la API": clasificar.llamadas,
        },
    ))

    # -- 3. Ordenar. Sobre lo de hoy MÁS lo que el paso 2 acaba de leer.
    extra = clasificar.lecturas if (clasificar.hay_trabajo and cabe) else 0
    #    SE PARTE EN VEZ DE CAERSE. El paso 3 se estrecha hasta lo que quepa en
    #    lo que queda del tope, igual que `plan_de_relectura`. Antes se pedía
    #    el lote entero y, si no cabía, el orden desaparecía de la cadena —o
    #    peor, salía como «nada que hacer»— cuando había un orden más corto
    #    perfectamente pagable. Lo que se recorta se dice; ver
    #    `plan_de_ranking_que_quepa`.
    ranking = plan_de_ranking_que_quepa(
        almacen, preferencias,
        esfuerzo=getattr(config.llm, "esfuerzo_ranking", None),
        tope_restante=(tope - acumulado) if tope > 0 else None,
        techo_configurado=getattr(config.llm, "max_ofertas_por_ranking", None),
        ofertas_extra=extra,
    )
    #    HAY TRABAJO ES QUE HAYA OFERTAS, NO QUE SE PUEDAN ORDENAR. Un lote
    #    que pasa de `asistente.cabida` vuelve con coste $0,00, y un tramo de
    #    coste cero entra en cualquier tope: el diálogo lo imprimía como «nada
    #    que hacer» justo encima de las ofertas sin ordenar, que es el único
    #    paso que ella lee entero. Los dos motivos se separan aquí.
    hay_ranking = ranking.ofertas > 0
    cabe_en_el_tope = tope <= 0 or acumulado + ranking.coste_usd <= tope
    entra_ranking = hay_ranking and ranking.cabe and cabe_en_el_tope
    if entra_ranking:
        acumulado += ranking.coste_usd
        segundos += 45
    tramos.append(Tramo(
        accion="ranking",
        etiqueta="Ordenar por dónde empezar",
        coste_usd=ranking.coste_usd,
        hay_trabajo=hay_ranking,
        entra=entra_ranking,
        nota=ranking.nota,
        detalle={
            "Ofertas a ordenar": ranking.ofertas,
            **({"De las que tienes sin revisar": ranking.disponibles}
               if ranking.recortado else {}),
        },
        recortado_de=ranking.disponibles if ranking.recortado else None,
        motivo_fuera=(
            None if entra_ranking
            else ("llamada" if not ranking.cabe else "tope")
        ),
    ))

    return PlanDelDia(
        tramos=tuple(tramos),
        tope_por_accion_usd=tope,
        segundos_estimados=segundos,
    )


def _plan_de_clasificar_despues_de_buscar(
    almacen: Any, config: Any, fichas: Any, *, llegadas_previstas: int
) -> PlanDeClasificacion:
    """El paso 2, contando las ofertas que el paso 1 va a traer.

    Se parte del plan de verdad —el mismo `plan_de_clasificacion` que usan la
    consola y el botón suelto, para no tener dos estimadores del mismo gasto— y
    se le suman las llegadas al lote del triaje.

    Solo cambia el TRIAJE. Las lecturas ya están acotadas por
    `ofertas_por_corrida`, así que lo que cuestan no depende de cuántas
    vacantes lleguen: es la perilla que existe justo para eso.
    """
    base = plan_de_clasificacion(almacen, config, fichas)
    if llegadas_previstas <= 0 or not getattr(config.llm.triaje, "activo", True):
        return base

    cuantas = config.llm.triaje.ofertas_por_corrida
    esfuerzo = getattr(config.llm.triaje, "esfuerzo", None)
    tope_lote = getattr(config.llm.triaje, "max_vacantes_por_triaje", None) or 0

    vacantes = base.vacantes + llegadas_previstas
    en_la_llamada = min(vacantes, tope_lote) if tope_lote else vacantes
    hay_triaje = en_la_llamada > cuantas
    if hay_triaje:
        _, _, coste_triaje = coste_estimado_triaje(en_la_llamada, esfuerzo)
    else:
        coste_triaje = 0.0

    lecturas = min(en_la_llamada, cuantas)

    # EL COSTE DE OCHO LECTURAS NO ES OCHO VECES EL DE UNA, y multiplicar aquí
    # daba un +64%. Las llamadas de una corrida se sirven de una caché de
    # prompt que la PRIMERA escribe, así que una lectura suelta cuesta ~$0,06 y
    # una de corrida ~$0,03 — es el mismo aviso que la bandeja le enseña al
    # leer una oferta suelta, aplicado del revés. Medido el 3 septiembre 2026
    # sobre esta misma base: extrapolando salía $0,5436 donde el estimador de
    # verdad dice $0,3313.
    #
    # Así que se le pregunta al MISMO estimador que usa la corrida, con
    # anuncios reales de la cola. Si la cola tiene menos de las que se van a
    # leer, se rellena con copias: lo que se está estimando son las ofertas que
    # la búsqueda de arriba todavía no ha traído, y un anuncio de la base es la
    # mejor muestra que hay de cómo van a ser.
    coste_lecturas = _coste_de_leer(almacen, config, fichas, lecturas)

    return PlanDeClasificacion(
        pendientes=base.pendientes,
        ya_triadas=base.ya_triadas,
        sin_triar=base.sin_triar,
        vacantes=vacantes,
        vacantes_esta_corrida=en_la_llamada,
        hay_triaje=hay_triaje,
        lecturas=lecturas,
        llamadas=lecturas + (1 if hay_triaje else 0),
        coste_triaje_usd=coste_triaje,
        coste_lecturas_usd=coste_lecturas,
        tokens_entrada=base.tokens_entrada,
        tokens_salida=base.tokens_salida,
        nota=(
            f"Un día normal entran ~{llegadas_previstas} vacantes nuevas. Con "
            f"las {base.vacantes} que ya esperan, el triaje miraría "
            f"{en_la_llamada} y se leerían enteras {lecturas}. Es una "
            "estimación de lo que la búsqueda de arriba va a traer: el importe "
            "se vuelve a calcular antes de gastar nada."
        ),
        muestra=base.muestra,
    )


def _coste_de_leer(almacen: Any, config: Any, fichas: Any, cuantas: int) -> float:
    """Lo que costaría leer enteras `cuantas` ofertas, con el estimador de verdad.

    Se le da al `Motor` una muestra de anuncios REALES de la cola, porque el
    coste de una lectura lo decide lo que mida su descripción, y eso solo lo
    sabe la base. Si la cola es más corta que la muestra, se rellena con copias:
    lo que se estima son ofertas que la búsqueda todavía no ha traído, y un
    anuncio de la base es la mejor muestra que hay de cómo van a ser las de
    mañana.

    Y si la cola está VACÍA del todo no hay nada que copiar, y solo ahí se cae
    a la mediana medida. Es el único camino de este archivo que usa una
    constante en vez de la base, y por eso está aislado en su propia función.
    """
    if cuantas <= 0:
        return 0.0
    filas = list(almacen.ofertas_pendientes(fichas_huella=fichas.huella))
    if not filas:
        return COSTE_MEDIANO_DE_UNA_LECTURA_USD * cuantas
    muestra = (filas * (cuantas // len(filas) + 1))[:cuantas]
    motor = Motor(almacen, fichas, cliente=ClienteInerte())
    return motor.coste_estimado(muestra)[2]
