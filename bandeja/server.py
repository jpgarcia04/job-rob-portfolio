"""
Servidor local de la bandeja.

    python -m bandeja

Levanta FastAPI en http://127.0.0.1:8765 y abre el navegador.

Cuatro decisiones que conviene entender:

1. **Solo escucha en 127.0.0.1**, nunca en 0.0.0.0. Eso significa que el
   servidor es inalcanzable desde fuera de su máquina, ni siquiera desde otro
   equipo de su misma red. Para una app que maneja datos personales y que va
   a acabar teniendo acceso a su sesión de LinkedIn, es la diferencia entre
   una herramienta local y un servicio expuesto.

2. **La API no inventa un modelo nuevo.** Devuelve las filas de SQLite tal
   como están, convertidas a JSON. Cuando el esquema cambie, cambia en un
   sitio.

3. **No hay ninguna acción de postulación.** La bandeja marca estados y abre
   la oferta en el navegador. Enviar una candidatura sigue siendo un acto
   suyo, en su sesión, con su clic — que es la decisión de producto que
   protege su cuenta.

4. **Nada gasta sin haber enseñado antes el número.** Los botones que cuestan
   dinero o cuota van en dos pasos: se pide el plan (`GET /api/plan/...`), que
   no llama a nada, y solo después se lanza (`POST /api/trabajos`). El plan lo
   calculan `clasificador/plan.py` y `recolector/plan.py`, los mismos que usa
   el `--dry-run` de la consola: dos estimadores del mismo gasto acabarían
   divergiendo, y ese día la promesa se rompe sin que nadie lo note.

   Y el servidor VUELVE A COMPROBARLO al lanzar. Que el cliente haya visto un
   número no es garantía de nada: entre que se enseña y se pulsa puede pasar
   cualquier cosa, y quien tiene que negarse es quien gasta.
"""

from __future__ import annotations

import json
import sqlite3
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from clasificador import ajustes as ajustes_propios
from clasificador.agrupador import agrupar
from clasificador.asistente import patrones_desde_json
from clasificador.fichas import FichasError, cargar_fichas, cargar_preferencias
from clasificador.plan import (
    plan_de_clasificacion,
    plan_de_ranking,
    plan_de_ranking_que_quepa,
    plan_de_una_lectura,
    plan_del_dia,
)
from clasificador.presupuesto import (
    GuardiaPresupuesto,
    LibroCorruptoError,
    LibroDeGastos,
    ruta_del_libro,
)
from recolector.config import load_config
from recolector.plan import plan_de_recoleccion
from recolector.registro import configurar as configurar_log, log, ruta_del_log
from recolector.storage import Storage
from recolector.version import version_actual

from . import estreno, programada
from .trabajos import Parada, Paso, Registro, TrabajoEnCursoError

STATIC_DIR = Path(__file__).parent / "static"

# EL ÚNICO DOCUMENTO ESCRITO PARA ELLA, y el único .md que viaja en el clon.
# Se resuelve desde este archivo y no desde el directorio de trabajo, que
# depende de quién arrancó la app —la cáscara de escritorio, el actualizador o
# una consola—. Mismo criterio que el icono de `favicon()`.
AYUDA_MD = Path(__file__).resolve().parent.parent / "AYUDA.md"

# Los estados que entiende la bandeja. Se valida contra esta lista para que un
# cliente mal escrito no meta un estado inventado en la base.
#
# `postulada` ES NUEVO (3 septiembre 2026) Y ARREGLA UN CALLEJÓN SIN SALIDA.
# Con tres estados, una oferta que ella marcaba «me interesa» se quedaba ahí
# para siempre: no había forma de decir que ya había enviado su candidatura.
# Medido en la base ese día: 2 ofertas llevaban semanas en «interesa» sin
# ninguna salida posible. Su trabajo de verdad tiene ese cuarto estado, así que
# el día que la app no lo tenga ella lo llevará en otro sitio — y ese día la
# bandeja deja de ser donde trabaja.
#
# Y sale del ranking igual que los otros dos, por la misma razón: el asistente
# ordena lo que ella todavía no ha decidido, y una oferta a la que ya se
# postuló está decidida. `ofertas_para_ranking` filtra por `status = 'nueva'`,
# así que eso salió gratis.
ESTADOS_VALIDOS = frozenset({"nueva", "interesa", "postulada", "descartada"})

# Los filtros de la capa 2. `criterio` es el que importa: son las que el
# sistema NO se atrevió a decidir solo. `aplazadas` y `sin_mirar` son la regla
# nº 1 aplicada al triaje: «la vio y la dejó en el puesto 34» y «nadie la ha
# mirado nunca» no se pueden ver igual.
RELEVANCIAS_VALIDAS = frozenset(
    {
        "todas", "relevantes", "criterio", "descartadas",
        "pendientes", "aplazadas", "sin_mirar",
    }
)

# QUÉ COMANDO HAY DETRÁS DE CADA BOTÓN.
#
# Todas las corridas lanzadas desde aquí llevan `--manual`, y no es un detalle:
# marca que la corrida la pidió ella, y eso decide contra QUÉ freno se mide:
# las automáticas tienen una ración mensual que las para en seco, y sus
# acciones se rigen por el tope POR ACCIÓN. Existe justo para esto: que su
# botón no se encuentre nunca la caja vacía porque la tarea programada se lo
# gastó todo a las siete de la mañana.
#
# El recolector no lleva `--manual` porque no gasta dinero, gasta cuota de
# Adzuna, y esa no tiene reserva: se regenera sola.
COMANDOS = {
    "recolectar": {
        "argv": ["-m", "recolector", "run"],
        "etiqueta": "Buscar ofertas nuevas",
    },
    "clasificar": {
        "argv": ["-m", "clasificador", "--manual"],
        "etiqueta": "Que el asistente lea lo nuevo",
    },
    "ranking": {
        "argv": ["-m", "clasificador", "ranking", "--manual"],
        "etiqueta": "Ordenar por dónde empezar",
    },
    "leer_una": {
        # La clave se añade al lanzar. Ver `lanzar_trabajo`.
        "argv": ["-m", "clasificador", "--manual", "--solo"],
        "etiqueta": "Que la lea el asistente",
    },
    # LOS TRES DE ARRIBA, SEGUIDOS. No tiene `argv` propio porque no es un
    # comando: es una cadena, y cada tramo se planifica justo antes de lanzarlo
    # con lo que quede del tope. Ver `_planificador_del_dia`.
    "al_dia": {
        "argv": None,
        "etiqueta": "Ponme al día",
    },
}

# En qué orden va la cadena, y por qué ese.
#
# Buscar primero porque es lo único que trae material nuevo; leer después,
# porque solo se puede leer lo que ya está; ordenar al final, porque el orden
# solo vale si incluye lo que se acaba de leer. Es el mismo orden en el que
# había que pulsar los tres botones — lo que cambia es que ahora es una sola
# pulsación, un solo importe y un solo tope.
PASOS_DEL_DIA = ("recolectar", "clasificar", "ranking")

# CUÁNTOS ANUNCIOS SE LLEVA UNA PANTALLA, Y POR QUÉ SE DICE CUANDO SE CORTA.
#
# `listar_ofertas_con_clasificacion` siempre ha traído 200 como máximo, y la
# bandeja nunca lo dijo. Medido el 3 septiembre 2026 sobre la base real, con el
# filtro que sale al abrir la app:
#
#     anuncios en estado «nueva» .................... 379
#     los que llegaban a la pantalla ................ 200
#     vacantes que eso son .......................... 307 → se veían 168
#
# O sea que **139 de sus 307 vacantes no existían en la pantalla**, sin una
# línea que lo dijera y sin ninguna forma de llegar a ellas: es la regla nº 1 de
# la casa —nada se descarta en silencio— rota justo donde ella trabaja. Y no era
# un caso extremo: pasaba al abrir la app, todos los días, y empeora solo,
# porque entran ~25 vacantes nuevas al día y el tope no se mueve.
#
# El tope se queda —pintar dos mil tarjetas haría la página inservible por otro
# lado—, pero sube a 600 (cubre la base de hoy con margen para dos meses) y
# sobre todo **deja de ser mudo**: se piden 601 para saber si sobraba algo, y
# lo que no cabe se cuenta y se dice, con los filtros que sí llegan a ello.
TOPE_DE_ANUNCIOS_POR_VISTA = 600

# Cuánto puede haber subido el coste entre que se le enseñó y ella confirmó.
#
# Escenario real: abre el diálogo, se va a comer, la tarea programada mete 200
# ofertas nuevas, vuelve y pulsa aceptar. Sin esto habría confirmado $0,30 y
# gastado $1,10. Por encima de este margen se rechaza y se le vuelve a enseñar
# el número — que es la regla, no una cortesía.
MARGEN_DE_CONFIRMACION = 1.25

app = FastAPI(
    title="Bandeja de ofertas",
    docs_url=None,      # sin documentación pública: es una app local
    redoc_url=None,
)

@app.middleware("http")
async def sin_caché_de_navegador(peticion, siguiente):
    """La pantalla se revalida SIEMPRE. Es media mitad del mantenimiento remoto.

    QUÉ PASABA. FastAPI sirve los estáticos con `etag` y `last-modified` pero
    **sin `Cache-Control`**, y sin esa cabecera el navegador se inventa cuánto
    tiempo puede quedarse el archivo (la regla habitual: un 10% del tiempo que
    lleva sin cambiar). Comprobado ejecutando el 3 septiembre 2026: se cambió
    `app.js`, se recargó la página, y **la ventana siguió corriendo el archivo
    viejo** — el arreglo estaba en el servidor y no en la pantalla.

    POR QUÉ IMPORTA MÁS AQUÍ QUE EN CUALQUIER WEB. La forma de arreglar algo
    desde España es etiquetar una versión y dejar que `actualizar.ps1` la traiga
    antes de abrir la app. Eso actualiza el Python —el servidor arranca de nuevo
    cada vez— pero no obliga a nada al navegador: su ventana podía quedarse con
    el HTML y el JavaScript anteriores durante días. Y lo peligroso no es
    quedarse atrás entero, es quedarse **a medias**: servidor nuevo y pantalla
    vieja, que es justo la combinación que nadie ha probado nunca.

    `no-cache` no significa «no guardes»: significa «pregunta antes de usarlo».
    Con el `etag` que ya se manda, la respuesta normal es un 304 sin cuerpo
    contra un servidor que corre en su propia máquina. El coste es cero y lo que
    compra es que una versión nueva se vea en cuanto se abre la app.
    """
    respuesta = await siguiente(peticion)
    if peticion.url.path == "/" or peticion.url.path.startswith("/static/"):
        respuesta.headers["Cache-Control"] = "no-cache"
    return respuesta


# UNO PARA TODO EL SERVIDOR. `uvicorn.run(app, ...)` levanta un solo proceso con
# un solo worker, así que este registro ve todos los trabajos. Si algún día se
# levantaran varios workers, cada uno tendría el suyo y el «uno cada vez»
# dejaría de valer — que es la razón de que esto esté escrito aquí.
trabajos = Registro()


def _abrir_almacen() -> Storage:
    """Abre la base de datos. Se abre y cierra por petición.

    SQLite no admite compartir una conexión entre hilos, y FastAPI atiende
    cada petición donde le conviene. Abrir por petición es lo simple y
    correcto aquí: son consultas de milisegundos sobre una base local.
    """
    config = load_config()
    if not config.database_path.exists():
        # ESTE TEXTO SE LO IBA A LEER ELLA, Y ESTABA ESCRITO PARA MÍ.
        #
        # Decía «Ejecuta primero una recolección: python -m recolector run
        # --sources adzuna». En la pantalla de una ejecutiva senior que acaba
        # de abrir la app por primera vez, eso es una instrucción imposible: no
        # hay consola donde escribirla, y el botón que hace exactamente eso
        # está a diez centímetros. Comprobado ejecutando contra una carpeta sin
        # base: `/api/plan/recolectar` SÍ funciona sin base de datos, así que
        # el botón que hace falta ya está ahí y ya funciona.
        raise HTTPException(
            status_code=503,
            detail=(
                "Todavía no hay ninguna oferta guardada. Pulsa «Buscar ofertas "
                "nuevas» y en un par de minutos tendrás la primera bandeja."
            ),
        )
    return Storage(config.database_path)


def _cargar_fichas():
    try:
        return cargar_fichas()
    except FichasError as exc:
        # El detalle técnico va al LOG, que es lo que se manda; en la pantalla
        # queda lo único que ella puede hacer con esto, que es avisar. Un
        # volcado de YAML delante no le da ninguna acción y sí la sensación de
        # que la ha roto ella.
        log("bandeja").error("config/cvs.yaml no se pudo cargar: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=(
                "Hay un problema con la configuración de tus CVs y por eso el "
                "asistente no puede leer ofertas. Avísale a Juan Pablo con el "
                "botón «Si algo va mal»: el detalle ya está en el registro."
            ),
        ) from exc


def _libro() -> LibroDeGastos:
    config = load_config()
    try:
        return LibroDeGastos(ruta_del_libro(config))
    except LibroCorruptoError as exc:
        # No se asume cero. Perder la cuenta del gasto cuesta dinero de otra
        # persona, así que se falla ruidosamente igual que en la consola.
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _fila_a_dict(fila: sqlite3.Row) -> dict[str, Any]:
    """Convierte una fila de SQLite en algo que FastAPI pueda serializar.

    Se excluye `description` en los listados por tamaño; el detalle sí la trae.
    """
    datos = dict(fila)
    datos["salary_is_predicted"] = bool(datos.get("salary_is_predicted"))
    # SQLite no tiene booleanos: guarda 0/1. Se convierten aquí para que el
    # JavaScript pueda hacer `if (o.clas_relevante)` sin sorpresas — en JS el
    # número 0 es falso, pero el string "0" sería verdadero.
    for campo in (
        "clas_relevante", "clas_es_reclutadora", "clas_necesita_criterio",
        "triaje_merecia", "triaje_seleccionada",
    ):
        if datos.get(campo) is not None:
            datos[campo] = bool(datos[campo])
    return datos


class CambioEstado(BaseModel):
    """Cuerpo de la petición para cambiar el estado de una oferta.

    Pydantic valida el JSON entrante antes de que llegue al código: si falta
    el campo o es de otro tipo, FastAPI responde 422 automáticamente.
    """

    estado: str

    # LOS DEMÁS ANUNCIOS DE LA MISMA VACANTE, y por qué los manda el cliente.
    #
    # La tarjeta es una vacante; los anuncios que reúne salen de agrupar las
    # filas YA FILTRADAS por estado, mercado y relevancia (ver
    # `listar_ofertas`). El servidor, aquí, no sabe con qué filtros se pintó
    # esa tarjeta, así que rehacer la agrupación por su cuenta podría marcar
    # un conjunto distinto del que ella vio en pantalla.
    #
    # Mandarlas desde el cliente hace que se marque EXACTAMENTE lo que decía
    # la ficha —«la misma vacante aparece 4 veces», y esas cuatro—, que es la
    # única versión que ella puede comprobar. Vienen del mismo JSON que pintó
    # la ficha: `grupo.miembros[].clave`.
    hermanas: list[str] = []


class LanzarTrabajo(BaseModel):
    """Lo que manda la interfaz al pulsar «aceptar» en la confirmación."""

    accion: str
    # Solo para `leer_una`.
    clave: str | None = None
    # El número que ella vio antes de aceptar. Se compara con el de ahora.
    coste_confirmado_usd: float | None = None


# ---------------------------------------------------------------------------
#  API — lo que ya había
# ---------------------------------------------------------------------------

@app.get("/api/version")
def version() -> dict[str, Any]:
    """Qué versión corre y dónde está el log.

    ES PARA ARREGLAR COSAS DESDE LEJOS. Cuando ella diga «no me funciona», la
    primera pregunta es qué versión tiene y la segunda es que mande el log; las
    dos respuestas están aquí, en la pantalla, sin que tenga que abrir una
    consola ni buscar una carpeta.

    `es_de_fiar` en `false` significa que ese árbol no corresponde a ninguna
    versión publicada —cambios locales, o commits por encima de la etiqueta—,
    o sea que el actualizador no lo está gobernando. Eso es un fallo de
    instalación y hay que poder verlo, no esconderlo.
    """
    v = version_actual()
    carpeta = ruta_del_log(load_config().database_path.parent).parent
    return {
        "version": v.para_pantalla(),
        "commit": v.commit,
        "es_de_fiar": v.es_de_fiar,
        "log": str(carpeta / "job-rob.log"),
        # LA CARPETA, Y NO SOLO EL ARCHIVO (3 septiembre 2026).
        #
        # Ya no hay un solo log: la app escribe `job-rob.log`, el actualizador
        # `actualizar.log` y la cáscara de escritorio `arranque.log`. Y no es
        # desorden: los tres NO pueden ser el mismo archivo porque los escriben
        # tres procesos distintos y rotar en Windows es renombrar, que falla si
        # otro lo tiene abierto. Lo que sí se puede es pedir los tres de una
        # vez, y para eso hace falta la carpeta.
        "carpeta_log": str(carpeta),
    }


@app.post("/api/abrir-carpeta-log")
def abrir_carpeta_log() -> dict[str, Any]:
    """Abre la carpeta de logs en el Explorador de Windows.

    ES LA DIFERENCIA ENTRE QUE EL LOG LLEGUE Y QUE NO. Hasta hoy el botón «Si
    algo va mal» copiaba una RUTA, y una ruta es algo que hay que pegar en la
    barra de direcciones del Explorador — un gesto que ella no tiene por qué
    conocer y que no va a hacer con el desarrollador a nueve mil kilómetros.
    Abriendo la carpeta, adjuntar los archivos a un correo es arrastrarlos.

    Es seguro porque el servidor solo escucha en 127.0.0.1 y la ruta no viene
    del cliente: la calcula aquí la configuración. No se abre nada que mande
    nadie de fuera.
    """
    carpeta = ruta_del_log(load_config().database_path.parent).parent
    carpeta.mkdir(parents=True, exist_ok=True)
    try:
        import os

        os.startfile(carpeta)  # type: ignore[attr-defined]  # solo existe en Windows
    except Exception as exc:  # pragma: no cover - depende del escritorio
        # No es un fallo que merezca romper nada: la ruta se le enseña igual.
        log("bandeja").warning("no se pudo abrir la carpeta de logs: %s", exc)
        return {"abierta": False, "carpeta": str(carpeta)}
    return {"abierta": True, "carpeta": str(carpeta)}


# Mercados que están en la base y ya no en el YAML. Solo para no repetir el
# aviso: ver `_etiqueta_de_mercado`.
_MERCADOS_SIN_CONFIGURAR: set[str] = set()


def _etiqueta_de_mercado(config: Any, clave: str) -> str:
    """El nombre bonito del mercado, y si ya no está en el YAML, su clave.

    PORQUE LA BASE SOBREVIVE A LA CONFIGURACIÓN, y esto se comprobó ejecutando
    (4 septiembre 2026). `config.market()` lanza `ConfigError` cuando la clave
    no está definida —y hace bien: **recolectar** de un mercado que no existe es
    un error de configuración que hay que gritar—. Pero la bandeja no recolecta:
    enseña lo que ya está guardado.

    Con las 147 ofertas de México que hay en la base, quitar `mexico` del YAML
    —una línea— hacía que `/api/resumen` y `/api/ofertas` reventaran los dos, o
    sea que **la app no abría**. Y ese cambio no es hipotético: la clienta pidió
    justo eso el mismo día (cambiar un mercado por otro), y cualquier ajuste de
    geografía que salga de su sesión con el headhunter pasa por aquí.

    Enseñar `mexico` en vez de «México» es feo; no abrir la herramienta porque
    su histórico ya no está en la configuración de hoy es otra cosa.
    """
    try:
        return config.market(clave).label
    except Exception:
        # UNA VEZ POR CLAVE Y POR PROCESO, no una por consulta. Una sola
        # pantalla resuelve la etiqueta más de cien veces —cada estado, cada
        # filtro, cada tarjeta— y avisar en todas llenaría `job-rob.log` de la
        # misma línea hasta rotar y llevarse por delante lo que sí hace falta
        # leer. El log es lo único que va a existir cuando ella diga «no me
        # funciona»: lo que lo inunda, lo rompe.
        if clave not in _MERCADOS_SIN_CONFIGURAR:
            _MERCADOS_SIN_CONFIGURAR.add(clave)
            log("bandeja").warning(
                "el mercado '%s' está en la base y ya no en la configuración: "
                "se enseña con su clave", clave,
            )
        return clave


def _vacantes(almacen: Storage, **filtros: Any) -> int:
    """Cuántas TARJETAS saldrían con esos filtros. Vacantes, no anuncios.

    ES LA REGLA Nº 14 DE LA CASA APLICADA A LOS CONTADORES, y hacía falta
    porque la pantalla se contradecía a sí misma. Medido el 4 septiembre 2026
    sobre la base real, con México elegido en el desplegable:

        la pastilla decía ......... «Relevantes 59»
        la rejilla enseñaba ....... 3 tarjetas

    Las dos cifras eran ciertas y ninguna era la respuesta a lo que ella estaba
    mirando: la pastilla contaba ANUNCIOS de TODA la base —los dos mercados,
    los cuatro estados— y la rejilla enseña VACANTES del filtro puesto. Un
    número que no describe lo que hay debajo se lee como un fallo del programa,
    y este proyecto ya decidió que dos cosas distintas no pueden verse igual.

    Se cuenta agrupando, que es exactamente lo que hace la rejilla: 65 ms sobre
    453 anuncios, medido. No se cuenta con un `COUNT(*)` porque entonces
    volvería a ser otro número.
    """
    filas = almacen.listar_ofertas_con_clasificacion(
        limit=TOPE_DE_ANUNCIOS_POR_VISTA + 1, **filtros
    )
    return len(agrupar(filas[:TOPE_DE_ANUNCIOS_POR_VISTA]))


def _zonas_del_mercado(config: Any, mercado: str) -> list[Any]:
    """Las zonas del mercado elegido, o ninguna.

    Con «todos los mercados», o con un mercado que ya no está en el YAML, no
    hay fila de zonas: una zona solo significa algo dentro de su mercado.
    """
    if mercado == "todos":
        return []
    try:
        return list(config.market(mercado).geo_filter.zonas)
    except Exception:  # noqa: BLE001 - mercado fuera del YAML: ver `_etiqueta_de_mercado`
        return []


@app.get("/api/resumen")
def resumen(
    estado: str = "nueva", mercado: str = "todos", zona: str = "todas",
) -> dict[str, Any]:
    """Contadores para la cabecera y los filtros.

    Recibe los filtros que ella tiene puestos porque los contadores son DE ESA
    pantalla: ver `_vacantes`. Los valores por defecto son los que trae la app
    al abrirse.
    """
    almacen = _abrir_almacen()
    try:
        config = load_config()
        por_mercado = almacen.counts_by_market()
        clas = almacen.resumen_clasificaciones()
        pendientes = almacen.resumen_pendientes()

        # Los estados se cuentan bajo el MERCADO elegido, no bajo el estado:
        # son las pastillas para cambiar de estado, y tienen que decir lo que
        # se va a encontrar al pulsarlas.
        por_estado = {
            nombre: _vacantes(almacen, status=nombre, market_key=mercado, zona=zona)
            for nombre in ESTADOS_VALIDOS
        }
        # Y la lectura del sistema, bajo el estado Y el mercado: es la fila que
        # acota lo que ya se está viendo.
        vistas = {
            nombre: _vacantes(
                almacen, status=estado, market_key=mercado, relevancia=nombre,
                zona=zona,
            )
            for nombre in ("relevantes", "criterio", "descartadas",
                           "aplazadas", "sin_mirar")
        }
        return {
            "total": almacen.total_offers(),
            # Vacantes, no anuncios: es lo que va a encontrar al pulsar.
            "por_estado": por_estado,
            # El desplegable también en vacantes: está a cuatro centímetros de
            # las pastillas, y dos números pegados en unidades distintas se
            # leen como que uno de los dos está mal. `anuncios` se manda al
            # lado por si algún día hace falta enseñar los dos.
            "mercados": [
                {
                    "clave": clave,
                    "etiqueta": _etiqueta_de_mercado(config, clave),
                    "total": _vacantes(almacen, market_key=clave, status=None),
                    "anuncios": por_mercado.get(clave, 0),
                }
                for clave in sorted(por_mercado)
            ],
            # LAS ZONAS DEL MERCADO ELEGIDO (24 septiembre 2026), para la fila
            # que sale debajo de la cabecera cuando elige Texas. No van en el
            # desplegable a propósito: el desplegable sigue siendo de
            # mercados, y lo pidió así el desarrollador.
            #
            # Cada zona cuenta en VACANTES bajo el estado elegido, igual que
            # las pastillas de estado cuentan bajo el mercado: cada fila cuenta
            # con los filtros de las filas de arriba, y dice lo que va a
            # encontrar al pulsar.
            "zonas": [
                {
                    "clave": z.clave,
                    "etiqueta": z.etiqueta,
                    "descripcion": z.descripcion,
                    "total": _vacantes(
                        almacen, status=estado, market_key=mercado, zona=z.clave
                    ),
                }
                for z in _zonas_del_mercado(config, mercado)
            ],
            "zonas_total": (
                _vacantes(almacen, status=estado, market_key=mercado)
                if _zonas_del_mercado(config, mercado) else 0
            ),
            # LO QUE VA EN LAS PASTILLAS DE «LECTURA DEL SISTEMA»: vacantes del
            # filtro que ella tiene puesto. Ver `_vacantes`.
            "vistas": vistas,
            # Lo que aportó la capa 2, sobre TODA la base y en anuncios. Ya no
            # se pinta en ninguna pastilla —para eso está `vistas`—: queda
            # porque `total` y `triadas` son lo que decide si la fila de
            # filtros se enseña siquiera, y porque el desglose por CV y el
            # coste acumulado no dependen de lo que se esté mirando.
            "clasificacion": {
                "total": clas["total"] or 0,
                "relevantes": clas["relevantes"] or 0,
                "descartadas": clas["descartadas"] or 0,
                "necesita_criterio": clas["necesita_criterio"] or 0,
                "sin_clasificar": clas["sin_clasificar"] or 0,
                "pendientes": pendientes["pendientes"],
                "coste_usd": round(clas["coste_usd"] or 0.0, 4),
                "por_cv": clas["por_cv"],
            },
            # Las dos mitades de «pendiente», que significan cosas opuestas.
            "triaje": {
                "aplazadas": pendientes["aplazadas"],
                "sin_mirar": pendientes["sin_mirar"],
                **almacen.resumen_triaje(),
            },
        }
    finally:
        almacen.close()


@app.get("/api/ofertas")
def listar_ofertas(
    estado: str = "nueva",
    mercado: str = "todos",
    relevancia: str = "todas",
    zona: str = "todas",
) -> dict[str, Any]:
    """Las ofertas de la bandeja, ya agrupadas.

    Devuelve UNA tarjeta por vacante, no una por anuncio: cinco publicaciones
    del mismo puesto en cinco ciudades son una oportunidad, no cinco. Las
    otras cuatro viajan dentro de `grupo.miembros` y se ven en el modal.
    Ninguna se borra ni se oculta de la base.
    """
    if relevancia not in RELEVANCIAS_VALIDAS:
        raise HTTPException(
            status_code=400,
            detail=f"Filtro de relevancia '{relevancia}' no válido. "
                   f"Admitidos: {', '.join(sorted(RELEVANCIAS_VALIDAS))}",
        )

    almacen = _abrir_almacen()
    try:
        # UNA DE MÁS, A PROPÓSITO: es lo que permite distinguir «caben todas»
        # de «se cortó», que es lo único que la pantalla no podía decir.
        filas = almacen.listar_ofertas_con_clasificacion(
            status=estado, market_key=mercado, relevancia=relevancia,
            limit=TOPE_DE_ANUNCIOS_POR_VISTA + 1, zona=zona,
        )
        se_cortan = len(filas) > TOPE_DE_ANUNCIOS_POR_VISTA
        if se_cortan:
            filas = filas[:TOPE_DE_ANUNCIOS_POR_VISTA]
        config = load_config()

        por_clave = {fila["dedupe_key"]: fila for fila in filas}

        # Se agrupa sobre las filas YA filtradas, no sobre toda la base: si
        # ella está mirando "descartadas", el representante del grupo tiene
        # que ser una de las descartadas, no una que no está en pantalla.
        ofertas = []
        for grupo in agrupar(filas):
            fila = por_clave[grupo.representante.dedupe_key]
            datos = _fila_a_dict(fila)
            datos["fuentes"] = almacen.sources_for_offer(fila["dedupe_key"])
            datos["mercado_etiqueta"] = _etiqueta_de_mercado(config, fila["market_key"])
            datos["grupo"] = {
                "tamano": grupo.tamano,
                "resumen": grupo.resumen(),
                "motivos": sorted(grupo.motivos),
                "miembros": [
                    {
                        "clave": m.dedupe_key,
                        "titulo": m.titulo,
                        "empresa": m.empresa,
                        "ubicacion": m.ubicacion,
                        "url": m.url,
                    }
                    for m in grupo.miembros
                ],
            }
            ofertas.append(datos)

        # `agrupar` ordena por tamaño de grupo; aquí manda el orden de la
        # consulta, que es el que pone arriba lo que ella tiene que mirar.
        orden = {clave: i for i, clave in enumerate(por_clave)}
        ofertas.sort(key=lambda o: orden[o["dedupe_key"]])

        # Solo se cuenta si de verdad sobró algo: una consulta de más en cada
        # pantalla, para nada, el 99% de los días.
        anuncios_totales = (
            almacen.contar_ofertas_de_bandeja(
                status=estado, market_key=mercado, relevancia=relevancia,
                zona=zona,
            )
            if se_cortan
            else len(filas)
        )
        return {
            "ofertas": ofertas,
            "total": len(ofertas),
            "anuncios": len(filas),
            # LO QUE FALTA, DICHO. Ver `TOPE_DE_ANUNCIOS_POR_VISTA`.
            "anuncios_totales": anuncios_totales,
            "hay_mas": se_cortan,
            "tope": TOPE_DE_ANUNCIOS_POR_VISTA,
        }
    finally:
        almacen.close()


@app.post("/api/ofertas/{clave}/estado")
def cambiar_estado(clave: str, cambio: CambioEstado) -> dict[str, Any]:
    if cambio.estado not in ESTADOS_VALIDOS:
        raise HTTPException(
            status_code=400,
            detail=f"Estado '{cambio.estado}' no válido. "
                   f"Admitidos: {', '.join(sorted(ESTADOS_VALIDOS))}",
        )

    # SE MARCA LA VACANTE ENTERA, NO EL ANUNCIO QUE HACE DE PORTADA.
    #
    # Marcar solo el representante dejaba a sus hermanas en «nueva»: la
    # tarjeta se iba de la pantalla y volvía en el siguiente refresco con un
    # anuncio menos. Ver `Storage.marcar_estado_de_la_vacante`, que es donde
    # está la medición.
    claves = [clave, *(h for h in cambio.hermanas if h != clave)]

    almacen = _abrir_almacen()
    try:
        # La portada tiene que existir; una hermana que ya no esté no es un
        # error —la base pudo cambiar entre que se pintó la ficha y ella
        # pulsó—, así que no se cuenta como fallo.
        if not almacen.set_status(clave, cambio.estado):
            raise HTTPException(status_code=404, detail="Esa oferta no existe.")
        anuncios = almacen.marcar_estado_de_la_vacante(claves, cambio.estado)
        return {
            "ok": True,
            "clave": clave,
            "estado": cambio.estado,
            # Lo que se cambió de verdad, para que el cliente no tenga que
            # suponerlo y para que se pueda comprobar desde fuera.
            "claves": claves,
            "anuncios": anuncios,
        }
    finally:
        almacen.close()


# ---------------------------------------------------------------------------
#  El dinero
# ---------------------------------------------------------------------------

MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def _nombre_del_mes(mes: str) -> str:
    """`2026-09` -> `septiembre`.

    A mano y no con `locale`: el nombre del mes en castellano depende de que
    la máquina tenga instalada esa configuración regional, y la de la clienta
    es un Windows en español de México pero la del desarrollador no tiene por
    qué serlo. Doce palabras que no cambian nunca no necesitan una dependencia
    del sistema operativo para salir bien.
    """
    return MESES[int(mes[5:7]) - 1]


def _cuando_se_reinicia(mes: str) -> str:
    """`2026-09` -> `el 1 de octubre`. El día en que el contador vuelve a cero.

    Se dice en la pantalla porque un acumulado que baja de golpe sin avisar se
    lee como un fallo, y porque la única pregunta que ella se puede hacer
    mirando esa cifra —«¿esto es de siempre o de este mes?»— tiene que estar
    contestada ahí mismo.
    """
    anio, numero = int(mes[:4]), int(mes[5:7])
    siguiente = 1 if numero == 12 else numero + 1
    if numero == 12:
        return f"el 1 de enero de {anio + 1}"
    return f"el 1 de {MESES[siguiente - 1]}"


@app.get("/api/presupuesto")
def presupuesto() -> dict[str, Any]:
    """Cuánto lleva gastado del mes. No llama a nada y no cuesta nada.

    Se devuelven las DOS vistas, la automática y la suya, por el mismo motivo
    que el comando de consola las imprime las dos: la diferencia entre ellas es
    la ración de las automáticas, y enseñar solo una haría que el número
    pareciera cambiar solo según quién pregunte.

    Y va el TOPE POR ACCIÓN, que desde el 3 septiembre 2026 es el freno de
    verdad. El techo del mes sigue aquí porque es la referencia de la barra,
    pero `frena_el_mes` dice si además para: sin ese campo, la pantalla diría
    «agotado» donde la app va a seguir funcionando.
    """
    config = load_config()
    libro = _libro()
    automatico = libro.estado(config.llm, es_manual=False)
    suyo = libro.estado(config.llm, es_manual=True)

    return {
        # DE QUÉ MES ES LA CIFRA, Y CUÁNDO SE PONE A CERO (8 sept 2026).
        #
        # La cabecera dejó de enseñar «de $12,00» —no había tal límite: el mes
        # solo avisa y la recarga de la cuenta es automática, así que un
        # denominador ahí era inventarse un techo—. Lo que queda es el gasto
        # acumulado, y un acumulado sin decir de qué periodo es no se puede
        # leer: el día 1 baja de golpe y parecería un fallo.
        #
        # Los dos salen del libro, no del navegador, porque el reloj que
        # agrupa el gasto tiene que ser el mismo que escribe la etiqueta.
        "mes": _libro().mes_actual(),
        "mes_nombre": _nombre_del_mes(_libro().mes_actual()),
        "reinicio": _cuando_se_reinicia(_libro().mes_actual()),
        "techo_mensual": automatico.techo_mensual,
        "gastado_mes": round(automatico.gastado_mes, 4),
        "llamadas_mes": automatico.llamadas_mes,
        "porcentaje_usado": round(automatico.porcentaje_usado, 1),
        "conviene_avisar": automatico.conviene_avisar,
        "avisar_al_superar_pct": automatico.avisar_al_superar_pct,
        "racion_automatica": automatico.racion_automatica,
        "tope_por_accion": suyo.tope_por_accion,
        "al_agotarse": config.llm.al_agotarse,
        "gastado_total": round(libro.gastado_total(), 4),
        "automatico": {
            "techo": automatico.techo_efectivo,
            "disponible": round(automatico.disponible, 4),
            "agotado": automatico.agotado,
        },
        # Lo que puede gastar un botón suyo. `frena_el_mes` es lo que
        # distingue «se acabó» de «llevas mucho»: con el techo mensual en modo
        # aviso, `agotado` sigue siendo verdad y NO significa que no se pueda
        # pulsar nada.
        "suyo": {
            "techo": suyo.techo_efectivo,
            "disponible": round(suyo.disponible, 4),
            "agotado": suyo.agotado,
            "frena_el_mes": suyo.frena_el_mes,
        },
    }


# ---------------------------------------------------------------------------
#  Los planes — el número que se enseña ANTES de gastar
# ---------------------------------------------------------------------------

def _plan_del_dia_como_json() -> dict[str, Any]:
    """«Ponme al día»: UN importe para los tres pasos, y qué queda fuera.

    El diálogo enseña dos cifras y las dos hacen falta:

      · lo que se estima —el número de siempre—, y
      · el TECHO, que es lo que esta acción no puede pasar pase lo que pase.

    La segunda es nueva y es la que sostiene la promesa en una cadena. Una
    estimación de tres tramos encadenados puede quedarse corta —el segundo trae
    ofertas que el tercero tiene que ordenar—, así que enseñar solo el estimado
    sería prometer un número que el propio diseño puede superar. El tope no se
    supera nunca: se comprueba antes de cada tramo con lo que lleve gastado la
    cadena. Ver `_planificador_del_dia`.
    """
    config = load_config()
    almacen = _abrir_almacen()
    try:
        plan = plan_del_dia(almacen, config, _cargar_fichas(), cargar_preferencias())
    finally:
        almacen.close()

    detalle: dict[str, Any] = {}
    for tramo in plan.tramos:
        if not tramo.hay_trabajo:
            detalle[tramo.etiqueta] = "nada que hacer"
        elif tramo.motivo_fuera == "llamada":
            # NO es «nada que hacer» y NO es el tope: hay ofertas esperando y
            # el lote no cabe en una sola llamada. Un coste de $0,00 aquí no
            # significa gratis, significa que no se puede hacer.
            cuantas = tramo.detalle.get("Ofertas a ordenar", "")
            detalle[tramo.etiqueta] = (
                f"{cuantas} esperando — NO caben en una sola llamada"
            )
        elif not tramo.entra:
            detalle[tramo.etiqueta] = f"${tramo.coste_usd:.4f} — NO entra en el tope"
        elif tramo.recortado_de:
            # UN PASO RECORTADO NO SE PINTA COMO UN PASO ENTERO. Sin esto, un
            # orden de 33 sobre 98 se lee igual que uno de 98 sobre 98, y ella
            # no tiene forma de saber que hay 65 que este orden no miró.
            cuantas = tramo.detalle.get("Ofertas a ordenar", "")
            detalle[tramo.etiqueta] = (
                f"${tramo.coste_usd:.4f} — ordena {cuantas} de "
                f"{tramo.recortado_de}"
            )
        elif tramo.coste_usd:
            detalle[tramo.etiqueta] = f"${tramo.coste_usd:.4f}"
        else:
            detalle[tramo.etiqueta] = "sin coste (cuota de los portales)"
    detalle["Tarda unos"] = f"{plan.segundos_estimados // 60} min"
    detalle["Nunca puede pasar de"] = f"${plan.tope_por_accion_usd:.2f}"

    avisos = []
    for tramo in plan.entran:
        if tramo.recortado_de:
            avisos.append(tramo.nota)
    for tramo in plan.quedan_fuera:
        if tramo.motivo_fuera == "llamada":
            # El motivo se redacta en `cliente_llm.Cabida.explicacion`, que es
            # donde vive el número accionable, y no se reescribe aquí: tres
            # redacciones del mismo número acaban divergiendo.
            avisos.append(
                f"«{tramo.etiqueta}» se queda fuera, y no por el importe: "
                f"{tramo.nota} Su botón suelto se negaría igual. Lo que lo "
                "desatasca es revisar ofertas en la bandeja —marcarlas como "
                "«me interesa» o «descartada» las saca de la lista por "
                "ordenar—, o bajar el esfuerzo del orden en la configuración."
            )
        else:
            avisos.append(
                f"«{tramo.etiqueta}» se queda fuera de esta acción: costaría "
                f"${tramo.coste_usd:.4f} más y la cadena entera se saldría del tope "
                f"de ${plan.tope_por_accion_usd:.2f}. No se pierde nada — sigue "
                "pendiente, y su botón está debajo."
            )

    disponible = _libro().estado(config.llm, es_manual=True).disponible
    return {
        "accion": "al_dia",
        "etiqueta": COMANDOS["al_dia"]["etiqueta"],
        "moneda": "usd",
        "hay_trabajo": plan.hay_trabajo,
        "cabe": plan.coste_usd <= disponible,
        "coste_usd": round(plan.coste_usd, 4),
        "coste_completo_usd": round(plan.coste_completo_usd, 4),
        "techo_usd": round(plan.tope_por_accion_usd, 4),
        "disponible_usd": round(disponible, 4),
        "pasos": len(plan.entran),
        "nota": plan.nota,
        "avisos": avisos,
        "detalle": detalle,
        "muestra": [],
    }


def _plan_como_json(accion: str) -> dict[str, Any]:
    config = load_config()

    if accion == "al_dia":
        return _plan_del_dia_como_json()

    if accion == "recolectar":
        plan = plan_de_recoleccion(config)
        return {
            "accion": accion,
            "etiqueta": COMANDOS[accion]["etiqueta"],
            "moneda": "peticiones",
            "hay_trabajo": plan.hay_trabajo,
            "cabe": plan.cabe,
            "coste_usd": 0.0,
            "peticiones": plan.peticiones,
            "nota": plan.nota,
            "avisos": list(plan.avisos),
            "detalle": {
                "Perfil": plan.perfil,
                "Mercados": ", ".join(plan.mercados),
                "Fuentes": ", ".join(plan.fuentes),
                # CUÁNTAS BÚSQUEDAS EN CADA SITIO, y no solo el total. Desde el
                # 17 septiembre 2026 México pregunta más cosas que Texas —las
                # combinaciones en español— y un número suelto no deja ver de
                # dónde salen las peticiones.
                "Combinaciones": (
                    ", ".join(f"{n} en {etiqueta}" for etiqueta, n in plan.combinaciones_por_mercado)
                    if len({n for _, n in plan.combinaciones_por_mercado}) > 1
                    else plan.combinaciones
                ),
                "Peticiones (máximo)": plan.peticiones,
                "Tarda unos": f"{plan.segundos_estimados // 60} min",
                # Las tres ventanas del plan Trial. Se enseñan las tres porque
                # la que aprieta cambia según el día: con una corrida diaria
                # sobra en todas, pero dos corridas caben en la semana y NO en
                # el mes, que es justo el tipo de cosa que no se ve mirando un
                # solo número.
                "Hoy": f"{plan.usadas_hoy} de {plan.tope_diario or '?'}",
                "Últimos 7 días": f"{plan.usadas_esta_semana} de {plan.tope_semanal or '?'}",
                "Este mes": f"{plan.usadas_este_mes} de {plan.tope_mensual or '?'}",
            },
        }

    almacen = _abrir_almacen()
    try:
        if accion == "clasificar":
            plan = plan_de_clasificacion(almacen, config, _cargar_fichas())
            detalle = {
                "Ofertas pendientes": plan.pendientes,
                "Ya tuvieron su turno": plan.ya_triadas,
                "Vacantes sin triar": plan.vacantes,
                "Se leerían enteras": plan.lecturas,
                "Llamadas a la API": plan.llamadas,
            }
            # EL TOPE DE LOTE SE DICE, NO SE APLICA EN SILENCIO.
            #
            # Si la cola son 273 y la llamada va a mirar 60, enseñar solo el
            # 273 haría que el coste no cuadrara con el número de al lado, y
            # enseñar solo el 60 escondería que hay 213 esperando. Las dos
            # líneas, y la segunda con las palabras exactas de lo que les pasa:
            # conservan su turno, que es distinto de estar aplazadas.
            if plan.fuera_del_tope:
                detalle["Entran en esta llamada"] = plan.vacantes_esta_corrida
                detalle["Esperan a la siguiente"] = (
                    f"{plan.fuera_del_tope} (con su turno intacto)"
                )
            if plan.hay_triaje:
                detalle["Coste del triaje"] = f"${plan.coste_triaje_usd:.4f}"
                detalle["Coste de las lecturas"] = f"${plan.coste_lecturas_usd:.4f}"
            coste, hay, nota = plan.coste_usd, plan.hay_trabajo, plan.nota
            muestra = [
                {"mercado": m, "titulo": t, "empresa": e} for m, t, e in plan.muestra
            ]
        elif accion == "ranking":
            # EL MISMO CORTE QUE LA CADENA, y por el mismo motivo. El botón
            # suelto se planificaba con `plan_de_ranking` a pelo, así que con
            # la cola por encima de `cabida` se negaba entero en vez de
            # ordenar lo que sí cabe. Dos caminos para el mismo número es la
            # regla nº 9 rota, y aquí además daba dos comportamientos.
            plan = plan_de_ranking_que_quepa(
                almacen, cargar_preferencias(),
                esfuerzo=config.llm.esfuerzo_ranking,
                tope_restante=config.llm.tope_por_accion_usd,
                techo_configurado=config.llm.max_ofertas_por_ranking,
            )
            detalle = {
                "Ofertas a ordenar": plan.ofertas,
                "Llamadas a la API": 1 if plan.hay_trabajo else 0,
            }
            if plan.recortado:
                detalle["De las que tienes sin revisar"] = plan.disponibles
                detalle["Este orden NO mira"] = (
                    f"{plan.dejadas_fuera} (las más antiguas; siguen en la bandeja)"
                )
            coste, hay, nota = plan.coste_usd, plan.hay_trabajo, plan.nota
            muestra = []
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Acción '{accion}' no válida. Admitidas: "
                       f"{', '.join(sorted(COMANDOS))}.",
            )
    finally:
        almacen.close()

    disponible = _libro().estado(config.llm, es_manual=True).disponible
    return {
        "accion": accion,
        "etiqueta": COMANDOS[accion]["etiqueta"],
        "moneda": "usd",
        "hay_trabajo": hay,
        "cabe": coste <= disponible,
        "coste_usd": round(coste, 4),
        "disponible_usd": round(disponible, 4),
        "nota": nota,
        "avisos": [],
        "detalle": detalle,
        "muestra": muestra,
    }


@app.get("/api/plan/{accion}")
def plan(accion: str) -> dict[str, Any]:
    """Qué haría ese botón y qué costaría. NO llama a nada y no gasta nada."""
    if accion not in COMANDOS or accion == "leer_una":
        raise HTTPException(
            status_code=400,
            detail=f"Acción '{accion}' no válida aquí. Para una oferta concreta, "
                   "usa /api/ofertas/{clave}/plan.",
        )
    return _plan_como_json(accion)


@app.get("/api/ofertas/{clave}/plan")
def plan_de_leer_una(clave: str) -> dict[str, Any]:
    """Lo que costaría que el asistente lea ESA oferta."""
    config = load_config()
    almacen = _abrir_almacen()
    try:
        plan = plan_de_una_lectura(almacen, _cargar_fichas(), clave)
    finally:
        almacen.close()

    if not plan.existe:
        raise HTTPException(status_code=404, detail="Esa oferta no existe.")

    disponible = _libro().estado(config.llm, es_manual=True).disponible
    return {
        "accion": "leer_una",
        "etiqueta": COMANDOS["leer_una"]["etiqueta"],
        "moneda": "usd",
        "clave": clave,
        "titulo": plan.titulo,
        "empresa": plan.empresa,
        "hay_trabajo": plan.se_puede,
        "ya_clasificada": plan.ya_clasificada,
        "cabe": plan.coste_usd <= disponible,
        "coste_usd": round(plan.coste_usd, 4),
        "disponible_usd": round(disponible, 4),
        "nota": (
            "Ya está clasificada: su veredicto está en la bandeja y no se vuelve "
            "a pagar por ella."
            if plan.ya_clasificada
            else
            # SIN CACHÉS NI PROMPTS (7 sept 2026). Lo que ella decide aquí
            # es si paga esta lectura suelta; para eso no necesita saber cómo
            # funciona el modelo, necesita el importe, que ya está arriba del
            # todo en el diálogo. El porqué técnico sigue en `plan.py`.
            "Leer esta oferta por separado suele costar más por oferta que "
            "leer varias juntas. El importe estimado de esta lectura aparece arriba."
        ),
        # EL INTENTO ANTERIOR SE CUENTA, Y EN CASTELLANO (7 sept 2026).
        #
        # El motivo que guarda la base es el que devuelve la API, y viene en
        # inglés: «Your credit balance is too low…». Eso al log, que es de
        # quien lo tiene que leer; a ella se le dice que no se pudo leer y que
        # el turno de la oferta sigue ahí. Sin este aviso, volver a pulsar es
        # pagar otra vez por el mismo error sin saberlo.
        "avisos": (
            [
                "El intento anterior no se pudo leer y la oferta conserva su "
                "turno. Si vuelve a fallar, avísame antes de repetirlo."
            ]
            if plan.fallo_anterior
            else []
        ),
        "detalle": {"Llamadas a la API": 1 if plan.se_puede else 0},
    }


# ---------------------------------------------------------------------------
#  Los trabajos
# ---------------------------------------------------------------------------

def _verificar_que_cabe(accion: str, clave: str | None, confirmado: float | None) -> float:
    """Recalcula el plan y se niega si no procede. Devuelve el coste previsto.

    Se comprueba aquí, y no solo en el cliente, porque quien tiene que negarse
    es quien gasta. Que el navegador enseñara un número no prueba nada: entre
    que se enseña y se pulsa puede haber entrado una tarea programada, o
    haberse agotado el mes.
    """
    if accion == "leer_una":
        if not clave:
            raise HTTPException(
                status_code=400, detail="Falta la clave de la oferta que leer."
            )
        datos = plan_de_leer_una(clave)
    else:
        datos = _plan_como_json(accion)

    if not datos["hay_trabajo"]:
        raise HTTPException(status_code=409, detail=datos["nota"])

    coste = float(datos["coste_usd"])

    if datos["moneda"] == "usd":
        config = load_config()
        guardia = GuardiaPresupuesto(_libro(), config.llm, es_manual=True)

        # EL FRENO QUE SE LE PROMETE, Y VA EL PRIMERO.
        #
        # Antes solo se miraba si cabía en el MES. Eso protegía de gastar
        # mucho a lo largo de treinta días, pero no de que un botón se llevara
        # un pellizco grande de una vez, que es lo que ella ve. Y el techo del
        # mes ya no frena a sus acciones (ver `presupuesto.py`), así que sin
        # esto no quedaría ningún freno delante de sus botones.
        #
        # 402 y no 409: es un problema de importe, igual que el del mes, y la
        # interfaz los pinta igual. Lo que cambia es la frase, que dice cuál
        # de los dos topes fue y cómo partir el trabajo.
        motivo = guardia.abrir_accion(coste)
        if motivo is not None:
            raise HTTPException(status_code=402, detail=motivo)

        if not datos["cabe"] and guardia.estado().frena_el_mes:
            raise HTTPException(
                status_code=402,
                detail=(
                    f"No cabe en el presupuesto del mes: se estiman "
                    f"${coste:.4f} y quedan ${datos['disponible_usd']:.4f}. "
                    "No se ha gastado nada y no se ha perdido ninguna oferta: "
                    "siguen pendientes y se clasifican solas en cuanto haya saldo."
                ),
            )

    if (
        confirmado is not None
        and coste > max(confirmado, 0.0) * MARGEN_DE_CONFIRMACION
        and coste > 0.01
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"El coste ha subido desde que se te enseñó: confirmaste "
                f"${confirmado:.4f} y ahora son ${coste:.4f}. No se ha lanzado "
                "nada. Vuelve a pedirlo para ver el número de ahora."
            ),
        )

    return coste


def _planificador_del_dia(gastado_al_empezar: float):
    """Decide, tramo a tramo, qué lanzar y con cuánto tope le queda.

    AQUÍ ESTÁ LA PROMESA DE «PONME AL DÍA», y es lo único que hace que
    encadenar no sea hacer trampas.

    Cada tramo se lanza con `--tope-accion-usd <lo que queda>`, calculado
    contra el libro de gastos en ese instante. Si se lanzaran los tres sin
    eso, cada uno se mediría contra el tope entero y una sola pulsación podría
    gastar $1,80 con un cartel de $0,60 delante — que es literalmente el error
    que la v8 corrigió al mover el freno de por-llamada a por-acción.

    Y SE VUELVE A PLANIFICAR ANTES DE CADA TRAMO, no solo a repartir. El primer
    paso trae ofertas nuevas y el segundo las clasifica, así que cuando le toca
    al tercero el mundo ya no es el que se estimó al pulsar. Un reparto hecho
    al principio sería un plan de una corrida que no es la que va a pasar.
    """
    def planificar(indice: int) -> Paso | Parada:
        config = load_config()
        tope = float(getattr(config.llm, "tope_por_accion_usd", 0.0) or 0.0)
        gastado = max(0.0, _libro().gastado_total() - gastado_al_empezar)
        restante = round(tope - gastado, 6)

        if indice == 0:
            # Buscar no cuesta dinero: gasta cuota de Adzuna, y esa se
            # regenera. No se le pone tope porque no hay nada que topar.
            plan = plan_de_recoleccion(config)
            if not (plan.hay_trabajo and plan.cabe):
                return Parada(f"No se buscaron ofertas nuevas: {plan.nota}", fue_bien=True)
            return Paso("Buscar ofertas nuevas", ["-m", "recolector", "run"])

        if restante <= 0:
            return Parada(
                f"Esta acción ya gastó sus ${tope:.2f}. Lo que falta sigue "
                "pendiente: vuelve a pulsar «Ponme al día» y sigue donde iba.",
                fue_bien=True,
            )

        almacen = _abrir_almacen()
        try:
            if indice == 1:
                plan = plan_de_clasificacion(almacen, config, _cargar_fichas())
                etiqueta, argv = "Que el asistente lea lo nuevo", [
                    "-m", "clasificador", "--manual",
                ]
            else:
                # SE PARTE EN VEZ DE NEGARSE, y el recorte viaja al comando.
                # Sin el `--limite`, el subproceso volvería a pedir la cola
                # entera y se negaría él: el plan diría una cosa y la corrida
                # haría otra, que es exactamente lo que `plan.py` existe para
                # impedir.
                plan = plan_de_ranking_que_quepa(
                    almacen, cargar_preferencias(),
                    esfuerzo=config.llm.esfuerzo_ranking,
                    tope_restante=restante,
                    techo_configurado=config.llm.max_ofertas_por_ranking,
                )
                etiqueta, argv = "Ordenar por dónde empezar", [
                    "-m", "clasificador", "ranking", "--manual",
                ]
                if plan.limite:
                    argv += ["--limite", str(plan.limite)]
        finally:
            almacen.close()

        if not plan.hay_trabajo:
            # NO es un fallo y no puede verse como tal: que no haya nada que
            # leer es el estado normal en cuanto la cola está al día.
            return Parada(plan.nota, fue_bien=True)

        if plan.coste_usd > restante:
            return Parada(
                f"«{etiqueta}» costaría ${plan.coste_usd:.4f} y a esta acción le "
                f"quedan ${restante:.4f} de su tope de ${tope:.2f}. No se ha "
                "gastado de más y no se ha perdido ninguna oferta: está "
                "pendiente, y su botón está debajo.",
                fue_bien=True,
            )

        # EL TRAMO SE LLEVA LO QUE QUEDA, NO EL TOPE ENTERO.
        argv += ["--tope-accion-usd", f"{restante:.6f}"]
        return Paso(etiqueta, argv)

    return planificar


@app.post("/api/trabajos")
def lanzar_trabajo(peticion: LanzarTrabajo) -> dict[str, Any]:
    """Lanza una corrida en segundo plano. Devuelve el trabajo para ir mirándolo.

    Nunca se llega aquí sin haber pedido antes el plan: la interfaz lo enseña y
    el servidor lo vuelve a comprobar. Ver `_verificar_que_cabe`.
    """
    if peticion.accion not in COMANDOS:
        raise HTTPException(
            status_code=400,
            detail=f"Acción '{peticion.accion}' no válida. Admitidas: "
                   f"{', '.join(sorted(COMANDOS))}.",
        )

    coste = _verificar_que_cabe(
        peticion.accion, peticion.clave, peticion.coste_confirmado_usd
    )

    comando = COMANDOS[peticion.accion]

    if peticion.accion == "al_dia":
        try:
            return trabajos.lanzar_cadena(
                accion="al_dia",
                etiqueta=comando["etiqueta"],
                # El punto de partida se toma AQUÍ, antes de lanzar nada: es
                # contra esto contra lo que se mide cuánto lleva gastado la
                # cadena, y por tanto cuánto le queda de tope.
                planificador=_planificador_del_dia(_libro().gastado_total()),
                pasos_total=len(PASOS_DEL_DIA),
                coste_estimado_usd=coste,
            ).a_json()
        except TrabajoEnCursoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    argv = list(comando["argv"])
    if peticion.accion == "leer_una":
        argv.append(peticion.clave or "")
    if peticion.accion == "ranking":
        # EL RECORTE VIAJA CON EL COMANDO. El diálogo dijo «ordena 33 de 98»;
        # sin este `--limite` el subproceso pediría las 98 y se negaría, y el
        # número enseñado no describiría la corrida que viene detrás.
        almacen = _abrir_almacen()
        try:
            config = load_config()
            plan = plan_de_ranking_que_quepa(
                almacen, cargar_preferencias(),
                esfuerzo=config.llm.esfuerzo_ranking,
                tope_restante=config.llm.tope_por_accion_usd,
                techo_configurado=config.llm.max_ofertas_por_ranking,
            )
        finally:
            almacen.close()
        if plan.limite:
            argv += ["--limite", str(plan.limite)]

    try:
        trabajo = trabajos.lanzar(
            accion=peticion.accion,
            argv=argv,
            etiqueta=comando["etiqueta"],
            coste_estimado_usd=coste,
            clave=peticion.clave,
        )
    except TrabajoEnCursoError as exc:
        # 409 y no 429: no es que vaya demasiado rápido, es que ese recurso
        # está ocupado. Dos corridas a la vez se pisarían el libro de gastos.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return trabajo.a_json()


@app.get("/api/trabajos")
def listar_trabajos() -> dict[str, Any]:
    """El que está corriendo, si lo hay, y los últimos que terminaron.

    Es lo que permite que ella recargue la página a mitad de una corrida y
    vuelva a ver la barra donde estaba: el trabajo vive en el servidor, no en
    la pestaña.
    """
    en_curso = trabajos.en_curso()
    return {
        "en_curso": en_curso.a_json() if en_curso else None,
        "ultimos": [t.a_json() for t in trabajos.ultimos()],
    }


@app.get("/api/trabajos/{trabajo_id}")
def ver_trabajo(trabajo_id: str) -> dict[str, Any]:
    trabajo = trabajos.obtener(trabajo_id)
    if trabajo is None:
        raise HTTPException(status_code=404, detail="Ese trabajo no existe.")
    return trabajo.a_json()


@app.post("/api/trabajos/{trabajo_id}/cancelar")
def cancelar_trabajo(trabajo_id: str) -> dict[str, Any]:
    """Para la corrida. Lo hecho hasta ahora se queda hecho.

    No hay nada que deshacer: cada clasificación se guarda en cuanto se tiene y
    cada gasto se apunta antes de guardarla, así que parar a mitad deja lo
    pagado contado y el resto PENDIENTE. Es el mismo final que quedarse sin
    presupuesto, que es un final previsto.
    """
    if trabajos.obtener(trabajo_id) is None:
        raise HTTPException(status_code=404, detail="Ese trabajo no existe.")
    if not trabajos.cancelar(trabajo_id):
        raise HTTPException(status_code=409, detail="Ese trabajo ya había terminado.")
    return {"ok": True, "id": trabajo_id}


# ---------------------------------------------------------------------------
#  El ranking
# ---------------------------------------------------------------------------

@app.get("/api/ranking")
def ranking() -> dict[str, Any]:
    """El último ranking guardado. No llama a nada y no cuesta nada.

    Hasta hoy esto solo se veía en consola con `ranking --ultimo`, y es
    precisamente lo único del sistema que ella lee entera antes de abrir nada.
    """
    almacen = _abrir_almacen()
    try:
        cabecera = almacen.ultimo_ranking()
        if cabecera is None:
            return {"hay": False}

        return {
            "hay": True,
            "id": cabecera["id"],
            "creado_at": cabecera["creado_at"],
            "lectura": cabecera["lectura"],
            "patrones": patrones_desde_json(cabecera["patrones"]),
            "ofertas_consideradas": cabecera["ofertas_consideradas"],
            # CUÁNTAS HABÍA SIN REVISAR CUANDO SE ORDENÓ.
            #
            # Si es mayor que las consideradas, este orden se recortó y la
            # pantalla tiene que decirlo: un orden de 20 pintado igual que uno
            # de 39 esconde 19 ofertas dentro de la única pieza que ella lee
            # entera. Se guarda con el ranking y no se recalcula al abrir la
            # bandeja, porque para entonces la cola ya es otra y el «de N»
            # dejaría de describir esta corrida.
            #
            # Los rankings de antes del 4 septiembre 2026 traen 0 y eso
            # significa «no consta», no «cero»: se pintan sin la coletilla.
            "ofertas_disponibles": cabecera["ofertas_disponibles"] or 0,
            "coste_usd": round(cabecera["coste_usd"], 4),
            # CUÁNTO HA ENVEJECIDO, Y LA PANTALLA LO DICE.
            #
            # Un ranking no es una caché: en cuanto entran ofertas nuevas o el
            # sistema cambia un veredicto, el orden que ella lee entero deja de
            # ser el de ahora — y hasta hoy lo único que se enseñaba era la
            # fecha. Ver `storage.desajuste_del_ranking`, que explica también
            # por qué NO se mide comparando fechas.
            "desajuste": almacen.desajuste_del_ranking(cabecera["id"]),
            "items": [
                {
                    "clave": item["dedupe_key"],
                    "posicion": item["posicion"],
                    "por_que": item["por_que"],
                    "accion": item["accion"],
                    "titulo": item["puesto_canonico"] or item["title"],
                    "empresa": item["empresa_normalizada"] or item["company"],
                    "url": item["canonical_url"],
                    "mercado": item["market_key"],
                }
                for item in almacen.items_de_ranking(cabecera["id"])
            ],
        }
    finally:
        almacen.close()


# ---------------------------------------------------------------------------
#  Estáticos
# ---------------------------------------------------------------------------

class CambioDeAjustes(BaseModel):
    """Lo que manda la pantalla de ajustes. Todo opcional: se manda lo que cambió."""

    criterios: list[str] | None = None
    cuantas_destacar: int | None = None
    automatica: dict[str, Any] | None = None


def _ajustes_actuales() -> ajustes_propios.Ajustes:
    """Sus ajustes de ahora: la semilla del YAML con lo suyo encima."""
    guardados = ajustes_propios.leer() or {}
    preferencias = cargar_preferencias()
    automatica = guardados.get("automatica") or {}
    return ajustes_propios.Ajustes(
        criterios=preferencias.criterios,
        cuantas_destacar=preferencias.cuantas_destacar,
        automatica=ajustes_propios.Automatica(
            activa=bool(automatica.get("activa", False)),
            hora=str(automatica.get("hora") or ajustes_propios.HORA_POR_DEFECTO),
        ),
    )


@app.get("/api/ajustes")
def ver_ajustes() -> dict[str, Any]:
    """Sus criterios y su corrida diaria, más lo que trae la app de fábrica.

    Los de fábrica viajan para que el botón «volver a los de fábrica» no tenga
    que inventárselos en el navegador: si algún día cambia la semilla, cambia
    en un sitio.
    """
    actuales = _ajustes_actuales()
    de_fabrica = cargar_preferencias(ajustes=Path("no-existe-a-proposito"))
    return {
        **actuales.a_json(),
        "de_fabrica": {
            "criterios": de_fabrica.criterios,
            "cuantas_destacar": de_fabrica.cuantas_destacar,
        },
        "max_criterios": ajustes_propios.MAX_CRITERIOS,
        "tarea_puesta": programada.existe(),
    }


@app.post("/api/ajustes")
def guardar_ajustes(cambio: CambioDeAjustes) -> dict[str, Any]:
    """Guarda lo que ella escribió y deja el reloj de Windows a juego.

    NO CUESTA NADA Y NO RECLASIFICA NADA: las preferencias no entran en la
    huella de las fichas, así que cambiar el orden no marca ninguna oferta
    como obsoleta. Ver `Preferencias`.
    """
    entra = {k: v for k, v in cambio.model_dump().items() if v is not None}
    try:
        nuevos = ajustes_propios.validar(entra, semilla=_ajustes_actuales())
    except ajustes_propios.AjustesError as exc:
        # 422 y no 500: lo que llegó no vale, y el motivo se le enseña tal cual.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    ajustes_propios.guardar(nuevos)

    # EL RELOJ SE PONE AL DÍA EN EL MISMO GESTO. Guardar «a las 8:00» y dejar
    # la tarea a las 7:00 sería el peor final: dos verdades distintas, y la
    # que manda es la que ella no ve.
    aviso = programada.asegurar(nuevos.automatica.activa, nuevos.automatica.hora)
    if aviso:
        log("bandeja").info("%s", aviso)

    return {**nuevos.a_json(), "tarea_puesta": programada.existe(), "nota": aviso}


@app.get("/api/ultima-corrida")
def ultima_corrida() -> dict[str, Any]:
    """Qué hizo la corrida automática, para poder enseñarlo al abrir.

    Gastar con ella dormida solo es aceptable si al llegar ve qué se hizo y
    cuánto costó. Lo escribe `herramientas/corrida_diaria.py`.
    """
    ruta = load_config().database_path.parent / "ultima_corrida.json"
    try:
        with open(ruta, "r", encoding="utf-8") as handle:
            datos = json.load(handle)
        return {"hay": True, **datos}
    except (OSError, ValueError):
        return {"hay": False}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    """El icono de la ventana, servido desde el MISMO archivo que el acceso
    directo del escritorio (`herramientas/job-rob.ico`).

    En modo `--app` el navegador toma de aquí el icono de la barra de tareas.
    Que sea el mismo archivo y no una copia es lo que impide que un día el
    escritorio enseñe un icono y la barra de tareas otro.
    """
    icono = Path(__file__).resolve().parent.parent / "herramientas" / "job-rob.ico"
    if not icono.exists():
        raise HTTPException(status_code=404, detail="No hay icono.")
    return FileResponse(icono, media_type="image/x-icon")


@app.get("/api/ayuda")
def ayuda() -> dict[str, Any]:
    """El texto de `AYUDA.md`, para pintarlo dentro de la app.

    POR QUÉ SE SIRVE EL TEXTO Y NO SE ABRE EL ARCHIVO. `os.startfile` sobre un
    `.md` deja que Windows decida, y en una máquina sin editor de Markdown
    asociado eso es un diálogo de «¿con qué quieres abrir esto?». Es el mismo
    motivo por el que «Si algo va mal» abre la carpeta en vez de copiar la
    ruta: el paso que no se puede dar es el que decide si la ayuda llega.

    POR QUÉ UN ARCHIVO Y NO TEXTO METIDO EN EL HTML. Porque desde España se
    arregla etiquetando: una duda que aparezca en octubre se contesta en este
    archivo, se etiqueta, y le llega al abrir la app sin tocar la pantalla.

    AQUÍ DECÍA ADEMÁS QUE EL ARCHIVO SE IMPRIME (corregido el 7 sept 2026).
    Se decidió no imprimirlo. Eso deja un hueco conocido y sin tapar: el día
    que la app no arranque, esta ayuda no se puede leer. Lo único que hay para
    ese caso es que ella escriba, y por eso `AYUDA.md` lo dice ahora en su
    primera línea en vez de prometer un papel que no existe.

    Si el archivo no está, se dice en la pantalla y la app sigue: esto no es
    una razón para que una ventana deje de funcionar.
    """
    try:
        return {"hay": True, "texto": AYUDA_MD.read_text(encoding="utf-8")}
    except OSError as exc:
        log("bandeja").warning("no se pudo leer AYUDA.md: %s", exc)
        return {"hay": False, "texto": ""}


# Se monta al final para que las rutas de la API tengan prioridad.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main(argv: list[str] | None = None) -> None:
    import argparse

    import uvicorn

    opciones = argparse.ArgumentParser(prog="python -m bandeja")
    # LO USA LA CÁSCARA DE ESCRITORIO, y sin esto sobraría una ventana.
    #
    # `herramientas/abrir.py` levanta este servidor y después abre la ventana
    # él mismo, en modo `--app`: sin barra de direcciones y sin pestañas. Si
    # además se abriera el navegador por defecto, ella vería las dos cosas —la
    # app y una pestaña normal con la URL y el puerto a la vista—, que es
    # exactamente lo que la cáscara existe para que no pase.
    opciones.add_argument(
        "--sin-navegador", action="store_true",
        help="No abre el navegador. Lo usa la cáscara de escritorio.",
    )
    args = opciones.parse_args(argv)

    host, puerto = "127.0.0.1", 8765
    url = f"http://{host}:{puerto}"

    # EL LOG SE ENCIENDE ANTES QUE NADA. Lo que falle a partir de aquí queda
    # escrito en un archivo que ella puede mandar; lo que falle antes, no. Va
    # antes incluso de cargar la configuración, porque una configuración rota
    # es justo uno de los fallos que hay que poder leer desde lejos.
    ruta_log = configurar_log(load_config().database_path.parent)
    v = version_actual()

    # LA ENTREGA QUE NO SE PUDO PREPARAR. Ver
    # `presupuesto.reparar_libro_de_la_entrega`, que es donde está el porqué.
    #
    # VA AQUÍ Y NO EN LA CÁSCARA, Y NO ES UN DETALLE (medido el 11 septiembre
    # 2026, ejecutándolo). `abrir.py` ya está cargado en memoria cuando el
    # actualizador cambia los archivos del disco, así que ese proceso sigue
    # corriendo la versión ANTERIOR: cualquier cosa que se ponga allí tarda
    # DOS aperturas en hacer efecto. El servidor, en cambio, arranca después
    # del `checkout`, o sea que es código nuevo desde la primera.
    #
    # Y es el momento bueno además por otro lado: nadie ha lanzado todavía
    # ninguna corrida, así que el libro está quieto y no hay dos escritores.
    #
    # Envuelto porque un contador con una cifra de más es un problema, y una
    # app que no abre en su casa, a 9.000 km, es otro mucho peor.
    try:
        from clasificador.presupuesto import reparar_libro_de_la_entrega

        hecho = reparar_libro_de_la_entrega(ruta_del_libro(load_config()))
        if hecho:
            print(f"  {hecho}")
            log("bandeja").info("%s", hecho)
    except Exception as exc:  # noqa: BLE001
        log("bandeja").warning("no se pudo revisar el libro de gastos: %s", exc)

    # EL RELOJ DE WINDOWS, A JUEGO CON SUS AJUSTES, EN CADA ARRANQUE.
    #
    # Se pone al guardar desde la pantalla, y también aquí, por lo que puede
    # pasar entre medias: que ella borrara la tarea a mano, que un perfil
    # nuevo se la llevara, o —el caso de verdad— que la actualización traiga
    # esta función por primera vez a una copia que ya tenía la casilla
    # encendida. Es idempotente: si ya está como tiene que estar, no dice ni
    # hace nada.
    try:
        _a = ajustes_propios.leer() or {}
        _auto = _a.get("automatica") or {}
        _aviso = programada.asegurar(
            bool(_auto.get("activa", False)),
            str(_auto.get("hora") or ajustes_propios.HORA_POR_DEFECTO),
        )
        if _aviso:
            print(f"  {_aviso}")
            log("bandeja").info("%s", _aviso)
    except Exception as exc:  # noqa: BLE001
        log("bandeja").warning("no se pudo revisar la corrida automática: %s", exc)

    # LA GEOGRAFÍA DE HOY, APLICADA A LO QUE YA ESTABA GUARDADO.
    #
    # Cuando cambia la geografía de un mercado —el 24 septiembre 2026, Texas
    # pasó de entero a cuatro zonas— lo que ya estaba en la base no se entera
    # solo. Ver `recolector/recorte.py`: hace copia, borra lo que hoy no
    # entraría y pone la zona a lo que se queda. Una vez por cambio, no en
    # cada apertura.
    #
    # VA ANTES DEL ESTRENO, y el orden importa: el estreno mira cuántas
    # ofertas tiene cada mercado para decidir si buscar.
    try:
        from recolector.recorte import recortar_si_hace_falta

        _config = load_config()
        _almacen = _abrir_almacen()
        try:
            for linea in recortar_si_hace_falta(
                _config, _almacen, _config.database_path.parent
            ):
                print(f"  {linea}")
                log("bandeja").info("recorte geográfico: %s", linea)
        finally:
            _almacen.close()
    except Exception as exc:  # noqa: BLE001
        log("bandeja").warning("no se pudo aplicar el recorte geográfico: %s", exc)

    # LA PRIMERA BÚSQUEDA DE UN MERCADO NUEVO, SOLA.
    #
    # Un mercado llega en una etiqueta de git; sus ofertas no, porque `data/`
    # está fuera de git. Y el desplegable de mercados se dibuja con las ofertas
    # GUARDADAS, así que sin esto ella abre la app el día que entra México y no
    # ve nada: ni el mercado ni una oferta. Ver `bandeja/estreno.py`, que
    # explica por qué esto sí puede hacerse sin preguntar (busca, no clasifica:
    # cuesta peticiones de Adzuna y cero dólares) y la corrida automática no.
    #
    # Va aquí, antes de `uvicorn.run`, por dos motivos: el registro de trabajos
    # lanza un subproceso y lo sigue en un hilo, así que no bloquea el arranque;
    # y cuando la pantalla cargue encontrará el trabajo en curso y enseñará la
    # barra sola (ver `iniciar()` en `app.js`, que ya retoma lo que haya).
    try:
        estrenado = estreno.estrenar_si_hace_falta(
            config=load_config(),
            almacen_abridor=_abrir_almacen,
            registro=trabajos,
            comando=COMANDOS["recolectar"],
            log=log,
        )
        if estrenado:
            print(f"  {estrenado}")
            log("bandeja").info("%s", estrenado)
    except Exception as exc:  # noqa: BLE001
        log("bandeja").warning("no se pudo lanzar la primera búsqueda: %s", exc)

    print("Bandeja de ofertas")
    print(f"  {url}")
    print(f"  versión {v.para_pantalla()}")
    print(f"  log: {ruta_log}")
    print("  Ctrl+C para parar\n")

    if not v.es_de_fiar:
        # No es un error: en la máquina del desarrollador es lo normal. En la
        # de la clienta significaría que el actualizador no manda sobre esta
        # copia, y eso hay que poder verlo en el log.
        log("bandeja").warning(
            "versión no publicada: %s. Si esto no es la máquina del "
            "desarrollador, el actualizador no gobierna esta copia.",
            v.para_log(),
        )

    # El navegador se abre solo, pero después de arrancar uvicorn habría que
    # esperar; abrirlo antes funciona porque el navegador reintenta.
    if not args.sin_navegador:
        webbrowser.open(url)
    uvicorn.run(app, host=host, port=puerto, log_level="warning")


if __name__ == "__main__":
    main()
