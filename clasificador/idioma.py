"""
Detección del idioma del anuncio — y con ella, el CV que le corresponde.

POR QUÉ ESTO NO USA EL LLM
--------------------------
Los dos CVs de la clienta describen la misma trayectoria; lo único que los
separa es el idioma. Así que "qué CV adjunto" no es una decisión de sector,
es una decisión de idioma, y el idioma de un texto de 500 caracteres se
resuelve contando palabras funcionales. Gastar una llamada a un modelo de
frontera en esto sería pagar por algo que un diccionario de 40 palabras hace
mejor, más rápido y sin conexión.

El modelo devuelve TAMBIÉN su lectura del idioma (ver `esquema.py`). No para
decidir: para poder discrepar. Cuando las dos lecturas no coinciden, el motor
lo trata como una duda y la oferta va a "necesita tu criterio". Que las dos
señales se contradigan es exactamente el caso raro que merece un humano.

POR QUÉ NO SE USA UNA LIBRERÍA
------------------------------
`langdetect` y compañía funcionan bien, pero traen un modelo entrenado, no
explican sus decisiones y añaden una dependencia a un proyecto que se
presume tirar entero. Aquí el problema es de tres idiomas, no de sesenta.

POR QUÉ HAY UN TERCER IDIOMA SI SOLO HAY DOS CVs
------------------------------------------------
Desde que Alemania sustituyó a México (4 septiembre 2026) entran anuncios
escritos en alemán, y hasta hoy este módulo no sabía nombrarlos: los medía
con la regla del español y la del inglés, y ganaba una de las dos. Salía
`en` —el CV correcto— pero **por casualidad**, y con el recorte de 500
caracteres de la API salía `es` en 4 de los 36 anuncios alemanes de la base.

Detectar `de` no es para adjuntar un CV alemán, que no existe: es para que la
decisión de adjuntar el inglés sea una decisión escrita (ver `CV_SUPLENTE` en
`motor.py`) y para que el idioma que se guarda con la clasificación diga la
verdad. Un `idioma_detectado = "es"` sobre un anuncio alemán es un dato
falso, y los datos falsos se leen igual que los buenos.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Palabras funcionales: artículos, preposiciones y conjunciones. Se eligen
# porque aparecen en cualquier texto del idioma independientemente del tema,
# y porque casi no se solapan entre los tres idiomas.
#
# Ojo con las que SÍ se solapan y por eso NO están en ninguna lista:
#   "a"  -> artículo en inglés, preposición en español
#   "no" -> existe en los dos
#   "in" -> inglés, pero aparece en "in situ" y en nombres propios
MARCADORES_ES = frozenset("""
    el la los las un una unos unas de del al y o pero
    que con para por en sobre entre desde hasta
    su sus este esta estos estas
    es son ser esta estan tiene tienen
    como mas muy tambien ademas donde cuando
""".split())

MARCADORES_EN = frozenset("""
    the of and or but to for with from into
    is are be been was were has have had
    this that these those
    you your we our they their its
    will would should can could
    as at on by about than
""".split())

# Terminaciones muy características. Ayudan cuando el texto es corto y hay
# pocas palabras funcionales: un anuncio que solo lista requisitos técnicos
# puede no tener ni un "the" ni un "de", pero sí "experiencia" y "gestión".
# Marcadores del alemán. Se eligen con el MISMO criterio que los de arriba:
# palabras funcionales que no se solapan con las otras dos listas. Van sin
# diéresis porque `_normalizar` quita los diacríticos antes de contar, así que
# "für" llega aquí como "fur" y "über" como "uber".
#
# Ojo con las que se solapan y por eso NO están:
#   "an", "am", "so", "man", "in"  -> palabras inglesas corrientes
#   "was"                          -> está en MARCADORES_EN como verbo
#   "hat", "war"                   -> sustantivos ingleses (sombrero, guerra)
#   "also"                         -> en alemán es "por tanto"; en inglés, "también"
#   "es"                           -> está en MARCADORES_ES como verbo, y en
#                                     alemán es el pronombre neutro. Choca de
#                                     frente y se queda fuera de esta lista
#   "die"                          -> SÍ está, y es la única concesión: en
#                                     inglés es un verbo ("morir") y un
#                                     sustantivo de matricería, y ninguno de
#                                     los dos aparece en una oferta directiva
MARCADORES_DE = frozenset("""
    der die das den dem des
    und oder aber sowie
    ein eine einen einem einer eines
    fur mit von bei aus auf nach vor uber unter zwischen durch ohne gegen seit bis
    zu zum zur im ins beim vom
    ist sind wird werden wurde wurden
    haben hatte hatten kann konnen muss mussen soll sollen
    wir uns unser unsere unseren unserem
    sie ihr ihre ihren ihrem ihrer ihnen
    nicht auch noch schon sehr mehr
    dass damit weil wenn denn sich als wie
""".split())

SUFIJOS_ES = ("cion", "ciones", "dad", "mente", "miento", "ncia", "ivo", "ivas")
SUFIJOS_EN = ("ing", "tion", "ment", "ness", "ship", "able", "ive")
# Terminaciones alemanas. "ung" no pisa al inglés porque `_contar` exige más
# de 5 letras y la única palabra inglesa que acaba así es "young", que tiene 5.
SUFIJOS_DE = ("ung", "ungen", "keit", "heit", "schaft", "lich",
              "isch", "ische", "ischen", "isches", "ischer",
              "ieren", "itat")

# Debajo de esto no hay señal suficiente para afirmar nada. Una descripción
# de dos líneas puede ser cualquier idioma.
MINIMO_PALABRAS = 12


@dataclass(frozen=True)
class DeteccionIdioma:
    """El resultado, con su razonamiento a la vista.

    `motivo` existe porque este módulo decide qué CV se manda, y una decisión
    que afecta a una postulación real no puede ser una caja negra ni siquiera
    cuando la toma un `if`.
    """

    idioma: str          # "es" | "en" | "de" | "desconocido"
    confianza: float     # 0.0 - 1.0
    motivo: str

    @property
    def es_fiable(self) -> bool:
        return self.idioma != "desconocido" and self.confianza >= 0.60


def _normalizar(texto: str) -> list[str]:
    """Pasa a minúsculas, quita acentos y parte en palabras.

    Se quitan los acentos a propósito: media internet publica en español sin
    tildes, y "gestion" tiene que contar igual que "gestión". La pista del
    acento se recupera aparte, en `_hay_caracteres_hispanos`.

    Nota de Python: `unicodedata.normalize("NFD", ...)` separa la letra de su
    tilde en dos caracteres, y el filtro de abajo se queda solo con la letra.

    La 'ß' se cambia a "ss" ANTES de nada, y no es cosmética. NFD no la
    descompone —es una letra por derecho propio, no una 's' con adorno— así
    que `[a-z]+` partía la palabra en dos: "großes" salía como ["gro", "es"],
    y ese "es" de regalo se contaba como marcador de ESPAÑOL. Un anuncio
    alemán se ganaba puntos de español por su propia ortografía.
    """
    con_ss = texto.lower().replace("ß", "ss")
    descompuesto = unicodedata.normalize("NFD", con_ss)
    sin_acentos = "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-z]+", sin_acentos)


# Caracteres que existen en UN idioma y no en los otros dos. Es la señal más
# barata y más fiable que hay, cuando aparece.
#
# La 'ü' NO está en la lista del español, y quitarla fue el arreglo del 5
# septiembre 2026: la comparten los dos idiomas ("vergüenza", "für"), y como
# el empujón vale 6 puntos, los 4 anuncios alemanes que salían como españoles
# con el recorte de la API lo hacían POR LA DIÉRESIS Y POR NADA MÁS — tres de
# ellos no tenían ni una sola palabra funcional española. Una señal que dos
# idiomas comparten no puede puntuar para uno solo.
CARACTERES_ES = r"[ñáéíóú¿¡]"
CARACTERES_DE = r"[äöß]"


def _hay_caracteres_hispanos(texto: str) -> bool:
    return bool(re.search(CARACTERES_ES, texto.lower()))


def _hay_caracteres_alemanes(texto: str) -> bool:
    return bool(re.search(CARACTERES_DE, texto.lower()))


def _contar(palabras: list[str], marcadores: frozenset[str], sufijos: tuple[str, ...]) -> int:
    directos = sum(1 for p in palabras if p in marcadores)
    # Los sufijos pesan la mitad: son una pista, no una prueba. "information"
    # y "información" comparten forma, y "ing" aparece dentro de palabras
    # españolas como "ingeniería".
    por_sufijo = sum(1 for p in palabras if len(p) > 5 and p.endswith(sufijos))
    return directos * 2 + por_sufijo


def detectar_idioma(*partes: str | None) -> DeteccionIdioma:
    """Decide el idioma —`es`, `en` o `de`— a partir del título y la descripción.

    Se pasan por separado en vez de concatenados por el llamante para que
    este módulo pueda ignorar los `None` sin que cada llamada tenga que
    acordarse de hacerlo.
    """
    texto = " ".join(p for p in partes if p)
    palabras = _normalizar(texto)

    if len(palabras) < MINIMO_PALABRAS:
        return DeteccionIdioma(
            idioma="desconocido",
            confianza=0.0,
            motivo=(
                f"Solo {len(palabras)} palabras de texto; hacen falta {MINIMO_PALABRAS} "
                "para afirmar un idioma."
            ),
        )

    puntos = {
        "es": _contar(palabras, MARCADORES_ES, SUFIJOS_ES),
        "en": _contar(palabras, MARCADORES_EN, SUFIJOS_EN),
        "de": _contar(palabras, MARCADORES_DE, SUFIJOS_DE),
    }

    # Un empujón fuerte, no una decisión: un anuncio en inglés puede contener
    # "Peña Nieto" o "Nuevo León" y eso no lo hace español, igual que un
    # "Düsseldorf" no hace alemán a un anuncio escrito en inglés.
    detalle_caracteres = ""
    if _hay_caracteres_hispanos(texto):
        puntos["es"] += 6
        detalle_caracteres = ", con caracteres propios del español (ñ/tildes)"
    if _hay_caracteres_alemanes(texto):
        puntos["de"] += 6
        detalle_caracteres += ", con caracteres propios del alemán (ä/ö/ß)"

    if sum(puntos.values()) == 0:
        return DeteccionIdioma(
            idioma="desconocido",
            confianza=0.0,
            motivo="El texto no contiene palabras funcionales reconocibles de ninguno de los tres idiomas.",
        )

    # Empates: el orden de esta lista es el desempate, y es el mismo de antes
    # —español por delante de inglés— con el alemán detrás. Que sea explícito
    # importa: `max()` sobre un diccionario desempata por orden de inserción,
    # que es una casualidad de implementación y no una decisión.
    orden = ("es", "en", "de")
    ranking = sorted(orden, key=lambda k: (-puntos[k], orden.index(k)))
    idioma = ranking[0]
    ganador, segundo = puntos[ranking[0]], puntos[ranking[1]]

    # La confianza se mide contra el SEGUNDO, no contra la suma de los tres.
    # Con dos idiomas las dos cuentas daban lo mismo; con tres, dividir por la
    # suma bajaría la confianza de todos los anuncios ingleses sin que hubiera
    # aparecido ninguna duda nueva, y algunos cruzarían el umbral de 0,60
    # hacia abajo. La pregunta que decide es "¿le gana claramente al que más
    # se le parece?", y esa es esta cuenta.
    confianza = ganador / (ganador + segundo) if (ganador + segundo) else 0.0

    return DeteccionIdioma(
        idioma=idioma,
        confianza=round(confianza, 2),
        motivo=(
            f"Marcadores de español: {puntos['es']}; de inglés: {puntos['en']}; "
            f"de alemán: {puntos['de']}"
            f"{detalle_caracteres}. Sobre {len(palabras)} palabras."
        ),
    )
