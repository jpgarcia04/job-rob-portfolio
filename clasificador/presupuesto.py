"""
El freno de gasto del LLM.

POR QUÉ EXISTE
--------------
Hasta ahora el clasificador MEDÍA el gasto (`--dry-run` lo estimaba, cada
corrida lo imprimía, el acumulado estaba en la base) pero nada lo LIMITABA.
Para un comando que lanzas tú da igual. Para una app que corre sola en la
máquina de la clienta, no: el número de llamadas depende de cuántas vacantes
publique el mercado ese mes, y eso no lo controla nadie. Lo que sí se puede
controlar es lo que se está dispuesto a gastar.

Medir no es controlar. Esto controla.

QUÉ FRENA, Y QUÉ SOLO AVISA (3 septiembre 2026)
------------------------------------------------
El primer diseño tenía un solo freno: una RACIÓN MENSUAL que al agotarse
paraba la app. Estaba bien mientras ese número fuera lo que se le cotizaba a
la clienta. Pero la API key es suya y la recarga automática es suya, así que
una ración que le apaga la herramienta el día 17 no la protege de nada: la
deja sin lo que ya pagó, y encima sin avisar antes de que pase.

Lo que sí hay que prometerle es que **ningún botón suyo se lleve una cantidad
que no vio antes de pulsarlo**. Eso es un tope POR ACCIÓN:

    tope_por_accion_usd   EL FRENO. Ninguna acción arranca por encima de eso,
                          y ninguna lo rebasa mientras corre. Se comprueba en
                          `abrir_accion` (antes de gastar) y en `permite`
                          (entre llamada y llamada).

    techo mensual         La REFERENCIA. Sigue en pantalla y sigue avisando al
                          pasar `avisar_al_superar_pct`; frena a las acciones
                          suyas solo con `al_agotarse: parar`.

    ración automática     Lo que pueden gastar las corridas AUTOMÁTICAS en el
                          mes. Eso frena SIEMPRE. Ahí ella no está delante.

POR QUÉ POR ACCIÓN Y NO POR LLAMADA. `permite()` se consultaba antes de cada
LLAMADA, y un tope ahí no promete nada: lo que ella pulsa es un botón que
dispara una corrida de nueve llamadas, y nueve topes de $0,50 son $4,50. El
importe que hay que enseñar y que hay que frenar es el de la acción entera —el
que `clasificador/plan.py` ya calcula para enseñárselo—, así que es ahí donde
se comprueba.

POR QUÉ UN ARCHIVO Y NO LA BASE DE DATOS
----------------------------------------
El coste de cada clasificación ya está en la tabla `clasificaciones`, así que
el gasto del mes se podría calcular con un SELECT. No se hace, por dos
razones:

  1. Si se borra la base —y se borra: es el gesto natural cuando algo va
     mal— el gasto del mes volvería a cero y el techo dejaría pasar un mes
     entero de gasto por segunda vez. El libro sobrevive a ese borrado, igual
     que `quota_jooble.json` sobrevive al suyo.

  2. Se apunta el gasto ANTES de guardar la clasificación. Si el guardado
     falla, el dinero se gastó igual y tiene que estar contado. Un contador
     que solo registra los éxitos miente justo el día que algo va mal.

Es el mismo patrón que `recolector/quota.py`, con una diferencia: allí se
cuentan peticiones (enteros) y aquí dólares (decimales), porque una llamada
al LLM no cuesta siempre lo mismo.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Nombre del archivo dentro de `data/`. Fuera del control de versiones, igual
# que los otros contadores.
ARCHIVO_USO_LLM = "uso_llm.json"


class LibroCorruptoError(Exception):
    """El libro de gastos existe pero no se puede leer. No se asume cero."""


@dataclass(frozen=True)
class EstadoPresupuesto:
    """Foto del presupuesto en un instante. Es lo que se imprime y se prueba."""

    techo_mensual: float      # la referencia del mes, entera
    techo_efectivo: float     # el techo mensual de ESTA corrida (ver ración)
    gastado_mes: float
    llamadas_mes: int
    es_manual: bool
    racion_automatica: float
    avisar_al_superar_pct: int
    # EL FRENO. Lo máximo que puede costar una acción entera.
    tope_por_accion: float = 0.0
    # ¿El techo mensual FRENA a esta corrida, o solo le avisa? Va en el estado
    # y no se deduce fuera porque lo miran la consola y la bandeja, y dos
    # lecturas del mismo booleano acaban diciendo cosas distintas: una diría
    # «PRESUPUESTO AGOTADO» donde la otra dice «vas por el 90%».
    frena_el_mes: bool = True

    @property
    def disponible(self) -> float:
        """Lo que queda del techo mensual. Nunca negativo.

        Se sigue calculando aunque el mes no frene: es la referencia que se le
        enseña, y un aviso sin número contra el que compararse no avisa de nada.
        """
        return max(0.0, self.techo_efectivo - self.gastado_mes)

    @property
    def agotado(self) -> bool:
        """Sin techo mensual disponible. Solo detiene si `frena_el_mes`."""
        return self.disponible <= 0.0

    @property
    def porcentaje_usado(self) -> float:
        if self.techo_mensual <= 0:
            return 100.0
        return min(100.0, self.gastado_mes / self.techo_mensual * 100.0)

    @property
    def conviene_avisar(self) -> bool:
        return self.porcentaje_usado >= self.avisar_al_superar_pct

    def cabe(self, coste: float) -> bool:
        """¿Cabe una llamada de `coste` dólares en lo que queda del mes?

        Responde sobre el TECHO MENSUAL y nada más. Si ese techo frena o solo
        avisa lo decide `GuardiaPresupuesto.permite`, y el tope por acción es
        otra pregunta distinta: ver `permite_accion`.
        """
        return coste <= self.disponible

    def explicacion(self) -> str:
        """Frase para la consola y para la interfaz.

        Se escribe una sola vez aquí y no en el CLI porque la bandeja tiene que
        decir exactamente lo mismo, y dos redacciones del mismo número acaban
        divergiendo.

        EL PRIMER RENGLÓN ES EL TOPE POR ACCIÓN Y NO EL DEL MES, y el orden es
        deliberado: lo que frena va arriba. Con el techo mensual el primero, un
        «$12,00 USD» encabezando el bloque se lee como la promesa, y la promesa
        es la otra.
        """
        lineas = [
            f"Tope por acción ........ ${self.tope_por_accion:.2f} USD"
            "   <- esto es lo que frena",
        ]
        if self.es_manual:
            lineas.append(
                f"Referencia del mes ..... ${self.techo_mensual:.2f} USD"
                + ("" if self.frena_el_mes else "   (avisa, no frena)")
            )
        else:
            lineas.append(
                f"Ración de automáticas .. ${self.techo_efectivo:.2f} USD  "
                f"(de ${self.techo_mensual:.2f} de referencia)"
            )
        lineas.append(
            f"Gastado este mes ....... ${self.gastado_mes:.4f}  "
            f"({self.porcentaje_usado:.0f}%, {self.llamadas_mes} llamadas)"
        )
        lineas.append(f"Disponible del mes ..... ${self.disponible:.4f}")
        return "\n".join(f"  {linea}" for linea in lineas)


class LibroDeGastos:
    """Gasto en USD por día natural, con vista mensual y total.

    Se guarda {fecha: {usd, llamadas}} en vez de una lista de apuntes: queda
    acotado (365 entradas al año), es legible a ojo, y basta para lo que hace
    falta. Igual que `WindowedUsage`, y por los mismos motivos.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = self._cargar()

    # -- persistencia --------------------------------------------------------

    @staticmethod
    def _vacio() -> dict[str, Any]:
        return {"by_day": {}, "total_usd": 0.0, "total_llamadas": 0, "last_used": None}

    def _cargar(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._vacio()
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            # AQUÍ NO SE EMPIEZA DE CERO EN SILENCIO.
            #
            # `WindowedUsage` sí puede permitírselo: su cuota se regenera y
            # perder la cuenta solo cuesta un 429. Perder la cuenta del gasto
            # cuesta dinero de otra persona, así que se falla ruidosamente y
            # que un humano decida.
            raise LibroCorruptoError(
                f"El libro de gastos {self.path} no se puede leer ({exc}).\n"
                "NO se va a asumir que el gasto del mes es cero: eso permitiría "
                "gastar el techo entero por segunda vez.\n"
                "Revisa el archivo, o bórralo A MANO si aceptas perder la cuenta "
                "de lo que va gastado este mes."
            ) from exc

    def _guardar(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, ensure_ascii=False, indent=2)

    # -- apuntes -------------------------------------------------------------

    @staticmethod
    def _hoy() -> str:
        """La fecha LOCAL, no UTC (cambiado el 8 septiembre 2026).

        EL MES DEL CONTADOR TIENE QUE SER SU MES. Estos son los cajones por
        los que se agrupa el gasto, y agrupándolos por UTC el mes de una
        usuaria en México (UTC−6) se ponía a cero **el último día a las seis de
        la tarde**, no el día 1. Mientras la pantalla enseñaba «$4,47 de
        $12,00» eso daba igual, porque nadie miraba la frontera; desde que la
        cabecera dice en qué mes está y cuándo se reinicia, una frontera a seis
        horas de donde se anuncia son dos números que se contradicen.

        Se cambian los CAJONES, no las marcas de tiempo: `last_used` y
        `apuntado_at` siguen en UTC con su huso dentro, porque son registros de
        cuándo pasó algo y no tienen que depender de dónde esté el portátil.

        Los apuntes viejos quedan con clave UTC y se leen como locales. No
        importa: la diferencia son seis horas y ninguno cae en otro mes. En su
        máquina el libro además arrancó a cero el día de la entrega.
        """
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def mes_actual() -> str:
        """El mes que está contando el libro, como `2026-09`.

        Lo usa la pantalla para decir de qué mes es la cifra. Sale de aquí y no
        del navegador a propósito: la etiqueta y el número tienen que venir del
        mismo reloj o pueden discrepar el día del cambio de mes.
        """
        return datetime.now().strftime("%Y-%m")

    def registrar(self, usd: float, llamadas: int = 1) -> None:
        """Apunta un gasto. Se llama DESPUÉS de la llamada y ANTES de guardar.

        Se escribe a disco en cada apunte, no al final de la corrida. Si el
        proceso muere a mitad, lo gastado hasta ese momento tiene que estar
        contado: un corte de luz no puede regalar presupuesto.
        """
        dia = self._hoy()
        by_day = self._data.setdefault("by_day", {})
        entrada = by_day.setdefault(dia, {"usd": 0.0, "llamadas": 0})
        entrada["usd"] = float(entrada.get("usd", 0.0)) + float(usd)
        entrada["llamadas"] = int(entrada.get("llamadas", 0)) + llamadas

        self._data["total_usd"] = float(self._data.get("total_usd", 0.0)) + float(usd)
        self._data["total_llamadas"] = int(self._data.get("total_llamadas", 0)) + llamadas
        self._data["last_used"] = datetime.now(timezone.utc).isoformat()
        self._guardar()

    def ajustar(self, usd: float, motivo: str, llamadas: int = 0) -> None:
        """Corrige el libro a mano, DEJANDO ESCRITO que es una corrección.

        POR QUÉ NO ES UN `registrar` NORMAL, QUE ERA LA OTRA OPCIÓN.

        El 3 septiembre 2026 el libro decía $2,2663 y el gasto real era
        $2,4250. Faltaban $0,1587 de un A/B de tres reclasificaciones cuyo
        script llamó al cliente sin pasar por el guardia. Las dos salidas
        obvias eran malas las dos: dejarlo fuera deja el libro mintiendo por
        el 7% del total, y apuntarlo como gasto normal deja el libro correcto
        pero inventa tres llamadas en un día en el que el programa no las
        hizo — y este archivo existe justamente para que el número apuntado
        se parezca al cobrado.

        Así que se apunta Y se marca, que es lo que este proyecto ya hace en
        `triajes.heredada_de`, en `clasificaciones.heredada_de` y en
        `description_full_via = 'nada'`: cuando las dos opciones pierden
        información, la salida es guardar de dónde viene el dato. Un ajuste
        queda en `ajustes` con su fecha, su importe y su motivo, y sigue
        sumando en `by_day` para que el techo cuente bien.

        `llamadas` va a 0 por defecto: el importe es real y las llamadas del
        programa no. Inflar el contador de llamadas para cuadrar el de dólares
        sería cambiar una mentira por otra.
        """
        dia = self._hoy()
        self._data.setdefault("ajustes", []).append(
            {
                "fecha": dia,
                "usd": float(usd),
                "motivo": motivo,
                "apuntado_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.registrar(usd, llamadas)

    def ajustes(self) -> list[dict[str, Any]]:
        """Las correcciones a mano. Vacío si no ha hecho falta ninguna."""
        return list(self._data.get("ajustes", []))


    # -- consultas -----------------------------------------------------------

    def _sumar_mes(self, campo: str) -> float:
        # El mismo mes que usa `_hoy` para escribir. Si estos dos no fueran el
        # mismo reloj, el día del cambio de mes se sumarían cajones que ya no
        # son del mes en curso.
        prefijo = self.mes_actual()
        return sum(
            float(valor.get(campo, 0))
            for dia, valor in self._data.get("by_day", {}).items()
            if dia.startswith(prefijo)
        )

    def gastado_este_mes(self) -> float:
        return self._sumar_mes("usd")

    def llamadas_este_mes(self) -> int:
        return int(self._sumar_mes("llamadas"))

    def gastado_total(self) -> float:
        return float(self._data.get("total_usd", 0.0))

    def estado(self, config: Any, es_manual: bool = False) -> EstadoPresupuesto:
        """`config` es un `recolector.config.LlmBudgetConfig`.

        Se tipa como Any para no atar este módulo al del recolector: aquí solo
        se necesitan unos números y un modo.
        """
        return EstadoPresupuesto(
            techo_mensual=config.presupuesto_mensual_usd,
            techo_efectivo=config.techo_para(es_manual),
            gastado_mes=self.gastado_este_mes(),
            llamadas_mes=self.llamadas_este_mes(),
            es_manual=es_manual,
            racion_automatica=config.racion_automatica_mensual_usd,
            avisar_al_superar_pct=config.avisar_al_superar_pct,
            tope_por_accion=config.tope_por_accion_usd,
            frena_el_mes=config.frena_el_mes(es_manual),
        )


def ruta_del_libro(config: Any) -> Path:
    """Dónde vive el libro: junto a los otros contadores, en `data/`.

    Lo calculaban tres sitios (la consola, la bandeja y ahora el cliente) con
    la misma línea copiada. Tres copias de una ruta es como se acaba teniendo
    dos libros abiertos en el mismo proceso, y dos libros es cómo se pierde un
    apunte: cada instancia guarda el archivo ENTERO desde su copia en memoria,
    así que el segundo en escribir borra lo del primero.
    """
    return Path(config.database_path).parent / ARCHIVO_USO_LLM


# ---------------------------------------------------------------------------
#  La entrega que no se pudo preparar
# ---------------------------------------------------------------------------
#  QUÉ PASÓ (10 septiembre 2026). `preparar_entrega.py` archiva el libro del
#  desarrollador y deja uno nuevo a cero, porque el gasto de las pruebas es
#  suyo y no de la clienta. En esta entrega no se llegó a correr: el portátil
#  se entregó con el libro tal cual, con **$4,6244 y 149 llamadas** de días en
#  los que la aplicación todavía no existía en esa máquina.
#
#  Eso no rompe nada —el mes es una cuenta, no un límite— pero deja el único
#  número que ella mira para saber lo que le cuesta la herramienta diciendo una
#  cifra que no es suya, y hace saltar el aviso del 75% semanas antes de
#  tiempo. Es exactamente la clase de mentira pequeña que este proyecto
#  persigue en todas partes, puesta en la cabecera.
#
#  Y NO SE PODÍA ARREGLAR A MANO: el portátil ya estaba entregado y a 9.000 km.
#  `data/` no viaja en git, así que una etiqueta no puede llevar un archivo
#  nuevo — pero sí puede llevar CÓDIGO que lo repare al arrancar. Esto es eso.
#
#  POR QUÉ NO BORRA EL LIBRO ENTERO, que era lo fácil. Porque para cuando esto
#  llegue a su máquina ella ya puede haber gastado dinero suyo, y ese sí cuenta.
#  Se quitan SOLO los días anteriores a la entrega y se recalculan los totales
#  con lo que queda. El archivo original se guarda al lado antes de tocarlo.
#
#  SE APAGA SOLA. En cuanto no quedan días anteriores a la entrega no hace
#  nada, así que puede correr en cada arranque sin volver a tocar nada.

FECHA_DE_LA_ENTREGA = "2026-09-10"


def reparar_libro_de_la_entrega(
    ruta: Path, entrega: str = FECHA_DE_LA_ENTREGA
) -> str | None:
    """Quita del libro el gasto anterior a la entrega. Devuelve qué hizo.

    `None` si no había nada que reparar, que es el caso normal a partir de la
    segunda vez. No lanza nunca: esto corre en el arranque y un fallo aquí no
    puede impedir que la aplicación se abra.
    """
    try:
        if not ruta.exists():
            return None

        with open(ruta, "r", encoding="utf-8") as handle:
            datos = json.load(handle)

        by_day = datos.get("by_day") or {}
        ajenos = sorted(dia for dia in by_day if dia < entrega)
        if not ajenos:
            return None

        usd = sum(float(by_day[d].get("usd", 0.0)) for d in ajenos)
        llamadas = sum(int(by_day[d].get("llamadas", 0)) for d in ajenos)

        # El original, entero, antes de tocar nada. Mismo criterio que
        # `preparar_entrega.archivar_el_libro`: cuando hay que quitar algo, se
        # guarda de dónde venía.
        copia = ruta.with_suffix(f".json.antes-de-la-entrega-{entrega}")
        shutil.copy2(ruta, copia)

        for dia in ajenos:
            del by_day[dia]

        datos["by_day"] = by_day
        datos["total_usd"] = sum(
            float(v.get("usd", 0.0)) for v in by_day.values()
        )
        datos["total_llamadas"] = sum(
            int(v.get("llamadas", 0)) for v in by_day.values()
        )
        # Los ajustes viejos hablaban de dinero que ya no está contado; se van
        # con él, y quedan en la copia.
        datos["ajustes"] = [
            a for a in (datos.get("ajustes") or []) if a.get("fecha", "") >= entrega
        ]
        datos["ajustes"].append({
            "fecha": entrega,
            "usd": 0.0,
            "motivo": (
                f"Se quitaron ${usd:.4f} y {llamadas} llamadas de días "
                f"anteriores al {entrega}: son pruebas del desarrollador con su "
                f"clave, hechas antes de que esta copia existiera, y no pueden "
                f"contar como gasto suyo. El libro completo está archivado en "
                f"{copia.name}. A partir de aquí la clave y el gasto son de la "
                f"clienta. (Lo hace `reparar_libro_de_la_entrega`, porque la "
                f"entrega se hizo sin poder correr `preparar_entrega.py`.)"
            ),
            "apuntado_at": datetime.now(timezone.utc).isoformat(),
        })

        tmp = ruta.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(datos, handle, ensure_ascii=False, indent=2)
        # Sustitución atómica: un corte de luz a mitad deja el libro viejo
        # entero, nunca uno a medio escribir.
        os.replace(tmp, ruta)

        return (
            f"libro de gastos reparado: fuera ${usd:.4f} y {llamadas} llamadas "
            f"de antes del {entrega} (copia en {copia.name})"
        )
    except Exception as exc:  # noqa: BLE001 - el arranque manda
        return f"no se pudo reparar el libro de gastos: {type(exc).__name__}: {exc}"


def libro_por_defecto() -> LibroDeGastos:
    """El libro del proyecto, sin tener que montar nada.

    Existe para que un script suelto —el que abrió el agujero de los $0,1587—
    no tenga excusa para no contar lo que gasta. Ver `ClienteAnthropic`.
    """
    from recolector.config import load_config

    return LibroDeGastos(ruta_del_libro(load_config()))


class GuardiaPresupuesto:
    """Quien DECIDE si se puede gastar. Quien APUNTA es el cliente del LLM.

    Junta lo que hace falta para decidir —el libro, los topes y si la corrida
    la lanzó ella— para que el motor no tenga que saber de raciones ni de
    porcentajes.

    SON DOS PREGUNTAS DISTINTAS Y SE HACEN EN MOMENTOS DISTINTOS:

        abrir_accion(coste)   Antes de gastar nada: ¿esta ACCIÓN entera cabe
                              en el tope por acción? Es el freno que se le
                              promete a la clienta, y el que comprueba la
                              bandeja antes de lanzar el trabajo.

        permite(coste)        Entre llamada y llamada: ¿cabe la SIGUIENTE?
                              Vigila dos cosas a la vez —el techo mensual y lo
                              que lleva gastado esta acción— porque una
                              estimación puede quedarse corta y el tope tiene
                              que aguantar igual.

    El motor lo recibe por parámetro y admite `None`, igual que admite un
    cliente falso: los tests que no van de presupuesto no tienen que montarlo.
    `None` significa «sin freno», y por eso el CLI nunca pasa `None`: los
    valores por defecto se aplican en la configuración, no aquí.
    """

    def __init__(
        self,
        libro: LibroDeGastos,
        config: Any,
        es_manual: bool = False,
        tope_accion: float | None = None,
    ) -> None:
        self.libro = libro
        self.config = config
        self.es_manual = es_manual
        # LO QUE QUEDA DEL TOPE, cuando esta corrida es un TRAMO de una acción
        # mayor. Lo pone «Ponme al día», que encadena tres comandos: si cada
        # uno se midiera contra el tope entero, una sola pulsación podría
        # gastar tres veces lo prometido — que es exactamente el error que se
        # corrigió el 3 septiembre 2026 al mover el freno de por-llamada a
        # por-acción, cometido otra vez un nivel más arriba.
        #
        # SOLO BAJA, NUNCA SUBE. Ver `tope_por_accion`.
        self._tope_accion = tope_accion
        # Lo que llevaba gastado el libro cuando esta acción empezó. Sirve para
        # saber cuánto lleva gastado LA ACCIÓN sin que el guardia tenga que
        # apuntar nada: se lee la diferencia. Así sigue valiendo aunque quien
        # apunte sea el cliente del LLM, que es lo que pasa desde el 3
        # septiembre 2026.
        self._gastado_al_abrir = libro.gastado_total()

    def estado(self) -> EstadoPresupuesto:
        return self.libro.estado(self.config, self.es_manual)

    @property
    def tope_por_accion(self) -> float:
        """El tope de ESTA corrida. El del YAML, o menos si es un tramo.

        **Solo hacia abajo, y esa es toda la guarda.** Un tramo puede quedarse
        con menos de lo que permite el YAML —es su parte de una acción
        compartida—, pero nunca con más: si esto pudiera subir el tope, la
        promesa que se le enseña dejaría de ser un techo y pasaría a ser una
        sugerencia que cualquier comando puede ignorar con un parámetro. Es la
        misma razón por la que el proyecto se niega a subir
        `tope_por_accion_usd` para que algo quepa.
        """
        del_yaml = float(getattr(self.config, "tope_por_accion_usd", 0.0) or 0.0)
        if self._tope_accion is None:
            return del_yaml
        del_tramo = max(0.0, float(self._tope_accion))
        return min(del_yaml, del_tramo) if del_yaml > 0 else del_tramo

    @property
    def sin_freno(self) -> bool:
        """¿No hay ningún tope por acción configurado?

        UN TRAMO CON CERO DISPONIBLE NO ES ESTO, y confundirlos costaba dinero.
        En todo este archivo «tope <= 0» significaba «no hay tope», que es
        correcto mientras el único origen sea el YAML. Con los tramos de
        «Ponme al día» dejó de serlo: el tercer tramo puede recibir
        legítimamente $0,00 de presupuesto restante, y con la comprobación
        vieja eso lo dejaba **sin freno ninguno** — justo al revés de lo que
        pide. Lo cazó `test_un_tramo_a_cero_no_deja_gastar_nada`, y no era
        teórico: es el caso normal de una cadena que agota su tope.

        Que hoy `_planificador_del_dia` devuelva `Parada` antes de llegar ahí
        no arregla nada: una red que solo funciona porque otro número está bien
        puesto no es una red. Es la misma regla que ya obligó a dejar
        `triaje.cabida()` aunque el tope de lote la haga casi inalcanzable.
        """
        if self._tope_accion is not None:
            return False
        return float(getattr(self.config, "tope_por_accion_usd", 0.0) or 0.0) <= 0

    def gastado_en_esta_accion(self) -> float:
        """Lo que se ha apuntado desde que se construyó este guardia."""
        return max(0.0, self.libro.gastado_total() - self._gastado_al_abrir)

    def abrir_accion(self, coste_estimado: float) -> str | None:
        """¿Puede arrancar una acción que se estima en `coste_estimado`?

        Devuelve `None` si sí, y si no el MOTIVO ya redactado —con los dos
        números dentro—, porque lo dicen la consola y la bandeja y dos
        redacciones del mismo rechazo acaban divergiendo.

        SE COMPRUEBA SOBRE LA ACCIÓN ENTERA, que es lo que ella ve y confirma:
        el botón «que el asistente lea lo nuevo» son la llamada del triaje más
        ocho lecturas, y un tope por llamada dejaría pasar nueve veces el tope.

        Lo que se rechaza NO se pierde: las ofertas siguen pendientes, sin fila
        en `clasificaciones`, igual que cuando se agota el mes.
        """
        tope = self.tope_por_accion
        if self.sin_freno or coste_estimado <= tope:
            return None
        return (
            f"Esta acción cuesta ~${coste_estimado:.4f} y el tope por acción "
            f"son ${tope:.2f}. No se ha gastado nada y no se ha perdido "
            "ninguna oferta: siguen pendientes. Para partirla en trozos que "
            "quepan, baja `llm.triaje.max_vacantes_por_triaje` o "
            "`llm.triaje.ofertas_por_corrida`; para subir el freno, "
            "`llm.tope_por_accion_usd` en config/search_profile.yaml."
        )

    def permite(self, coste_estimado: float) -> bool:
        """¿Se puede pagar la siguiente llamada de esta acción?

        Dos guardas, y hacen falta las dos:

        1. EL TOPE POR ACCIÓN, contra lo que la acción lleva gastado de
           verdad. `abrir_accion` comprobó una ESTIMACIÓN, y una estimación se
           queda corta el día que tocan ocho anuncios largos. Sin esta mitad,
           el tope sería una intención y no una promesa.

        2. EL TECHO MENSUAL. Frena siempre a las corridas automáticas —ahí ella
           no está mirando— y a las suyas solo con `al_agotarse: parar`. Es su
           API key y su recarga automática: apagarle la herramienta a mediados
           de mes no la protege de nada, y para eso está el aviso.
        """
        if not self._cabe_en_la_accion(coste_estimado):
            return False
        if not self.config.frena_el_mes(self.es_manual):
            return True
        return self.estado().cabe(coste_estimado)

    # Cuánto se admite que la siguiente llamada cueste MÁS de lo estimado.
    #
    # POR QUÉ HACE FALTA (medido el 9 septiembre 2026, forzando que el modelo
    # escribiera seis veces lo que tiene medido). La acción acabó gastando
    # $0,6485 con el tope en $0,60: cada llamada cabía por estimación y
    # costaba el doble al volver, y el sobrante se fue acumulando. La cuenta
    # de esta guarda era exacta —lo gastado DE VERDAD más lo estimado de la
    # siguiente— y aun así se pasaba, porque «lo estimado» era la mitad.
    #
    # 1,4 es el mismo margen que `cliente_llm.MARGEN_PENSAMIENTO` usa para
    # decidir si un lote cabe en una llamada, y por el mismo motivo: lo que el
    # modelo piensa depende de lo difícil que sea el lote, no solo de su
    # tamaño. Cuesta como mucho no leer una oferta más por corrida —que se
    # queda pendiente y entra en la siguiente—, y compra que la frase «ningún
    # botón puede costar más de $0,60» sea verdad y no una intención.
    #
    # LO QUE ESTO NO PUEDE ARREGLAR, Y SE DICE: una llamada SUELTA que se
    # dispare. El triaje es una sola llamada y su coste no se conoce hasta que
    # vuelve; si esa se pasa, ya está pagada y no hay guarda posible delante.
    # Lo que este margen impide es que ese exceso arrastre a las siguientes.
    MARGEN_DE_LA_SIGUIENTE = 1.4

    def _cabe_en_la_accion(self, coste_estimado: float) -> bool:
        """La condición del tope por acción, en UN solo sitio.

        `permite` y `motivo_de_parada` tienen que decidir y explicar con la
        misma cuenta: si divergen, el mensaje cuenta una historia distinta de
        la que provocó el frenazo. Antes estaba escrita dos veces.
        """
        if self.sin_freno:
            return True
        previsto = coste_estimado * self.MARGEN_DE_LA_SIGUIENTE
        return self.gastado_en_esta_accion() + previsto <= self.tope_por_accion

    def motivo_de_parada(self, coste_rechazado: float = 0.0) -> str:
        """Por qué se paró: por la acción o por el mes. No es lo mismo.

        Una acción que topa se vuelve a lanzar y sigue donde iba; un mes
        agotado no se arregla pulsando otra vez. Decir «no había saldo» en los
        dos casos es la regla nº 1 de la casa rota justo en la pantalla.

        SE PREGUNTA POR EL COSTE QUE SE RECHAZÓ, no por lo que lleva gastado, y
        esa es toda la diferencia. La corrida se para cuando la SIGUIENTE
        llamada no cabe, así que lo gastado siempre se queda por debajo del
        tope —por el importe de una llamada— y mirar solo eso daba «se agotó el
        mes» en todos los casos. Aquí se repite la condición exacta de
        `permite()`: si las dos no fueran la misma, el mensaje contaría una
        historia distinta de la que provocó el frenazo.
        """
        tope = self.tope_por_accion
        gastado = self.gastado_en_esta_accion()
        if not self._cabe_en_la_accion(coste_rechazado):
            return (
                f"Esta acción llegó a su tope de ${tope:.2f} (lleva "
                f"${gastado:.4f} gastados y la siguiente son "
                f"~${coste_rechazado:.4f}). Lo que falta sigue pendiente: "
                "vuelve a lanzarla y sigue donde iba."
            )
        return (
            "Se agotó el techo del mes. Lo que falta sigue pendiente y se "
            "clasifica solo en cuanto haya saldo."
        )

    def apuntar(self, usd: float, llamadas: int = 1) -> None:
        """Apunta un gasto ya hecho.

        SIGUE AQUÍ PERO YA CASI NADIE LO LLAMA. Desde el 3 septiembre 2026 el
        que apunta es `ClienteAnthropic`, en el mismo sitio donde se calcula el
        coste con el desglose de caché. Tres capas llamando a esto era tres
        veces la misma regla escrita a mano, y la cuarta —un script de
        medición— se olvidó y se perdieron $0,1587. Ver `cliente_llm.py`.
        """
        self.libro.registrar(usd, llamadas)
