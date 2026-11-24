"""
Lo que ella decide. Fuera del control de versiones, a propósito.

POR QUÉ NO SE ESCRIBE EN `config/cvs.yaml`
------------------------------------------
Porque ese archivo ESTÁ VERSIONADO, y el actualizador se niega a actualizar
una copia con cambios locales:

    if ($antes.EndsWith('+')) { 'hay cambios locales: no se actualiza' }

O sea que la primera vez que ella tocara sus criterios desde la pantalla, su
máquina dejaría de recibir arreglos **para siempre**, en silencio, y a 9.000
km. El precio de guardar sus preferencias donde estaba el ejemplo sería
perder el único camino que hay hasta su portátil.

Así que el YAML se queda como SEMILLA —lo que trae la app de fábrica, escrito
por el desarrollador y versionado— y lo que ella escriba vive en
`data/preferencias.json`, junto a los contadores, fuera de git y a salvo de
un `checkout`.

QUÉ HAY AQUÍ DENTRO
-------------------
    criterios          Sus prioridades, con sus palabras. Van TAL CUAL al
                       modelo (ver `asistente.construir_sistema`), así que no
                       se reformulan ni se traducen a puntuaciones.
    cuantas_destacar   Cuántas quiere ver arriba del todo.
    automatica         Si la corrida diaria se hace sola, y a qué hora.

CAMBIAR ESTO NO CUESTA DINERO Y NO RECLASIFICA NADA. Las preferencias no
entran en la huella de las fichas (ver `Preferencias`), así que reordenar es
gratis y ninguna oferta se marca como obsoleta. Lo dice el YAML desde el
primer día y sigue siendo verdad.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AJUSTES_PATH = PROJECT_ROOT / "data" / "preferencias.json"

# CUÁNTOS CRITERIOS Y DE QUÉ LARGO. No es una manía: cada criterio viaja en el
# bloque de sistema de CADA llamada del asistente. Doce frases de 200
# caracteres son ~600 tokens, que con la caché de prompts es ruido; doscientas
# frases serían otra cosa. El tope está para que una caja de texto no pueda
# convertirse en una factura.
MAX_CRITERIOS = 12
MAX_LARGO_CRITERIO = 200

# La hora por defecto de la corrida diaria. Temprano, para que el trabajo esté
# hecho cuando ella abra; no a las 3 de la mañana, porque el portátil está
# apagado y lo que se quiere es que corra, no que quede bonito en el
# calendario.
HORA_POR_DEFECTO = "07:00"


class AjustesError(ValueError):
    """Lo que ella escribió no vale, y hay que decírselo con palabras suyas."""


@dataclass(frozen=True)
class Automatica:
    """La corrida diaria: si se hace sola y cuándo.

    VIENE APAGADA DE FÁBRICA, y es una decisión, no un olvido. Encenderla es
    gastar dinero suyo sin que ella esté delante; eso lo enciende ella, con el
    importe a la vista, no una actualización que le llega sin avisar.
    """

    activa: bool = False
    hora: str = HORA_POR_DEFECTO

    def a_json(self) -> dict[str, Any]:
        return {"activa": self.activa, "hora": self.hora}


@dataclass(frozen=True)
class Ajustes:
    criterios: list[str] = field(default_factory=list)
    cuantas_destacar: int = 3
    automatica: Automatica = field(default_factory=Automatica)

    def a_json(self) -> dict[str, Any]:
        return {
            "criterios": list(self.criterios),
            "cuantas_destacar": self.cuantas_destacar,
            "automatica": self.automatica.a_json(),
        }


def _validar_hora(valor: Any) -> str:
    texto = str(valor or "").strip()
    partes = texto.split(":")
    if len(partes) != 2:
        raise AjustesError("La hora se escribe como 07:00.")
    try:
        h, m = int(partes[0]), int(partes[1])
    except ValueError:
        raise AjustesError("La hora se escribe como 07:00.") from None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise AjustesError("La hora tiene que estar entre 00:00 y 23:59.")
    return f"{h:02d}:{m:02d}"


def validar(datos: dict[str, Any], *, semilla: Ajustes) -> Ajustes:
    """Lo que llega de la pantalla, comprobado. Lanza `AjustesError` con
    frases que se le pueden enseñar tal cual.

    Lo que no venga se queda como estaba: la pantalla puede mandar solo lo que
    cambió sin arrastrar el resto.
    """
    crudos = datos.get("criterios", semilla.criterios)
    if not isinstance(crudos, list):
        raise AjustesError("Los criterios son una lista de frases.")

    criterios = [" ".join(str(c).split()) for c in crudos]
    criterios = [c for c in criterios if c]

    if len(criterios) > MAX_CRITERIOS:
        raise AjustesError(
            f"Como mucho {MAX_CRITERIOS} criterios. Tienes {len(criterios)}: "
            "junta los que digan lo mismo, que el orden importa más que el "
            "número."
        )
    largos = [c for c in criterios if len(c) > MAX_LARGO_CRITERIO]
    if largos:
        raise AjustesError(
            f"Hay {len(largos)} criterio(s) de más de {MAX_LARGO_CRITERIO} "
            "caracteres. Pártelos en frases más cortas: se los lee un modelo, "
            "y una frase clara ordena mejor que un párrafo."
        )

    cuantas = datos.get("cuantas_destacar", semilla.cuantas_destacar)
    try:
        cuantas = int(cuantas)
    except (TypeError, ValueError):
        raise AjustesError("«Cuántas destacar» tiene que ser un número.") from None
    if cuantas < 1:
        raise AjustesError(
            "Hay que destacar al menos una. Un orden que no destaca nada no "
            "es un orden."
        )
    if cuantas > 20:
        raise AjustesError(
            "Como mucho 20 destacadas. Por encima de ahí no es destacar, es "
            "la lista entera otra vez."
        )

    bloque = datos.get("automatica")
    if bloque is None:
        automatica = semilla.automatica
    else:
        if not isinstance(bloque, dict):
            raise AjustesError("`automatica` es un bloque con `activa` y `hora`.")
        automatica = Automatica(
            activa=bool(bloque.get("activa", semilla.automatica.activa)),
            hora=_validar_hora(bloque.get("hora", semilla.automatica.hora)),
        )

    return Ajustes(
        criterios=criterios, cuantas_destacar=cuantas, automatica=automatica
    )


def leer(ruta: Path | None = None) -> dict[str, Any] | None:
    """Lo que ella guardó, o `None` si no ha guardado nada todavía.

    Un archivo ilegible se trata como si no existiera Y NO SE BORRA: se cae a
    los valores de fábrica, la app abre igual, y el archivo se queda ahí para
    poder mirarlo. Perder sus criterios en silencio sería peor que ignorarlos
    una vez.
    """
    destino = ruta or DEFAULT_AJUSTES_PATH
    try:
        with open(destino, "r", encoding="utf-8") as handle:
            datos = json.load(handle)
        return datos if isinstance(datos, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def guardar(ajustes: Ajustes, ruta: Path | None = None) -> Path:
    """Escribe sus ajustes. Sustitución atómica: un corte a mitad no los parte."""
    import os

    destino = ruta or DEFAULT_AJUSTES_PATH
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(ajustes.a_json(), handle, ensure_ascii=False, indent=2)
    os.replace(tmp, destino)
    return destino
