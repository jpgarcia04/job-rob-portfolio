"""
La primera búsqueda de un mercado nuevo, lanzada sola al abrir.

EL PROBLEMA QUE RESUELVE, Y NO ES DE PROGRAMACIÓN
-------------------------------------------------
Un mercado nuevo llega a su portátil dentro de una etiqueta de git. La etiqueta
lleva código y configuración; NO lleva ofertas, porque `data/` está fuera de
git y nunca sale de la máquina del desarrollador.

Y la lista de mercados de la pantalla se construye con las ofertas GUARDADAS
(`Storage.counts_by_market`), no con la configuración. O sea que el día que
México entró en el YAML, ella abrió la app y no vio absolutamente nada: ni el
mercado en el desplegable, ni una sola oferta mexicana. Todo correcto por
dentro y nada visible por fuera, que es la peor forma de entregar un cambio —
parece que no funcionó.

El arreglo es que la app haga ella sola, UNA VEZ, la búsqueda que hasta ahora
había que pedirle a mano.

POR QUÉ ESTO SE PUEDE HACER SIN PREGUNTAR, Y LA CORRIDA AUTOMÁTICA NO
---------------------------------------------------------------------
Son dos cosas distintas y conviene no confundirlas:

  · `bandeja/programada.py` enciende un reloj que CLASIFICA todos los días, y
    clasificar gasta dinero de su clave de Anthropic. Por eso viene apagado de
    fábrica y lo decide ella con el importe delante.

  · Esto solo BUSCA (`python -m recolector run`). Buscar cuesta peticiones de
    Adzuna —que se regeneran solas— y **cero dólares**. El propio diálogo de
    la bandeja lo dice: `moneda: peticiones`, `coste_usd: 0.0`.

Lo que se trae queda PENDIENTE de clasificar, igual que lo que trae el botón.
Ningún modelo se dispara detrás de esto.

POR QUÉ VIVE EN EL SERVIDOR Y NO EN `herramientas/abrir.py`
-----------------------------------------------------------
Medido el 11 septiembre 2026: la cáscara de escritorio ya está cargada en
memoria cuando el actualizador cambia los archivos, así que sigue corriendo la
versión ANTERIOR y lo que se ponga allí tarda DOS aperturas en hacer efecto. El
servidor arranca después del `checkout`. Un estreno que llega tarde no es un
estreno. Mismo razonamiento que `reparar_libro_de_la_entrega` y que
`programada.asegurar`.

CUÁNDO SE DISPARA, EXACTAMENTE
------------------------------
Cuando hay un mercado en el perfil que cumple las dos cosas:

    1. no tiene NI UNA oferta en la base, y
    2. no está apuntado en `data/estrenos.json`.

La segunda condición es la que impide que esto corra todos los días. Sin ella,
un mercado que de verdad no devuelve nada —Tamaulipas es el candidato obvio—
lanzaría una búsqueda en cada apertura para siempre.

Se apuntan los mercados ANTES de lanzar y no después: si la búsqueda falla, o
ella cierra la ventana a mitad, no se reintenta sola en bucle. El botón
«Buscar ofertas nuevas» sigue ahí y es de ella.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ARCHIVO = "estrenos.json"


def _ruta(directorio_datos: Path) -> Path:
    return directorio_datos / ARCHIVO


def ya_estrenados(directorio_datos: Path) -> set[str]:
    """Los mercados a los que ya se les lanzó su primera búsqueda.

    Un archivo ilegible NO se trata como «ninguno»: eso volvería a lanzar la
    búsqueda de todos los mercados. Se trata como «todos», que es el lado que
    no hace nada raro a sus espaldas.
    """
    ruta = _ruta(directorio_datos)
    if not ruta.exists():
        return set()
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _TODOS
    if not isinstance(datos, dict):
        return _TODOS
    return {str(k) for k in datos.get("mercados", []) if k}


class _Todos(frozenset):
    """Un conjunto que contiene cualquier cosa. Ver `ya_estrenados`."""

    def __contains__(self, item: object) -> bool:  # noqa: D105
        return True


_TODOS = _Todos()


def apuntar(directorio_datos: Path, mercados: list[str]) -> None:
    """Deja constancia de que a estos mercados ya se les hizo el estreno."""
    ruta = _ruta(directorio_datos)
    previos = ya_estrenados(directorio_datos)
    # `_TODOS` no se puede unir: ahí el archivo estaba roto y lo que toca es
    # reescribirlo con lo que sabemos ahora.
    anteriores = [] if isinstance(previos, _Todos) else sorted(previos)
    todos = sorted(set(anteriores) | set(mercados))
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(
        json.dumps({"mercados": todos}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def mercados_a_estrenar(
    claves_del_perfil: list[str],
    ofertas_por_mercado: dict[str, int],
    estrenados: set[str],
) -> list[str]:
    """Los mercados que están configurados y todavía no se han visto nunca.

    Función pura, sin disco y sin red, para poder probar la decisión entera sin
    abrir un proceso. Es la misma separación que usa `trabajos.Registro` con su
    lanzador.
    """
    return [
        clave
        for clave in claves_del_perfil
        if ofertas_por_mercado.get(clave, 0) == 0 and clave not in estrenados
    ]


def estrenar_si_hace_falta(
    config: Any,
    almacen_abridor: Any,
    registro: Any,
    comando: dict[str, Any],
    log: Any,
) -> str | None:
    """Lanza la primera búsqueda si hay algún mercado sin estrenar.

    Devuelve una línea para la consola y el log, o `None` si no había nada que
    hacer. No lanza nunca: quien llama está arrancando la app, y una app que no
    abre en su casa a 9.000 km es mucho peor que un estreno que no se hizo.
    """
    try:
        directorio = config.database_path.parent
        perfil = config.profile()

        if not config.database_path.exists():
            # Primera instalación: no hay base y el botón grande de la pantalla
            # vacía ya dice exactamente esto. No se adelanta nada.
            return None

        with almacen_abridor() as almacen:
            por_mercado = almacen.counts_by_market()

        pendientes = mercados_a_estrenar(
            perfil.market_keys, por_mercado, ya_estrenados(directorio)
        )
        if not pendientes:
            return None

        etiquetas = ", ".join(_etiqueta(config, clave) for clave in pendientes)
        peticiones = perfil.planned_queries()

        # APUNTAR ANTES DE LANZAR. Si esto falla a mitad, no se reintenta sola
        # en cada apertura: el botón sigue ahí y es de ella.
        apuntar(directorio, pendientes)

        registro.lanzar(
            accion="recolectar",
            argv=[*comando["argv"]],
            etiqueta=comando["etiqueta"],
            coste_estimado_usd=0.0,
        )
        return (
            f"Primera búsqueda de {etiquetas}: {peticiones} peticiones, sin coste. "
            "Se está haciendo sola en segundo plano."
        )
    except Exception as exc:  # noqa: BLE001 - abrir la app manda sobre esto
        log("bandeja").warning("no se pudo estrenar el mercado nuevo: %s", exc)
        return None


def _etiqueta(config: Any, clave: str) -> str:
    try:
        return config.market(clave).label
    except Exception:  # noqa: BLE001
        return clave
