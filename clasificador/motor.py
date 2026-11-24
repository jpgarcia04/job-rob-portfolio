"""
Orquestación de la capa 2.

El camino de una oferta, de principio a fin:

    pendiente  ->  ¿ya clasificada?  -> sí: NO se toca (esa es la caché)
                                     -> no: detectar idioma (gratis)
                                             -> llamar al LLM
                                             -> contrastar las dos señales
                                             -> guardar

La caché no es una optimización, es un requisito: sin ella cada corrida del
recolector reclasificaría todo el histórico y el gasto crecería solo.

DÓNDE ENCAJA CON LA CAPA 1
--------------------------
No se llama desde `recolector/pipeline.py`. Se ejecuta después, con su propio
comando, sobre lo que ya está en la base. Y lo que está en la base ya pasó
por los filtros duros de `recolector/filters.py`: una oferta que el filtro
iba a tirar nunca llega hasta aquí, y por tanto no gasta un token. Esa es la
condición del punto 2 del alcance.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from recolector.storage import Storage

from . import prompt as constructor
from .cliente_llm import (
    ClienteClasificador,
    RespuestaLLM,
    coste_de,
    coste_estimado_de_llamada,
    estimar_tokens,
)
from .esquema import Clasificacion
from .fichas import Fichas
from .idioma import DeteccionIdioma, detectar_idioma
from .presupuesto import GuardiaPresupuesto


@dataclass
class ResultadoOferta:
    """Lo que pasó con UNA oferta. Es lo que el CLI imprime línea a línea."""

    dedupe_key: str
    titulo: str
    empresa: str
    mercado: str

    estado: str                       # "ok" | "sin_clasificar"
    clasificacion: Clasificacion | None = None
    deteccion: DeteccionIdioma | None = None

    cv_final: str | None = None
    necesita_criterio: bool = False
    motivo_criterio: str | None = None
    error: str | None = None
    modelo: str = ""

    tokens_entrada: int = 0
    tokens_salida: int = 0

    # `dedupe_key` del anuncio que SÍ se pagó, cuando este copió su veredicto.
    # None = este se clasificó de verdad.
    heredada_de: str | None = None

    # Con qué texto se produjo ESTE veredicto: "entera" o "recorte", y cuántos
    # caracteres tenía. En una heredada son los del PADRE, no los suyos: el
    # veredicto salió de la lectura del padre, así que decir otra cosa sería
    # atribuirle una lectura que nadie hizo. Es lo que hace que un hermano de
    # una vacante juzgada a ciegas cuente también como ciego.
    descripcion_via: str | None = None
    caracteres_descripcion: int | None = None

    # Lo que costó DE VERDAD, con el desglose de caché que solo conoce
    # `cliente_llm`. `None` = no hubo llamada real (una heredada, un cliente
    # falso de los tests) y entonces vale la fórmula plana.
    coste_medido_usd: float | None = None

    @property
    def coste_usd(self) -> float:
        if self.coste_medido_usd is not None:
            return self.coste_medido_usd
        return coste_de(self.tokens_entrada, self.tokens_salida)


@dataclass
class ResumenCorrida:
    """Los totales de la corrida. Van al resumen de consola."""

    resultados: list[ResultadoOferta] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    modelo: str = ""

    # Ofertas que NO se clasificaron porque se acabó el presupuesto del mes.
    # No son un fallo y no son un descarte: siguen pendientes y se clasifican
    # solas en la siguiente corrida que tenga saldo. Se cuentan aparte porque
    # "no hay ofertas nuevas" y "no había dinero para mirarlas" se parecen
    # muchísimo en pantalla y significan cosas opuestas.
    detenidas_por_presupuesto: int = 0
    estado_presupuesto: Any = None

    @property
    def se_detuvo_por_presupuesto(self) -> bool:
        return self.detenidas_por_presupuesto > 0

    @property
    def total(self) -> int:
        return len(self.resultados)

    @property
    def modelo_usado(self) -> str:
        for r in self.resultados:
            if r.modelo:
                return r.modelo
        return self.modelo or "(ninguno)"

    @property
    def clasificadas(self) -> int:
        return sum(1 for r in self.resultados if r.estado == "ok")

    @property
    def llamadas(self) -> int:
        """Cuántas veces se llamó de verdad a la API. Es lo que se paga."""
        return sum(1 for r in self.resultados if r.heredada_de is None)

    @property
    def heredadas(self) -> int:
        """Ofertas resueltas copiando el veredicto de otra. Coste cero."""
        return sum(1 for r in self.resultados if r.heredada_de is not None)

    @property
    def fallidas(self) -> int:
        return sum(1 for r in self.resultados if r.estado != "ok")

    @property
    def relevantes(self) -> int:
        return sum(
            1 for r in self.resultados
            if r.clasificacion and r.clasificacion.relevante and not r.necesita_criterio
        )

    @property
    def descartadas(self) -> int:
        return sum(
            1 for r in self.resultados
            if r.clasificacion and not r.clasificacion.relevante and not r.necesita_criterio
        )

    @property
    def necesitan_criterio(self) -> int:
        return sum(1 for r in self.resultados if r.necesita_criterio)

    @property
    def tokens_entrada(self) -> int:
        return sum(r.tokens_entrada for r in self.resultados)

    @property
    def tokens_salida(self) -> int:
        return sum(r.tokens_salida for r in self.resultados)

    @property
    def coste_usd(self) -> float:
        """La suma de lo que costó CADA llamada, no la fórmula plana.

        AQUÍ SE QUEDÓ VIVO EL ERROR DE LA CACHÉ (medido el 3 septiembre 2026).

        Esto era `coste_de(tokens_entrada, tokens_salida)`, la fórmula que
        cobra la entrada entera a tarifa completa. Es la buena para una
        ESTIMACIÓN —`--dry-run` no sabe todavía qué parte vendrá de caché— y su
        propio docstring dice que para lo ya gastado no vale. Pero este número
        no es una estimación: es lo que la consola imprime como «esta corrida
        costó X» y lo que la bandeja le enseña a la clienta.

        Es el mismo fallo que el 2 de septiembre se corrigió en
        `cliente_llm.py` («de 5.400 tokens de entrada, 5.110 venían de caché;
        el coste apuntado era 3,25 veces el real»), que se arregló allí y se
        quedó aquí. Nadie lo vio porque el libro de gastos se escribía desde
        `resultado.coste_usd` —el bueno— y este total no se comparaba con nada.
        Al mover el apunte al cliente, los dos caminos quedaron enfrentados y
        la diferencia saltó a la primera corrida: **$0,7378 anunciados contra
        $0,4453 cobrados**, un 66% de más, sobre las 14 reclasificaciones con
        descripción entera.

        Cada `ResultadoOferta` ya trae su coste medido (`coste_medido_usd`, que
        sí conoce el desglose de caché) y las heredadas traen 0. Sumarlos es la
        respuesta correcta y no hace falta nada más.
        """
        return sum(r.coste_usd for r in self.resultados)

    def por_cv(self) -> dict[str, int]:
        conteo: dict[str, int] = {}
        for r in self.resultados:
            if r.cv_final:
                conteo[r.cv_final] = conteo.get(r.cv_final, 0) + 1
        return conteo


# ---------------------------------------------------------------------------
#  Contraste de las dos señales
# ---------------------------------------------------------------------------

# Qué CV se adjunta cuando el anuncio está en un idioma del que ella NO tiene
# ficha. Hoy solo pasa con el alemán, y la respuesta es el CV en inglés.
#
# POR QUÉ ESTO ES UNA TABLA Y NO UN `else`
# ----------------------------------------
# Hasta el 5 septiembre 2026 un anuncio alemán acababa con el CV en inglés,
# pero por descarte: `idioma.py` no sabía decir "de", lo leía como inglés, y
# acertaba de casualidad porque los anuncios directivos alemanes llevan mucho
# inglés técnico. Medido: con el anuncio entero los 36 alemanes salían `en`;
# con el recorte de 500 caracteres de la API —que es el estado de CUALQUIER
# anuncio alemán recién recolectado— 4 salían `es`, y a esos se les habría
# adjuntado la ficha en español.
#
# La decisión es de la clienta y está tomada: ella tiene dos fichas, español e
# inglés, y no va a escribir una tercera. Un anuncio en alemán lleva la
# inglesa. Escrito aquí, esa decisión se puede leer, discutir y cambiar en una
# línea; escondida en una rama de fallback, no se podía ni ver.
CV_SUPLENTE = {"de": "en"}


def _decidir_cv(
    fichas: Fichas,
    deteccion: DeteccionIdioma,
    dicho_por_el_modelo: str,
) -> tuple[str | None, str | None]:
    """Decide qué CV se adjunta y si hay motivo para dudar.

    Devuelve `(cv_id, motivo_de_duda)`. `motivo_de_duda` es None cuando las
    dos señales coinciden.

    Manda la detección determinista cuando es fiable, y no por desconfianza
    del modelo: es que para "en qué idioma está este texto" un contador de
    palabras funcionales es MÁS fiable que un juicio, y además es reproducible
    — el mismo texto da el mismo resultado siempre.
    """
    del_modelo = fichas.por_id(dicho_por_el_modelo)

    if not deteccion.es_fiable:
        if del_modelo is None:
            return None, (
                f"No se pudo detectar el idioma del anuncio y el modelo devolvió un "
                f"cv_id que no existe ('{dicho_por_el_modelo}')."
            )
        return del_modelo.id, (
            f"El idioma del anuncio no se pudo determinar automáticamente "
            f"({deteccion.motivo}); se usa el CV que eligió el modelo."
        )

    por_idioma = fichas.por_idioma(deteccion.idioma)

    if por_idioma is None:
        suplente = fichas.por_idioma(CV_SUPLENTE.get(deteccion.idioma, ""))
        if suplente is not None:
            # Decisión escrita, así que no es una duda: no se le pide criterio
            # sobre algo que ya está resuelto. Una advertencia que sale siempre
            # y siempre dice lo mismo enseña a no leer las advertencias.
            if del_modelo is not None and del_modelo.id != suplente.id:
                return suplente.id, (
                    f"El anuncio está en '{deteccion.idioma}' y no hay CV en ese "
                    f"idioma: le corresponde {suplente.id}. El modelo eligió "
                    f"{del_modelo.id}, que es otro."
                )
            return suplente.id, None

        if del_modelo is None:
            return None, (
                f"No hay ningún CV en idioma '{deteccion.idioma}' y el modelo devolvió "
                f"un cv_id inexistente ('{dicho_por_el_modelo}')."
            )
        return del_modelo.id, (
            f"El anuncio está en '{deteccion.idioma}' y no hay ningún CV en ese idioma."
        )

    if del_modelo is not None and del_modelo.id != por_idioma.id:
        # Las dos señales se contradicen. Ninguna de las dos manda: decide ella.
        return por_idioma.id, (
            f"Discrepancia: el texto parece '{deteccion.idioma}' "
            f"(CV {por_idioma.id}), pero el modelo eligió {del_modelo.id}."
        )

    return por_idioma.id, None


def _evaluar(
    fichas: Fichas,
    respuesta: RespuestaLLM,
    deteccion: DeteccionIdioma,
) -> tuple[str | None, bool, str | None]:
    """Aplica el umbral y el contraste. Devuelve (cv, necesita_criterio, motivo)."""
    assert respuesta.clasificacion is not None
    clas = respuesta.clasificacion

    cv_final, motivo_cv = _decidir_cv(fichas, deteccion, clas.cv_id)

    motivos: list[str] = []
    if clas.confianza < fichas.umbral_confianza:
        motivos.append(
            f"Confianza {clas.confianza:.2f}, por debajo del umbral "
            f"{fichas.umbral_confianza:.2f}."
        )
    if motivo_cv:
        motivos.append(motivo_cv)
    if cv_final is None:
        motivos.append("No se pudo determinar qué CV corresponde.")

    return cv_final, bool(motivos), " ".join(motivos) or None


# ---------------------------------------------------------------------------
#  Agrupar ANTES de llamar, para no pagar dos veces por la misma vacante
# ---------------------------------------------------------------------------

# Los dos orígenes posibles del texto que se le manda al modelo. Se guardan
# en `clasificaciones.descripcion_via`, así que son valores de la base y no
# se cambian a la ligera.
VIA_ENTERA = "entera"
VIA_RECORTE = "recorte"


def _elegir_descripcion(fila: Any) -> tuple[str, str]:
    """(texto, vía) — LA ÚNICA decisión de qué descripción se usa.

    Existe para que `descripcion_de` y `descripcion_via_de` no puedan
    responder cosas distintas. Si fueran dos condiciones escritas por
    separado, la fila diría que juzgó con el anuncio entero mientras el
    prompt llevaba el recorte, y eso es peor que no guardar la vía: una
    procedencia equivocada se lee como un dato.
    """
    try:
        entera = fila["description_full"]
    except (KeyError, IndexError):
        # Una fila que no trae la columna: un test con un `SELECT` a medida, o
        # una base de antes de la migración. Se cae al recorte de la API.
        entera = None

    if entera and entera.strip():
        return entera.strip(), VIA_ENTERA
    return (fila["description"] or "").strip(), VIA_RECORTE


def descripcion_via_de(fila: Any) -> str:
    """`'entera'` o `'recorte'`: con cuál de los dos textos se está juzgando.

    Es lo que se guarda con la clasificación, y con ello «obsoleta» gana un
    segundo motivo —*se juzgó con el recorte y ahora existe el anuncio
    entero*— sin depender de ningún umbral sobre longitudes. Ver
    `Storage.claves_juzgadas_a_ciegas`.

    Se guarda la CATEGORÍA y no solo la longitud a propósito: lo que la
    pantalla tiene que poder decir es «este veredicto se hizo con media
    descripción», y una etiqueta necesita una categoría. Deducirla de los
    caracteres es el apaño que `cliente_llm.CARACTERES_TEXTO_CON_ENTERA` ya
    documenta como frágil.
    """
    return _elegir_descripcion(fila)[1]


def descripcion_de(fila: Any) -> str:
    """El mejor texto que hay de esta oferta: el entero si se trajo, si no el
    recorte de la API.

    UN SOLO SITIO DECIDE, y por eso esto es una función y no un `or` repetido.
    El texto de la oferta manda en tres cosas a la vez —el prompt que se manda,
    el idioma que decide qué CV se adjunta, y la estimación de coste, que se
    calcula sobre su longitud—. Si dos de las tres leyeran columnas distintas,
    el número que se le enseña a la clienta dejaría de ser el que se cobra sin
    que fallara nada. Es la regla nº 9 de la casa aplicada al texto en vez de
    al precio.

    Que la detección de idioma también use el texto entero es deliberado:
    cuenta palabras funcionales, y 5.500 caracteres dan mucha más señal que
    500. No cambia el criterio, lo alimenta mejor.

    `description_full` está vacía mientras `descripciones.habilitado` sea
    `false`, así que sin encender nada esta función devuelve exactamente lo de
    siempre.

    Quién de los dos se eligió lo dice `descripcion_via_de`, que lee la misma
    decisión.
    """
    return _elegir_descripcion(fila)[0]


def _clave_laxa(fila: Any) -> str | None:
    """`loose_key` de la fila, o None si la consulta no la trajo.

    Nota de Python: un `sqlite3.Row` se indexa como un diccionario pero NO
    tiene `.get()`, así que pedirle una columna ausente lanza IndexError en
    vez de devolver None.
    """
    try:
        return fila["loose_key"]
    except (IndexError, KeyError):
        return None


def agrupar_para_ahorrar(
    filas: Sequence[sqlite3.Row],
) -> list[tuple[sqlite3.Row, list[sqlite3.Row]]]:
    """Junta los anuncios que son la MISMA vacante antes de gastar tokens.

    Devuelve `[(representante, [las demás]), ...]`. Solo se llama al modelo
    por el representante; las demás copian su veredicto.

    La clave es la `loose_key` que la v0 ya calculaba (empresa + título, sin
    ubicación) y que hasta ahora solo servía para MEDIR la sub-fusión en el
    reporte. Aquí pasa a ahorrar dinero: los cinco anuncios de "Chief
    Operating Officer" de Northline Asset Services en cinco ciudades de Texas
    son una llamada, no cinco.

    POR QUÉ ES SEGURO COPIAR EL VEREDICTO
    -------------------------------------
    Porque la clave laxa exige que empresa y título normalizados coincidan
    EXACTAMENTE. Lo único que cambia entre esos anuncios es la ciudad, y la
    ciudad no entra en la decisión: ni en si el puesto le encaja, ni en qué
    CV le corresponde. Si algún día la clave laxa se volviera más laxa, esta
    suposición dejaría de valer — por eso está escrita aquí.

    Lo que NO hace: unir "Northline Asset Services" con "Harlan Contracting
    Services". Eso solo lo ve el modelo, y para verlo tiene que haber leído
    los dos anuncios. Se resuelve después, en `agrupador.py`.
    """
    grupos: dict[str, list[sqlite3.Row]] = {}
    sueltas: list[sqlite3.Row] = []

    for fila in filas:
        clave = _clave_laxa(fila)
        if clave:
            grupos.setdefault(clave, []).append(fila)
        else:
            # Sin clave laxa no se puede afirmar que sean la misma vacante.
            # Se clasifica por separado: gastar de más es preferible a copiar
            # un veredicto a una oferta que no era la misma.
            sueltas.append(fila)

    # Se conserva el orden en que llegaron las filas (la consulta las trae
    # ordenadas por fecha), en vez del orden del diccionario de grupos.
    resultado: list[tuple[sqlite3.Row, list[sqlite3.Row]]] = []
    ya_emitidas: set[str] = set()

    for fila in filas:
        clave = _clave_laxa(fila)
        if not clave:
            continue
        if clave in ya_emitidas:
            continue
        ya_emitidas.add(clave)
        miembros = grupos[clave]
        resultado.append((miembros[0], miembros[1:]))

    resultado.extend((fila, []) for fila in sueltas)
    return resultado


def _heredar(base: ResultadoOferta, fila: sqlite3.Row) -> ResultadoOferta:
    """Copia el veredicto del representante a otro anuncio de la misma vacante.

    Los tokens van a CERO a propósito: esta oferta no costó nada, y el
    resumen de coste tiene que decir la verdad. El modelo se conserva para
    poder responder "¿con qué se decidió esto?" mirando solo esta fila.
    """
    return ResultadoOferta(
        dedupe_key=fila["dedupe_key"],
        titulo=fila["title"],
        empresa=fila["company"],
        mercado=fila["market_key"],
        estado=base.estado,
        clasificacion=base.clasificacion,
        deteccion=base.deteccion,
        cv_final=base.cv_final,
        necesita_criterio=base.necesita_criterio,
        motivo_criterio=base.motivo_criterio,
        error=base.error,
        modelo=base.modelo,
        tokens_entrada=0,
        tokens_salida=0,
        heredada_de=base.dedupe_key,
        # Del PADRE, no de `fila`: este veredicto es el que salió de leer al
        # representante. Si el hermano guardara su propia longitud, una
        # vacante juzgada con el recorte cuyo hermano sí tiene texto entero
        # parecería juzgada con el anuncio completo, y la relectura se la
        # saltaría.
        descripcion_via=base.descripcion_via,
        caracteres_descripcion=base.caracteres_descripcion,
    )


# ---------------------------------------------------------------------------
#  El motor
# ---------------------------------------------------------------------------

class Motor:
    """Clasifica ofertas pendientes y las guarda.

    Recibe el cliente ya construido en vez de crearlo dentro. Eso es lo que
    permite que los tests le pasen un cliente falso y prueben todo el camino
    (detección de idioma, contraste, umbral, persistencia) sin red, sin API
    key y sin gastar un céntimo.
    """

    def __init__(
        self,
        almacen: Storage,
        fichas: Fichas,
        cliente: ClienteClasificador,
        presupuesto: GuardiaPresupuesto | None = None,
    ) -> None:
        self.almacen = almacen
        self.fichas = fichas
        self.cliente = cliente
        # `None` = sin techo. Solo lo usan los tests que no van de presupuesto;
        # el CLI siempre pasa uno, porque el techo por defecto se aplica en la
        # configuración. Ver clasificador/presupuesto.py.
        self.presupuesto = presupuesto
        self.sistema = constructor.construir_sistema(fichas)

    # -- plan ----------------------------------------------------------------

    def pendientes(
        self,
        incluir_obsoletas: bool = False,
        incluir_fallidas: bool = False,
        limite: int | None = None,
    ) -> list[sqlite3.Row]:
        return self.almacen.ofertas_pendientes(
            fichas_huella=self.fichas.huella,
            incluir_obsoletas=incluir_obsoletas,
            incluir_fallidas=incluir_fallidas,
            limit=limite,
        )

    def coste_estimado(self, filas: Sequence[sqlite3.Row]) -> tuple[int, int, float]:
        """(tokens_entrada, tokens_salida, USD) aproximados, SIN llamar a nada.

        Es lo que imprime `--dry-run`. La entrada se estima contando
        caracteres; la salida se fija en 400 tokens por clasificación, que es
        lo que ocupa la estructura más un razonamiento adaptativo corto. Es
        una estimación para decidir si lanzar, no una factura.

        Se cuenta sobre las vacantes AGRUPADAS, no sobre los anuncios: si el
        `--dry-run` cobrara por los cinco anuncios de la misma vacante,
        estaría prometiendo un gasto que la corrida real no va a hacer.
        """
        representantes = [rep for rep, _ in agrupar_para_ahorrar(filas)]

        entrada = salida = 0
        coste = 0.0
        for indice, fila in enumerate(representantes):
            # Se pasa la longitud de la DESCRIPCIÓN además de la del texto
            # entero: es lo que decide cuánto va a escribir el modelo, y
            # deducirlo del total depende de lo que mida el envoltorio del
            # prompt. Ver `cliente_llm.salida_tipica`.
            e, s_, c = coste_estimado_de_llamada(
                len(self._texto_oferta(fila)),
                primera_de_la_corrida=(indice == 0),
                caracteres_descripcion=len(descripcion_de(fila)),
            )
            entrada += e
            salida += s_
            coste += c
        return entrada, salida, coste

    def llamadas_previstas(self, filas: Sequence[sqlite3.Row]) -> int:
        """Cuántas llamadas costará clasificar esas filas, ya agrupadas."""
        return len(agrupar_para_ahorrar(filas))

    # -- ejecución -----------------------------------------------------------

    def clasificar(
        self,
        filas: Sequence[sqlite3.Row],
        avisar: Callable[[int, int, str], None] | None = None,
    ) -> ResumenCorrida:
        """`avisar(hecho, total, titulo)` se llama antes de cada vacante.

        Existe porque una corrida son ocho llamadas de ~10 segundos y sin esto
        no se distingue «va por la tercera» de «se colgó». La consola lo
        imprime y la bandeja lo convierte en su barra de progreso; que las dos
        lean la misma señal es lo que evita que una de ellas mienta.

        Es opcional y por defecto no hace nada: los tests no tienen que montarlo.
        """
        resumen = ResumenCorrida()

        # Una llamada por VACANTE, no por anuncio. Ver `agrupar_para_ahorrar`.
        grupos = agrupar_para_ahorrar(filas)

        for indice, (representante, companeras) in enumerate(grupos):
            if avisar is not None:
                avisar(indice + 1, len(grupos), representante["title"])

            # EL TECHO SE COMPRUEBA ANTES DE CADA LLAMADA, no una vez al
            # arrancar. Una estimación hecha al principio puede quedarse corta
            # —los anuncios largos cuestan más de lo previsto— y comprobar
            # sobre el gasto real es lo único que garantiza que no se pase.
            if self.presupuesto is not None:
                coste_previsto = self._coste_de_una(representante)
                if not self.presupuesto.permite(coste_previsto):
                    # Se para en seco. Lo que queda NO se toca: sigue sin fila
                    # en `clasificaciones`, así que la próxima corrida con
                    # saldo lo recoge sin que nadie tenga que acordarse.
                    restantes = grupos[indice:]
                    resumen.detenidas_por_presupuesto = sum(
                        1 + len(companeras_pendientes)
                        for _, companeras_pendientes in restantes
                    )
                    # EL MOTIVO LO REDACTA EL GUARDIA, que es quien sabe
                    # cuál de los dos frenos saltó. Aquí había una frase fija
                    # —«Presupuesto del mes agotado»— y desde que existe el
                    # tope por acción esa frase es falsa la mitad de las
                    # veces: una acción que topa se relanza y sigue, un mes
                    # agotado no. Se le pasa el coste rechazado porque es la
                    # misma condición con la que se dijo que no.
                    resumen.avisos.append(
                        f"{self.presupuesto.motivo_de_parada(coste_previsto)} "
                        f"Se pararon {resumen.detenidas_por_presupuesto} ofertas "
                        "sin clasificar; no se ha perdido ninguna."
                    )
                    break

            # EL GASTO SE APUNTA EN `ClienteAnthropic`, NO AQUÍ (3 sept 2026).
            #
            # Estaba en esta línea, con el comentario «se apunta ANTES de
            # guardar la clasificación», y esa regla se sigue cumpliendo: el
            # cliente apunta en cuanto la llamada vuelve, o sea aún antes que
            # esto. Lo que se ganó al moverlo es que deje de depender de que
            # cada capa se acuerde — eran tres capas y la cuarta, un script de
            # medición, se olvidó y se perdieron $0,1587 del libro.
            resultado = self._clasificar_una(representante)

            resumen.resultados.append(resultado)
            self._guardar(representante, resultado)

            for otra in companeras:
                heredado = _heredar(resultado, otra)
                resumen.resultados.append(heredado)
                self._guardar(otra, heredado)

        if self.presupuesto is not None:
            resumen.estado_presupuesto = self.presupuesto.estado()

        return resumen

    def _coste_de_una(self, fila: sqlite3.Row) -> float:
        """Lo que se estima que costará clasificar esta oferta.

        Misma fórmula que `coste_estimado`, sobre una sola fila. Es una
        estimación con la que decidir si cabe, no una factura: la de verdad
        sale de los tokens que devuelve la API.

        Se estima como una llamada NO primera, es decir leyendo la caché. En
        la primera de la corrida se queda corta por el precio de escribirla
        (~$0,03 una sola vez), y es el error barato: pasarse por arriba aquí
        haría que el techo se plantara antes de tiempo, que es justo el fallo
        que este arreglo viene a quitar.
        """
        _, _, coste = coste_estimado_de_llamada(
            len(self._texto_oferta(fila)),
            caracteres_descripcion=len(descripcion_de(fila)),
        )
        return coste

    def _texto_oferta(self, fila: sqlite3.Row) -> str:
        descripcion = descripcion_de(fila)
        deteccion = detectar_idioma(fila["title"], descripcion)
        return constructor.construir_oferta(
            titulo=fila["title"],
            empresa=fila["company"],
            ubicacion=fila["location_raw"] or "(sin ubicación)",
            mercado=fila["market_key"],
            descripcion=descripcion,
            idioma_detectado=deteccion.idioma,
        )

    def _clasificar_una(self, fila: sqlite3.Row) -> ResultadoOferta:
        descripcion, via = _elegir_descripcion(fila)

        # La empresa NO entra en la detección de idioma: "Instituto Global
        # Institute" es un nombre en inglés dentro de un anuncio en español, y
        # contarlo desviaría la decisión hacia el idioma equivocado.
        deteccion = detectar_idioma(fila["title"], descripcion)

        base = ResultadoOferta(
            dedupe_key=fila["dedupe_key"],
            titulo=fila["title"],
            empresa=fila["company"],
            mercado=fila["market_key"],
            estado="sin_clasificar",
            deteccion=deteccion,
            # Se apunta ANTES de la llamada, y por el mismo motivo que el
            # gasto: si la respuesta viene mal, la fila que quede tiene que
            # decir con qué texto se intentó. Una fila `sin_clasificar` sin
            # vía no se sabría si hay que releerla.
            descripcion_via=via,
            caracteres_descripcion=len(descripcion),
        )

        respuesta = self.cliente.clasificar(self.sistema, self._texto_oferta(fila))

        base.tokens_entrada = respuesta.tokens_entrada
        base.tokens_salida = respuesta.tokens_salida
        base.modelo = respuesta.modelo
        base.coste_medido_usd = respuesta.coste_usd

        if not respuesta.ok:
            base.error = respuesta.error
            base.necesita_criterio = True
            base.motivo_criterio = respuesta.error
            return base

        cv_final, necesita, motivo = _evaluar(self.fichas, respuesta, deteccion)

        base.estado = "ok"
        base.clasificacion = respuesta.clasificacion
        base.cv_final = cv_final
        base.necesita_criterio = necesita
        base.motivo_criterio = motivo
        return base

    def _guardar(self, fila: sqlite3.Row, resultado: ResultadoOferta) -> None:
        clas = resultado.clasificacion
        datos: dict[str, Any] = {
            "dedupe_key": resultado.dedupe_key,
            "estado": resultado.estado,
            "relevante": None if clas is None else int(clas.relevante),
            "motivo_descarte": None if clas is None else clas.motivo_descarte,
            "confianza": None if clas is None else clas.confianza,
            "razon": None if clas is None else clas.razon,
            "cv_id": resultado.cv_final,
            "idioma_anuncio": None if clas is None else clas.idioma_anuncio,
            "idioma_detectado": resultado.deteccion.idioma if resultado.deteccion else None,
            "empresa_normalizada": None if clas is None else clas.empresa_normalizada,
            "puesto_canonico": None if clas is None else clas.puesto_canonico,
            "es_reclutadora": None if clas is None else int(clas.es_reclutadora),
            "seniority_detectado": None if clas is None else clas.seniority_detectado,
            "modalidad": None if clas is None else clas.modalidad,
            "necesita_criterio": int(resultado.necesita_criterio),
            "motivo_criterio": resultado.motivo_criterio,
            "error": resultado.error,
            "modelo": resultado.modelo or "desconocido",
            "tokens_entrada": resultado.tokens_entrada,
            "tokens_salida": resultado.tokens_salida,
            "coste_usd": resultado.coste_usd,
            "fichas_huella": self.fichas.huella,
            "heredada_de": resultado.heredada_de,
            "descripcion_via": resultado.descripcion_via,
            "caracteres_descripcion": resultado.caracteres_descripcion,
        }
        self.almacen.guardar_clasificacion(datos)
