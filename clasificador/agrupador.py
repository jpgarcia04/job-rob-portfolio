"""
Ofertas que son la MISMA vacante.

EL PROBLEMA, CON DATOS REALES
-----------------------------
De las 21 ofertas de la primera corrida, siete eran esta misma vacante:

    Northline Asset Services              | Chief Operating Officer   | Austin
    Northline Asset Services              | Chief Operating Officer   | Dallas
    Northline Asset Services              | Chief Operating Officer   | Fort Worth
    Northline Asset Services              | Chief Operating Officer   | Houston
    Northline Asset Services              | Chief Operating Officer   | San Antonio
    Northline Asset Services              | COO, Asset Maintenance…   | Dallas
    Harlan Contracting Group, LLC | COO — National Infra…     | Austin

Un tercio del inventario de Texas era un solo puesto. Si ella ve siete
tarjetas, revisa siete veces lo mismo y pierde la confianza en la herramienta.

CÓMO SE RESUELVE, EN DOS MITADES
--------------------------------
1. **Gratis.** Las cinco primeras comparten `loose_key` — la clave laxa que
   la v0 ya calculaba (empresa + título, sin ubicación) y que hasta ahora
   solo servía para MEDIR la sub-fusión en el reporte. Cero tokens.

2. **Con el modelo.** Las dos últimas no: cambia el título en una y el nombre
   de la empresa en la otra. Ahí entran `empresa_normalizada` y
   `puesto_canonico`, que vienen de la MISMA llamada que ya se hizo para
   clasificar. No cuesta una llamada extra.

Esto es comparar ofertas ENTRE SÍ, que es lo único que la clasificación por
oferta no puede hacer. Pero lo hace Python sobre lo que el modelo normalizó,
no un bucle agéntico: el modelo sigue viendo una oferta y devolviendo una
estructura, como fija el brief.

POR QUÉ LA CLAVE LAXA NO SE USÓ ANTES
-------------------------------------
Porque en la v0 medía, no decidía. `dedupe.py` lo explica: una empresa que
publica el mismo puesto en Monterrey, CDMX y Guadalajara son tres
oportunidades reales. Eso sigue siendo cierto, y por eso aquí **no se borra
nada**: las siete filas siguen en la base, con sus siete URLs. Lo que cambia
es que la bandeja enseña una tarjeta y dice "publicada en 5 ubicaciones".
Agrupar no es deduplicar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from recolector.textnorm import normalize_company, normalize_title


@dataclass
class Miembro:
    """Una oferta dentro de un grupo."""

    dedupe_key: str
    titulo: str
    empresa: str
    ubicacion: str
    mercado: str
    url: str | None
    estado: str


@dataclass
class Grupo:
    """Varias ofertas que son la misma vacante."""

    miembros: list[Miembro] = field(default_factory=list)
    motivos: set[str] = field(default_factory=set)

    @property
    def tamano(self) -> int:
        return len(self.miembros)

    @property
    def representante(self) -> Miembro:
        """La que se enseña. Se elige la de título más largo a propósito:
        "COO — National Logistics Growth Leader" dice más que
        "Chief Operating Officer", y si hay que enseñar una, mejor la
        informativa.
        """
        return max(self.miembros, key=lambda m: len(m.titulo))

    @property
    def ubicaciones(self) -> list[str]:
        vistas: list[str] = []
        for m in self.miembros:
            if m.ubicacion and m.ubicacion not in vistas:
                vistas.append(m.ubicacion)
        return vistas

    @property
    def claves_secundarias(self) -> list[str]:
        rep = self.representante.dedupe_key
        return [m.dedupe_key for m in self.miembros if m.dedupe_key != rep]

    def resumen(self) -> str:
        if self.tamano == 1:
            return ""
        # Separador " · " y no coma: `location_raw` ya trae comas dentro
        # ("Austin, Travis County") y unir con comas produce una lista
        # ilegible en la que no se ve dónde acaba cada ubicación.
        sitios = " · ".join(self.ubicaciones[:5])
        cola = "…" if len(self.ubicaciones) > 5 else ""
        return (
            f"La misma vacante aparece {self.tamano} veces"
            + (f" ({sitios}{cola})" if sitios else "")
            + "."
        )


class _Union:
    """Union-find: junta elementos en conjuntos por parejas.

    Nota de Python / algoritmia: cada elemento apunta a un "padre"; el
    representante de un conjunto es el que se apunta a sí mismo. `unir(a, b)`
    hace que los dos conjuntos pasen a tener el mismo representante. Es la
    forma estándar de resolver "si A y B son lo mismo, y B y C son lo mismo,
    entonces A y C también" sin escribir tres bucles anidados.

    Hace falta exactamente por eso: la oferta de Dallas se junta con las otras
    cuatro por la clave laxa, y la de Harlan se junta con una de ellas por
    el nombre normalizado. El resultado tiene que ser UN grupo de siete, no
    dos grupos sueltos.
    """

    def __init__(self) -> None:
        self._padre: dict[str, str] = {}

    def anadir(self, x: str) -> None:
        self._padre.setdefault(x, x)

    def raiz(self, x: str) -> str:
        self.anadir(x)
        while self._padre[x] != x:
            # Compresión de caminos: de paso, se acorta la cadena.
            self._padre[x] = self._padre[self._padre[x]]
            x = self._padre[x]
        return x

    def unir(self, a: str, b: str) -> None:
        ra, rb = self.raiz(a), self.raiz(b)
        if ra != rb:
            self._padre[rb] = ra


def _valor(fila: Any, clave: str) -> Any:
    """Lee una columna tanto de un `sqlite3.Row` como de un diccionario.

    Nota de Python: `sqlite3.Row` se indexa como un diccionario (`fila["x"]`)
    pero NO tiene `.get()`, así que pedirle una columna que no existe lanza
    IndexError en vez de devolver None. Los tests pasan diccionarios normales;
    esta función hace que las dos cosas funcionen igual.
    """
    try:
        return fila[clave]
    except (IndexError, KeyError):
        return None


def _clave_normalizada(empresa: str | None, puesto: str | None) -> str | None:
    """La clave que sale de lo que normalizó el modelo.

    Se pasa por `normalize_company` / `normalize_title` de la v0 encima, para
    que "Harlan Contracting Group, LLC" y "Harlan Contracting Group"
    colapsen aunque el modelo deje la forma jurídica en una de las dos.
    """
    if not empresa or not puesto:
        return None
    e = normalize_company(empresa)
    p = normalize_title(puesto)
    if not e or not p:
        return None
    return f"{e}||{p}"


def agrupar(filas: Iterable[Any]) -> list[Grupo]:
    """Agrupa las ofertas. Devuelve los grupos ordenados de mayor a menor.

    `filas` son las que devuelve `Storage.claves_de_agrupacion()`: cada una
    trae la oferta y, si ya pasó por el LLM, sus campos `clas_*`.
    """
    filas = list(filas)
    union = _Union()

    # Índices inversos: qué ofertas comparten cada clave.
    por_laxa: dict[str, list[str]] = {}
    por_normalizada: dict[str, list[str]] = {}
    # El nombre TAL CUAL lo escribió el modelo, para poder citarlo en el
    # motivo. La clave de agrupación va en minúsculas y sin forma jurídica,
    # y enseñarla así ("harlan contracting group") queda descuidado.
    nombre_para_mostrar: dict[str, str] = {}

    for fila in filas:
        clave = fila["dedupe_key"]
        union.anadir(clave)

        laxa = _valor(fila, "loose_key")
        if laxa:
            por_laxa.setdefault(laxa, []).append(clave)

        normalizada = _clave_normalizada(
            _valor(fila, "clas_empresa"), _valor(fila, "clas_puesto")
        )
        if normalizada:
            por_normalizada.setdefault(normalizada, []).append(clave)
            nombre_para_mostrar.setdefault(normalizada, _valor(fila, "clas_empresa"))

    motivos: dict[tuple[str, str], str] = {}

    def _unir_todas(claves: list[str], motivo: str) -> None:
        for otra in claves[1:]:
            union.unir(claves[0], otra)
            motivos[(claves[0], otra)] = motivo

    for claves in por_laxa.values():
        if len(claves) > 1:
            _unir_todas(claves, "misma empresa y mismo título (sin contar la ubicación)")

    for clave_norm, claves in por_normalizada.items():
        if len(claves) > 1:
            empresa = nombre_para_mostrar.get(clave_norm) or clave_norm.split("||")[0]
            _unir_todas(
                claves,
                f"el modelo reconoció que son la misma empresa: «{empresa}»",
            )

    # Reunir los miembros de cada conjunto.
    grupos: dict[str, Grupo] = {}
    for fila in filas:
        raiz = union.raiz(fila["dedupe_key"])
        grupo = grupos.setdefault(raiz, Grupo())
        grupo.miembros.append(
            Miembro(
                dedupe_key=fila["dedupe_key"],
                titulo=fila["title"],
                empresa=fila["company"],
                ubicacion=_valor(fila, "location_raw") or "",
                mercado=_valor(fila, "market_key") or "",
                url=_valor(fila, "canonical_url"),
                estado=_valor(fila, "status") or "nueva",
            )
        )

    for (a, b), motivo in motivos.items():
        grupo = grupos.get(union.raiz(a))
        if grupo is not None:
            grupo.motivos.add(motivo)

    return sorted(grupos.values(), key=lambda g: (-g.tamano, g.representante.titulo))


def grupos_con_repeticion(grupos: Iterable[Grupo]) -> list[Grupo]:
    """Solo los que tienen más de una oferta. Es lo que hay que avisar."""
    return [g for g in grupos if g.tamano > 1]
