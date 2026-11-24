"""
Las corridas largas, lanzadas desde la bandeja.

EL PROBLEMA
-----------
Una corrida del recolector tarda ~90 segundos y una de clasificación cerca de
dos minutos. Un `POST` que devuelva al terminar no sirve: el navegador se
rinde, y aunque no lo hiciera, ella se queda mirando una rueda sin saber si va
por la tercera oferta o si se colgó. Así que se lanza en segundo plano, se
guarda el estado del trabajo, y la interfaz pregunta cómo va.

POR QUÉ UN SUBPROCESO Y NO UNA LLAMADA A LA FUNCIÓN
---------------------------------------------------
Los comandos de consola —`comando_clasificar`, `command_run`— imprimen y
devuelven un código de salida. No están hechos para devolver datos, y
convertirlos en funciones que sí lo hagan es reescribir lo que ya funciona.
Lanzarlos como proceso aparte tiene cuatro ventajas que se pagan solas:

  1. **Los códigos de salida ya significan algo.** `3` es «no había saldo», que
     NO es un fallo: es la regla de la casa —lo que no se hizo por falta de
     dinero se queda pendiente— y la interfaz tiene que decirlo distinto de
     «se rompió». Ver `ESTADOS_POR_CODIGO`.
  2. **El progreso viene gratis.** El recolector imprime `[ 12/46]` por
     consulta y el clasificador `[3/8]` por vacante. Se leen esas líneas y ya
     hay barra, sin inventar un canal de eventos.
  3. **Aislamiento.** Si el recolector revienta se lleva su proceso, no el
     servidor que ella está mirando.
  4. **Cancelar es matar un proceso**, y aquí eso es seguro: el gasto se apunta
     llamada a llamada y cada clasificación se guarda en cuanto se tiene, así
     que morir a mitad deja el trabajo hecho contado y el resto PENDIENTE, que
     es exactamente lo que pasa al agotarse el presupuesto.

UNO CADA VEZ, Y NO ES POR PULCRITUD
-----------------------------------
`data/uso_llm.json` se lee, se modifica y se escribe entero, sin ningún
cerrojo. Dos corridas a la vez se pisan los apuntes: la segunda escribe encima
de lo que apuntó la primera y **ese dinero desaparece de la cuenta del mes**,
que es la única cuenta que impide pasarse del techo. Con un comando de consola
nunca pasó porque nadie lanza dos a mano; con botones, un doble clic basta.

Por eso el registro admite UN trabajo en curso y rechaza el segundo. Vale para
todas las acciones, no solo las que gastan dinero: el recolector escribe en la
misma base SQLite que el clasificador lee.

CÓMO SE PRUEBA SIN LANZAR NADA
------------------------------
El lanzador entra por parámetro, igual que `Motor` recibe el cliente del LLM.
Los tests le pasan uno falso que devuelve las líneas que quieran y un código de
salida elegido, así que se prueba el camino entero —estados, progreso, el
rechazo del segundo trabajo, la cancelación— sin abrir un proceso, sin red y
sin gastar un céntimo.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from recolector.registro import log

# Raíz del proyecto: `bandeja/trabajos.py` -> `bandeja/` -> aquí.
RAIZ = Path(__file__).resolve().parent.parent

# Lanzar un hijo SIN abrirle una consola. Solo existe en Windows; en otro
# sistema vale 0, que es «sin banderas» y no cambia nada. Ver el comentario en
# `lanzador_de_procesos`.
SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Cuántas líneas de salida se guardan por trabajo. Es una cola circular, así
# que lo que sobra se cae POR ARRIBA — y eso resultó importar.
#
# ESTABA EN 400 Y SE COMÍA LO ÚNICO QUE ELLA NECESITA (medido el 9 septiembre
# 2026, abriendo «Ver la salida» de una corrida real). La cuenta de «~120
# líneas» solo contaba la corrida; el informe de duplicados que va detrás
# —«MISMA VACANTE, VARIOS ANUNCIOS»— tenía 290 líneas él solo, porque crece
# con las vacantes agrupadas de TODA la base (32 aquel día, y sube). Total
# ~450, con este resultado:
#
#     lo que sobrevivía ... el final del informe de duplicados
#     lo que se caía ...... los veredictos, el RESUMEN y el COSTE
#
# O sea que el registro guardaba el apéndice y tiraba las conclusiones, justo
# en la pantalla a la que se va cuando algo ha ido mal.
#
# 1.500 cubre esa corrida con tres veces de margen y sigue acotando la
# memoria. No se sube más porque estas líneas viajan ENTERAS en cada tic del
# seguimiento (cada 1,2 s mientras corre): a 80 caracteres por línea son
# ~120 KB por tic, que en local no se nota y en un número mayor empezaría a
# notarse.
MAX_LINEAS = 1500

# El progreso que imprimen los dos comandos: `[ 12/46]` y `[3/8]`.
PATRON_PROGRESO = re.compile(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]")


class TrabajoEnCursoError(Exception):
    """Ya hay uno corriendo. No se encolan: se rechaza y se dice cuál es."""

    def __init__(self, en_curso: "Trabajo") -> None:
        self.en_curso = en_curso
        super().__init__(
            f"Ya hay un trabajo en curso: {en_curso.etiqueta}. "
            "Espera a que termine o cancélalo."
        )


# Qué significa cada código de salida. Los comandos los comparten a propósito
# (ver la cabecera de `clasificador/cli.py`), y aquí es donde esa decisión paga:
# «no había saldo» tiene que llegar a la pantalla como algo que NO es un fallo.
ESTADOS_POR_CODIGO = {
    0: "terminado",
    2: "con_errores",
    3: "sin_saldo",
}


@dataclass(frozen=True)
class Paso:
    """Un tramo de una acción encadenada: qué comando y cómo se llama.

    `argv` ya lleva dentro `--tope-accion-usd` cuando el tramo gasta: quien lo
    calcula es el planificador, con el libro de gastos en la mano, justo antes
    de lanzarlo. Aquí no se sabe de dólares a propósito — este módulo lanza
    procesos y lee su salida, y meterle la aritmética del presupuesto sería el
    segundo sitio que decide lo mismo.
    """

    etiqueta: str
    argv: list[str]


@dataclass(frozen=True)
class Parada:
    """No hay siguiente tramo, y este es el motivo, ya redactado.

    Existe para que «se acabaron los pasos» y «el siguiente no cabía en lo que
    queda del tope» no se vean igual. Lo segundo NO es un fallo: es la promesa
    funcionando, y lo que quedó sin hacer sigue pendiente. Es la misma regla
    que separa «sin saldo» de «se rompió».
    """

    motivo: str
    # ¿Se cuenta como final bueno? Quedarse sin tope no lo es del todo —queda
    # trabajo— pero tampoco es un fallo: es `con_errores` en la escala de la
    # pantalla, que ya significa «terminó, pero algo quedó sin hacer».
    fue_bien: bool = True


# Dado el índice del tramo (0, 1, 2...), qué lanzar ahora. Se pregunta JUSTO
# ANTES de cada tramo y no todo al principio: entre el primero y el tercero
# pueden haber entrado ofertas nuevas y haberse gastado dinero, así que un plan
# calculado al arrancar ya no describiría lo que va a pasar.
Planificador = Callable[[int], "Paso | Parada"]


@dataclass
class Trabajo:
    """Una corrida lanzada desde la bandeja, viva o ya terminada."""

    id: str
    accion: str
    etiqueta: str
    # "corriendo" | "terminado" | "con_errores" | "sin_saldo" | "fallido"
    # | "cancelado"
    estado: str = "corriendo"
    lanzado_at: str = ""
    terminado_at: str | None = None
    codigo_salida: int | None = None

    # Lo que se le enseñó ANTES de lanzar. Se guarda para poder comparar
    # después lo prometido con lo gastado: la regla de la casa no es solo
    # enseñar un número, es que ese número se parezca a la factura.
    coste_estimado_usd: float = 0.0

    # Sobre qué oferta, cuando la acción es de una sola.
    clave: str | None = None

    # Se pidió matarlo, pero el proceso todavía se está muriendo. NO se marca
    # como terminado hasta que lo esté de verdad: si `terminado` se pusiera a
    # True aquí, el registro dejaría lanzar otra corrida mientras la anterior
    # sigue viva, que es justo el solapamiento que el cerrojo existe para
    # impedir. Lo cazó `test_tras_cancelar_se_puede_lanzar_otro`.
    cancelacion_pedida: bool = False

    hechos: int = 0
    total: int = 0

    # -- las acciones encadenadas -------------------------------------------
    # «Ponme al día» son tres comandos seguidos bajo UN solo tope. La pantalla
    # tiene que poder decir «paso 2 de 3» y, sobre todo, qué quedó sin hacer:
    # una cadena que se para por el tope no ha fallado, ha cumplido.
    pasos_total: int = 1
    paso_actual: int = 1
    paso_etiqueta: str = ""
    # Por qué se paró antes de tiempo, si se paró. Va a la pantalla tal cual.
    quedo_sin_hacer: str | None = None

    _lineas: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LINEAS))

    @property
    def terminado(self) -> bool:
        return self.estado != "corriendo"

    @property
    def fue_bien(self) -> bool:
        return self.estado in ("terminado", "con_errores")

    def a_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "accion": self.accion,
            "etiqueta": self.etiqueta,
            "estado": self.estado,
            "terminado": self.terminado,
            "cancelacion_pedida": self.cancelacion_pedida,
            "lanzado_at": self.lanzado_at,
            "terminado_at": self.terminado_at,
            "codigo_salida": self.codigo_salida,
            "coste_estimado_usd": round(self.coste_estimado_usd, 4),
            "clave": self.clave,
            "hechos": self.hechos,
            "total": self.total,
            "pasos_total": self.pasos_total,
            "paso_actual": self.paso_actual,
            "paso_etiqueta": self.paso_etiqueta,
            "quedo_sin_hacer": self.quedo_sin_hacer,
            "lineas": list(self._lineas),
        }


class Proceso(Protocol):
    """Lo que el registro necesita de un proceso. `subprocess.Popen` lo cumple."""

    stdout: Iterable[str] | None

    def wait(self) -> int: ...
    def kill(self) -> None: ...


Lanzador = Callable[[list[str]], Proceso]


def lanzador_de_procesos(argv: list[str]) -> Proceso:
    """Arranca `python -m ...` con el mismo intérprete que corre el servidor.

    `sys.executable` es lo que hace que use el Python del entorno virtual sin
    tener que saber dónde está. La salida de error se junta con la normal
    porque para la pantalla de ella son lo mismo: lo que pasó.

    PYTHONIOENCODING NO ES DECORACIÓN. Aquí se lee la salida del hijo como
    UTF-8, pero en Windows un proceso Python escribe en la página de códigos de
    la consola (cp1252) salvo que alguien le diga lo contrario. Los comandos de
    este proyecto lo dicen —`_prepare_console()` reconfigura los flujos— y por
    eso funcionaba; pero eso deja fuera todo lo que se imprime ANTES de esa
    línea, y en particular los `Traceback` que escribe el propio intérprete
    cuando algo revienta al arrancar. O sea: justo el texto que ella necesita
    leer entero es el que se vería con los acentos rotos.

    Poniéndolo en el entorno, el hijo nace hablando UTF-8 y no depende de que
    ningún módulo se acuerde de reconfigurarse. Medido: sin esto, un script que
    no llama a `_prepare_console()` devuelve «energ�a» en vez de «manufactura».
    """
    entorno = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.Popen(  # noqa: S603 - argv lo arma este módulo, no el cliente
        [sys.executable, *argv],
        cwd=RAIZ,
        env=entorno,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        # NI UNA VENTANA NEGRA, Y ES CONDICIÓN DE LA CÁSCARA (3 sept 2026).
        #
        # Desde el icono del escritorio el servidor corre sin consola, y en
        # Windows un proceso sin consola que lanza un hijo de consola le abre
        # una NUEVA. Sin esto, cada «que el asistente lea lo nuevo» haría
        # parpadear una ventana negra en su pantalla durante dos minutos —y el
        # síntoma no se ve aquí, porque desde `python -m bandeja` el hijo
        # hereda la consola que ya hay y no pasa nada.
        creationflags=SIN_VENTANA,
    )


class Registro:
    """Los trabajos de esta sesión del servidor.

    En memoria y no en la base, a propósito: un trabajo es de la sesión que lo
    lanzó. Si el servidor se reinicia, no hay ningún proceso al que preguntarle
    cómo va, así que guardar su estado solo serviría para enseñar una barra de
    progreso que ya no avanza. Lo que sí sobrevive al reinicio es lo único que
    importa —las ofertas clasificadas y el gasto apuntado—, y eso está en la
    base y en `data/uso_llm.json`.
    """

    def __init__(
        self,
        lanzador: Lanzador | None = None,
        maximo_historico: int = 20,
    ) -> None:
        self._lanzador = lanzador or lanzador_de_procesos
        self._cerrojo = threading.Lock()
        self._trabajos: "OrderedDict[str, Trabajo]" = OrderedDict()
        self._maximo = maximo_historico
        self._en_curso: str | None = None
        self._proceso: Proceso | None = None

    # -- consultas -----------------------------------------------------------

    def en_curso(self) -> Trabajo | None:
        with self._cerrojo:
            if self._en_curso is None:
                return None
            return self._trabajos.get(self._en_curso)

    def obtener(self, trabajo_id: str) -> Trabajo | None:
        with self._cerrojo:
            return self._trabajos.get(trabajo_id)

    def ultimos(self, cuantos: int = 10) -> list[Trabajo]:
        with self._cerrojo:
            return list(self._trabajos.values())[-cuantos:][::-1]

    # -- lanzar --------------------------------------------------------------

    def lanzar(
        self,
        accion: str,
        argv: list[str],
        etiqueta: str,
        coste_estimado_usd: float = 0.0,
        clave: str | None = None,
    ) -> Trabajo:
        """Arranca el comando y devuelve el trabajo ya en curso.

        Lanza `TrabajoEnCursoError` si hay otro corriendo. No se encola: una
        cola invisible haría que ella pulsara «buscar» y no pasara nada visible
        durante dos minutos, que es peor que un «espera a que acabe el otro».
        """
        with self._cerrojo:
            actual = self._trabajos.get(self._en_curso) if self._en_curso else None
            if actual is not None and not actual.terminado:
                raise TrabajoEnCursoError(actual)

            trabajo = Trabajo(
                id=uuid.uuid4().hex[:12],
                accion=accion,
                etiqueta=etiqueta,
                lanzado_at=datetime.now(timezone.utc).isoformat(),
                coste_estimado_usd=coste_estimado_usd,
                clave=clave,
            )
            self._trabajos[trabajo.id] = trabajo
            while len(self._trabajos) > self._maximo:
                self._trabajos.popitem(last=False)
            self._en_curso = trabajo.id

        log("trabajos").info(
            "lanza %s [%s] estimado $%.4f", accion, trabajo.id, coste_estimado_usd
        )

        try:
            proceso = self._lanzador(argv)
        except Exception as exc:  # noqa: BLE001 - cualquier fallo al arrancar
            # AL LOG CON LA TRAZA ENTERA. Este es el fallo que peor se
            # diagnostica en remoto —falta Python, falta el entorno, cambió
            # una ruta— y el único rastro que dejaba era una línea en una
            # pantalla que ella no va a saber leer ni transcribir.
            log("trabajos").exception("no se pudo lanzar %s: %s", accion, exc)
            with self._cerrojo:
                self._terminar(trabajo, codigo=None, estado="fallido")
                trabajo._lineas.append(f"No se pudo lanzar el comando: {exc}")
            return trabajo

        with self._cerrojo:
            self._proceso = proceso

        hilo = threading.Thread(
            target=self._seguir,
            args=(trabajo, proceso),
            name=f"trabajo-{trabajo.id}",
            daemon=True,
        )
        hilo.start()
        return trabajo

    def lanzar_cadena(
        self,
        accion: str,
        etiqueta: str,
        planificador: Planificador,
        pasos_total: int,
        coste_estimado_usd: float = 0.0,
    ) -> Trabajo:
        """Lanza varios comandos seguidos como UNA sola acción.

        ES «PONME AL DÍA», Y LA PARTE DELICADA ES EL TOPE.
        Encadenar tres comandos y dejar que cada uno se mida contra el tope por
        acción sería prometer $0,60 y permitir $1,80 — el mismo error que se
        corrigió el 3 septiembre 2026 al mover el freno de por-llamada a
        por-acción, cometido otra vez un nivel más arriba. Por eso el
        `planificador` se pregunta ANTES DE CADA TRAMO y es él quien pone el
        `--tope-accion-usd` con lo que queda: un importe, un tope, una promesa.

        Y por eso también se pregunta tramo a tramo y no todo de golpe: el
        tercer comando cuesta según lo que el segundo haya clasificado, así que
        un plan hecho al arrancar describiría otra corrida.

        Cuando el planificador devuelve `Parada`, la cadena termina ahí y lo
        que faltaba queda escrito en `quedo_sin_hacer`. No es un fallo: es la
        regla de la casa —lo que no se hace por falta de saldo se queda
        pendiente, nunca hecho— aplicada a una acción de tres tramos.
        """
        with self._cerrojo:
            actual = self._trabajos.get(self._en_curso) if self._en_curso else None
            if actual is not None and not actual.terminado:
                raise TrabajoEnCursoError(actual)

            trabajo = Trabajo(
                id=uuid.uuid4().hex[:12],
                accion=accion,
                etiqueta=etiqueta,
                lanzado_at=datetime.now(timezone.utc).isoformat(),
                coste_estimado_usd=coste_estimado_usd,
                pasos_total=max(1, pasos_total),
                paso_actual=0,
            )
            self._trabajos[trabajo.id] = trabajo
            while len(self._trabajos) > self._maximo:
                self._trabajos.popitem(last=False)
            self._en_curso = trabajo.id

        log("trabajos").info(
            "lanza cadena %s [%s], %d paso(s), estimado $%.4f",
            accion, trabajo.id, pasos_total, coste_estimado_usd,
        )

        hilo = threading.Thread(
            target=self._seguir_cadena,
            args=(trabajo, planificador),
            name=f"cadena-{trabajo.id}",
            daemon=True,
        )
        hilo.start()
        return trabajo

    def _seguir_cadena(self, trabajo: Trabajo, planificador: Planificador) -> None:
        """Ejecuta los tramos en orden. Se para al primero que no vaya bien."""
        estado_final = "terminado"
        codigo_final: int | None = 0

        for indice in range(trabajo.pasos_total):
            with self._cerrojo:
                if trabajo.cancelacion_pedida:
                    self._terminar(trabajo, codigo=codigo_final, estado="cancelado")
                    return

            try:
                siguiente = planificador(indice)
            except Exception as exc:  # noqa: BLE001 - un plan roto no puede colgar la app
                log("trabajos").exception("el plan del paso %d falló: %s", indice, exc)
                with self._cerrojo:
                    trabajo._lineas.append(f"No se pudo planificar el paso: {exc}")
                    self._terminar(trabajo, codigo=None, estado="fallido")
                return

            if isinstance(siguiente, Parada):
                with self._cerrojo:
                    trabajo.quedo_sin_hacer = siguiente.motivo
                    trabajo._lineas.append("")
                    trabajo._lineas.append(siguiente.motivo)
                if not siguiente.fue_bien:
                    estado_final = "con_errores"
                break

            with self._cerrojo:
                trabajo.paso_actual = indice + 1
                trabajo.paso_etiqueta = siguiente.etiqueta
                # La barra es de CADA tramo: el recolector cuenta peticiones y
                # el clasificador vacantes, y sumarlas daría un porcentaje que
                # no significa nada. Lo que da el avance de la acción entera es
                # «paso 2 de 3», que va al lado.
                trabajo.hechos = trabajo.total = 0
                trabajo._lineas.append(f"=== {indice + 1}/{trabajo.pasos_total} · {siguiente.etiqueta}")

            try:
                proceso = self._lanzador(siguiente.argv)
            except Exception as exc:  # noqa: BLE001
                log("trabajos").exception("no se pudo lanzar %s: %s", siguiente.etiqueta, exc)
                with self._cerrojo:
                    trabajo._lineas.append(f"No se pudo lanzar el comando: {exc}")
                    self._terminar(trabajo, codigo=None, estado="fallido")
                return

            with self._cerrojo:
                self._proceso = proceso

            codigo = self._consumir(trabajo, proceso)
            if codigo is None:
                return  # `_consumir` ya lo cerró como fallido

            codigo_final = codigo
            with self._cerrojo:
                cancelado = trabajo.cancelacion_pedida
            if cancelado:
                with self._cerrojo:
                    self._terminar(trabajo, codigo=codigo, estado="cancelado")
                return

            # 0 y 2 siguen; cualquier otro para la cadena con SU significado.
            # El 3 —«no había saldo»— es el caso importante: no es un fallo, y
            # lo que falta sigue pendiente, así que la pantalla lo tiene que
            # decir distinto de «se rompió». Ver ESTADOS_POR_CODIGO.
            if codigo not in (0, 2):
                estado_final = ESTADOS_POR_CODIGO.get(codigo, "fallido")
                with self._cerrojo:
                    trabajo.quedo_sin_hacer = (
                        f"Se paró en «{siguiente.etiqueta}». Lo que faltaba sigue "
                        "pendiente y no se ha perdido ninguna oferta."
                    )
                break
            if codigo == 2:
                estado_final = "con_errores"

        with self._cerrojo:
            self._terminar(trabajo, codigo=codigo_final, estado=estado_final)

    def _consumir(self, trabajo: Trabajo, proceso: Proceso) -> int | None:
        """Lee la salida de un proceso hasta que muere. `None` = ya se cerró."""
        try:
            if proceso.stdout is not None:
                for linea in proceso.stdout:
                    self._anotar(trabajo, linea.rstrip("\n"))
            return proceso.wait()
        except Exception as exc:  # noqa: BLE001
            log("trabajos").exception(
                "se perdió el contacto con %s [%s]: %s", trabajo.accion, trabajo.id, exc
            )
            with self._cerrojo:
                trabajo._lineas.append(f"Se perdió el contacto con el proceso: {exc}")
                self._terminar(trabajo, codigo=None, estado="fallido")
            return None

    def cancelar(self, trabajo_id: str) -> bool:
        """Mata el proceso. Devuelve si había algo que matar.

        Es seguro por cómo está construido el resto: el gasto se apunta llamada
        a llamada y cada clasificación se guarda en cuanto se tiene, así que
        morir a mitad deja lo hecho contado y lo demás PENDIENTE. Es el mismo
        final que quedarse sin presupuesto, y por eso no hace falta ninguna
        transacción especial aquí.
        """
        with self._cerrojo:
            trabajo = self._trabajos.get(trabajo_id)
            proceso = self._proceso
            if trabajo is None or trabajo.terminado or proceso is None:
                return False
            trabajo.cancelacion_pedida = True

        try:
            proceso.kill()
        except Exception:  # noqa: BLE001 - ya había muerto
            pass
        return True

    # -- el hilo que sigue la corrida ---------------------------------------

    def _seguir(self, trabajo: Trabajo, proceso: Proceso) -> None:
        """Lee la salida línea a línea hasta que el proceso termina."""
        codigo = self._consumir(trabajo, proceso)
        if codigo is None:
            return  # `_consumir` ya lo cerró como fallido

        with self._cerrojo:
            if trabajo.cancelacion_pedida:
                # Lo mató ella. El código de salida de un proceso muerto no
                # dice nada útil, así que no se traduce a "fallido".
                self._terminar(trabajo, codigo=codigo, estado="cancelado")
                return
            estado = ESTADOS_POR_CODIGO.get(codigo, "fallido")
            self._terminar(trabajo, codigo=codigo, estado=estado)

    def _anotar(self, trabajo: Trabajo, linea: str) -> None:
        with self._cerrojo:
            trabajo._lineas.append(linea)
            encontrado = PATRON_PROGRESO.search(linea)
            if encontrado:
                hechos, total = int(encontrado.group(1)), int(encontrado.group(2))
                # Solo hacia adelante. Una corrida del recolector imprime
                # `[12/46]` y luego el reporte final puede traer otros números
                # entre corchetes; retroceder la barra parecería un fallo.
                if total > 0 and hechos >= trabajo.hechos:
                    trabajo.hechos, trabajo.total = hechos, total

    def _terminar(self, trabajo: Trabajo, codigo: int | None, estado: str) -> None:
        """Cierra el trabajo. Se llama SIEMPRE con el cerrojo cogido."""
        trabajo.estado = estado
        trabajo.codigo_salida = codigo
        trabajo.terminado_at = datetime.now(timezone.utc).isoformat()
        if trabajo.total and estado in ("terminado", "con_errores"):
            trabajo.hechos = trabajo.total
        if self._en_curso == trabajo.id:
            self._en_curso = None
            self._proceso = None

        # EL DESENLACE SE APUNTA SIEMPRE, incluido el bueno. Un log que solo
        # guarda los fracasos no deja distinguir «falló» de «no llegó a
        # correr», que son las dos hipótesis entre las que hay que elegir
        # cuando el único dato es «no me funciona». Misma regla que el libro
        # de gastos y que `triaje_corridas`.
        registro = log("trabajos")
        mensaje = "%s [%s] -> %s (código %s)"
        datos = (trabajo.accion, trabajo.id, estado, codigo)
        if estado in ("fallido", "con_errores"):
            # Con las últimas líneas de salida: es lo que de verdad dice qué
            # pasó, y se pierde en cuanto ella cierra la ventana.
            registro.error(
                mensaje + " · últimas líneas: %s", *datos,
                " | ".join(list(trabajo._lineas)[-5:]) or "(ninguna)",
            )
        else:
            registro.info(mensaje, *datos)
