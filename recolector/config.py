"""
Carga de configuración.

Dos fuentes, separadas a propósito:

  - `config/search_profile.yaml` — lo que la clienta puede cambiar. Versionado.
  - `.env`                       — API keys y datos personales. Ignorado por git.

El YAML nunca contiene una key: contiene el NOMBRE de la variable de entorno
donde vive esa key. Así el archivo de configuración se puede compartir, subir
al repo o mandar por correo sin filtrar nada.

Todo se valida al arrancar. Si falta algo, el programa muere aquí con un
mensaje que dice qué falta y dónde ponerlo, en vez de fallar más tarde con un
KeyError incomprensible en mitad de una corrida.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .models import KeywordCombo
from .textnorm import normalize_text

# Raíz del proyecto = carpeta que contiene el paquete `recolector`.
# `__file__` es la ruta de este archivo; `.parent` sube un nivel.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "search_profile.yaml"


def cargar_env(env_path: Path | None = None) -> None:
    """Vuelca `.env` en el entorno del proceso. Idempotente.

    ESTÁ SUELTA Y NO DENTRO DE `Credentials` A PROPÓSITO.

    Hasta el 2 septiembre 2026 esto vivía dentro de `Credentials.load()`, que
    es la clase de las keys de Adzuna y Jooble. El efecto era que `.env` solo
    se leía si alguien pedía credenciales de una FUENTE: el recolector lo
    hacía y funcionaba, y el clasificador —que no necesita Adzuna para nada—
    no lo hacía y nunca encontraba `ANTHROPIC_API_KEY`, aunque estuviera
    escrita en el archivo. El clasificador entero era inejecutable salvo que
    exportaras la variable a mano en la consola, y ese es el motivo real de
    que no hubiera corrido nunca de verdad.

    El fallo no era la línea: era que cargar el entorno fuese responsabilidad
    de quien pide una key concreta. Cualquier punto de entrada nuevo volvía a
    olvidarse. Por eso ahora lo llama `load_config()`, por donde pasan los dos
    comandos: cargar la configuración carga también el entorno, siempre.

    `override=False` porque una variable ya presente en el entorno del sistema
    gana al archivo: permite sobreescribir una key desde la consola para una
    corrida suelta sin editar `.env`.
    """
    path = env_path or (PROJECT_ROOT / ".env")
    if path.exists():
        # `utf-8-sig` Y NO `utf-8`: se come el BOM si lo hay.
        #
        # Un BOM al principio del archivo se pega al nombre de la PRIMERA
        # variable —`﻿ADZUNA_APP_ID` en vez de `ADZUNA_APP_ID`—, así que esa
        # credencial deja de existir sin que falte ninguna línea ni falle
        # nada. Pasó de verdad: `instalar.ps1` escribía el archivo con
        # `Set-Content -Encoding UTF8`, que en PowerShell 5.1 pone BOM.
        #
        # Aquello está arreglado en su origen, y aun así esto se queda: el
        # Bloc de notas de Windows también guarda con BOM según cómo esté
        # configurado, y ella va a abrir este archivo el día que cambie una
        # clave. Un archivo de configuración no puede romperse por el editor
        # con el que se guardó.
        load_dotenv(path, override=False, encoding="utf-8-sig")


class ConfigError(Exception):
    """Configuración ausente, incompleta o incoherente."""


class MissingCredentialsError(ConfigError):
    """Falta una API key en el entorno."""


# ---------------------------------------------------------------------------
#  Estructuras tipadas
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SalaryFloor:
    """Umbral salarial de la clienta (170.000 USD/año de partida).

    `mode` decide si esto CORTA o solo MIDE:

      report -> no se descarta nada; cada oferta se clasifica en
                cumple / no cumple / desconocido y se cuenta en el reporte
      filter -> se descarta de verdad

    En la v0 va en `report` a propósito: la mayoría de las ofertas no traen
    salario, y filtrar por un dato ausente descartaría el grueso del mercado
    por falta de información en vez de por falta de sueldo.
    """

    mode: str                     # "report" | "filter"
    amount: float
    currency: str
    period: str                   # de momento solo "year"
    on_missing_salary: str        # "keep" | "drop"  (solo aplica en mode=filter)
    use_range_top: bool
    fx_to_usd: dict[str, float]
    fx_updated: str

    @property
    def is_active_filter(self) -> bool:
        return self.mode == "filter"

    def to_usd(self, amount: float, currency: str | None) -> float | None:
        """Convierte a USD con el tipo de cambio declarado en el YAML.

        Devuelve None si la divisa no está en la tabla: preferimos no saber a
        inventarnos una conversión.
        """
        if currency is None:
            return None
        rate = self.fx_to_usd.get(currency.upper())
        if rate is None:
            return None
        return amount * rate


@dataclass(frozen=True)
class HardFilters:
    max_days_old: int
    exclude_title_terms: list[str]
    exclude_companies: list[str]
    require_all_terms_in_text: bool
    # Lista blanca sobre el TÍTULO. Si está vacía, no se aplica y el filtro de
    # pertinencia vuelve a ser `require_all_terms_in_text`. Ver
    # `uses_title_whitelist` y el paso 3 de `filters.apply_filters`.
    include_title_terms: list[str] = field(default_factory=list)
    salary_floor: SalaryFloor | None = None

    @property
    def uses_title_whitelist(self) -> bool:
        """La lista blanca manda cuando tiene términos.

        Existe como propiedad y no como comparación suelta porque la
        precedencia entre los dos filtros de pertinencia se consulta en tres
        sitios (el filtro, el reporte y `check`), y tenerla escrita una sola
        vez evita que un día divergan y el reporte explique un filtro que no
        es el que corrió.
        """
        return bool(self.include_title_terms)


@dataclass(frozen=True)
class Zona:
    """Un trozo de un mercado: lo que la bandeja deja elegir DENTRO de Texas.

    Existe desde el 24 septiembre 2026. La clienta pidió dejar de ver todo
    Texas y quedarse con unas zonas concretas (de Houston y
    Austin hasta Brownsville). Un mercado con zonas acepta SOLO lo que cae
    dentro de alguna, y cada oferta guardada lleva la clave de su zona para
    que la pantalla pueda agruparlas.

    `condados` se escriben como los devuelve Adzuna («Harris County») y
    mandan sobre `ciudades`, que son el plan B para cuando la fuente no trae
    condado (Jooble, o un «Houston, TX» a secas).
    """

    clave: str
    etiqueta: str
    # Lo que se lee al pasar el ratón por el botón de la zona: sus ciudades
    # conocidas, para que «Valle y sur de Texas» no sea un nombre a ciegas.
    descripcion: str = ""
    condados: list[str] = field(default_factory=list)
    ciudades: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GeoFilter:
    """Cómo se filtra geográficamente lo que devuelve el índice de un país.

    mode = "none"           -> se acepta todo (Alemania: el país entero)
    mode = "allow_regions"  -> solo las regiones/ciudades listadas (México)

    Con `zonas`, `allow_regions` se vuelve más estricto (Texas): la región es
    condición NECESARIA pero no suficiente, y además la oferta tiene que caer
    en el condado o la ciudad de alguna zona. Ver `geo.classify_location`.
    """

    mode: str
    regions: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    # Qué hacer con las ofertas cuya ubicación no permite decidir el estado
    # ("Remote", "United States"): "drop" o "keep". En ambos casos se cuentan
    # aparte y salen en el reporte; nunca desaparecen en silencio.
    unresolved_policy: str = "drop"
    zonas: list[Zona] = field(default_factory=list)

    def zona(self, clave: str) -> Zona | None:
        return next((z for z in self.zonas if z.clave == clave), None)


@dataclass(frozen=True)
class AdzunaMarketConfig:
    country: str   # código de índice de país en la URL de Adzuna: "mx", "us"
    where: str     # parámetro `where`; "" = sin restricción


@dataclass(frozen=True)
class JoobleMarketConfig:
    api_key_env: str   # NOMBRE de la variable de entorno, no la key
    host: str          # Jooble tiene un host por país: mx.jooble.org, jooble.org
    location: str      # `location` de Jooble es texto libre


@dataclass(frozen=True)
class Market:
    key: str
    label: str
    country_code: str
    adzuna: AdzunaMarketConfig
    jooble: JoobleMarketConfig
    geo_filter: GeoFilter


@dataclass(frozen=True)
class SearchProfile:
    key: str
    label: str
    description: str
    keyword_combinations: list[KeywordCombo]
    market_keys: list[str]
    hard_filters: HardFilters

    # LO QUE SE PREGUNTA EN CADA MERCADO, YA RESUELTO (17 septiembre 2026).
    #
    # Una clave por cada mercado de `market_keys`, y su valor es la lista
    # DEFINITIVA de combinaciones de ese mercado: la general más su `ademas`
    # menos su `menos`. Se calcula una sola vez al leer el YAML y no en cada
    # consumidor, porque son cuatro los que cuentan peticiones —la consola, la
    # bandeja, el plan y la propia corrida— y cuatro sitios calculando lo mismo
    # es la divergencia que ya costó un disgusto en `clasificador/plan.py`.
    #
    # Si nadie escribió `combinaciones_por_mercado`, todos los mercados apuntan
    # a la misma lista y el comportamiento es exactamente el de antes.
    combinations_by_market: dict[str, list[KeywordCombo]] = field(default_factory=dict)

    def combinations_for(self, market_key: str) -> list[KeywordCombo]:
        """Las combinaciones que se lanzan en ese mercado.

        Cae a la lista general si el mercado no tiene entrada propia, que es lo
        que pasa con los mercados sin ajuste y con cualquier clave que llegue
        por `--markets` sin estar en el perfil.
        """
        return self.combinations_by_market.get(market_key, self.keyword_combinations)

    def planned_queries(
        self, market_keys: list[str] | None = None, limit_combos: int | None = None
    ) -> int:
        """Cuántas consultas por fuente costaría la corrida.

        Es la ÚNICA cuenta de «cuántas consultas son». Antes era
        `len(combos) * len(mercados)` escrito en cuatro sitios, y eso dejó de
        ser cierto en cuanto un mercado pregunta cosas distintas que otro.
        """
        claves = market_keys if market_keys is not None else self.market_keys
        total = 0
        for clave in claves:
            cuantas = len(self.combinations_for(clave))
            total += min(cuantas, limit_combos) if limit_combos else cuantas
        return total


@dataclass(frozen=True)
class AdzunaSourceConfig:
    enabled: bool
    results_per_page: int
    max_pages: int
    sleep_seconds_between_calls: float
    timeout_seconds: int
    # La cuota de Adzuna se regenera por ventana de tiempo. Estos topes pueden
    # ser None: en ese caso solo se cuenta y se avisa, sin negarse a correr.
    #
    # Los del plan Trial, leídos de su panel el 3 septiembre 2026, son TRES de
    # volumen —250/día, 1000/semana, 2500/mes— más uno de RITMO, 25/minuto, que
    # no es un tope de volumen y por eso no vive aquí sino en la pausa entre
    # peticiones. Ver `max_calls_per_minute`.
    daily_call_budget: int | None = None
    weekly_call_budget: int | None = None
    monthly_call_budget: int | None = None
    warn_daily_above: int = 200

    # Peticiones por minuto que admite el plan. NO se compara con un contador:
    # se compara con `sleep_seconds_between_calls` al cargar la configuración,
    # y si la pausa es demasiado corta el programa se niega a arrancar con el
    # número que hay que escribir. Es un límite de ritmo, y un ritmo no se
    # controla contando después: se controla yendo más despacio.
    max_calls_per_minute: int | None = None


@dataclass(frozen=True)
class JoobleSourceConfig:
    enabled: bool
    lifetime_request_budget: int
    warn_when_remaining_below: int
    refuse_when_remaining_below: int
    max_pages: int
    sleep_seconds_between_calls: float
    timeout_seconds: int


@dataclass(frozen=True)
class CacheConfig:
    enabled: bool
    directory: Path
    ttl_hours: float


@dataclass(frozen=True)
class DescripcionesConfig:
    """Traerse la descripción entera de la web de la fuente.

    APAGADO POR DEFECTO, igual que el triaje y por la misma clase de razón:
    encenderlo cambia lo que cuesta cada clasificación (+1.710 tokens de
    entrada medidos, ~+$3/mes) y empieza a hacer peticiones a un sitio web que
    no es su API. Ninguna de las dos cosas puede empezar a pasar porque alguien
    copiara un YAML sin leerlo.

    Ver `connectors/adzuna_descripcion.py` y el bloque `descripciones:` del
    YAML, donde está la medición entera.
    """

    habilitado: bool = False
    max_por_corrida: int = 40
    sleep_seconds_between_calls: float = 1.5
    timeout_seconds: int = 30


@dataclass(frozen=True)
class TriajeConfig:
    """Cuántas ofertas se leen A FONDO por corrida.

    POR QUÉ EXISTE
    --------------
    Sin esto, la factura del mes la decide cuántas vacantes publique el
    mercado, que no lo controla nadie: medido el 2 septiembre 2026, entre 20 y
    30 nuevas al día, y una ensanchada más del filtro las duplicaría. Eso no
    es una cifra que se pueda poner en una recarga automática.

    Con esto, la decide un número que elegimos. El triaje mira TODAS las
    ofertas de una sola llamada (~70 tokens cada una) y ordena; solo las
    `ofertas_por_corrida` primeras se leen enteras.

    `ofertas_por_corrida` ES UN TECHO, NO UNA CUOTA. Si el modelo dice que solo
    tres del lote merecen lectura, se leen tres y se ahorra el resto. Rellenar
    hasta N sería gastar por gastar.

    LO QUE NO SE QUEDA FUERA ES LA OFERTA. Las no seleccionadas siguen enteras
    en la bandeja con su título, empresa, sueldo y su puesto en el triaje, y a
    un clic de que el asistente las lea. Se aplaza la atención, no se esconde
    nada — que es la regla nº 1 de la casa.
    """

    activo: bool
    ofertas_por_corrida: int
    # CUÁNTAS VACANTES ENTRAN COMO MUCHO EN UNA LLAMADA DE TRIAJE.
    #
    # Son dos perillas distintas y se confunden con facilidad:
    # `ofertas_por_corrida` es cuántas se LEEN enteras (8), y esta es cuántas
    # se MIRAN por metadatos en la llamada de triaje (60). La primera manda en
    # el grueso de la factura; esta manda en que la llamada quepa.
    #
    # POR QUÉ EXISTE. La respuesta del triaje —una línea por vacante, más el
    # pensamiento— cuenta dentro de `max_tokens`, y por encima de ~228
    # vacantes a `low` no cabe. `triaje.cabida()` se niega antes de llamar,
    # que es lo correcto, pero con 273 vacantes atrasadas eso dejaba la app
    # bloqueada: el botón devolvía un 409 corrida tras corrida y no había
    # salida. El tope hace que el caso no llegue a darse.
    #
    # LO QUE NO ENTRA CONSERVA SU TURNO ENTERO: no se le escribe fila en
    # `triajes`, así que entra completa en la corrida siguiente. Ver
    # `clasificador/triaje.preparar_lote`, donde está el porqué del sitio
    # exacto en que se aplica.
    max_vacantes_por_triaje: int
    # Cuánto puede pensar el modelo al triar. En el TRIAJE sí cambia el
    # coste (medido el 2 sept 2026 sobre 30 ofertas: por defecto $0,117;
    # medium $0,092; low $0,065), porque comparar treinta anuncios genera
    # mucho pensamiento y el pensamiento se factura como salida.
    #
    # En la CLASIFICACIÓN no cambia nada y por eso allí no hay perilla:
    # se midió y las tres opciones dieron la misma salida. La tarea ya era
    # pequeña y no había nada que recortar.
    esfuerzo: str | None


@dataclass(frozen=True)
class LlmBudgetConfig:
    """Lo que el clasificador puede gastar, en DÓLARES.

    En dólares y no en llamadas a propósito: una llamada cuesta lo que cuesta
    según lo largo que sea el anuncio, así que un tope en llamadas no es una
    promesa que se le pueda hacer a la clienta.

    SON DOS FRENOS DISTINTOS Y NO SE PUEDEN CONFUNDIR (3 septiembre 2026)
    ---------------------------------------------------------------------
    Hasta hoy había uno solo: una RACIÓN MENSUAL de $12 que, al agotarse,
    paraba la app. Eso se diseñó cuando ese número era lo que se le cotizaba
    a la clienta. Pero la API key es SUYA y la recarga es SUYA, así que una
    ración mensual que le apaga la herramienta a mediados de mes no la
    protege de nada: la deja sin lo que ya pagó.

    Lo que sí hay que prometerle es que **ningún botón suyo se le lleve una
    cantidad que no vio antes de pulsarlo**. Eso es un tope POR ACCIÓN, no
    por mes, y son cosas distintas:

        tope_por_accion_usd     El FRENO. Ninguna acción arranca por encima
                                de esto, y ninguna lo rebasa mientras corre.
                                Es lo que se le promete.

        presupuesto_mensual_usd La REFERENCIA del mes. Con `al_agotarse:
                                avisar` no la frena a ella —avisa—; con
                                `parar` vuelve a ser el freno duro de antes.

        racion_automatica_mensual_usd
                                Lo que pueden gastar en el mes las corridas
                                AUTOMÁTICAS. Eso sí frena SIEMPRE, porque ahí
                                ella no está mirando.

    POR QUÉ EL TOPE VA POR ACCIÓN Y NO POR LLAMADA. El guardia se consultaba
    por LLAMADA (`motor.py`), y un tope ahí no promete nada: lo que ella pulsa
    es un botón que dispara una corrida de nueve llamadas. El importe que hay
    que enseñar y que hay que frenar es el de la acción entera, que es
    justamente el que `clasificador/plan.py` ya calcula.

    DE DÓNDE SALE `racion_automatica_mensual_usd`. Es `reserva_manual_usd`
    con el sentido invertido, y por eso cambió de nombre. Antes decía «$2 que
    las automáticas NO pueden tocar» y el techo de las automáticas quedaba
    implícito ($12 − $2 = $10). Ahora ese $10 se escribe directamente. Es la
    misma cantidad dicha al derecho: el número de la línea pasa de 2 a 10
    porque nombra la cosa en vez de su complemento. Con el techo mensual
    convertido en aviso, la lectura vieja ya no significaba nada —no se puede
    reservar una parte de un techo que no frena—.

    Vive junto a `sources` porque el LLM es el tercer servicio externo con
    cuota del proyecto, y los tres presupuestos tienen que poder leerse de una
    sentada. Quien lo consume es el clasificador, no el recolector.
    """

    presupuesto_mensual_usd: float
    # "parar"  -> el techo mensual frena TAMBIÉN a las acciones suyas
    # "avisar" -> el techo mensual solo avisa; a ella la frena el tope por acción
    al_agotarse: str
    # Lo que las corridas AUTOMÁTICAS pueden gastar en el mes. Frena siempre,
    # diga lo que diga `al_agotarse`: ahí ella no está delante para decidir.
    racion_automatica_mensual_usd: float
    # Avisa al superar este % del techo, mucho antes de agotarlo.
    avisar_al_superar_pct: int
    # EL FRENO. Lo máximo que puede costar UNA acción —un botón de la bandeja,
    # un comando de consola—, contando todas sus llamadas. Ver `presupuesto.py`.
    tope_por_accion_usd: float
    # Cuántas ofertas se leen a fondo por corrida. Ver `TriajeConfig`.
    #
    # Lleva defecto —y el defecto es APAGADO— para que construir esta
    # configuración a mano, como hacen las pruebas, siga leyéndolo todo. Que
    # el triaje se encienda tiene que ser una decisión escrita en el YAML,
    # nunca algo que pase por omisión: gastar de más se ve en la factura,
    # leer de menos no se ve en ningún sitio.
    # Cuánto puede pensar el modelo al ORDENAR el lote para ella.
    #
    # Es la única perilla de esfuerzo que se decide por calidad y no por
    # precio: el texto del ranking es lo que ella lee, así que aquí se paga
    # por la comparación buena. Medido sobre las mismas 11 ofertas: por
    # defecto $0,110; `medium` $0,077; `low` $0,061 — y las tres cazaron los
    # anuncios duplicados. Se queda en `medium` porque sus razones comparan
    # contra ofertas concretas («más cercana a tu terreno que la de data
    # centers en Dallas») y eso es justo el producto.
    esfuerzo_ranking: str | None = "medium"

    # CUÁNTAS OFERTAS ORDENA EL RANKING COMO MUCHO. `None` = sin techo propio.
    #
    # Es la segunda mitad de la decisión del 4 septiembre 2026, y hace una cosa
    # distinta de `triaje.ofertas_por_corrida`: aquella manda en la FACTURA del
    # mes, esta manda en que «Ponme al día» quepa entero. Sin ella, el coste de
    # ordenar crece con lo que la clienta no ha revisado y no tiene tope: cada
    # oferta sin revisar son ~$0,0059, así que 45 ofertas se comen los $0,28
    # que le quedan al tope de $0,60 después de leer seis anuncios.
    #
    # MEDIDO el 4 septiembre 2026 sobre la base real, con seis lecturas:
    #
    #     paso 2 (triaje de 60 + 6 lecturas) ....... $0,3217
    #     lo que le queda al tope de $0,60 ......... $0,2783
    #     ofertas que compra eso ................... 45
    #     ofertas que caben en UNA llamada a medium  60  (`asistente.cabida`)
    #
    # O sea que quien aprieta es el tope, no la llamada, y por eso el número
    # está aquí y no en `cabida`.
    #
    # Y RECORTA POR RECENCIA, que es lo único malo que tiene:
    # `ofertas_para_ranking` ordena por fecha de publicación descendente, así
    # que el techo deja fuera las más viejas sin mirar si son buenas. Eso sería
    # inaceptable en silencio —esconder ofertas dentro de la pieza que existe
    # para no esconder ninguna— y por eso NO es silencioso: el ranking guarda
    # cuántas había (`rankings.ofertas_disponibles`) y la bandeja dice «las 45
    # más recientes de 61». Las que se quedan fuera siguen enteras en la
    # bandeja y con sus filtros.
    max_ofertas_por_ranking: int | None = None

    triaje: TriajeConfig = field(
        default_factory=lambda: TriajeConfig(
            activo=False,
            ofertas_por_corrida=8,
            max_vacantes_por_triaje=60,
            esfuerzo="medium",
        )
    )

    @property
    def detiene_al_agotarse(self) -> bool:
        """¿El techo MENSUAL frena también a las acciones suyas?

        A las automáticas las frena siempre su ración, diga esto lo que diga.
        """
        return self.al_agotarse == "parar"

    def techo_para(self, es_manual: bool) -> float:
        """El techo MENSUAL que aplica a esta corrida. No es el freno por acción.

        Una corrida automática (la tarea programada, el día que exista) tiene
        su propia ración y se para al agotarla: ahí ella no está mirando, así
        que el que decide es un número escrito de antemano.

        Una corrida suya se rige por el tope POR ACCIÓN. El techo mensual solo
        la frena si `al_agotarse: parar`, que es el comportamiento de antes y
        se deja para quien lo quiera: es su API key y su recarga, y apagarle la
        herramienta a mediados de mes no la protege de nada.
        """
        if not es_manual:
            return max(0.0, self.racion_automatica_mensual_usd)
        return self.presupuesto_mensual_usd

    def frena_el_mes(self, es_manual: bool) -> bool:
        """¿Se PARA al agotar el techo mensual, o solo se avisa?

        Va aparte de `techo_para` a propósito. El número se sigue enseñando
        siempre —«llevas $9,12 de $12» es información útil aunque no frene—, y
        lo que cambia es solo si se convierte en una negativa. Si esto se
        resolviera devolviendo un techo infinito, la barra de la bandeja
        perdería la referencia y no habría nada contra lo que avisar.
        """
        return (not es_manual) or self.detiene_al_agotarse


@dataclass(frozen=True)
class AppConfig:
    profiles: dict[str, SearchProfile]
    active_profile_key: str
    markets: dict[str, Market]
    adzuna: AdzunaSourceConfig
    jooble: JoobleSourceConfig
    llm: LlmBudgetConfig
    database_path: Path
    cache: CacheConfig
    descripciones: DescripcionesConfig
    config_path: Path

    def profile(self, key: str | None = None) -> SearchProfile:
        """Devuelve un perfil por clave, o el activo si no se indica."""
        chosen = key or self.active_profile_key
        if chosen not in self.profiles:
            available = ", ".join(sorted(self.profiles))
            raise ConfigError(
                f"El perfil '{chosen}' no existe en {self.config_path}.\n"
                f"Perfiles disponibles: {available}"
            )
        return self.profiles[chosen]

    def market(self, key: str) -> Market:
        if key not in self.markets:
            available = ", ".join(sorted(self.markets))
            raise ConfigError(
                f"El mercado '{key}' no está definido en {self.config_path}.\n"
                f"Mercados disponibles: {available}"
            )
        return self.markets[key]


# ---------------------------------------------------------------------------
#  Credenciales (.env)
# ---------------------------------------------------------------------------

@dataclass
class Credentials:
    """Las API keys. Se leen del entorno, nunca del YAML.

    Esta clase NO imprime sus valores en ningún sitio: si necesitas depurar
    una key, usa `fingerprint()`, que da un identificador corto y no
    reversible, suficiente para saber si la key cambió.
    """

    adzuna_app_id: str | None
    adzuna_app_key: str | None
    _env: dict[str, str]

    @classmethod
    def load(cls, env_path: Path | None = None) -> "Credentials":
        """Carga `.env` (si existe) y toma los valores del entorno.

        Sigue llamando a `cargar_env` aunque `load_config` ya lo haga: esta
        clase tiene que poder usarse suelta (y con otro `.env`) sin obligar a
        montar la configuración entera.
        """
        cargar_env(env_path)
        return cls(
            adzuna_app_id=os.getenv("ADZUNA_APP_ID") or None,
            adzuna_app_key=os.getenv("ADZUNA_APP_KEY") or None,
            _env=dict(os.environ),
        )

    def require_adzuna(self) -> tuple[str, str]:
        missing = [
            name
            for name, value in (
                ("ADZUNA_APP_ID", self.adzuna_app_id),
                ("ADZUNA_APP_KEY", self.adzuna_app_key),
            )
            if not value
        ]
        if missing:
            raise MissingCredentialsError(
                "Faltan credenciales de Adzuna: " + ", ".join(missing) + ".\n"
                "Copia `.env.example` a `.env` y rellena esos valores.\n"
                "Se obtienen gratis en https://developer.adzuna.com/"
            )
        # El type checker no sabe que `missing` vacío implica no-None; el
        # `assert` lo documenta y falla ruidosamente si algún día no se cumple.
        assert self.adzuna_app_id and self.adzuna_app_key
        return self.adzuna_app_id, self.adzuna_app_key

    def require_jooble(self, env_name: str) -> str:
        value = os.getenv(env_name)
        if not value:
            raise MissingCredentialsError(
                f"Falta la API key de Jooble en la variable {env_name}.\n"
                "Recuerda que Jooble emite una key DISTINTA por país/dominio:\n"
                "  JOOBLE_API_KEY_MX  -> mx.jooble.org\n"
                "  JOOBLE_API_KEY_US  -> jooble.org\n"
                "Copia `.env.example` a `.env` y rellena ese valor."
            )
        return value

    @staticmethod
    def fingerprint(secret: str) -> str:
        """Identificador corto y no reversible de una key.

        Sirve para detectar que se cambió de key (y por tanto que el contador
        de cuota ya no corresponde) sin escribir nunca la key en disco.
        """
        import hashlib

        return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
#  Lectura del YAML
# ---------------------------------------------------------------------------

def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    """Lee una clave obligatoria o muere con un mensaje útil."""
    if key not in mapping:
        raise ConfigError(f"Falta la clave obligatoria '{key}' en {where}.")
    return mapping[key]


def _optional_int(value: Any) -> int | None:
    """`null` en YAML llega como None en Python. Aquí significa "sin tope
    conocido": se cuenta el consumo pero no se impone un límite inventado.
    """
    if value is None or value == "":
        return None
    return int(value)


def _resolve_path(raw: str) -> Path:
    """Las rutas del YAML son relativas a la raíz del proyecto, no al
    directorio desde el que se lanza el comando. Así `python -m recolector`
    funciona igual desde cualquier carpeta.
    """
    path = Path(raw)
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def _parse_geo_filter(data: dict[str, Any], where: str) -> GeoFilter:
    mode = _require(data, "mode", where)
    if mode not in ("none", "allow_regions"):
        raise ConfigError(
            f"geo_filter.mode = '{mode}' no es válido en {where}. "
            "Valores admitidos: 'none', 'allow_regions'."
        )
    policy = data.get("unresolved_policy", "drop")
    if policy not in ("drop", "keep"):
        raise ConfigError(
            f"geo_filter.unresolved_policy = '{policy}' no es válido en {where}. "
            "Valores admitidos: 'drop', 'keep'."
        )
    regions = [str(r) for r in data.get("regions", [])]
    cities = [str(c) for c in data.get("cities", [])]
    zonas = _parse_zonas(data.get("zonas") or [], f"{where}.zonas")

    if zonas:
        # Una zona sin región que la encierre volvería a aceptar «Brownsville,
        # Tennessee» por el nombre de la ciudad, que es justo lo que las zonas
        # existen para impedir.
        if mode != "allow_regions" or not regions:
            raise ConfigError(
                f"{where}: las zonas necesitan `mode: allow_regions` y al menos "
                "una región en `regions` (la región es lo que impide aceptar una "
                "ciudad homónima de otro estado)."
            )
        # Con zonas, una ciudad suelta aceptaría ofertas que no caen en
        # ninguna zona y que la bandeja no sabría dónde enseñar.
        if cities:
            raise ConfigError(
                f"{where}: con zonas, las ciudades van DENTRO de cada zona "
                "(`zonas[].ciudades`), no en `cities`."
            )

    return GeoFilter(
        mode=mode,
        regions=regions,
        cities=cities,
        unresolved_policy=policy,
        zonas=zonas,
    )


# `todas` es lo que manda la pantalla para decir «sin filtro de zona».
_CLAVES_DE_ZONA_RESERVADAS = frozenset({"todas"})


def _parse_zonas(data: list[Any], where: str) -> list[Zona]:
    """Las zonas de un mercado, validadas.

    SE NIEGA A ARRANCAR ANTE UN CONDADO REPETIDO, y no es celo: con el
    condado en dos zonas, la oferta cae en la primera que se mire y la
    segunda enseña un recuento que no incluye algo que el YAML dice que es
    suyo. Ese fallo no da ningún síntoma. Lo mismo con una ciudad.
    """
    if not isinstance(data, list):
        raise ConfigError(f"{where} tiene que ser una lista de zonas.")

    zonas: list[Zona] = []
    vistos_condados: dict[str, str] = {}
    vistas_ciudades: dict[str, str] = {}
    for i, bloque in enumerate(data):
        sitio = f"{where}[{i}]"
        if not isinstance(bloque, dict):
            raise ConfigError(f"{sitio} tiene que ser un bloque con `clave` y `etiqueta`.")
        clave = str(_require(bloque, "clave", sitio)).strip()
        if (not clave or clave in _CLAVES_DE_ZONA_RESERVADAS
                or not all(c.isascii() and (c.isalnum() or c == "_") for c in clave)
                or clave != clave.lower()):
            raise ConfigError(
                f"{sitio}.clave = '{clave}' no vale: minúsculas, números y «_», "
                f"y no puede ser {', '.join(sorted(_CLAVES_DE_ZONA_RESERVADAS))}."
            )
        if any(z.clave == clave for z in zonas):
            raise ConfigError(f"{sitio}: la clave '{clave}' ya la usa otra zona.")

        condados = [str(c) for c in bloque.get("condados") or []]
        ciudades = [str(c) for c in bloque.get("ciudades") or []]
        if not condados and not ciudades:
            raise ConfigError(f"{sitio} ('{clave}') no tiene ni condados ni ciudades.")

        for nombre, vistos, que in (
            *((c, vistos_condados, "condado") for c in condados),
            *((c, vistas_ciudades, "ciudad") for c in ciudades),
        ):
            normal = normalize_text(nombre)
            if not normal:
                raise ConfigError(f"{sitio}: hay un {que} vacío.")
            if normal in vistos:
                raise ConfigError(
                    f"{sitio}: el {que} «{nombre}» ya está en la zona "
                    f"'{vistos[normal]}'. Cada {que} va en una sola zona."
                )
            vistos[normal] = clave

        zonas.append(Zona(
            clave=clave,
            etiqueta=str(_require(bloque, "etiqueta", sitio)),
            descripcion=str(bloque.get("descripcion") or ""),
            condados=condados,
            ciudades=ciudades,
        ))
    return zonas


def _parse_market(key: str, data: dict[str, Any], where: str) -> Market:
    market_where = f"{where} -> markets.{key}"
    adzuna_data = _require(data, "adzuna", market_where)
    jooble_data = _require(data, "jooble", market_where)
    return Market(
        key=key,
        label=str(_require(data, "label", market_where)),
        country_code=str(_require(data, "country_code", market_where)),
        adzuna=AdzunaMarketConfig(
            country=str(_require(adzuna_data, "country", f"{market_where}.adzuna")),
            where=str(adzuna_data.get("where", "")),
        ),
        jooble=JoobleMarketConfig(
            api_key_env=str(_require(jooble_data, "api_key_env", f"{market_where}.jooble")),
            host=str(_require(jooble_data, "host", f"{market_where}.jooble")),
            location=str(jooble_data.get("location", "")),
        ),
        geo_filter=_parse_geo_filter(
            _require(data, "geo_filter", market_where), f"{market_where}.geo_filter"
        ),
    )


def _parse_salary_floor(data: dict[str, Any] | None, where: str) -> SalaryFloor | None:
    """El umbral salarial es opcional: si no está el bloque, no hay umbral."""
    if not data:
        return None

    mode = str(data.get("mode", "report"))
    if mode not in ("report", "filter"):
        raise ConfigError(
            f"{where}.mode = '{mode}' no es válido. Valores admitidos: 'report', 'filter'.\n"
            "  report -> solo clasifica y cuenta (recomendado para la v0)\n"
            "  filter -> descarta de verdad"
        )

    on_missing = str(data.get("on_missing_salary", "keep"))
    if on_missing not in ("keep", "drop"):
        raise ConfigError(
            f"{where}.on_missing_salary = '{on_missing}' no es válido. Admitidos: 'keep', 'drop'."
        )

    period = str(data.get("period", "year"))
    if period != "year":
        raise ConfigError(
            f"{where}.period = '{period}' no está soportado. En esta v0 solo se maneja 'year'.\n"
            "Si una fuente empezara a devolver salarios mensuales, la conversión debe hacerse "
            "en su conector, no aquí."
        )

    currency = str(_require(data, "currency", where)).upper()
    fx_raw = data.get("fx_to_usd", {}) or {}
    fx_to_usd = {str(k).upper(): float(v) for k, v in fx_raw.items()}

    if currency not in fx_to_usd:
        raise ConfigError(
            f"{where}: la divisa del umbral ({currency}) no está en la tabla `fx_to_usd`. "
            f"Divisas declaradas: {', '.join(sorted(fx_to_usd)) or '(ninguna)'}"
        )

    return SalaryFloor(
        mode=mode,
        amount=float(_require(data, "amount", where)),
        currency=currency,
        period=period,
        on_missing_salary=on_missing,
        use_range_top=bool(data.get("use_range_top", True)),
        fx_to_usd=fx_to_usd,
        fx_updated=str(data.get("fx_updated", "(sin fecha)")),
    )


def _parse_llm_budget(data: dict[str, Any] | None, where: str) -> LlmBudgetConfig:
    """El bloque `llm:` es opcional, pero su AUSENCIA no significa 'sin techo'.

    Si no está, se aplica un techo por defecto de $10 al mes. Un proyecto que
    gasta dinero de otra persona no puede tener como comportamiento por
    defecto «gasta lo que haga falta»: el fallo por defecto tiene que ser el
    seguro, no el caro.
    """
    data = data or {}

    modo = str(data.get("al_agotarse", "parar"))
    if modo not in ("parar", "avisar"):
        raise ConfigError(
            f"{where}.al_agotarse = '{modo}' no es válido. "
            "Valores admitidos: 'parar' (deja de gastar al llegar al techo) o "
            "'avisar' (sigue gastando y solo advierte)."
        )

    techo = float(data.get("presupuesto_mensual_usd", 10.0))
    if techo <= 0:
        raise ConfigError(
            f"{where}.presupuesto_mensual_usd debe ser mayor que 0. "
            "Para no clasificar nada, no ejecutes el clasificador."
        )

    # LA CLAVE VIEJA NO SE IGNORA EN SILENCIO.
    #
    # `reserva_manual_usd` decía «lo que las automáticas NO pueden tocar» y
    # ahora se escribe al derecho: `racion_automatica_mensual_usd`, lo que SÍ
    # pueden gastar. Traducirla sola sería adivinar (¿2 significa una ración de
    # 2 o de 10?), y dejarla pasar sin decir nada dejaría a las automáticas con
    # la ración por defecto sin que nadie se entere. Se falla y se dice la
    # cuenta exacta.
    if "reserva_manual_usd" in data:
        vieja = float(data["reserva_manual_usd"])
        raise ConfigError(
            f"{where}.reserva_manual_usd ya no existe. Decía cuánto NO podían "
            "tocar las corridas automáticas; ahora se escribe lo que SÍ pueden "
            "gastar, que es lo que antes quedaba implícito.\n"
            f"  Cámbialo por:  racion_automatica_mensual_usd: "
            f"{max(0.0, techo - vieja):.2f}\n"
            f"  (era {techo:.2f} de techo menos {vieja:.2f} de reserva)"
        )

    racion = float(data.get("racion_automatica_mensual_usd", techo))
    if racion <= 0:
        raise ConfigError(
            f"{where}.racion_automatica_mensual_usd debe ser mayor que 0. Las "
            "corridas automáticas no harían nada y no se notaría por qué; para "
            "apagarlas, no las programes."
        )
    if racion > techo:
        raise ConfigError(
            f"{where}.racion_automatica_mensual_usd ({racion}) es mayor que el "
            f"techo mensual de referencia ({techo}). Las automáticas correrían "
            "con más margen que el número que se le enseña a la clienta en la "
            "barra de gasto, que es justo al revés de lo que se pretende."
        )

    # EL FRENO DE VERDAD. Ver `LlmBudgetConfig`.
    #
    # El defecto es un número y no «sin tope» por lo mismo que el techo
    # mensual: un proyecto que gasta dinero de otra persona no puede tener
    # «gasta lo que haga falta» como comportamiento por omisión.
    tope_accion = float(data.get("tope_por_accion_usd", 0.60))
    if tope_accion <= 0:
        raise ConfigError(
            f"{where}.tope_por_accion_usd debe ser mayor que 0. Es el freno que "
            "se le promete a la clienta; para quitarlo hay que subirlo, no "
            "ponerlo a cero."
        )
    if tope_accion > techo:
        raise ConfigError(
            f"{where}.tope_por_accion_usd ({tope_accion}) es mayor que el techo "
            f"mensual ({techo}): una sola acción podría gastar el mes entero y "
            "el tope no frenaría nunca. Baja el tope o sube el techo."
        )

    esfuerzo_ranking = data.get("esfuerzo_ranking", "medium")
    if esfuerzo_ranking is not None:
        esfuerzo_ranking = str(esfuerzo_ranking)
        if esfuerzo_ranking not in ("low", "medium", "high", "xhigh", "max"):
            raise ConfigError(
                f"{where}.esfuerzo_ranking = '{esfuerzo_ranking}' no es válido. "
                "Valores admitidos: low, medium, high, xhigh, max, o vacío."
            )

    # El techo del lote del ranking. `None` (o ausente) = sin techo propio, que
    # es como corrió el proyecto hasta el 4 septiembre 2026. A cero no: apagar
    # el ranking se hace no pulsándolo, y un cero aquí sería un ranking vacío
    # que parece que ordenó.
    max_ranking = data.get("max_ofertas_por_ranking")
    if max_ranking is not None:
        max_ranking = int(max_ranking)
        if max_ranking < 1:
            raise ConfigError(
                f"{where}.max_ofertas_por_ranking = {max_ranking}. Tiene que ser "
                "1 o más, o estar vacío para no poner techo. Un cero daría un "
                "orden sin ninguna oferta, que se lee como que ya está todo "
                "revisado."
            )

    pct = int(data.get("avisar_al_superar_pct", 75))
    if not 1 <= pct <= 100:
        raise ConfigError(f"{where}.avisar_al_superar_pct debe estar entre 1 y 100.")

    return LlmBudgetConfig(
        presupuesto_mensual_usd=techo,
        al_agotarse=modo,
        racion_automatica_mensual_usd=racion,
        avisar_al_superar_pct=pct,
        tope_por_accion_usd=tope_accion,
        esfuerzo_ranking=esfuerzo_ranking,
        max_ofertas_por_ranking=max_ranking,
        triaje=_parse_triaje(data.get("triaje"), f"{where}.triaje"),
    )


def _parse_triaje(data: dict[str, Any] | None, where: str) -> TriajeConfig:
    """El bloque `llm.triaje:` es opcional. Sin él, se lee todo.

    El defecto es APAGADO y no un número, a propósito: encendido, el triaje
    decide a cuántas ofertas se les presta atención completa, y eso no puede
    empezar a pasar porque alguien copiara un YAML sin leerlo. Que cueste más
    de la cuenta se ve en la factura; que se lea de menos, no se ve.
    """
    data = data or {}
    activo = bool(data.get("activo", False))

    esfuerzo = data.get("esfuerzo", "medium")
    if esfuerzo is not None:
        esfuerzo = str(esfuerzo)
        if esfuerzo not in ("low", "medium", "high", "xhigh", "max"):
            raise ConfigError(
                f"{where}.esfuerzo = '{esfuerzo}' no es válido. "
                "Valores admitidos: low, medium, high, xhigh, max, o vacío "
                "para dejar el del modelo."
            )

    n = int(data.get("ofertas_por_corrida", 8))
    if n < 1:
        raise ConfigError(
            f"{where}.ofertas_por_corrida debe ser 1 o más. "
            "Para no clasificar nada, pon `activo: false` o no ejecutes el "
            "clasificador: un cero silencioso parecería que no hay ofertas."
        )

    # EL TOPE SÍ TIENE DEFECTO, Y AL REVÉS QUE `activo`.
    #
    # `activo` viene apagado porque encendido decide a cuántas ofertas se les
    # presta atención completa, y eso no puede empezar a pasar por copiar un
    # YAML. Aquí el razonamiento se invierte: SIN tope, un atraso de más de
    # ~228 vacantes hace que la corrida se niegue entera y no se tríe ninguna;
    # CON tope se trían 60 y las demás esperan enteras a la siguiente. El
    # defecto peligroso es no tenerlo.
    tope = int(data.get("max_vacantes_por_triaje", 60))
    if tope < n:
        raise ConfigError(
            f"{where}.max_vacantes_por_triaje = {tope} es menor que "
            f"ofertas_por_corrida = {n}. El tope dice cuántas vacantes MIRA el "
            "triaje y `ofertas_por_corrida` cuántas se LEEN enteras, así que un "
            "tope más bajo haría que se leyeran menos de las pedidas sin decirlo "
            "y que el triaje no llegara a dispararse nunca. Sube el tope o baja "
            "las lecturas."
        )

    return TriajeConfig(
        activo=activo,
        ofertas_por_corrida=n,
        max_vacantes_por_triaje=tope,
        esfuerzo=esfuerzo,
    )


def _parse_descripciones(
    data: dict[str, Any] | None, where: str
) -> DescripcionesConfig:
    """El bloque `descripciones:` es opcional. Sin él, apagado.

    Se valida aunque esté apagado: un YAML con `max_por_corrida: 0` escrito hoy
    tiene que fallar hoy, no el día que alguien ponga `habilitado: true` y se
    encuentre con que no trae ninguna y no entienda por qué.
    """
    data = data or {}

    n = int(data.get("max_por_corrida", 40))
    if n < 1:
        raise ConfigError(
            f"{where}.max_por_corrida debe ser 1 o más. "
            "Para no traer ninguna, pon `habilitado: false`: un cero "
            "silencioso parecería que ya están todas traídas."
        )

    pausa = float(data.get("sleep_seconds_between_calls", 1.5))
    if pausa < 0:
        raise ConfigError(f"{where}.sleep_seconds_between_calls no puede ser negativo.")

    return DescripcionesConfig(
        habilitado=bool(data.get("habilitado", False)),
        max_por_corrida=n,
        sleep_seconds_between_calls=pausa,
        timeout_seconds=int(data.get("timeout_seconds", 30)),
    )


def _parse_profile(key: str, data: dict[str, Any], where: str) -> SearchProfile:
    profile_where = f"{where} -> profiles.{key}"

    raw_combos = _require(data, "keyword_combinations", profile_where)
    if not raw_combos:
        raise ConfigError(f"{profile_where}.keyword_combinations está vacío.")

    combos = _parse_combos(raw_combos, f"{profile_where}.keyword_combinations")

    filters_data = _require(data, "hard_filters", profile_where)
    hard_filters = HardFilters(
        max_days_old=int(_require(filters_data, "max_days_old", f"{profile_where}.hard_filters")),
        exclude_title_terms=[str(t) for t in filters_data.get("exclude_title_terms", [])],
        exclude_companies=[str(c) for c in filters_data.get("exclude_companies", [])],
        require_all_terms_in_text=bool(filters_data.get("require_all_terms_in_text", False)),
        include_title_terms=[str(t) for t in filters_data.get("include_title_terms", [])],
        salary_floor=_parse_salary_floor(
            filters_data.get("salary_floor"), f"{profile_where}.hard_filters.salary_floor"
        ),
    )

    market_keys = [str(m) for m in _require(data, "markets", profile_where)]
    if not market_keys:
        raise ConfigError(f"{profile_where}.markets está vacío.")

    combinations_by_market = _parse_combos_por_mercado(
        data.get("combinaciones_por_mercado"), combos, market_keys, profile_where
    )

    return SearchProfile(
        key=key,
        label=str(data.get("label", key)),
        description=str(data.get("description", "")).strip(),
        keyword_combinations=combos,
        market_keys=market_keys,
        hard_filters=hard_filters,
        combinations_by_market=combinations_by_market,
    )


def _parse_combos(raw: Any, where: str) -> list[KeywordCombo]:
    """Una lista de listas de términos -> combinaciones."""
    if not isinstance(raw, list):
        raise ConfigError(f"{where} debe ser una lista de combinaciones. Se recibió: {raw!r}")
    salida: list[KeywordCombo] = []
    for index, item in enumerate(raw):
        if not isinstance(item, list) or not item:
            raise ConfigError(
                f"{where}[{index}] debe ser una lista de términos, p. ej. "
                f"[\"COO\", \"Logistics\"]. Se recibió: {item!r}"
            )
        salida.append(KeywordCombo(terms=tuple(str(t).strip() for t in item)))
    return salida


def _parse_combos_por_mercado(
    data: Any,
    generales: list[KeywordCombo],
    market_keys: list[str],
    profile_where: str,
) -> dict[str, list[KeywordCombo]]:
    """Resuelve qué combinaciones se preguntan en cada mercado.

    POR QUÉ EXISTE ESTO, Y NO ES UNA COMODIDAD.
    -------------------------------------------
    Hasta el 17 septiembre 2026 las combinaciones eran del PERFIL y se lanzaban
    en todos sus mercados. Con dos mercados que se buscan en inglés eso estaba
    bien. Con México dentro deja de estarlo, porque cada petición de más sale de
    una cuota mensual de 2.500 que además se comparte con la máquina de la
    clienta:

      · las 10 combinaciones en español dan CERO en Texas —medido, siempre— y
        en Alemania se busca en inglés: 20 peticiones por corrida tiradas;
      · las 2 que llevan «Mexico» dentro valen en Texas (4 relevantes medidas)
        y en el índice mexicano son ruido: buscan una palabra que está en todos
        los anuncios del país, devuelven 1.068 y 282 resultados y entre las dos
        aportan 3 anuncios, los tres basura.

    Medido el 17 septiembre 2026: 23 × 3 son 69 peticiones por corrida (2.070 al
    mes, el 83% de la cuota); repartidas son 47 (1.410, el 56%).

    CÓMO SE ESCRIBE, Y POR QUÉ NO SE REPITE LA LISTA ENTERA.
    -------------------------------------------------------
    Se declara la DIFERENCIA contra la lista general, no una lista completa por
    mercado. Copiar las 13 en cada bloque garantizaba que un día se edite una y
    se olvide la otra, y ese fallo no da ningún síntoma: simplemente se deja de
    preguntar algo y no se nota nunca.

        combinaciones_por_mercado:
          mexico:
            ademas: [["Director de Operaciones", "Manufactura"], ...]
            menos:  [["Managing Director", "Mexico"], ...]

    SE NIEGA ANTE UNA ERRATA, en vez de seguir. Los tres errores posibles
    —nombrar un mercado que no está en el perfil, quitar algo que no está en la
    lista general, o añadir algo que ya está— cuestan cobertura o cuota y
    ninguno se vería en pantalla. Ver `tests/test_combinaciones_por_mercado.py`.
    """
    resuelto: dict[str, list[KeywordCombo]] = {k: list(generales) for k in market_keys}
    if data is None:
        return resuelto

    where = f"{profile_where}.combinaciones_por_mercado"
    if not isinstance(data, dict):
        raise ConfigError(
            f"{where} debe ser un diccionario de mercado -> ajustes. Se recibió: {data!r}"
        )

    for market_key, ajustes in data.items():
        clave = str(market_key)
        if clave not in market_keys:
            raise ConfigError(
                f"{where}.{clave} nombra un mercado que este perfil no usa.\n"
                f"Mercados del perfil: {', '.join(market_keys)}.\n"
                "O se escribió mal, o falta añadirlo a `markets`. Se para aquí "
                "porque un ajuste que no se aplica a nadie no da ningún aviso."
            )
        if not isinstance(ajustes, dict):
            raise ConfigError(
                f"{where}.{clave} debe ser un diccionario con `ademas` y/o `menos`. "
                f"Se recibió: {ajustes!r}"
            )
        desconocidas = set(ajustes) - {"ademas", "menos"}
        if desconocidas:
            raise ConfigError(
                f"{where}.{clave} tiene claves que no existen: "
                f"{', '.join(sorted(desconocidas))}. Solo se admiten `ademas` y `menos`."
            )

        generales_set = set(generales)
        combos_menos = _parse_combos(ajustes.get("menos", []), f"{where}.{clave}.menos")
        for combo in combos_menos:
            if combo not in generales_set:
                raise ConfigError(
                    f"{where}.{clave}.menos quiere quitar «{combo.label}», que no está "
                    f"en `keyword_combinations`.\n"
                    "Quitar algo que no está no hace nada, y parece que sí. "
                    "Revisa cómo está escrita la combinación en la lista general."
                )

        combos_ademas = _parse_combos(ajustes.get("ademas", []), f"{where}.{clave}.ademas")
        for combo in combos_ademas:
            if combo in generales_set:
                raise ConfigError(
                    f"{where}.{clave}.ademas añade «{combo.label}», que YA está en "
                    f"`keyword_combinations`.\n"
                    "Se preguntaría dos veces y se pagarían dos peticiones por lo "
                    "mismo. Si la quieres solo en este mercado, sácala de la lista "
                    "general y déjala aquí."
                )
        repetidas = [c for c in combos_ademas if combos_ademas.count(c) > 1]
        if repetidas:
            raise ConfigError(
                f"{where}.{clave}.ademas repite «{repetidas[0].label}». "
                "Cada combinación cuesta una petición: duplicarla cuesta dos."
            )

        quitar = set(combos_menos)
        final = [c for c in generales if c not in quitar] + combos_ademas
        if not final:
            raise ConfigError(
                f"{where}.{clave} deja el mercado sin ninguna combinación que "
                "preguntar. Si la idea era apagarlo, quítalo de `markets`."
            )
        resuelto[clave] = final

    return resuelto


def load_config(path: Path | None = None) -> AppConfig:
    """Lee y valida el archivo de configuración.

    Carga `.env` de paso. Los dos comandos del proyecto pasan por aquí, así
    que es el único sitio donde ponerlo garantiza que ninguno se lo salte
    —que es exactamente lo que le pasó al clasificador—. Ver `cargar_env`.
    """
    cargar_env()
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(
            f"No encuentro el archivo de configuración: {config_path}\n"
            "Se esperaba en config/search_profile.yaml"
        )

    # encoding='utf-8' explícito: en Windows el encoding por defecto es cp1252
    # y el archivo tiene acentos. Sin esto, revienta o lee basura.
    with open(config_path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    if not isinstance(data, dict):
        raise ConfigError(f"{config_path} no contiene un mapa YAML válido.")

    where = str(config_path)

    markets_data = _require(data, "markets", where)
    markets = {key: _parse_market(key, value, where) for key, value in markets_data.items()}

    profiles_data = _require(data, "profiles", where)
    profiles = {key: _parse_profile(key, value, where) for key, value in profiles_data.items()}

    active_profile_key = str(_require(data, "active_profile", where))
    if active_profile_key not in profiles:
        raise ConfigError(
            f"active_profile = '{active_profile_key}' pero ese perfil no está definido. "
            f"Perfiles: {', '.join(sorted(profiles))}"
        )

    # Coherencia cruzada: cada perfil solo puede apuntar a mercados existentes.
    for profile in profiles.values():
        for market_key in profile.market_keys:
            if market_key not in markets:
                raise ConfigError(
                    f"El perfil '{profile.key}' referencia el mercado '{market_key}', "
                    f"que no está definido en `markets`. "
                    f"Mercados definidos: {', '.join(sorted(markets))}"
                )

    sources_data = _require(data, "sources", where)
    adzuna_data = _require(sources_data, "adzuna", f"{where} -> sources")
    jooble_data = _require(sources_data, "jooble", f"{where} -> sources")

    adzuna = AdzunaSourceConfig(
        enabled=bool(adzuna_data.get("enabled", True)),
        results_per_page=int(adzuna_data.get("results_per_page", 50)),
        max_pages=int(adzuna_data.get("max_pages", 1)),
        sleep_seconds_between_calls=float(adzuna_data.get("sleep_seconds_between_calls", 1.0)),
        timeout_seconds=int(adzuna_data.get("timeout_seconds", 30)),
        daily_call_budget=_optional_int(adzuna_data.get("daily_call_budget")),
        weekly_call_budget=_optional_int(adzuna_data.get("weekly_call_budget")),
        monthly_call_budget=_optional_int(adzuna_data.get("monthly_call_budget")),
        warn_daily_above=int(adzuna_data.get("warn_daily_above", 200)),
        max_calls_per_minute=_optional_int(adzuna_data.get("max_calls_per_minute")),
    )

    # EL RITMO SE VALIDA AL ARRANCAR, no se descubre con un 429 a mitad de
    # corrida. Si el plan admite 25 peticiones por minuto, la pausa entre
    # peticiones no puede bajar de 60/25 = 2,4 segundos. Los dos números viven
    # en el mismo bloque del YAML y los edita la misma persona, así que
    # desincronizarlos es fácil y no da ningún síntoma hasta que la corrida se
    # cae por la mitad — el mismo tipo de fallo silencioso que vigila
    # `tests/test_lista_blanca.py` entre los dos archivos de configuración.
    if adzuna.max_calls_per_minute:
        minimo = 60.0 / adzuna.max_calls_per_minute
        if adzuna.sleep_seconds_between_calls < minimo:
            raise ConfigError(
                f"{where} -> sources.adzuna: con max_calls_per_minute="
                f"{adzuna.max_calls_per_minute}, sleep_seconds_between_calls "
                f"tiene que ser al menos {minimo:.2f} y vale "
                f"{adzuna.sleep_seconds_between_calls}. Tal como está, una "
                "corrida se comería el límite por minuto y devolvería HTTP 429 "
                "a mitad de faena."
            )
    jooble = JoobleSourceConfig(
        enabled=bool(jooble_data.get("enabled", True)),
        lifetime_request_budget=int(jooble_data.get("lifetime_request_budget", 500)),
        warn_when_remaining_below=int(jooble_data.get("warn_when_remaining_below", 100)),
        refuse_when_remaining_below=int(jooble_data.get("refuse_when_remaining_below", 20)),
        max_pages=int(jooble_data.get("max_pages", 1)),
        sleep_seconds_between_calls=float(jooble_data.get("sleep_seconds_between_calls", 1.0)),
        timeout_seconds=int(jooble_data.get("timeout_seconds", 30)),
    )

    if adzuna.results_per_page > 50:
        raise ConfigError(
            "sources.adzuna.results_per_page no puede pasar de 50: es el máximo que "
            "admite la API de Adzuna."
        )

    llm = _parse_llm_budget(data.get("llm"), f"{where} -> llm")

    storage_data = _require(data, "storage", where)
    cache_data = data.get("cache", {})

    return AppConfig(
        profiles=profiles,
        active_profile_key=active_profile_key,
        markets=markets,
        adzuna=adzuna,
        jooble=jooble,
        llm=llm,
        database_path=_resolve_path(str(_require(storage_data, "database_path", f"{where} -> storage"))),
        cache=CacheConfig(
            enabled=bool(cache_data.get("enabled", True)),
            directory=_resolve_path(str(cache_data.get("directory", "data/cache"))),
            ttl_hours=float(cache_data.get("ttl_hours", 12)),
        ),
        descripciones=_parse_descripciones(
            data.get("descripciones"), f"{where} -> descripciones"
        ),
        config_path=config_path,
    )
