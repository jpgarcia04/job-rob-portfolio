"""
Carga y validación de `config/cvs.yaml`.

Mismo criterio que `recolector/config.py`: todo se valida AL ARRANCAR. Si
falta una ficha, si un archivo de CV no está en disco o si el umbral es un
disparate, el programa muere aquí con un mensaje que dice qué pasa y dónde
arreglarlo — no más tarde, a mitad de una corrida, con un KeyError.

El LLM NUNCA lee el archivo del CV. Lee la ficha: cuesta ~100 tokens en vez
de ~4.000, el ruteo se ajusta cambiando cuatro líneas en vez de reescribiendo
un CV, y el documento completo de la clienta —teléfono, dirección, historial—
no sale nunca de su máquina.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import ajustes as ajustes_propios

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CVS_PATH = PROJECT_ROOT / "config" / "cvs.yaml"

# Idiomas que el sistema sabe manejar. Es una lista corta a propósito: si
# algún día hay un CV en italiano, se añade aquí y en `idioma.py`, y el error
# de abajo te obliga a acordarte de los dos sitios.
IDIOMAS_SOPORTADOS = ("es", "en")


class FichasError(Exception):
    """`config/cvs.yaml` ausente, incompleto o incoherente."""


@dataclass(frozen=True)
class Ficha:
    """La descripción corta de un CV. Esto es lo que ve el modelo."""

    id: str
    nombre: str
    archivo: Path
    idioma: str
    sector: str
    puestos_objetivo: list[str]
    palabras_clave: list[str]
    seniority: str
    no_aplica: list[str]
    # Lo que SÍ le encaja pero un lector desprevenido descartaría. Hoy: la
    # consultoría independiente y los encargos interinos, que por el título son
    # indistinguibles de un consultor de sistemas o de recursos humanos. Es
    # justo el tipo de matiz que ninguna lista de términos puede expresar y que
    # el modelo sí resuelve leyendo la descripción.
    tambien_aplica: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Fichas:
    """Todas las fichas más el umbral, ya validadas."""

    umbral_confianza: float
    lista: list[Ficha]
    # Huella de las fichas. Se guarda con cada clasificación para poder decir
    # "esto se clasificó con una versión anterior de las fichas" sin tener que
    # guardar una copia del YAML entero. Ver `motor.py`.
    huella: str

    @property
    def ids(self) -> list[str]:
        return [f.id for f in self.lista]

    def por_id(self, cv_id: str) -> Ficha | None:
        """La ficha con ese id, o None. Devolver None en vez de lanzar es
        deliberado: si el modelo se inventa un `cv_id`, el motor lo trata como
        "sin clasificar" con motivo, no como una excepción.
        """
        for ficha in self.lista:
            if ficha.id == cv_id:
                return ficha
        return None

    def por_idioma(self, idioma: str) -> Ficha | None:
        """La ficha escrita en ese idioma. Con los dos CVs de hoy, esta es la
        función que resuelve el ruteo entero, sin gastar un token.
        """
        for ficha in self.lista:
            if ficha.idioma == idioma:
                return ficha
        return None


# ---------------------------------------------------------------------------
#  Lectura
# ---------------------------------------------------------------------------

def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    """Lee una clave obligatoria o muere con un mensaje útil."""
    if key not in mapping:
        raise FichasError(f"Falta la clave obligatoria '{key}' en {where}.")
    return mapping[key]


def _lista_de_textos(value: Any, where: str) -> list[str]:
    """Convierte una lista del YAML en una lista de textos no vacíos.

    Una lista vacía se acepta (una ficha puede no tener `no_aplica`), pero un
    valor que no sea lista es un error de escritura y hay que decirlo.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise FichasError(
            f"{where} debe ser una lista, y es {type(value).__name__}.\n"
            "En YAML una lista se escribe con guiones:\n"
            "  puestos_objetivo:\n"
            '    - "Director General"\n'
            '    - "COO"'
        )
    return [str(v).strip() for v in value if str(v).strip()]


def _resolver_ruta(raw: str) -> Path:
    """Las rutas del YAML son relativas a la raíz del proyecto, no al
    directorio desde el que se lanza el comando. Igual que en `config.py`.
    """
    path = Path(raw)
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def _parse_ficha(data: Any, indice: int) -> Ficha:
    where = f"config/cvs.yaml -> cvs[{indice}]"

    if not isinstance(data, dict):
        raise FichasError(f"{where} debe ser un bloque de claves, y es {type(data).__name__}.")

    cv_id = str(_require(data, "id", where)).strip()
    if not cv_id:
        raise FichasError(f"{where}.id está vacío.")

    idioma = str(_require(data, "idioma", where)).strip().lower()
    if idioma not in IDIOMAS_SOPORTADOS:
        raise FichasError(
            f"{where}.idioma = '{idioma}' no está soportado.\n"
            f"Admitidos: {', '.join(IDIOMAS_SOPORTADOS)}.\n"
            "Si añades un CV en otro idioma, hay que declararlo también en "
            "`clasificador/idioma.py`, o el ruteo no sabrá reconocerlo."
        )

    archivo = _resolver_ruta(str(_require(data, "archivo", where)))
    if not archivo.exists():
        raise FichasError(
            f"{where}.archivo apunta a un archivo que NO existe:\n"
            f"  {archivo}\n"
            "El archivo del CV vive en `data/cvs/`, que está ignorado por git: si acabas de\n"
            "clonar el repositorio, esa carpeta está vacía y hay que copiar los CVs a mano.\n"
            "El LLM no lo lee nunca, pero se comprueba que esté porque es lo que se adjuntará\n"
            "a la postulación, y descubrirlo el día del envío sería tarde."
        )

    return Ficha(
        id=cv_id,
        nombre=str(_require(data, "nombre", where)),
        archivo=archivo,
        idioma=idioma,
        sector=" ".join(str(_require(data, "sector", where)).split()),
        puestos_objetivo=_lista_de_textos(
            _require(data, "puestos_objetivo", where), f"{where}.puestos_objetivo"
        ),
        palabras_clave=_lista_de_textos(
            data.get("palabras_clave"), f"{where}.palabras_clave"
        ),
        seniority=" ".join(str(_require(data, "seniority", where)).split()),
        no_aplica=_lista_de_textos(data.get("no_aplica"), f"{where}.no_aplica"),
        tambien_aplica=_lista_de_textos(
            data.get("tambien_aplica"), f"{where}.tambien_aplica"
        ),
    )


def _calcular_huella(bloque_cvs: Any, umbral: float) -> str:
    """SHA-256 de las fichas, estable frente al orden de las claves.

    `sort_keys=True` es lo que la hace estable: sin eso, reordenar dos líneas
    del YAML cambiaría la huella y marcaría como obsoleto todo el histórico
    sin que nada haya cambiado de verdad.

    El umbral entra en la huella a propósito: si ella lo baja de 0,70 a 0,50,
    las clasificaciones que quedaron en "necesita tu criterio" podrían pasar a
    resolverse solas, y eso es un cambio de criterio real.
    """
    payload = json.dumps(
        {"cvs": bloque_cvs, "umbral_confianza": umbral},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def cargar_fichas(path: Path | None = None) -> Fichas:
    """Lee `config/cvs.yaml`, lo valida entero y devuelve las fichas."""
    ruta = path or DEFAULT_CVS_PATH

    if not ruta.exists():
        raise FichasError(
            f"No existe el archivo de fichas de CV:\n  {ruta}\n"
            "Es el archivo donde se describe cada CV (sector, puestos, palabras clave).\n"
            "Sin él no se puede clasificar nada."
        )

    with ruta.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    if not isinstance(data, dict):
        raise FichasError(f"{ruta} no contiene un YAML válido de claves y valores.")

    bloque_cvs = _require(data, "cvs", "config/cvs.yaml")
    if not isinstance(bloque_cvs, list) or not bloque_cvs:
        raise FichasError(
            "config/cvs.yaml -> `cvs` debe ser una lista con al menos una ficha.\n"
            "Sin fichas no hay criterio con el que decidir nada."
        )

    umbral = float(data.get("umbral_confianza", 0.70))
    if not 0.0 < umbral < 1.0:
        raise FichasError(
            f"config/cvs.yaml -> umbral_confianza = {umbral} está fuera de rango.\n"
            "Tiene que estar entre 0 y 1 (sin incluirlos): es una probabilidad, no un porcentaje.\n"
            "Con 0 no se dudaría nunca; con 1 se dudaría siempre."
        )

    lista = [_parse_ficha(item, i) for i, item in enumerate(bloque_cvs)]

    # Ids repetidos: sería un fallo silencioso caro. `por_id` devolvería
    # siempre la primera y la segunda ficha no se usaría jamás.
    vistos: set[str] = set()
    for ficha in lista:
        if ficha.id in vistos:
            raise FichasError(
                f"El id de CV '{ficha.id}' está repetido en config/cvs.yaml.\n"
                "Cada ficha necesita un id único: es la clave con la que se guarda "
                "la decisión y con la que la bandeja sabe qué CV enseñar."
            )
        vistos.add(ficha.id)

    # Dos fichas en el mismo idioma: hoy no se sabría cuál elegir por idioma.
    # No es un error fatal (podrían ser dos sectores distintos en español),
    # pero el ruteo por idioma dejaría de ser suficiente, así que se avisa
    # devolviendo el aviso al llamante en vez de morir.
    return Fichas(
        umbral_confianza=umbral,
        lista=lista,
        huella=_calcular_huella(bloque_cvs, umbral),
    )


@dataclass(frozen=True)
class Preferencias:
    """Cómo quiere ella que se le ordenen las ofertas que ya pasaron el filtro.

    NO ENTRA EN LA HUELLA DE LAS FICHAS, y es deliberado. La huella marca las
    clasificaciones como obsoletas, y reclasificar cuesta dinero. Cambiar el
    orden en el que se le enseñan las ofertas no cambia el veredicto de
    ninguna, así que no puede provocar un gasto. Ordenar es barato;
    reclasificar no.
    """

    criterios: list[str]
    cuantas_destacar: int

    @property
    def hay_criterios(self) -> bool:
        return bool(self.criterios)


def cargar_preferencias(
    path: Path | None = None, ajustes: Path | None = None
) -> Preferencias:
    """Sus prioridades: la semilla de `config/cvs.yaml`, y encima las suyas.

    DE DÓNDE SALE CADA MITAD, Y POR QUÉ SON DOS. El YAML trae lo que la app
    lleva de fábrica —escrito por el desarrollador, versionado, el mismo en
    todas las copias—. Lo que ella escriba desde la pantalla vive en
    `data/preferencias.json` y MANDA sobre el YAML.

    No se escriben en el YAML porque ese archivo está versionado y el
    actualizador se niega a actualizar una copia con cambios locales: la
    primera vez que ella tocara sus criterios, su portátil dejaría de recibir
    arreglos para siempre. Ver `clasificador/ajustes.py`.

    Se carga aparte de `cargar_fichas` a propósito: son dos cosas con ciclos
    de vida distintos que casualmente viven en el mismo archivo. Las fichas
    describen sus CVs; esto describe sus prioridades, y las prioridades cambian
    mucho más a menudo.

    El bloque es opcional: sin él, el asistente ordena solo por lo que sabe de
    cada oferta, sin criterio propio de ella.
    """
    ruta = path or DEFAULT_CVS_PATH

    if not ruta.exists():
        raise FichasError(f"No existe {ruta}.")

    with ruta.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    bloque = data.get("preferencias") or {}
    if not isinstance(bloque, dict):
        raise FichasError(
            "config/cvs.yaml -> `preferencias` debe ser un mapa con `criterios` "
            "y `cuantas_destacar`."
        )

    crudos = bloque.get("criterios") or []
    if not isinstance(crudos, list):
        raise FichasError(
            "config/cvs.yaml -> `preferencias.criterios` debe ser una lista de frases.\n"
            "Se le pasan al modelo tal cual, así que escríbelas como se lo dirías a "
            "una persona."
        )

    criterios = [str(c).strip() for c in crudos if str(c).strip()]

    cuantas = int(bloque.get("cuantas_destacar", 3))
    if cuantas < 1:
        raise FichasError(
            "config/cvs.yaml -> `preferencias.cuantas_destacar` tiene que ser al menos 1. "
            "Un ranking que no destaca nada no es un ranking."
        )

    # LO SUYO MANDA. Se aplica al final y campo a campo: un archivo suyo a
    # medias —solo la hora de la corrida, por ejemplo— no puede borrarle los
    # criterios que trae la semilla.
    suyos = ajustes_propios.leer(ajustes)
    if suyos:
        propios = suyos.get("criterios")
        if isinstance(propios, list):
            limpios = [" ".join(str(c).split()) for c in propios]
            criterios = [c for c in limpios if c]
        try:
            cuantas = max(1, int(suyos.get("cuantas_destacar", cuantas)))
        except (TypeError, ValueError):
            pass

    return Preferencias(criterios=criterios, cuantas_destacar=cuantas)


def avisos_de_fichas(fichas: Fichas) -> list[str]:
    """Cosas raras que no impiden funcionar pero conviene ver en consola."""
    avisos: list[str] = []

    por_idioma: dict[str, list[str]] = {}
    for ficha in fichas.lista:
        por_idioma.setdefault(ficha.idioma, []).append(ficha.id)

    for idioma, ids in sorted(por_idioma.items()):
        if len(ids) > 1:
            avisos.append(
                f"Hay {len(ids)} fichas en idioma '{idioma}' ({', '.join(ids)}). "
                "El idioma ya no basta para elegir CV: la decisión pasa a depender "
                "del sector, y ahí es donde el criterio del modelo empieza a importar."
            )

    faltan = [i for i in IDIOMAS_SOPORTADOS if i not in por_idioma]
    if faltan:
        avisos.append(
            f"No hay ninguna ficha en idioma: {', '.join(faltan)}. "
            "Las ofertas en ese idioma recibirán el CV disponible más cercano."
        )

    return avisos
