"""
El comando de consola de la capa 2.

    python -m clasificador                  clasifica lo pendiente
    python -m clasificador --dry-run        enseña el plan y el coste, y sale
    python -m clasificador --limite 3       solo las N primeras (para probar)
    python -m clasificador --reclasificar   incluye las que usan fichas viejas
    python -m clasificador --reintentar     reintenta las que fallaron
    python -m clasificador grupos           informe de vacantes repetidas
    python -m clasificador --solo CLAVE     clasifica esa oferta y ya

Códigos de salida, iguales en espíritu a los del recolector:
    0  todo bien
    1  problema de configuración (fichas, base de datos, API key)
    2  la corrida terminó con errores en alguna oferta
    3  no se clasificó todo porque se agotó el presupuesto del mes

El 3 es el mismo código que usa el recolector para "problema de cuota", y a
propósito: el día que esto corra desde una tarea programada, quien la vigile
tiene que poder distinguir "falló" de "no había saldo" sin leer la salida.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import textwrap
import uuid
from datetime import datetime, timezone
from typing import Any

from recolector.config import ConfigError, load_config
from recolector.storage import Storage

from .agrupador import agrupar, grupos_con_repeticion
from .asistente import Asistente, patrones_a_json, patrones_desde_json
from .cliente_llm import (
    MODELO,
    TOKENS_SALIDA_ENTERA,
    TOKENS_SALIDA_RECORTE,
    PRECIOS_ACTUALIZADOS,
    PRECIO_ENTRADA_USD_POR_MILLON,
    PRECIO_SALIDA_USD_POR_MILLON,
    ClienteAnthropic,
)
from .fichas import (
    FichasError,
    avisos_de_fichas,
    cargar_fichas,
    cargar_preferencias,
)
from .motor import VIA_ENTERA, VIA_RECORTE, Motor, ResumenCorrida
from .plan import (
    ClienteInerte,
    plan_de_clasificacion,
    plan_de_ranking,
    plan_de_ranking_que_quepa,
    plan_de_relectura,
    plan_de_una_lectura,
)
from .triaje import (
    _salida_por_oferta,
    coste_estimado as triaje_coste_estimado,
)
from .presupuesto import (
    ruta_del_libro,
    GuardiaPresupuesto,
    LibroCorruptoError,
    LibroDeGastos,
)

ANCHO = 78


def _prepare_console() -> None:
    """Igual que en `recolector/cli.py`: que la consola de Windows no reviente
    con un acento. cp1252 no cubre todo lo que imprimimos.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _titulo(texto: str) -> None:
    print()
    print(texto)
    print("=" * ANCHO)


def _recortar(texto: str, ancho: int) -> str:
    texto = " ".join((texto or "").split())
    return texto if len(texto) <= ancho else texto[: ancho - 1] + "…"


def _envolver(texto: str, ancho: int = ANCHO) -> str:
    """Un párrafo repartido en líneas, sin perder el final.

    LAS NOTAS NO SE RECORTAN. Se recortaba la del plan a tres anchos y se
    imprimía en una sola línea, así que en la terminal se veía cortada — y lo
    que se perdía era el final, que es donde va el número accionable: «caben
    ~228 vacantes por llamada». Un motivo al que se le corta la salida es un
    motivo que no sirve. Recortar vale para un título en una tabla; para una
    explicación, no.
    """
    return textwrap.fill(" ".join((texto or "").split()), ancho)


# ---------------------------------------------------------------------------
#  Informe de la corrida
# ---------------------------------------------------------------------------

def _imprimir_detalle(resumen: ResumenCorrida) -> None:
    _titulo("OFERTA POR OFERTA")

    if not resumen.resultados:
        print("(ninguna)")
        return

    for r in resumen.resultados:
        if r.estado != "ok":
            marca, veredicto = "!!", "SIN CLASIFICAR"
        elif r.necesita_criterio:
            marca, veredicto = "??", "TU CRITERIO"
        elif r.clasificacion and r.clasificacion.relevante:
            marca, veredicto = "->", "RELEVANTE"
        else:
            marca, veredicto = "  ", "descartada"

        print()
        print(f"{marca} [{veredicto:>14}] {_recortar(r.titulo, 52)}")
        print(f"   {_recortar(r.empresa, 40):<42} {r.mercado}")

        if r.heredada_de:
            # Sin esto, cinco anuncios idénticos con la misma razón parecen
            # cinco decisiones distintas del modelo. Fueron una.
            print("   (misma vacante que un anuncio anterior; veredicto copiado, "
                  "sin coste)")
            continue

        if r.clasificacion is not None:
            c = r.clasificacion
            cv = r.cv_final or "(sin CV)"
            print(f"   CV: {cv}   confianza: {c.confianza:.2f}   "
                  f"nivel: {c.seniority_detectado}   modalidad: {c.modalidad}")
            if c.es_reclutadora:
                print("   Publica una AGENCIA de reclutamiento, no el empleador final.")
            print(f"   Razón: {_recortar(c.razon, ANCHO - 11)}")
            if not c.relevante and c.motivo_descarte:
                print(f"   Descarte: {_recortar(c.motivo_descarte, ANCHO - 14)}")

        if r.motivo_criterio:
            # «DUDA» NO ES LO MISMO QUE «FALLO», Y SE ESCRIBÍAN IGUAL.
            #
            # `motivo_criterio` guarda dos cosas distintas: la duda del modelo
            # —«confianza 0.68, por debajo del umbral»— y, cuando la lectura
            # se cae, el error de la API. Con la misma etiqueta delante, «la
            # cuenta de Anthropic se ha quedado sin saldo» se leía como una
            # duda del asistente. Medido el 9 septiembre 2026: es el mensaje
            # más accionable que da la app, y se disfrazaba del menos urgente.
            #
            # Y NO SE RECORTA. Iba con `_recortar`, así que se leía «No se ha
            # leído nada…» y lo que se perdía era justo qué hacer al respecto.
            if r.estado != "ok":
                print(f"   No se pudo leer: {_envolver(r.motivo_criterio)}")
            else:
                print(f"   Duda: {_recortar(r.motivo_criterio, ANCHO - 10)}")


def _imprimir_resumen(resumen: ResumenCorrida) -> None:
    _titulo("RESUMEN DE LA CORRIDA")

    print(f"Ofertas procesadas ....... {resumen.total}")
    print(f"  Clasificadas ........... {resumen.clasificadas}")
    print(f"  Sin clasificar ......... {resumen.fallidas}")
    print()
    print(f"Llamadas a la API ........ {resumen.llamadas}")
    if resumen.heredadas:
        print(f"  Heredadas (coste 0) .... {resumen.heredadas}  "
              f"anuncios que eran la misma vacante")
    print()
    print(f"  Relevantes ............. {resumen.relevantes}")
    print(f"  Descartadas por ruido .. {resumen.descartadas}")
    print(f"  Necesitan tu criterio .. {resumen.necesitan_criterio}")

    por_cv = resumen.por_cv()
    if por_cv:
        print()
        print("Distribución por CV:")
        for cv_id, total in sorted(por_cv.items(), key=lambda kv: -kv[1]):
            print(f"  {cv_id:<28} {total}")

    _titulo("COSTE")
    print(f"Modelo ................... {resumen.modelo_usado}")
    print(f"Tokens de entrada ........ {resumen.tokens_entrada:,}")
    print(f"Tokens de salida ......... {resumen.tokens_salida:,}")
    print(f"Coste de esta corrida .... ${resumen.coste_usd:.4f} USD")
    print()
    print(f"(a ${PRECIO_ENTRADA_USD_POR_MILLON:.2f} y ${PRECIO_SALIDA_USD_POR_MILLON:.2f} "
          f"por millón de tokens, tarifa de {PRECIOS_ACTUALIZADOS})")


def _imprimir_grupos(almacen: Storage) -> int:
    """Informe de vacantes repetidas. Es lo que evita que ella revise siete
    veces el mismo puesto publicado en cinco ciudades.
    """
    grupos = grupos_con_repeticion(agrupar(almacen.claves_de_agrupacion()))

    _titulo("MISMA VACANTE, VARIOS ANUNCIOS")

    if not grupos:
        print("No se detectó ninguna vacante repetida.")
        return 0

    total_anuncios = sum(g.tamano for g in grupos)
    print(f"{len(grupos)} vacante(s) aparecen repetidas en {total_anuncios} anuncios.")
    print(f"Son {total_anuncios - len(grupos)} tarjetas menos que revisar.")
    print()
    print("NADA se ha borrado: las ofertas siguen en la base con sus URLs.")
    print("Agrupar no es deduplicar.")

    for grupo in grupos:
        rep = grupo.representante
        print()
        print("-" * ANCHO)
        print(f"{_recortar(rep.titulo, ANCHO)}")
        print(f"  {_recortar(rep.empresa, 44):<46} {rep.mercado}")
        print(f"  {grupo.resumen()}")
        for motivo in sorted(grupo.motivos):
            print(f"  Agrupadas porque: {motivo}")
        for m in grupo.miembros:
            print(f"    - {_recortar(m.ubicacion or '(sin ubicación)', 34):<36} "
                  f"{_recortar(m.titulo, 36)}")

    return 0


# ---------------------------------------------------------------------------
#  Comandos
# ---------------------------------------------------------------------------

def comando_clasificar(args: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ERROR de configuración: {exc}", file=sys.stderr)
        return 1

    if not config.database_path.exists():
        print(
            "No hay base de datos todavía. Ejecuta primero una recolección:\n"
            "  python -m recolector run --sources adzuna",
            file=sys.stderr,
        )
        return 1

    try:
        fichas = cargar_fichas()
    except FichasError as exc:
        print(f"ERROR en config/cvs.yaml: {exc}", file=sys.stderr)
        return 1

    _titulo("FICHAS DE CV")
    print(f"Archivo .................. config/cvs.yaml")
    print(f"Huella ................... {fichas.huella}")
    print(f"Umbral de confianza ...... {fichas.umbral_confianza:.2f}")
    for f in fichas.lista:
        print(f"  {f.id:<22} idioma={f.idioma}  archivo={f.archivo.name}")

    for aviso in avisos_de_fichas(fichas):
        print(f"  AVISO: {aviso}")

    with Storage(config.database_path) as almacen:
        obsoletas = almacen.contar_obsoletas(fichas.huella)
        if obsoletas:
            print()
            print(f"  AVISO: {obsoletas} clasificación(es) se hicieron con una versión")
            print( "         anterior de las fichas. NO se rehacen solas: cuestan dinero.")
            print( "         Usa --reclasificar si quieres rehacerlas.")

        filas = almacen.ofertas_pendientes(
            fichas_huella=fichas.huella,
            incluir_obsoletas=args.reclasificar,
            incluir_fallidas=args.reintentar,
            limit=args.limite,
        )

        if not filas:
            _titulo("NADA QUE HACER")
            print("Todas las ofertas de la base ya están clasificadas.")
            print("La caché hizo su trabajo: no se ha gastado un token.")
            resumen_db = almacen.resumen_clasificaciones()
            print()
            print(f"En total hay {resumen_db['total']} clasificación(es) guardadas, "
                  f"por ${resumen_db['coste_usd']:.4f} USD acumulados.")
            return 0

        try:
            guardia = _construir_guardia(config, es_manual=args.manual, args=args)
        except LibroCorruptoError as exc:
            print(f"\nERROR en el libro de gastos: {exc}", file=sys.stderr)
            return 1

        # EL FRENO POR ACCIÓN VA AQUÍ, ANTES DEL TRIAJE, y el sitio es todo.
        #
        # El triaje ya es una llamada que se paga, así que comprobar después
        # sería comprobar cuando ya se ha gastado. Y se comprueba sobre el
        # PLAN de la corrida entera —triaje más lecturas—, que es el mismo
        # número que la bandeja le enseña antes de que ella confirme: si el
        # freno mirara otra cosa, habría dos importes y uno de los dos sería
        # mentira. Regla nº 9 de la casa.
        if not args.dry_run:
            plan_previo = plan_de_clasificacion(
                almacen, config, fichas,
                sin_triaje=args.sin_triaje,
                limite=args.limite,
                reclasificar=args.reclasificar,
                reintentar=args.reintentar,
            )
            if _no_cabe_en_una_accion(guardia, plan_previo.coste_usd):
                print()
                print(_envolver(plan_previo.nota))
                return 3

        # EL TRIAJE VA AQUÍ, y el sitio importa: después de saber qué hay
        # pendiente y de tener el guardia —su llamada también cuesta dinero—,
        # y antes de construir el cliente de clasificación, porque puede
        # reducir mucho el trabajo que viene detrás.
        triaje = None
        if config.llm.triaje.activo and not args.dry_run and not args.sin_triaje:
            filas, triaje, codigo = _aplicar_triaje(almacen, config, filas, guardia)
            if not filas:
                # Sin nada que leer no se construye cliente ni se gasta más.
                #
                # EL CÓDIGO LO DECIDE `_aplicar_triaje`, NO ESTA LÍNEA. Aquí
                # había un `0 if triaje.ok else 3`, y ese 3 significa «no había
                # saldo» en todo el proyecto — la bandeja lo pinta literalmente
                # así. Con él, «no hay nada nuevo que triar» (que es el estado
                # normal en cuanto todo está triado), «falta la API key» y «el
                # lote no cabe en la llamada» se le presentaban a la clienta
                # las tres como si se hubiera quedado sin dinero. Solo una de
                # las cuatro cosas era verdad.
                return codigo

        estado = guardia.estado()

        # El cliente se construye DESPUÉS de saber que hay trabajo: así el
        # `--dry-run` y el caso "nada pendiente" no exigen una API key.
        if args.dry_run:
            # EL PLAN LO CALCULA `plan.py`, NO ESTA FUNCIÓN.
            #
            # Lo calculaba aquí, y había divergido de la corrida real: estimaba
            # el triaje sobre TODAS las pendientes, sin quitar las que ya
            # tuvieron su turno ni agrupar los anuncios que son la misma
            # vacante. Sobre la base del 2 septiembre 2026, 302 contra 228.
            # El error caía del lado seguro, pero un plan que no describe la
            # corrida que viene detrás no es un plan.
            #
            # Ahora hay un solo cálculo y lo comparten esta consola y el botón
            # de confirmar de la bandeja. Dos estimadores del mismo gasto
            # acaban divergiendo, y ese día la promesa de «nunca se gasta sin
            # enseñar antes el número» se rompe sin que nadie lo note.
            plan = plan_de_clasificacion(
                almacen, config, fichas,
                sin_triaje=args.sin_triaje,
                limite=args.limite,
                reclasificar=args.reclasificar,
                reintentar=args.reintentar,
            )

            _titulo("PLAN (--dry-run: no se llama a nada)")
            print(f"Ofertas pendientes ....... {plan.pendientes}")
            if plan.ya_triadas:
                print(f"  Ya tuvieron su turno ... {plan.ya_triadas}  "
                      "(esperan a que ella las pida)")
            if plan.vacantes and plan.vacantes < plan.sin_triar:
                print(f"  Vacantes distintas ..... {plan.vacantes}  "
                      f"({plan.sin_triar - plan.vacantes} anuncio(s) son la misma "
                      "vacante que otro)")
            if plan.fuera_del_tope:
                print(f"  Entran en esta llamada . {plan.vacantes_esta_corrida}  "
                      f"(tope de lote; {plan.fuera_del_tope} esperan a la "
                      "siguiente con su turno intacto)")
            if plan.hay_triaje:
                print(f"Triaje ................... 1 llamada sobre "
                      f"{plan.vacantes_esta_corrida}, ~${plan.coste_triaje_usd:.4f}")
            print(f"Se leerían a fondo ....... {plan.lecturas}")
            print(f"Llamadas a la API ........ {plan.llamadas}")
            print(f"Modelo ................... {MODELO}")
            print()
            print(_envolver(plan.nota))
            print()
            print("Estimación (prefijo cacheado medido, 400 de salida por llamada):")
            print(f"  Tokens de entrada ...... ~{plan.tokens_entrada:,}")
            print(f"  Tokens de salida ....... ~{plan.tokens_salida:,}")
            print(f"  Coste .................. ~${plan.coste_usd:.4f} USD")
            print()
            print("La cifra real se mide con lo que devuelva la API y se guarda en la base.")
            print()

            _titulo("PRESUPUESTO")
            print(estado.explicacion())
            print()
            if plan.coste_usd <= estado.disponible:
                print(f"  El plan CABE en lo que queda del mes.")
            else:
                print(f"  EL PLAN NO CABE: se estiman ${plan.coste_usd:.4f} y quedan "
                      f"${estado.disponible:.4f}.")
                print( "  La corrida se pararía a media faena y lo que quede seguiría")
                print( "  pendiente, sin perderse ninguna.")
                print( "  Para que quepa: sube `llm.presupuesto_mensual_usd` en "
                       "config/search_profile.yaml,")
                print( "  o espera al mes que viene, o córrelo por partes con --limite.")
            print()

            for mercado, titulo, empresa in plan.muestra:
                print(f"  {mercado:<9} {_recortar(titulo, 46):<48} "
                      f"{_recortar(empresa, 18)}")
            if plan.lecturas > len(plan.muestra):
                print(f"  ... y {plan.lecturas - len(plan.muestra)} más")
            return 0

        try:
            cliente = ClienteAnthropic(libro=guardia.libro)
        except RuntimeError as exc:
            print(f"\nERROR: {exc}", file=sys.stderr)
            return 1

        # Se comprueba ANTES de la primera llamada, no solo entre llamadas:
        # si el mes ya está agotado, no tiene ningún sentido construir el
        # cliente ni imprimir "CLASIFICANDO".
        if estado.agotado and config.llm.detiene_al_agotarse:
            _titulo("PRESUPUESTO AGOTADO")
            print(estado.explicacion())
            print()
            print(f"Hay {len(filas)} oferta(s) pendiente(s) y no se va a gastar nada.")
            print("NO SE HA PERDIDO NINGUNA: siguen pendientes y se clasificarán solas")
            print("en cuanto haya saldo.")
            print()
            print("Para seguir este mes, sube `llm.presupuesto_mensual_usd` en")
            print("config/search_profile.yaml.")
            if not args.manual:
                print()
                print("(Esto es la ración de las corridas automáticas, "
                      f"${config.llm.racion_automatica_mensual_usd:.2f} al mes. "
                      "Lo que lanza ella va con `--manual` y se rige por el tope "
                      "por acción.)")
            return 3

        motor = Motor(almacen, fichas, cliente, presupuesto=guardia)

        _titulo("CLASIFICANDO")
        print(f"{len(filas)} oferta(s) pendiente(s). Modelo: {MODELO}.")
        print(f"Disponible este mes: ${estado.disponible:.4f} de "
              f"${estado.techo_efectivo:.2f}.")
        print("Cada una es una llamada. Ctrl+C para parar.")
        print()

        # El progreso se imprime en vivo, no al final. Ocho llamadas de ~10
        # segundos con la consola muda parecen un cuelgue, y la bandeja lee
        # justo estas líneas para su barra: que las dos miren la misma señal es
        # lo que impide que una de las dos mienta.
        def _progreso(hecho: int, total: int, titulo: str) -> None:
            print(f"  [{hecho}/{total}] {_recortar(titulo, 56)}", flush=True)

        resumen = motor.clasificar(filas, avisar=_progreso)

        _imprimir_detalle(resumen)
        _imprimir_resumen(resumen)
        _imprimir_grupos(almacen)

        if resumen.se_detuvo_por_presupuesto:
            print()
            _titulo("SE PARÓ POR PRESUPUESTO")
            # QUÉ TOPE FUE, Y NO SIEMPRE EL DEL MES.
            #
            # Esta frase decía «al agotarse el techo del mes», fijo, y tres
            # líneas más abajo la tabla decía «Disponible del mes: $10.71» y
            # «Tope por acción $0.60 <- esto es lo que frena». Medido el 9
            # septiembre 2026: la corrida se paró por el tope POR ACCIÓN con
            # el 89% del mes sin gastar, y le contaba lo contrario.
            #
            # Desde que el mes es una cuenta y no un límite, en una acción
            # suya el que frena es casi siempre el de la acción — pero se
            # pregunta en vez de suponerlo, que es lo mismo que se hace con
            # todo lo demás que se le enseña.
            estado = resumen.estado_presupuesto
            fue_el_mes = bool(estado is not None and estado.frena_el_mes)
            cual = "el techo del mes" if fue_el_mes else "el tope de esta acción"
            print(f"{resumen.detenidas_por_presupuesto} oferta(s) se quedaron sin "
                  f"clasificar al agotarse {cual}.")
            print("NO SE HAN PERDIDO: siguen pendientes, sin fila en `clasificaciones`,")
            print("y la próxima corrida con saldo las recoge sola.")
            print()
            print("Se gastó primero en las más recientes, que es donde está el valor.")
            if resumen.estado_presupuesto is not None:
                print()
                print(resumen.estado_presupuesto.explicacion())
            return 3

        if resumen.estado_presupuesto is not None and resumen.estado_presupuesto.conviene_avisar:
            e = resumen.estado_presupuesto
            print()
            print(f"AVISO: llevas el {e.porcentaje_usado:.0f}% del presupuesto del mes "
                  f"(${e.gastado_mes:.4f} de ${e.techo_mensual:.2f}).")

        if resumen.fallidas:
            print()
            print(f"ATENCIÓN: {resumen.fallidas} oferta(s) quedaron SIN CLASIFICAR.")
            print("Están marcadas como tal en la bandeja, con el motivo. "
                  "Reintenta con --reintentar.")
            return 2

    return 0


def _aplicar_triaje(
    almacen: Any,
    config: Any,
    filas: list,
    guardia: GuardiaPresupuesto,
    cliente: Any | None = None,
) -> tuple[list, Any, int]:
    """Reduce `filas` a las que merecen lectura completa.

    Devuelve (filas, triaje, codigo_de_salida). El código es el que hay que
    devolver SI `filas` sale vacía, y lo decide quien conoce el motivo:

        0  no había nada nuevo que triar, o nada mereció lectura. No es fallo
        1  problema de configuración (falta la API key)
        2  el triaje no se hizo: la API, el esquema, o un lote que no cabe
        3  no quedaba saldo este mes

    Distinguirlos no es cosmética: el 3 hace que la bandeja diga «sin saldo»,
    y decir eso cuando no es verdad es exactamente el tipo de mentira pequeña
    que hace que se deje de confiar en el resto de los números.

    Lo que NO hace es descartar. Las que no salen elegidas se quedan sin fila
    en `clasificaciones` —o sea, PENDIENTES— y con su puesto guardado en
    `triajes`. La siguiente corrida las vuelve a considerar, y el botón de la
    bandeja puede clasificar cualquiera de ellas en el momento.
    """
    from .triaje import Triador, claves_de, preparar_lote, rescatar_sin_leer

    cuantas = config.llm.triaje.ofertas_por_corrida
    tope = getattr(config.llm.triaje, "max_vacantes_por_triaje", None)

    # LO QUE UNA CORRIDA CORTADA DEJÓ ELEGIDO Y SIN LEER, PRIMERO.
    # Mismo criterio y misma función que el plan. Ver `triaje.rescatar_sin_leer`.
    grupos_rescate, huecos = rescatar_sin_leer(
        filas, almacen.claves_elegidas_sin_leer(), cuantas
    )
    claves_rescate = claves_de(grupos_rescate)
    rescatadas = [f for f in filas if f["dedupe_key"] in claves_rescate]

    # CADA OFERTA TIENE UN TURNO. Las que ya pasaron por un triaje no vuelven
    # a competir: si volvieran, la cola crecería 13 al día y el triaje acabaría
    # costando más que leerlo todo. Ver `claves_ya_triadas`.
    ya_vistas = almacen.claves_ya_triadas()
    nuevas = [f for f in filas if f["dedupe_key"] not in ya_vistas]
    aplazadas = len(filas) - len(nuevas) - len(rescatadas)

    _titulo("TRIAJE")
    print(f"Pendientes ............... {len(filas)}")
    if grupos_rescate:
        print(f"  Se quedaron sin leer ... {len(grupos_rescate)}  "
              "(elegidas por un triaje anterior; se leen ya)")
    if aplazadas:
        print(f"  Ya triadas antes ....... {aplazadas}  "
              "(esperan a que ella las pida)")
    print(f"  Sin triar .............. {len(nuevas)}")
    print(f"Se leen a fondo .......... hasta {cuantas}"
          + (f"  ({len(grupos_rescate)} ya ocupadas)" if grupos_rescate else ""))

    if not nuevas or huecos <= 0:
        print()
        if grupos_rescate:
            # No se gasta la llamada del triaje: esta corrida solo termina lo
            # que la anterior dejó a medias.
            print(f"Se leen las {len(grupos_rescate)} que quedaron elegidas y sin leer.")
            print("No se tría nada nuevo: su turno ya estaba gastado y pagado.")
            return rescatadas, None, 0
        print("No hay ofertas nuevas que triar. Las pendientes ya tuvieron su turno")
        print("y siguen en la bandeja, a un clic de que el asistente las lea.")
        return [], None, 0

    filas = nuevas
    if len(filas) <= huecos:
        print("Caben todas: no se gasta la llamada del triaje.")
        return rescatadas + filas, None, 0

    # El cliente por parámetro, igual que en `Motor`, `Triador` y `Asistente`.
    # Aquí sirve para lo mismo: probar offline QUÉ CÓDIGO DE SALIDA devuelve
    # cada final, que es lo que decide si la bandeja dice «sin saldo» o dice
    # la verdad. Sin esto, esa decisión no se puede probar sin una API key.
    if cliente is None:
        try:
            cliente = ClienteAnthropic(libro=guardia.libro)
        except RuntimeError as exc:
            print()
            print(f"ERROR: {exc}", file=sys.stderr)
            return [], None, 1

    triador = Triador(
        # `huecos` y no `cuantas`: los rescates ya ocuparon parte de las
        # lecturas de esta corrida. Ver `triaje.rescatar_sin_leer`.
        almacen, cargar_preferencias(), cliente, huecos,
        presupuesto=guardia, esfuerzo=config.llm.triaje.esfuerzo,
        tope_lote=tope,
    )
    esfuerzo = config.llm.triaje.esfuerzo

    # SE ESTIMA SOBRE EL LOTE QUE SE VA A MANDAR, NO SOBRE `filas`.
    #
    # Aquí se cobraba `len(filas)`, que son ANUNCIOS y antes de agrupar: sobre
    # la base del 3 septiembre 2026, 330 anuncios contra las 273 vacantes que
    # la llamada iba a llevar de verdad, o sea ~$0,53 anunciados contra ~$0,44
    # reales. Caía del lado seguro y por eso no dolía. Con el tope de lote deja
    # de caer del lado seguro y pasa a ser absurdo: anunciaría 330 cuando se
    # mandan 60. `preparar_lote` es la misma función que usa `Triador.triar`.
    grupos, fuera = preparar_lote(filas, tope)
    en_la_llamada = len(grupos)
    if fuera:
        print(f"Vacantes en cola ......... {en_la_llamada + fuera}")
        print(f"  Se trían esta corrida .. {en_la_llamada}  (tope de lote: {tope})")
        print(f"  Quedan para la próxima . {fuera}  "
              "(no se marcan: conservan su turno entero)")

    _, salida_prevista, coste_previsto = triaje_coste_estimado(en_la_llamada, esfuerzo)
    print(f"Coste previsto ........... ~${coste_previsto:.4f}  "
          f"(~{salida_prevista:,} tokens de salida, esfuerzo {esfuerzo or 'por defecto'})")
    print()
    print(f"  [1/1] Comparando {en_la_llamada} vacantes en una sola llamada…",
          flush=True)
    resultado = triador.triar(filas)

    if not resultado.ok:
        # NO se cae hacia "clasificarlo todo": eso convertiría un fallo del
        # triaje en la factura que el triaje existe para evitar. Se para y se
        # dice, y las ofertas siguen pendientes.
        print()
        print(f"El triaje no se hizo: {resultado.error}")
        print("No se clasifica nada. Las ofertas siguen pendientes y no se ha")
        print("perdido ninguna. Con --sin-triaje se leen todas, pero mira antes")
        print("lo que eso cuesta con --dry-run.")
        return [], resultado, 3 if resultado.causa == "presupuesto" else 2

    # Un hueco compra la VACANTE entera: el representante y sus anuncios
    # hermanos, que heredarán su veredicto sin coste en el motor.
    elegidas = set(resultado.claves_con_hermanas(huecos, triador._grupos))
    print(f"Merecían lectura ......... {sum(1 for _, _, m, _ in resultado.items if m)}")
    print(f"Se van a leer ............ {len(elegidas)}")
    # LO PREVISTO Y LO REAL, JUNTOS Y EN TOKENS.
    #
    # Hasta hoy la corrida solo imprimía el coste, y los tokens del triaje no
    # se guardaban en ningún sitio: ni en `triajes`, que no tiene columnas para
    # ellos, ni en `uso_llm.json`, que solo agrega dólares por día. Por eso la
    # constante de salida por vacante llevaba tres sesiones desviándose sin que
    # nadie lo notara: no había con qué notarlo.
    #
    # Y SE DIVIDE ENTRE LAS VACANTES QUE FUERON EN LA LLAMADA, NO ENTRE
    # `filas`. `filas` son ANUNCIOS y antes de agrupar y de aplicar el tope;
    # `resultado.consideradas` es exactamente lo que se le mandó al modelo, que
    # es entre lo que hay que repartir su salida. Dividir mal aquí no da un
    # número feo: da una constante mal calibrada que se escribe en el código
    # como si estuviera medida — el mismo error, por cuarta vez.
    #
    # De esta línea sale el número con el que corregir `SALIDA_POR_OFERTA`.
    enviadas = max(1, resultado.consideradas)
    por_vacante = resultado.tokens_salida / enviadas
    estimado_por_vacante = salida_prevista / enviadas
    print(f"Coste del triaje ......... ${resultado.coste_usd:.4f}  "
          f"(previsto ~${coste_previsto:.4f})")
    print(f"Tokens ................... {resultado.tokens_entrada:,} entrada / "
          f"{resultado.tokens_salida:,} salida")
    print(f"Salida por vacante ....... {por_vacante:.0f} tokens  "
          f"(la estimación usa {estimado_por_vacante:.0f})")
    if resultado.nota:
        print()
        print(_envolver(resultado.nota))

    print()
    for clave, posicion, merece, por_que in resultado.items[:huecos]:
        marca = "->" if clave in elegidas else "  "
        print(f"  {marca} {posicion:>2}. {_recortar(por_que, ANCHO - 8)}")

    # TRES ESTADOS, NO DOS, Y NO SE PUEDEN DECIR IGUAL.
    #
    # Antes del tope de lote solo había dos finales para una oferta: leída, o
    # triada-y-aplazada. Ahora hay un tercero —la que el tope dejó fuera— y no
    # es lo mismo que el segundo: a esa no la ha mirado nadie, no tiene puesto
    # ni motivo escrito, y no ha gastado su turno. Decirle a la clienta que
    # «sigue en la bandeja con su puesto del triaje» sería mentira, y es
    # justo el tipo de mentira pequeña que la regla nº 1 persigue.
    triadas_sin_leer = resultado.consideradas - len(resultado.seleccionadas(huecos))
    if triadas_sin_leer:
        print()
        print(f"  {triadas_sin_leer} vacante(s) se quedan SIN LEER esta vez. No están")
        print( "  descartadas: siguen en la bandeja con su puesto del triaje, y se")
        print( "  pueden clasificar una a una desde ahí.")
    if resultado.fuera_del_tope:
        print()
        print(f"  Y {resultado.fuera_del_tope} vacante(s) no han entrado siquiera en el triaje: el")
        print( "  tope de lote las deja para la próxima corrida. No se ha marcado")
        print( "  ninguna, así que entran ENTERAS y con su turno intacto.")

    # Se conserva el orden que traía la consulta, no el del triaje: quien
    # ordena para ella es el asistente, al final, y con las ofertas ya leídas.
    return rescatadas + [f for f in filas if f["dedupe_key"] in elegidas], resultado, 0


def _construir_guardia(
    config: Any, es_manual: bool, args: Any = None
) -> GuardiaPresupuesto:
    """El libro vive junto a los otros contadores, en `data/`.

    `--tope-accion-usd` llega hasta aquí y NO es una perilla de usuario: lo
    pone «Ponme al día» de la bandeja al encadenar tres comandos, para que los
    tres compartan UN tope en vez de tener uno cada uno. Solo puede bajarlo
    —ver `GuardiaPresupuesto.tope_por_accion`—, así que ponerlo a mano no
    afloja nada.
    """
    return GuardiaPresupuesto(
        LibroDeGastos(ruta_del_libro(config)),
        config.llm,
        es_manual=es_manual,
        tope_accion=getattr(args, "tope_accion_usd", None) if args else None,
    )


def _tope_de_esta_accion(config: Any, args: Any = None) -> float:
    """Lo máximo que puede costar ESTA corrida, contando `--tope-accion-usd`.

    Es la misma cuenta que hace `GuardiaPresupuesto.tope_por_accion` —el flag
    solo puede BAJAR el tope del YAML, nunca subirlo— pero hace falta ANTES de
    construir el guardia, porque decide cuántas ofertas se le mandan al
    asistente. Sin esto, dentro de «Ponme al día» el ranking se planificaría
    contra los $0,60 enteros cuando la cadena solo le deja lo que sobró: el
    plan diría una cosa y el comando gastaría contra otra.
    """
    tope = float(getattr(config.llm, "tope_por_accion_usd", 0.0) or 0.0)
    flag = getattr(args, "tope_accion_usd", None) if args else None
    if flag is not None:
        tope = min(tope, float(flag)) if tope > 0 else float(flag)
    return tope


def _no_cabe_en_una_accion(guardia: GuardiaPresupuesto, coste: float) -> bool:
    """El freno por acción, con su mensaje. `True` = no se lanza.

    Es el espejo en consola de lo que hace `bandeja/server.py`: quien tiene
    que negarse es quien gasta, y los dos caminos gastan.
    """
    motivo = guardia.abrir_accion(coste)
    if motivo is None:
        return False
    _titulo("EL TOPE POR ACCIÓN LO IMPIDE")
    print(_envolver(motivo))
    return True


def comando_solo(args: argparse.Namespace) -> int:
    """Clasifica UNA oferta concreta, la que ella señale.

    Es lo que hay detrás del botón «que la lea el asistente» de la bandeja, y
    es la mitad del diseño del triaje, no un extra. El triaje aplaza la
    atención sobre doce de las veinte que entran al día, y cada oferta tiene un
    turno y uno solo: sin este comando, «aplazada» acabaría significando
    «descartada en silencio», que es justo lo que la capa entera existe para no
    hacer. Con él, aplazar es una decisión de ella y siempre reversible.

    Va aparte de `comando_clasificar` porque no comparte casi nada: no hay
    triaje que aplicar —ella ya decidió cuál—, no hay pendientes que buscar, y
    el número que se enseña es el de una llamada suelta, que cuesta distinto.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ERROR de configuración: {exc}", file=sys.stderr)
        return 1

    if not config.database_path.exists():
        print("No hay base de datos todavía.", file=sys.stderr)
        return 1

    try:
        fichas = cargar_fichas()
    except FichasError as exc:
        print(f"ERROR en config/cvs.yaml: {exc}", file=sys.stderr)
        return 1

    with Storage(config.database_path) as almacen:
        filas = almacen.vacante_pendiente_por_clave(args.solo)

        if not filas:
            # Los dos noes se distinguen, que es la regla de la casa hasta para
            # un mensaje de error: "no existe" es un fallo y "ya está hecha" no.
            if almacen.obtener_oferta_con_clasificacion(args.solo) is None:
                print(f"No hay ninguna oferta con la clave '{args.solo}'.",
                      file=sys.stderr)
                return 1
            print("Esa oferta ya está clasificada. No se vuelve a pagar por ella: "
                  "su veredicto está en la bandeja.")
            return 0

        plan = plan_de_una_lectura(almacen, fichas, args.solo)

        _titulo("QUE LA LEA EL ASISTENTE")
        print(f"Oferta ................... {_recortar(plan.titulo, 50)}")
        print(f"Empresa .................. {_recortar(plan.empresa, 50)}")
        if len(filas) > 1:
            print(f"Anuncios de esta vacante . {len(filas)}  "
                  "(se paga uno; los demás heredan su veredicto)")
        print(f"Llamadas a la API ........ 1")
        print(f"Coste estimado ........... ~${plan.coste_usd:.4f} USD")
        # SI YA SE INTENTÓ Y FALLÓ, SE DICE ANTES DE VOLVER A PAGAR. Es el
        # caso de una cuenta sin crédito o una API caída: la oferta conserva
        # su turno (ver `Storage.vacante_pendiente_por_clave`), pero repetir a
        # ciegas es comprar el mismo error otra vez.
        if plan.fallo_anterior:
            print()
            print("Un intento anterior no llegó a leerla:")
            print(_envolver(plan.fallo_anterior))
        print()
        print("Una lectura suelta cuesta más que una de corrida (~$0,011): las de")
        print("una corrida leen una caché de prompt que la PRIMERA escribe, y esta")
        print("es siempre la primera. No es un recargo, es lo que cuesta.")

        if args.dry_run:
            print()
            print("(--dry-run: no se ha llamado a nada)")
            return 0

        try:
            guardia = _construir_guardia(config, es_manual=args.manual, args=args)
        except LibroCorruptoError as exc:
            print(f"\nERROR en el libro de gastos: {exc}", file=sys.stderr)
            return 1

        if _no_cabe_en_una_accion(guardia, plan.coste_usd):
            return 3

        if not guardia.permite(plan.coste_usd):
            _titulo("PRESUPUESTO AGOTADO")
            print(guardia.estado().explicacion())
            print()
            print("La oferta sigue pendiente y en la bandeja. No se ha perdido.")
            if not args.manual:
                print()
                print("(Las corridas automáticas tienen su propia ración de "
                      f"${config.llm.racion_automatica_mensual_usd:.2f}; lo que "
                      "lanza ella va con `--manual`.)")
            return 3

        try:
            cliente = ClienteAnthropic(libro=guardia.libro)
        except RuntimeError as exc:
            print(f"\nERROR: {exc}", file=sys.stderr)
            return 1

        resumen = Motor(almacen, fichas, cliente, presupuesto=guardia).clasificar(filas)

        _imprimir_detalle(resumen)

        _titulo("COSTE")
        print(f"Esta lectura ............. ${resumen.coste_usd:.4f} USD")
        print(f"Disponible este mes ...... ${guardia.estado().disponible:.4f}")

        if resumen.se_detuvo_por_presupuesto:
            return 3
        return 2 if resumen.fallidas else 0


def _resumen(valores: list[float]) -> dict[str, float]:
    """Los percentiles que hacen falta para juzgar una constante."""
    orden = sorted(valores)
    n = len(orden)

    def p(fraccion: float) -> float:
        return orden[min(n - 1, int(n * fraccion))]

    return {
        "n": n, "min": orden[0], "p25": p(0.25),
        "mediana": statistics.median(orden), "p75": p(0.75),
        "p90": p(0.90), "max": orden[-1],
        "media": statistics.mean(orden),
        "desviacion": statistics.pstdev(orden) if n > 1 else 0.0,
    }


def _correlacion(xs: list[float], ys: list[float]) -> float | None:
    """Pearson. `None` si no hay varianza en alguno de los dos."""
    if len(xs) < 3:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = (
        sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)
    ) ** 0.5
    return num / den if den else None


def comando_lecturas(args: argparse.Namespace) -> int:
    """Previsto contra medido en las LECTURAS. Hermano de `triajes`.

    POR QUÉ HACE FALTA. `triaje_corridas` existe porque `SALIDA_POR_OFERTA`
    llevaba tres sesiones desviándose y no había dónde notarlo. Las lecturas
    tenían el mismo agujero con una constante más nueva y peor medida:
    `TOKENS_SALIDA_ENTERA = 700`, sacada de 28 lecturas de un solo día — y
    esas 28 eran las que habían vuelto con «necesita tu criterio», o sea la
    parte difícil de la bandeja.

    No hace falta una tabla nueva: `clasificaciones` ya guarda tokens y coste
    por fila. Lo que faltaba era `caracteres_descripcion`, sin la cual no se
    sabe qué constante le tocaba a cada fila. Ver *Y la mitad de la bandeja
    estaba juzgada a ciegas* en el README.

    No llama a nada y no cuesta nada.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ERROR de configuración: {exc}", file=sys.stderr)
        return 1

    if not config.database_path.exists():
        print("No hay base de datos todavía.", file=sys.stderr)
        return 1

    with Storage(config.database_path) as almacen:
        filas = almacen.lecturas_medidas()

    _titulo("LECTURAS: PREVISTO CONTRA MEDIDO")
    if not filas:
        print("Todavía no hay ninguna lectura con tokens medidos.")
        print()
        print("Las heredadas no cuentan: tienen los tokens a cero por diseño,")
        print("porque nadie las leyó. Ver `clasificaciones.heredada_de`.")
        return 0

    constantes = (
        (VIA_RECORTE, "el recorte de 500", TOKENS_SALIDA_RECORTE),
        (VIA_ENTERA, "el anuncio entero", TOKENS_SALIDA_ENTERA),
    )

    desviaciones: list[tuple[str, float, int]] = []
    for via, etiqueta, constante in constantes:
        grupo = [f for f in filas if f["descripcion_via"] == via]
        print()
        print(f"Con {etiqueta}  —  la estimación usa {constante} tokens de salida")
        print("-" * 74)
        if not grupo:
            print("  (todavía no hay ninguna lectura de este tipo)")
            continue

        r = _resumen([f["tokens_salida"] for f in grupo])
        desvio = (constante / r["media"] - 1) * 100
        desviaciones.append((etiqueta, desvio, r["n"]))

        print(f"  Lecturas medidas ....... {r['n']}")
        print(f"  Tokens de salida ....... mín {r['min']:.0f} · p25 {r['p25']:.0f} · "
              f"mediana {r['mediana']:.0f} · p75 {r['p75']:.0f} · "
              f"p90 {r['p90']:.0f} · máx {r['max']:.0f}")
        print(f"  Media / desviación ..... {r['media']:.1f} / {r['desviacion']:.1f}")
        print(f"  La constante se pasa ... {desvio:+.1f}%  "
              f"(se queda corta en {sum(1 for f in grupo if f['tokens_salida'] > constante)} "
              f"de {r['n']})")

        entrada = _resumen([f["tokens_entrada"] for f in grupo])
        coste = sum(f["coste_usd"] for f in grupo)
        print(f"  Entrada media .......... {entrada['media']:,.0f} tokens")
        print(f"  Coste de esas lecturas . ${coste:.4f} USD  "
              f"(${coste / r['n']:.4f} por lectura)")

    # QUÉ MUEVE LA SALIDA, que es lo que explica por qué cada muestra da un
    # número distinto. Se imprime porque sin esto la tabla de arriba invita a
    # corregir la constante con la primera muestra que llegue.
    enteras = [f for f in filas if f["descripcion_via"] == VIA_ENTERA]
    if len(enteras) >= 3:
        salidas = [float(f["tokens_salida"]) for f in enteras]
        con_confianza = [f for f in enteras if f["confianza"] is not None]
        print()
        print("Qué mueve la salida")
        print("-" * 74)
        r_largo = _correlacion(
            [float(f["caracteres_descripcion"] or 0) for f in enteras], salidas
        )
        if r_largo is not None:
            print(f"  Con los caracteres del anuncio ... r = {r_largo:+.2f}")
        if len(con_confianza) >= 3:
            r_conf = _correlacion(
                [float(f["confianza"]) for f in con_confianza],
                [float(f["tokens_salida"]) for f in con_confianza],
            )
            if r_conf is not None:
                print(f"  Con la CONFIANZA del veredicto ... r = {r_conf:+.2f}")
        print()
        print(_envolver(
            "Si el segundo es fuerte y negativo, lo que hace escribir al modelo "
            "no es la longitud del anuncio sino lo difícil que es la decisión. "
            "Eso importa para elegir la constante: una muestra de ofertas "
            "dudosas da un número alto y una de ofertas claras lo da bajo, y "
            "ninguna de las dos representa un día normal."
        ))

    print()
    _titulo("VEREDICTO")
    for etiqueta, desvio, n in desviaciones:
        estado = "fuera del 20%" if abs(desvio) > 20 else "dentro del 20%"
        aviso = "  <- n pequeño" if n < 20 else ""
        print(f"  {etiqueta:<20} {desvio:+7.1f}%   {estado}  (n={n}){aviso}")

    print()
    if any(abs(d) > 20 for _, d, n in desviaciones if n >= 20):
        print(_envolver(
            "Alguna constante se ha ido de más del 20% con muestra suficiente. "
            "Antes de tocarla, mira de dónde salen esas lecturas: si vienen "
            "todas del mismo tipo de oferta, el problema es la muestra y no la "
            "constante. Las constantes viven en clasificador/cliente_llm.py, y "
            "cambiarlas obliga a anotar en el README la fecha y las corridas "
            "que lo justifican."
        ))
    else:
        print("Las constantes aguantan y no hay que tocar nada.")
    return 0


def comando_releer(args: argparse.Namespace) -> int:
    """Rehace los veredictos que se emitieron con el recorte de 500 caracteres.

    ES LA CAPACIDAD QUE NO EXISTÍA, y su ausencia costó dinero: las 28 de la
    sesión del 3 septiembre 2026 hubo que releerlas con un script suelto, y
    ese script llamó a la API sin pasar por el guardia — de ahí el agujero de
    $0,1587 en el libro de gastos, y de ahí también que 10 filas heredadas se
    quedaran con el veredicto viejo mientras su padre tenía el nuevo.

    VA APARTE DE `comando_clasificar` POR UNA RAZÓN Y ES TODO EL DISEÑO: aquí
    no hay triaje. El triaje decide a qué ofertas se les lee la descripción
    entera, y estas YA se leyeron: esa decisión se tomó y se pagó. Pasarlas
    otra vez por él, además, no funcionaría —`claves_ya_triadas` las excluye,
    medido: de 295 pendientes solo 1 llega al motor— y devolverles el turno
    resucitaría 33 vacantes en una cola que se drena a 35 por corrida.

    Y NO SE ESCRIBE NADA EN `triajes`. Ni una fila, ni siquiera para decir que
    se releyeron. Su turno ya se gastó cuando se las leyó la primera vez; los
    tres estados de una oferta (leída / aplazada / fuera del tope) siguen
    siendo tres.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ERROR de configuración: {exc}", file=sys.stderr)
        return 1

    if not config.database_path.exists():
        print("No hay base de datos todavía.", file=sys.stderr)
        return 1

    try:
        fichas = cargar_fichas()
    except FichasError as exc:
        print(f"ERROR en config/cvs.yaml: {exc}", file=sys.stderr)
        return 1

    with Storage(config.database_path) as almacen:
        # PRIMERO LO QUE ES GRATIS. Un hermano que discrepa de su padre se
        # arregla copiando, y hacerlo antes de calcular el plan quita de la
        # lista las vacantes cuyo padre ya había leído el anuncio entero —
        # tres, sobre esta base—. Pagar por releerlas sería pagar por algo que
        # una regla resuelve.
        reparadas = almacen.propagar_veredictos_heredados()

        plan = plan_de_relectura(
            almacen, fichas,
            tope_por_accion_usd=config.llm.tope_por_accion_usd,
            limite=args.limite,
        )

        _titulo("RELEER LO QUE SE JUZGÓ A CIEGAS")
        if reparadas:
            print(f"Hermanos puestos al día .. {reparadas}  "
                  "(copiado del representante, coste cero)")
        print(f"Veredictos con el recorte  {plan.anuncios} anuncio(s)")
        print(f"  Vacantes que se pagan .. {plan.vacantes}")
        print(f"En esta acción ........... {plan.esta_accion}")
        if plan.fuera_de_esta_accion:
            print(f"  Para la siguiente ...... {plan.fuera_de_esta_accion}  "
                  "(siguen en la bandeja con su veredicto viejo)")
        print(f"Llamadas a la API ........ {plan.esta_accion}")
        print(f"Coste estimado ........... ~${plan.coste_usd:.4f} USD")
        print()
        print(_envolver(plan.nota))

        if not plan.hay_trabajo:
            return 0

        if args.dry_run:
            print()
            print("(--dry-run: no se ha llamado a nada)")
            return 0

        try:
            guardia = _construir_guardia(config, es_manual=args.manual, args=args)
        except LibroCorruptoError as exc:
            print(f"\nERROR en el libro de gastos: {exc}", file=sys.stderr)
            return 1

        # El plan ya trocea por debajo del tope, así que llegar aquí y no
        # caber solo puede pasar si una sola vacante se pasa ella sola. Se
        # comprueba igualmente: quien tiene que negarse es quien gasta.
        if _no_cabe_en_una_accion(guardia, plan.coste_usd):
            return 3

        if not guardia.permite(plan.coste_usd):
            _titulo("PRESUPUESTO AGOTADO")
            print(guardia.estado().explicacion())
            print()
            print("No se ha perdido nada: los veredictos viejos siguen en la")
            print("bandeja y siguen marcados como juzgados con el recorte.")
            return 3

        try:
            cliente = ClienteAnthropic(libro=guardia.libro)
        except RuntimeError as exc:
            print(f"\nERROR: {exc}", file=sys.stderr)
            return 1

        # EXACTAMENTE las que el plan costeó, ni una más. Volver a consultar
        # aquí abriría la puerta a leer un conjunto distinto del que se
        # confirmó — y el importe enseñado dejaría de ser el cobrado.
        filas = almacen.ofertas_por_clave(plan.claves)

        def _progreso(hecho: int, total: int, titulo: str) -> None:
            print(f"[{hecho}/{total}] {_recortar(titulo, 60)}")

        print()
        resumen = Motor(almacen, fichas, cliente, presupuesto=guardia).clasificar(
            filas, avisar=_progreso
        )

        _imprimir_detalle(resumen)
        _imprimir_resumen(resumen)

        restantes = almacen.contar_juzgadas_a_ciegas()
        print()
        print(f"Quedan juzgados a ciegas . {restantes} anuncio(s)")
        if restantes:
            print("Vuelve a lanzar `python -m clasificador releer` para el resto.")

        if resumen.se_detuvo_por_presupuesto:
            return 3
        return 2 if resumen.fallidas else 0


def comando_presupuesto(args: argparse.Namespace) -> int:
    """Estado del gasto del mes. No llama a nada y no cuesta nada.

    Es el equivalente de `python -m recolector quota` para el LLM, y existe
    por la misma razón: poder responder «¿cuánto llevo gastado?» sin lanzar
    una corrida para averiguarlo.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ERROR de configuración: {exc}", file=sys.stderr)
        return 1

    try:
        automatica = _construir_guardia(config, es_manual=False)
        suya = _construir_guardia(config, es_manual=True)
    except LibroCorruptoError as exc:
        print(f"ERROR en el libro de gastos: {exc}", file=sys.stderr)
        return 1

    guardia = automatica
    estado = automatica.estado()

    _titulo("PRESUPUESTO DEL LLM")
    print(estado.explicacion())

    print()
    print("  Si la corrida la lanza ella (`--manual`), el mes no la frena:")
    print(f"    Referencia del mes ... ${suya.estado().techo_mensual:.2f}")
    print(f"    Disponible ........... ${suya.estado().disponible:.4f}")
    print(f"    La frena el tope por acción, ${config.llm.tope_por_accion_usd:.2f}.")

    print()
    print(f"Modelo ................. {MODELO}")
    print(f"Precios ................ ${PRECIO_ENTRADA_USD_POR_MILLON:.2f} entrada / "
          f"${PRECIO_SALIDA_USD_POR_MILLON:.2f} salida por millón de tokens "
          f"(verificados {PRECIOS_ACTUALIZADOS})")
    print(f"Al agotarse el mes ..... {config.llm.al_agotarse}"
          f"{'' if config.llm.detiene_al_agotarse else '  (a las automáticas las para su ración igual)'}")
    print(f"Tope por acción ........ ${config.llm.tope_por_accion_usd:.2f}"
          "   <- el freno que se le promete")
    print(f"Gastado desde el inicio  ${guardia.libro.gastado_total():.4f}")
    print(f"Libro .................. {guardia.libro.path}")
    print()

    if estado.agotado:
        print("AGOTADO. Las corridas automáticas no van a gastar más este mes.")
        return 3
    if estado.conviene_avisar:
        print(f"AVISO: llevas el {estado.porcentaje_usado:.0f}% del techo del mes.")
    return 0


def comando_ranking(args: argparse.Namespace) -> int:
    """El asistente: ordena lo ya clasificado y dice por dónde empezar.

    UNA llamada, no una por oferta. Ver clasificador/asistente.py.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ERROR de configuración: {exc}", file=sys.stderr)
        return 1

    if not config.database_path.exists():
        print("No hay base de datos todavía.", file=sys.stderr)
        return 1

    try:
        preferencias = cargar_preferencias()
    except FichasError as exc:
        print(f"ERROR en config/cvs.yaml: {exc}", file=sys.stderr)
        return 1

    with Storage(config.database_path) as almacen:
        # Si se pide, se enseña el último ranking guardado sin gastar nada.
        if args.ultimo:
            return _imprimir_ultimo_ranking(almacen)

        # UN SOLO ESTIMADOR Y UN SOLO SITIO QUE DECIDE EL CORTE, que son las
        # reglas nº 9 y nº 8. Aquí se construía un `Asistente` aparte para
        # preguntarle el coste mientras la bandeja lo pedía por
        # `plan_de_ranking`; y desde el 4 septiembre 2026 hay una segunda cosa
        # que no puede vivir en dos sitios, que es CUÁNTAS se ordenan.
        #
        # `--limite` explícito manda sobre el techo del YAML: es el comando de
        # depuración y quien lo escribe sabe lo que pide. Sin él, el techo lo
        # ponen `llm.max_ofertas_por_ranking` y lo que quede del tope.
        disponibles = almacen.contar_ofertas_para_ranking()
        if args.limite:
            plan = plan_de_ranking(
                almacen, preferencias,
                limite=args.limite,
                esfuerzo=config.llm.esfuerzo_ranking,
            )
        else:
            plan = plan_de_ranking_que_quepa(
                almacen, preferencias,
                esfuerzo=config.llm.esfuerzo_ranking,
                tope_restante=_tope_de_esta_accion(config, args),
                techo_configurado=config.llm.max_ofertas_por_ranking,
            )

        filas = almacen.ofertas_para_ranking(limite=plan.limite or args.limite)

        if not filas:
            _titulo("NADA QUE ORDENAR")
            print("No hay ofertas clasificadas como relevantes todavía.")
            print("Clasifica primero:  python -m clasificador")
            return 0

        coste = plan.coste_usd

        _titulo("EL ASISTENTE")
        print(f"Ofertas a ordenar ........ {len(filas)}")
        # EL RECORTE SE DICE, SIEMPRE. Un orden de 20 sobre 47 impreso igual
        # que uno de 47 sobre 47 esconde 27 ofertas dentro de la pieza que
        # existe para no esconder ninguna.
        if len(filas) < disponibles:
            print(f"Sin revisar tienes ....... {disponibles}"
                  f"   <- este orden NO mira {disponibles - len(filas)}")
            print(f"                            (las más antiguas; siguen en la bandeja)")
        print(f"Llamadas a la API ........ 1   (una por lote, no una por oferta)")
        print(f"Criterios declarados ..... {len(preferencias.criterios)}")
        print(f"Destaca arriba ........... {preferencias.cuantas_destacar}")
        print(f"Esfuerzo ................. {config.llm.esfuerzo_ranking}")
        print(f"Coste estimado ........... ~${coste:.4f} USD")

        if args.dry_run:
            print()
            print("(--dry-run: no se ha llamado a nada)")
            print()
            for i, fila in enumerate(filas[:15], 1):
                print(f"  {i:>2}. {_recortar(fila['puesto_canonico'] or fila['title'], 44):<46} "
                      f"{_recortar(fila['empresa_normalizada'] or fila['company'], 24)}")
            if len(filas) > 15:
                print(f"  ... y {len(filas) - 15} más")
            return 0

        try:
            guardia = _construir_guardia(config, es_manual=args.manual, args=args)
        except LibroCorruptoError as exc:
            print(f"\nERROR en el libro de gastos: {exc}", file=sys.stderr)
            return 1

        if _no_cabe_en_una_accion(guardia, coste):
            return 3

        if not guardia.permite(coste):
            _titulo("PRESUPUESTO AGOTADO")
            print(guardia.estado().explicacion())
            print()
            print(f"El ranking costaría ~${coste:.4f} y no cabe. No se ha llamado a nada.")
            return 3

        try:
            cliente = ClienteAnthropic(libro=guardia.libro)
        except RuntimeError as exc:
            print(f"\nERROR: {exc}", file=sys.stderr)
            return 1

        asistente = Asistente(
            cliente=cliente,
            preferencias=preferencias,
            esfuerzo=config.llm.esfuerzo_ranking,
        )
        resultado = asistente.ordenar(filas)

        # El gasto ya está apuntado: lo hace `ClienteAnthropic` en cuanto la
        # llamada vuelve, pase lo que pase con la respuesta. Aquí estaba la
        # tercera copia de esa regla. Ver `cliente_llm.py`.

        if not resultado.ok:
            print(f"\nERROR: {resultado.error}", file=sys.stderr)
            print(f"(la llamada costó ${resultado.coste_usd:.4f} y está apuntada)",
                  file=sys.stderr)
            return 2

        ranking_id = uuid.uuid4().hex[:12]
        almacen.guardar_ranking(
            {
                "id": ranking_id,
                "creado_at": datetime.now(timezone.utc).isoformat(),
                "modelo": resultado.modelo,
                "lectura": resultado.lectura,
                "patrones": patrones_a_json(resultado.patrones),
                "ofertas_consideradas": resultado.consideradas,
                # Cuántas HABÍA sin revisar, para que la bandeja pueda seguir
                # diciendo «20 de 47» días después de la corrida.
                "ofertas_disponibles": disponibles,
                "tokens_entrada": resultado.tokens_entrada,
                "tokens_salida": resultado.tokens_salida,
                "coste_usd": resultado.coste_usd,
            },
            resultado.items,
        )

        _imprimir_ultimo_ranking(almacen)

        _titulo("COSTE")
        print(f"Tokens ................... {resultado.tokens_entrada:,} entrada / "
              f"{resultado.tokens_salida:,} salida")
        print(f"Esta llamada ............. ${resultado.coste_usd:.4f} USD")
        print(f"Disponible este mes ...... ${guardia.estado().disponible:.4f}")

    return 0


def _imprimir_ultimo_ranking(almacen: Storage) -> int:
    cabecera = almacen.ultimo_ranking()
    if cabecera is None:
        print("No hay ningún ranking guardado todavía.")
        return 0

    _titulo("POR DÓNDE EMPEZAR")
    print(cabecera["lectura"])

    patrones = patrones_desde_json(cabecera["patrones"])
    if patrones:
        print()
        for p in patrones:
            print(f"  · {p}")

    marca = {"postular_ya": "->", "revisar": "  ", "descartar": " x"}

    print()
    for item in almacen.items_de_ranking(cabecera["id"]):
        flecha = marca.get(item["accion"], "  ")
        titulo = item["puesto_canonico"] or item["title"]
        empresa = item["empresa_normalizada"] or item["company"]
        print(f"{flecha} {item['posicion']:>2}. {_recortar(titulo, 44):<46} "
              f"{_recortar(empresa, 24)}")
        print(f"      {_recortar(item['por_que'], ANCHO - 8)}")

    print()
    print(f"(ranking {cabecera['id']} · {cabecera['ofertas_consideradas']} ofertas · "
          f"${cabecera['coste_usd']:.4f})")
    return 0


def comando_grupos(args: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ERROR de configuración: {exc}", file=sys.stderr)
        return 1

    if not config.database_path.exists():
        print("No hay base de datos todavía.", file=sys.stderr)
        return 1

    with Storage(config.database_path) as almacen:
        return _imprimir_grupos(almacen)


def comando_triajes(args: argparse.Namespace) -> int:
    """El historial de llamadas del triaje, previsto contra medido.

    POR QUÉ ES UN COMANDO Y NO UNA LÍNEA MÁS DE LA CORRIDA. La corrida ya
    imprime su propia salida por vacante, pero eso se lee una vez y se pierde
    con el scrollback: fue exactamente lo que pasó el 3 septiembre 2026, con
    una corrida real de 60 vacantes cuyo número hubo que reconstruir restando
    el coste de las lecturas al gasto del día.

    Lo que corrige la constante no es un dato, son VARIOS a tamaños de lote
    distintos — el ~50 de `SALIDA_POR_OFERTA` se sostiene hoy sobre lotes de
    21, 25 y 60. Esta tabla es la que deja verlos juntos.

    No llama a nada y no cuesta nada.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ERROR de configuración: {exc}", file=sys.stderr)
        return 1

    if not config.database_path.exists():
        print("No hay base de datos todavía.", file=sys.stderr)
        return 1

    with Storage(config.database_path) as almacen:
        corridas = almacen.corridas_de_triaje()

    _titulo("CORRIDAS DEL TRIAJE")
    if not corridas:
        print("Todavía no hay ninguna llamada de triaje registrada.")
        print()
        print("Las corridas anteriores al 3 septiembre 2026 no se apuntaron: la")
        print("tabla no existía, y esa es justo la razón de que la estimación")
        print("llevara tres sesiones desviándose sin que nadie se enterara.")
        return 0

    print(f"{'fecha':<11} {'vacantes':>8} {'salida/vac':>11} "
          f"{'estimado':>9} {'desvío':>8} {'coste':>9} {'previsto':>9}")
    print("-" * 74)
    for c in corridas:
        vacantes = max(1, c["vacantes"])
        medido = c["tokens_salida"] / vacantes
        previsto = c["salida_prevista"] / vacantes
        desvio = (medido / previsto - 1) * 100 if previsto else 0.0
        print(
            f"{c['triado_at'][:10]:<11} {c['vacantes']:>8} "
            f"{medido:>10.1f}  {previsto:>8.0f} {desvio:>+7.1f}% "
            f"${c['coste_usd']:>8.4f} ${c['coste_previsto']:>8.4f}"
        )

    print()
    # LA CONCLUSIÓN, NO SOLO LA TABLA. El número que hay que mirar es la media
    # ponderada por vacantes: un lote de 60 dice más sobre la constante que uno
    # de 9, y promediar los porcentajes a secas les daría el mismo peso.
    vacantes = sum(c["vacantes"] for c in corridas)
    salida = sum(c["tokens_salida"] for c in corridas)
    if vacantes:
        medio = salida / vacantes
        usa = _salida_por_oferta(config.llm.triaje.esfuerzo)
        desvio = (medio / usa - 1) * 100
        print(f"Salida media por vacante . {medio:.1f} tokens sobre "
              f"{vacantes} vacantes en {len(corridas)} llamada(s)")
        print(f"La estimación usa ........ {usa} tokens  ({desvio:+.1f}%)")
        print()
        if abs(desvio) > 20:
            print("SE HA DESVIADO MÁS DE UN 20%. Toca corregir SALIDA_POR_OFERTA en")
            print("clasificador/triaje.py, y anotar en el README la fecha y las")
            print("corridas que lo justifican.")
        else:
            print("Dentro del 20%: la constante aguanta y no hay que tocar nada.")
    return 0

# ---------------------------------------------------------------------------
#  Punto de entrada
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m clasificador",
        description="Capa 2: decide si una oferta es relevante y qué CV le corresponde.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Enseña el plan y el coste estimado, y sale sin llamar a la API.",
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=None,
        help="Clasifica solo las N primeras ofertas pendientes.",
    )
    parser.add_argument(
        "--reclasificar",
        action="store_true",
        help="Incluye las clasificadas con una versión anterior de las fichas.",
    )
    parser.add_argument(
        "--reintentar",
        action="store_true",
        help="Reintenta las que quedaron en 'sin clasificar'.",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help=(
            "Marca la corrida como lanzada por la clienta: no la frena la "
            "ración mensual de las automáticas. La sigue frenando el tope por "
            "acción, que es el que se le promete."
        ),
    )
    parser.add_argument(
        "--tope-accion-usd",
        dest="tope_accion_usd",
        type=float,
        default=None,
        help=(
            "Baja el tope de ESTA corrida porque es un TRAMO de una acción "
            "mayor. Lo pone «Ponme al día», que encadena tres comandos bajo un "
            "solo tope. Solo puede bajarlo, nunca subirlo."
        ),
    )
    parser.add_argument(
        "--solo",
        metavar="CLAVE",
        default=None,
        help=(
            "Clasifica SOLO la oferta con esa clave de deduplicación, ahora y "
            "sin triaje. Es lo que hay detrás del botón «que la lea el "
            "asistente» de la bandeja: lo que convierte una oferta aplazada en "
            "una decisión de ella. Se lleva consigo los anuncios hermanos de la "
            "misma vacante, que heredan el veredicto sin coste."
        ),
    )
    parser.add_argument(
        "--sin-triaje",
        dest="sin_triaje",
        action="store_true",
        help=(
            "Salta el triaje y lee TODAS las pendientes. Mira antes lo que "
            "cuesta con --dry-run: el triaje existe justo para que esa cifra "
            "no la decida el mercado."
        ),
    )

    subparsers = parser.add_subparsers(dest="comando")
    subparsers.add_parser("grupos", help="Informe de vacantes repetidas. No gasta nada.")
    subparsers.add_parser(
        "triajes",
        help="Historial de llamadas del triaje: previsto contra medido. No gasta nada.",
    )
    subparsers.add_parser(
        "lecturas",
        help="Las LECTURAS: previsto contra medido. Hermano de `triajes`. No gasta nada.",
    )
    # `presupuesto` NO declara su propio `--manual`, aunque sería lo natural.
    #
    # argparse escribe el valor por defecto de un subcomando SIEMPRE, así que
    # un `--manual` aquí pisaría el del parser principal y
    # `clasificador --manual presupuesto` habría enseñado el saldo automático
    # sin decir una palabra. Un flag que se ignora en silencio en un comando
    # que informa de dinero es exactamente el tipo de fallo que este proyecto
    # no se permite. En vez de eso, el comando enseña las DOS vistas siempre.
    subparsers.add_parser(
        "presupuesto", help="Cuánto llevas gastado este mes. No gasta nada."
    )

    releer_parser = subparsers.add_parser(
        "releer",
        help=(
            "Rehace los veredictos que se emitieron con el recorte de 500 "
            "caracteres teniendo hoy el anuncio entero. Se salta el triaje."
        ),
    )
    releer_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Enseña qué se releería y lo que cuesta, y sale sin llamar a nada.",
    )
    releer_parser.add_argument(
        "--limite",
        type=int,
        default=None,
        help="Relee solo las N primeras VACANTES (no anuncios).",
    )
    releer_parser.add_argument(
        "--tope-accion-usd",
        dest="tope_accion_usd",
        type=float,
        default=None,
        help=(
            "Baja el tope de ESTA corrida porque es un TRAMO de una acción "
            "mayor. Lo pone «Ponme al día», que encadena tres comandos bajo un "
            "solo tope. Solo puede bajarlo, nunca subirlo."
        ),
    )
    releer_parser.add_argument(
        "--manual",
        action="store_true",
        help="Corrida lanzada por ella: no la frena la ración de las automáticas.",
    )

    ranking_parser = subparsers.add_parser(
        "ranking",
        help="El asistente: ordena lo clasificado y dice por dónde empezar.",
    )
    ranking_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Enseña el plan y el coste, y sale sin llamar a la API.",
    )
    ranking_parser.add_argument(
        "--ultimo",
        action="store_true",
        help="Enseña el último ranking guardado. No llama a nada, no cuesta nada.",
    )
    ranking_parser.add_argument(
        "--limite",
        type=int,
        default=None,
        help="Ordena solo las N más recientes.",
    )
    ranking_parser.add_argument(
        "--tope-accion-usd",
        dest="tope_accion_usd",
        type=float,
        default=None,
        help=(
            "Baja el tope de ESTA corrida porque es un TRAMO de una acción "
            "mayor. Lo pone «Ponme al día», que encadena tres comandos bajo un "
            "solo tope. Solo puede bajarlo, nunca subirlo."
        ),
    )
    ranking_parser.add_argument(
        "--manual",
        action="store_true",
        help="Corrida lanzada por ella: no la frena la ración de las automáticas.",
    )

    # El triaje no aplica al ranking (ordena lo YA clasificado), pero el
    # atributo tiene que existir: `comando_clasificar` lo consulta siempre.
    parser.set_defaults(sin_triaje=False)

    return parser


def main(argv: list[str] | None = None) -> int:
    _prepare_console()
    args = build_parser().parse_args(argv)

    if args.comando == "triajes":
        return comando_triajes(args)
    if args.comando == "lecturas":
        return comando_lecturas(args)
    if args.comando == "grupos":
        return comando_grupos(args)
    if args.comando == "presupuesto":
        return comando_presupuesto(args)
    if args.comando == "ranking":
        return comando_ranking(args)
    if args.comando == "releer":
        return comando_releer(args)
    if args.solo:
        return comando_solo(args)
    return comando_clasificar(args)
