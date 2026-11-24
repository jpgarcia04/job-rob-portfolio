"""
Persistencia en SQLite.

Cuatro tablas, y ninguna de más:

  runs           una fila por corrida, con sus estadísticas en JSON
  offers         una fila por oferta ÚNICA (clave = el hash de deduplicación)
  offer_sources  por qué fuentes llegó cada oferta  -> responde "¿aporta Jooble?"
  offer_queries  qué búsqueda la encontró           -> responde "¿qué combo funciona?"

Las dos últimas son las que convierten esta sonda en una respuesta de negocio
en lugar de un montón de filas. Sin `offer_sources` no se puede saber cuántas
ofertas aporta Jooble EN EXCLUSIVA, que es la pregunta 2 del encargo.

`status` existe porque el producto final la necesitará, pero en esta v0 es una
columna con un valor por defecto, no una máquina de estados. No se construye
lo que todavía no se usa.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import JobOffer

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    profile      TEXT NOT NULL,
    sources      TEXT NOT NULL,
    markets      TEXT NOT NULL,
    stats_json   TEXT
);

CREATE TABLE IF NOT EXISTS offers (
    dedupe_key          TEXT PRIMARY KEY,
    loose_key           TEXT NOT NULL,
    title               TEXT NOT NULL,
    company             TEXT NOT NULL,
    location_raw        TEXT,
    location_city       TEXT,
    location_region     TEXT,
    location_country    TEXT,
    market_key          TEXT NOT NULL,
    salary_min          REAL,
    salary_max          REAL,
    salary_currency     TEXT,
    salary_raw          TEXT,
    salary_is_predicted INTEGER NOT NULL DEFAULT 0,
    posted_at           TEXT,
    canonical_url       TEXT,
    -- Lo que devuelve la API. El plan Trial la corta a 500 caracteres: 367 de
    -- las 381 ofertas de la base miden exactamente eso.
    description         TEXT,
    -- La descripción ENTERA, traída de la página pública. Va en columna aparte
    -- y NO pisa a `description`, por tres razones: se puede comparar una con
    -- otra sin volver a pedir nada, se puede apagar la función y volver al
    -- comportamiento de antes sin perder datos, y el día que el extractor se
    -- rompa se ve cuál de las dos cambió. Ver
    -- `connectors/adzuna_descripcion.py`.
    description_full    TEXT,
    -- POR DÓNDE se sacó: "json-ld", "html" o "nada". No es telemetría: es la
    -- regla nº 1 de la casa. «no se pudo traer» y «se trajo del HTML visible,
    -- que es la vía frágil» son finales distintos, y el día que Adzuna cambie
    -- la página el reparto entre las dos vías se mueve ANTES de que nada se
    -- rompa del todo. Un `NULL` aquí con `description_full` vacía significa
    -- «se intentó y no salió», que no es lo mismo que «no se ha intentado».
    description_full_via TEXT,
    description_full_at  TEXT,
    status              TEXT NOT NULL DEFAULT 'nueva',
    status_changed_at   TEXT,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    first_seen_run      TEXT,
    -- La zona del mercado en la que cae (24 septiembre 2026). Solo la tienen
    -- los mercados con `geo_filter.zonas` —hoy Texas—; en los demás es NULL.
    -- La pone el filtro geográfico al recolectar y `recorte.py` sobre lo que
    -- ya estaba guardado. Es lo que filtra la fila de zonas de la bandeja.
    zona                TEXT
);

-- Estados posibles de `status`, de momento tres:
--   nueva       sin revisar todavía
--   interesa    ella la quiere; en la v1 pasará a ser el paso previo a postular
--   descartada  revisada y rechazada
-- No hay máquina de estados ni transiciones prohibidas: con tres estados y
-- una sola usuaria, sería complejidad sin beneficio.

CREATE INDEX IF NOT EXISTS idx_offers_loose  ON offers (loose_key);
CREATE INDEX IF NOT EXISTS idx_offers_market ON offers (market_key);
CREATE INDEX IF NOT EXISTS idx_offers_run    ON offers (first_seen_run);

CREATE TABLE IF NOT EXISTS offer_sources (
    dedupe_key    TEXT NOT NULL,
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    market_key    TEXT NOT NULL,
    url           TEXT,
    posted_at     TEXT,
    raw_json      TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY (dedupe_key, source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_offer_sources_key ON offer_sources (dedupe_key);

CREATE TABLE IF NOT EXISTS offer_queries (
    dedupe_key  TEXT NOT NULL,
    query_label TEXT NOT NULL,
    market_key  TEXT NOT NULL,
    source      TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    PRIMARY KEY (dedupe_key, query_label, market_key, source, run_id)
);

CREATE INDEX IF NOT EXISTS idx_offer_queries_run ON offer_queries (run_id);

-- Una fila por oferta clasificada. La clave primaria es el MISMO hash de
-- deduplicación que usa `offers`, y eso es lo que convierte esta tabla en la
-- caché: si hay fila, no se vuelve a llamar al LLM. Sin esto, cada corrida
-- del recolector reclasificaría todo el histórico y la factura crecería sola.
--
-- `estado` distingue los dos finales posibles:
--   ok               el modelo respondió y la respuesta encajó con el esquema
--   sin_clasificar   algo falló; el motivo está en `error`
-- Nunca hay un tercer caso "clasificación por defecto": una decisión
-- inventada sería peor que no tener ninguna.
--
-- `fichas_huella` es el SHA-256 corto de config/cvs.yaml. Cuando cambia,
-- estas filas quedan OBSOLETAS: se avisa en consola, pero no se recalculan
-- solas. Reclasificar en silencio es gasto invisible.
CREATE TABLE IF NOT EXISTS clasificaciones (
    dedupe_key          TEXT PRIMARY KEY,
    estado              TEXT NOT NULL,
    relevante           INTEGER,
    motivo_descarte     TEXT,
    confianza           REAL,
    razon               TEXT,
    cv_id               TEXT,
    idioma_anuncio      TEXT,
    idioma_detectado    TEXT,
    empresa_normalizada TEXT,
    puesto_canonico     TEXT,
    es_reclutadora      INTEGER,
    seniority_detectado TEXT,
    modalidad           TEXT,
    necesita_criterio   INTEGER NOT NULL DEFAULT 0,
    motivo_criterio     TEXT,
    error               TEXT,
    modelo              TEXT NOT NULL,
    tokens_entrada      INTEGER NOT NULL DEFAULT 0,
    tokens_salida       INTEGER NOT NULL DEFAULT 0,
    coste_usd           REAL NOT NULL DEFAULT 0,
    fichas_huella       TEXT NOT NULL,
    clasificado_at      TEXT NOT NULL,
    -- Cuando cinco anuncios son la MISMA vacante (misma empresa, mismo
    -- título, distinta ciudad), se clasifica uno y los otros cuatro copian su
    -- veredicto sin gastar una llamada. Aquí queda la `dedupe_key` del que
    -- sí se pagó, para que la copia no parezca una clasificación propia.
    -- NULL = esta fila se clasificó de verdad.
    heredada_de         TEXT,
    -- CON QUÉ TEXTO SE JUZGÓ. Sin esto, un veredicto hecho con el recorte de
    -- 500 caracteres y otro hecho con el anuncio entero son indistinguibles
    -- en la base y en la pantalla, y el primero envejeció en cuanto llegó el
    -- segundo. Es el mismo razonamiento que `fichas_huella`: cambió una
    -- entrada, el veredicto caducó.
    --
    -- Son dos columnas y cada una tiene su trabajo:
    --   `descripcion_via`        'entera' | 'recorte' — el SIGNIFICADO, que
    --                            es lo que la pantalla puede rotular y lo que
    --                            decide si hay que releer.
    --   `caracteres_descripcion` cuánto texto entró — la MEDIDA, que es lo
    --                            que permite reconstruir qué predijo el
    --                            estimador en esta fila. Sin ella no se puede
    --                            comparar previsto contra medido, porque el
    --                            texto de hoy puede no ser el de entonces.
    --
    -- NULL en las dos = fila escrita antes de que existieran. No se rellenan
    -- adivinando: ver `Storage.claves_juzgadas_a_ciegas`.
    descripcion_via        TEXT,
    caracteres_descripcion INTEGER
);

CREATE INDEX IF NOT EXISTS idx_clas_cv     ON clasificaciones (cv_id);
CREATE INDEX IF NOT EXISTS idx_clas_huella ON clasificaciones (fichas_huella);
-- Índice del agrupador: "dame todas las ofertas que son esta misma vacante".
CREATE INDEX IF NOT EXISTS idx_clas_grupo  ON clasificaciones (empresa_normalizada, puesto_canonico);

-- El ranking del asistente. Una fila por CORRIDA, no por oferta: ordenar el
-- lote entero es una sola llamada. Ver clasificador/asistente.py.
--
-- No es una caché, a diferencia de `clasificaciones`. Un ranking envejece: en
-- cuanto entran ofertas nuevas, el orden anterior ya no es el bueno. Se
-- guardan todos y vale el último; los viejos se quedan para poder mirar atrás.
CREATE TABLE IF NOT EXISTS rankings (
    id                   TEXT PRIMARY KEY,
    creado_at            TEXT NOT NULL,
    modelo               TEXT NOT NULL,
    lectura              TEXT NOT NULL,
    patrones             TEXT NOT NULL DEFAULT '[]',   -- JSON
    ofertas_consideradas INTEGER NOT NULL DEFAULT 0,
    -- Cuántas HABÍA sin revisar cuando se ordenó. Si es mayor que
    -- `ofertas_consideradas`, el lote se recortó y la pantalla tiene que
    -- decirlo: un orden que no incluye todo lo que ella no ha mirado esconde
    -- ofertas dentro de la pieza que existe para no esconder ninguna.
    ofertas_disponibles  INTEGER NOT NULL DEFAULT 0,
    tokens_entrada       INTEGER NOT NULL DEFAULT 0,
    tokens_salida        INTEGER NOT NULL DEFAULT 0,
    coste_usd            REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ranking_items (
    ranking_id  TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    posicion    INTEGER NOT NULL,
    por_que     TEXT NOT NULL,
    accion      TEXT NOT NULL,
    PRIMARY KEY (ranking_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_rank_items ON ranking_items (ranking_id, posicion);

-- El triaje: qué ofertas merecen que se lean enteras. Ver clasificador/triaje.py.
--
-- UNA FILA POR OFERTA, y se pisa en cada corrida. El triaje mira metadatos
-- (título, empresa, ubicación, sueldo, cuántos anuncios) de TODAS las ofertas
-- pendientes en una sola llamada, y ordena. Solo las N primeras se clasifican.
--
-- ESTA TABLA EXISTE PARA QUE NO SE ESCONDA NADA. Sin ella, una oferta no
-- seleccionada sería indistinguible de una que nadie miró nunca, y la bandeja
-- no podría decir «el asistente la vio y la dejó en el puesto 34 de 60». Esa
-- frase es la diferencia entre aplazar la atención y ocultar una oferta, que
-- es la regla nº 1 de la casa.
--
-- No lleva `fichas_huella`: el triaje no usa las fichas de los CVs, solo los
-- criterios de `preferencias`, y esos no marcan nada como obsoleto (ordenar es
-- barato, reclasificar no).
CREATE TABLE IF NOT EXISTS triajes (
    dedupe_key     TEXT PRIMARY KEY,
    posicion       INTEGER NOT NULL,
    merece_lectura INTEGER NOT NULL DEFAULT 0,
    -- Si entró en el corte de N. Distinto de `merece_lectura`: el modelo puede
    -- decir que sí y quedarse fuera del corte por posición.
    seleccionada   INTEGER NOT NULL DEFAULT 0,
    por_que        TEXT,
    modelo         TEXT NOT NULL,
    triado_at      TEXT NOT NULL,
    -- El triaje se hace POR VACANTE: de los cinco anuncios de la misma vacante
    -- se manda uno al modelo y los otros cuatro heredan su puesto. Aquí queda
    -- la `dedupe_key` del que sí se mandó. NULL = a esta la ordenó el modelo.
    --
    -- SIN ESTA COLUMNA HABÍA QUE ELEGIR ENTRE DOS COSAS MALAS, y hasta el 3
    -- septiembre 2026 estaba elegida la peor. Los hermanos no llevaban fila,
    -- así que `claves_ya_triadas` no los veía y volvían a entrar; como el
    -- representante SÍ estaba excluido, la corrida siguiente ascendía a otro
    -- hermano y la vacante entera se triaba otra vez. Medido sobre la base:
    -- 29 vacantes con más de una fila, 73 filas, 44 huecos del lote gastados
    -- en cosas ya vistas (12,4%). Una misma vacante —un `Senior Vice President
    -- of Development & Construction` publicado en seis ciudades de Texas— pasó
    -- por el modelo en SEIS corridas seguidas, y cada repetición le quitaba el
    -- sitio a una vacante nueva de verdad.
    --
    -- La otra cosa mala era marcar al hermano y ya está: entonces aparecería
    -- en «Sin revisar todavía» con un puesto que ningún modelo le dio, que es
    -- exactamente lo que el tope de lote tiene prohibido hacer. Con la columna
    -- no hay que elegir: el hermano queda marcado —deja de volver a entrar— y
    -- queda marcado DICIENDO DE QUIÉN heredó el puesto.
    --
    -- Es la misma solución que `clasificaciones.heredada_de`, para el mismo
    -- problema una capa más abajo. Que hiciera falta dos veces es la señal de
    -- que agrupar sin dejar rastro de quién pagó no funciona en ninguna capa.
    heredada_de    TEXT
);

CREATE INDEX IF NOT EXISTS idx_triaje_pos ON triajes (seleccionada, posicion);

-- Una fila por LLAMADA de triaje. `triajes` guarda el veredicto de cada oferta;
-- esta guarda lo que costó preguntarlo.
--
-- POR QUÉ HACE FALTA, Y ES LA CAUSA RAÍZ DE TRES ERRORES DE ESTE PROYECTO.
-- La estimación del triaje cobra ~50 tokens de salida por vacante, y ese número
-- llevaba tres sesiones desviándose sin que nadie lo notara. No por descuido:
-- es que no había DÓNDE notarlo. `triajes` no tiene columnas de tokens y
-- `uso_llm.json` solo agrega dólares por día, así que los tokens de la llamada
-- vivían un instante en la salida de consola y desaparecían.
--
-- Pasó otra vez el 3 septiembre 2026, con esta tabla ya decidida y sin escribir:
-- una corrida real de 60 vacantes y el número hubo que reconstruirlo restando
-- el coste de las lecturas (`clasificaciones`) al gasto del día (`uso_llm.json`)
-- para despejar la salida. Salió bien —45,9 tokens por vacante contra los 50
-- estimados— pero eso es arqueología, no medición, y no se puede pedir en cada
-- corrida.
--
-- Se guarda LO PREVISTO al lado de LO MEDIDO a propósito: la pregunta útil no
-- es «cuánto costó» sino «cuánto se desvió de lo que le enseñamos a la clienta»,
-- y esa no se puede responder después si solo se guardó una de las dos.
CREATE TABLE IF NOT EXISTS triaje_corridas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    triado_at       TEXT    NOT NULL,
    -- Vacantes que fueron EN LA LLAMADA, ya agrupadas y ya recortadas por el
    -- tope de lote. Es el denominador de «salida por vacante»: dividir entre
    -- anuncios o entre la cola entera da una constante mal calibrada.
    vacantes        INTEGER NOT NULL,
    fuera_del_tope  INTEGER NOT NULL DEFAULT 0,
    tokens_entrada  INTEGER NOT NULL DEFAULT 0,
    tokens_salida   INTEGER NOT NULL DEFAULT 0,
    coste_usd       REAL    NOT NULL DEFAULT 0,
    salida_prevista INTEGER NOT NULL DEFAULT 0,
    coste_previsto  REAL    NOT NULL DEFAULT 0,
    modelo          TEXT    NOT NULL,
    esfuerzo        TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt_to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class Storage:
    """Envoltorio fino sobre sqlite3. Nada de ORM: para cuatro tablas, SQL."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path)
        # row_factory=sqlite3.Row permite leer las filas por nombre de columna
        # (row["title"]) en vez de por índice. Más legible y menos frágil.
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    # Columnas añadidas después de que alguien ya tuviera una base creada.
    # `CREATE TABLE IF NOT EXISTS` no toca una tabla que ya existe, así que
    # sin esto una base antigua se queda sin la columna nueva y el INSERT
    # falla con "no such column" a mitad de una corrida.
    #
    # Formato: (tabla, columna, tipo). Solo se admiten columnas OPCIONALES
    # (sin NOT NULL): SQLite no puede añadir una obligatoria a una tabla que
    # ya tiene filas sin un valor por defecto.
    _COLUMNAS_AÑADIDAS = (
        ("clasificaciones", "heredada_de", "TEXT"),
        ("clasificaciones", "descripcion_via", "TEXT"),
        ("clasificaciones", "caracteres_descripcion", "INTEGER"),
        ("triajes", "heredada_de", "TEXT"),
        ("offers", "description_full", "TEXT"),
        ("offers", "description_full_via", "TEXT"),
        ("offers", "description_full_at", "TEXT"),
        ("rankings", "ofertas_disponibles", "INTEGER NOT NULL DEFAULT 0"),
        ("offers", "zona", "TEXT"),
    )

    def _initialize(self) -> None:
        # executescript ejecuta varias sentencias separadas por ';'.
        self.connection.executescript(SCHEMA)
        self._migrar_columnas()
        self._derivar_descripcion_via()
        self.connection.commit()

    def _derivar_descripcion_via(self) -> int:
        """Rellena `descripcion_via` y `caracteres_descripcion` en las filas
        escritas antes de que las columnas existieran. Idempotente.

        NO ES ADIVINAR, Y LA DIFERENCIA IMPORTA. La fecha lo decide sin margen:
        `description_full_at` es cuándo llegó el anuncio entero y
        `clasificado_at` cuándo se emitió el veredicto. Si el texto llegó
        DESPUÉS, en el momento de juzgar la columna estaba vacía y
        `motor.descripcion_de` no pudo devolver otra cosa que el recorte. No
        hay tercera posibilidad, así que esto es una derivación y no una
        estimación — que es justo la distinción que este proyecto ya hace
        entre «se intentó y no la tiene» (`description_full_via = 'nada'`) y
        «no se ha mirado» (NULL).

        Los caracteres salen de la columna que se usó, y ninguna de las dos se
        reescribe después de la primera vez, así que la longitud de hoy es la
        de entonces. Si el anuncio entero SE hubiera vuelto a traer, su fecha
        sería posterior al veredicto y la fila caería del lado «recorte», que
        es exactamente lo que se quiere: hay que releerla.

        Se deja NULL lo que no se puede derivar —una oferta sin
        `description_full_at` teniendo texto entero—, porque una procedencia
        equivocada se lee como un dato y es peor que no tenerla.

        LAS HEREDADAS TAMBIÉN SE DERIVAN DE SU PROPIA FECHA, y no copiando la
        del padre como sí hace `motor._heredar` con las filas nuevas. La
        diferencia es que aquí se mira hacia atrás: el padre puede haberse
        releído DESPUÉS, y entonces su vía de hoy describe un veredicto que el
        hijo no tiene. Medido el 3 septiembre 2026 sobre esta base: 10 filas
        heredadas llevaban el veredicto ciego de las 08:52 mientras su padre ya
        tenía el de las 16:47, porque la relectura de la sesión pasada se hizo
        con un script suelto que no propagó a los hermanos. Copiarle la vía al
        padre las habría marcado «entera» siendo ciegas, que es justo el
        veredicto invisible que esta columna viene a hacer visible.

        Su propia `clasificado_at` sí es fiable: es el instante en que se
        escribió ESA copia, y si el anuncio entero no existía entonces, nadie
        pudo leerlo.
        """
        filas = self.connection.execute(
            """
            SELECT c.dedupe_key,
                   LENGTH(TRIM(COALESCE(o.description, '')))      AS n_recorte,
                   LENGTH(TRIM(COALESCE(o.description_full, ''))) AS n_entera,
                   o.description_full_at,
                   c.clasificado_at
              FROM clasificaciones c
              JOIN offers o ON o.dedupe_key = c.dedupe_key
             WHERE c.descripcion_via IS NULL
            """
        ).fetchall()

        actualizadas = 0
        for fila in filas:
            tiene_entera = (fila["n_entera"] or 0) > 0
            if tiene_entera and not fila["description_full_at"]:
                continue  # no derivable: hay texto pero no cuándo llegó
            # ESTRICTAMENTE MAYOR, NO `>=` (corregido el 7 septiembre 2026).
            #
            # Con `>=`, dos marcas iguales se leían como «se clasificó con el
            # anuncio entero», que es afirmar lo que no se sabe: si las dos
            # cosas constan en el mismo instante, no hay evidencia de cuál fue
            # primero. De las dos lecturas posibles, la que no inventa es la de
            # recorte, igual que cuando falta `description_full_at`.
            #
            # No es teórico: `datetime.now()` en Windows avanza a saltos de
            # milisegundos, y la prueba que clasifica y guarda el texto seguido
            # empataba de vez en cuando y se ponía roja sola. Ver
            # `test_el_mismo_instante_no_prueba_que_lo_leyera`.
            if tiene_entera and fila["clasificado_at"] > fila["description_full_at"]:
                via, cuantos = "entera", fila["n_entera"]
            else:
                via, cuantos = "recorte", fila["n_recorte"]
            self.connection.execute(
                "UPDATE clasificaciones SET descripcion_via = ?, "
                "caracteres_descripcion = ? WHERE dedupe_key = ?",
                (via, cuantos, fila["dedupe_key"]),
            )
            actualizadas += 1
        return actualizadas

    def _migrar_columnas(self) -> None:
        """Añade las columnas que falten en una base creada por una versión
        anterior. Es idempotente: si ya están, no hace nada.

        Nota de SQLite: `PRAGMA table_info(t)` devuelve una fila por columna;
        el campo `name` (índice 1) es el nombre. No hay `ALTER TABLE ... ADD
        COLUMN IF NOT EXISTS`, así que hay que mirar antes.
        """
        for tabla, columna, tipo in self._COLUMNAS_AÑADIDAS:
            existentes = {
                fila["name"]
                for fila in self.connection.execute(f"PRAGMA table_info({tabla})")
            }
            if not existentes:
                continue  # la tabla no existe todavía; el SCHEMA ya la creará
            if columna not in existentes:
                self.connection.execute(
                    f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}"
                )

    def close(self) -> None:
        self.connection.close()

    # Soporte para `with Storage(...) as db:` — garantiza el cierre aunque
    # salte una excepción. Es el equivalente a try-with-resources de Java.
    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    # -- corridas ------------------------------------------------------------

    def start_run(self, profile: str, sources: Iterable[str], markets: Iterable[str]) -> str:
        run_id = uuid.uuid4().hex[:12]
        self.connection.execute(
            "INSERT INTO runs (run_id, started_at, profile, sources, markets) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, _now_iso(), profile, ",".join(sources), ",".join(markets)),
        )
        self.connection.commit()
        return run_id

    def finish_run(self, run_id: str, stats: dict[str, Any]) -> None:
        self.connection.execute(
            "UPDATE runs SET finished_at = ?, stats_json = ? WHERE run_id = ?",
            (_now_iso(), json.dumps(stats, ensure_ascii=False, default=str), run_id),
        )
        self.connection.commit()

    # -- ofertas -------------------------------------------------------------

    def save_offer(self, offer: JobOffer, run_id: str) -> bool:
        """Guarda (o actualiza) una oferta. Devuelve True si es NUEVA en la base.

        "Nueva" significa que su hash no se había visto nunca, ni en esta
        corrida ni en ninguna anterior. Es lo que permite responder "de las 40
        de hoy, ¿cuántas no habíamos visto ya?".
        """
        now = _now_iso()
        cursor = self.connection.execute(
            "SELECT dedupe_key FROM offers WHERE dedupe_key = ?", (offer.dedupe_key,)
        )
        is_new = cursor.fetchone() is None

        if is_new:
            self.connection.execute(
                """
                INSERT INTO offers (
                    dedupe_key, loose_key, title, company,
                    location_raw, location_city, location_region, location_country,
                    market_key, salary_min, salary_max, salary_currency, salary_raw,
                    salary_is_predicted, posted_at, canonical_url, description,
                    status, first_seen_at, last_seen_at, first_seen_run, zona
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'nueva', ?, ?, ?, ?)
                """,
                (
                    offer.dedupe_key, offer.loose_key, offer.title, offer.company,
                    offer.location_raw, offer.location_city, offer.location_region,
                    offer.location_country, offer.market_key,
                    offer.salary_min, offer.salary_max, offer.salary_currency, offer.salary_raw,
                    1 if offer.salary_is_predicted else 0,
                    _dt_to_iso(offer.posted_at), offer.url, offer.description,
                    now, now, run_id, offer.zona,
                ),
            )
        else:
            # Ya la conocíamos: solo se refresca la última vez que se vio.
            # No se pisan título ni empresa: la primera versión vista es la
            # que se conserva, y las variantes quedan en offer_sources.
            #
            # La zona SÍ se refresca, porque la decide la configuración de hoy
            # y no la del día que se vio. Pero solo con un valor: si esta
            # aparición no trae zona, se queda la que había.
            self.connection.execute(
                "UPDATE offers SET last_seen_at = ?, zona = COALESCE(?, zona) "
                "WHERE dedupe_key = ?",
                (now, offer.zona, offer.dedupe_key),
            )

        self._save_source_link(offer, now)
        self._save_query_link(offer, run_id, now)
        return is_new

    def _save_source_link(self, offer: JobOffer, now: str) -> None:
        """Registra POR QUÉ FUENTE llegó esta oferta.

        ON CONFLICT ... DO UPDATE es el "upsert" de SQLite: si la fila ya
        existe (misma oferta, misma fuente, mismo id externo), en vez de
        fallar actualiza la fecha.
        """
        self.connection.execute(
            """
            INSERT INTO offer_sources (
                dedupe_key, source, external_id, market_key, url, posted_at,
                raw_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (dedupe_key, source, external_id)
            DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (
                offer.dedupe_key, offer.source, offer.external_id, offer.market_key,
                offer.url, _dt_to_iso(offer.posted_at),
                json.dumps(offer.raw, ensure_ascii=False, default=str),
                now, now,
            ),
        )

    def _save_query_link(self, offer: JobOffer, run_id: str, now: str) -> None:
        self.connection.execute(
            """
            INSERT INTO offer_queries (
                dedupe_key, query_label, market_key, source, run_id, seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (dedupe_key, query_label, market_key, source, run_id)
            DO NOTHING
            """,
            (offer.dedupe_key, offer.query_label, offer.market_key, offer.source, run_id, now),
        )

    def commit(self) -> None:
        self.connection.commit()

    # -- consultas para el reporte -------------------------------------------

    def offers_by_source_count(self, run_id: str) -> dict[str, int]:
        """Ofertas únicas alcanzadas por cada fuente en esta corrida."""
        rows = self.connection.execute(
            """
            SELECT source, COUNT(DISTINCT dedupe_key) AS total
            FROM offer_queries WHERE run_id = ?
            GROUP BY source
            """,
            (run_id,),
        ).fetchall()
        return {row["source"]: row["total"] for row in rows}

    def exclusive_by_source(self, run_id: str) -> dict[str, int]:
        """Ofertas que SOLO alcanzó una fuente en esta corrida.

        Este es el número que decide si Jooble merece la pena.
        """
        rows = self.connection.execute(
            """
            SELECT source, COUNT(*) AS total FROM (
                SELECT dedupe_key, MIN(source) AS source
                FROM offer_queries
                WHERE run_id = ?
                GROUP BY dedupe_key
                HAVING COUNT(DISTINCT source) = 1
            )
            GROUP BY source
            """,
            (run_id,),
        ).fetchall()
        return {row["source"]: row["total"] for row in rows}

    def shared_between_sources(self, run_id: str) -> int:
        """Ofertas que llegaron por más de una fuente (solapamiento real)."""
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS total FROM (
                SELECT dedupe_key FROM offer_queries WHERE run_id = ?
                GROUP BY dedupe_key HAVING COUNT(DISTINCT source) > 1
            )
            """,
            (run_id,),
        ).fetchone()
        return row["total"] if row else 0

    def unique_counts(self, run_id: str) -> tuple[int, int]:
        """(claves estrictas distintas, claves laxas distintas) de esta corrida.

        La diferencia entre ambas mide cuánto está SUB-FUSIONANDO la clave
        estricta, es decir cuántos duplicados probables se están contando como
        ofertas distintas. Ver la explicación larga en dedupe.py.
        """
        row = self.connection.execute(
            """
            SELECT COUNT(DISTINCT o.dedupe_key) AS strict,
                   COUNT(DISTINCT o.loose_key)  AS loose
            FROM offers o
            WHERE o.dedupe_key IN (SELECT dedupe_key FROM offer_queries WHERE run_id = ?)
            """,
            (run_id,),
        ).fetchone()
        return (row["strict"], row["loose"]) if row else (0, 0)

    def sample_offers(self, run_id: str, limit: int = 10) -> list[sqlite3.Row]:
        """Muestra de ofertas de la corrida, repartida entre mercados.

        ROW_NUMBER() OVER (PARTITION BY ...) numera las filas dentro de cada
        mercado; quedarse con las primeras de cada uno evita que la muestra
        salga entera de México y no se vea nada de Texas.
        """
        return self.connection.execute(
            """
            SELECT * FROM (
                SELECT o.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY o.market_key
                           ORDER BY o.posted_at DESC
                       ) AS rn
                FROM offers o
                WHERE o.dedupe_key IN (SELECT dedupe_key FROM offer_queries WHERE run_id = ?)
            )
            ORDER BY rn, market_key
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()

    def sources_for_offer(self, dedupe_key: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT DISTINCT source FROM offer_sources WHERE dedupe_key = ? ORDER BY source",
            (dedupe_key,),
        ).fetchall()
        return [row["source"] for row in rows]

    def total_offers(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS total FROM offers").fetchone()
        return row["total"] if row else 0

    # -- consultas para la bandeja de revisión --------------------------------
    #
    # A partir de aquí, lo que usa la interfaz de la v1. El recolector no
    # necesita nada de esto: son lecturas y cambios de estado.

    def list_offers(
        self,
        status: str | None = None,
        market_key: str | None = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        """Ofertas para la bandeja, de la más reciente a la más antigua.

        Se construye el WHERE por partes en lugar de concatenar texto con los
        valores dentro: los valores van SIEMPRE como parámetros (`?`), que es
        lo que evita la inyección de SQL.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if status and status != "todas":
            clauses.append("status = ?")
            params.append(status)
        if market_key and market_key != "todos":
            clauses.append("market_key = ?")
            params.append(market_key)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        return self.connection.execute(
            f"""
            SELECT * FROM offers
            {where}
            ORDER BY COALESCE(posted_at, first_seen_at) DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    def get_offer(self, dedupe_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM offers WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()

    def set_status(self, dedupe_key: str, status: str) -> bool:
        """Cambia el estado de UN anuncio. Devuelve False si no existe.

        Casi nunca es lo que se quiere desde la bandeja: allí una tarjeta es
        una VACANTE y puede reunir varios anuncios. Ver
        `marcar_estado_de_la_vacante`.
        """
        cursor = self.connection.execute(
            "UPDATE offers SET status = ?, status_changed_at = ? WHERE dedupe_key = ?",
            (status, _now_iso(), dedupe_key),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def marcar_estado_de_la_vacante(
        self, claves: Sequence[str], status: str
    ) -> int:
        """Cambia el estado de TODOS los anuncios de una vacante, de una vez.

        POR QUÉ EXISTE (medido el 9 septiembre 2026, sobre la base real).
        La bandeja pinta una tarjeta por vacante y dentro dice «la misma
        vacante aparece 10 veces», pero al marcarla solo se cambiaba el
        anuncio REPRESENTANTE. Los otros nueve seguían en «nueva», así que:

          - la tarjeta desaparecía al pulsar (la quita el navegador) y
            **volvía en el siguiente refresco**, ahora como «9 ANUNCIOS»;
          - la misma vacante contaba a la vez en «Sin revisar» y en
            «Interesan»;
          - la corrida siguiente **pagaba por releerla**, porque sus hermanas
            seguían pendientes de clasificar.

        Sobre las 272 vacantes de esa mañana, 32 tenían más de un anuncio y
        dos tenían diez: descartarlas de verdad costaba diez clics. Es la
        regla nº 1 de la casa —nada desaparece ni reaparece en silencio— rota
        justo donde ella decide.

        UNA SOLA TRANSACCIÓN, y no un bucle de `set_status`. Un cierre a
        mitad dejaría la vacante medio marcada, que es exactamente el estado
        que este método existe para que no exista.

        Devuelve cuántas filas se cambiaron.
        """
        if not claves:
            return 0
        # `dict.fromkeys` y no `set`: quita repetidas conservando el orden,
        # que es el que la pantalla enseñó.
        unicas = list(dict.fromkeys(claves))
        marcas = ", ".join("?" for _ in unicas)
        cursor = self.connection.execute(
            f"UPDATE offers SET status = ?, status_changed_at = ? "
            f"WHERE dedupe_key IN ({marcas})",
            (status, _now_iso(), *unicas),
        )
        self.connection.commit()
        return cursor.rowcount

    def counts_by_status(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS total FROM offers GROUP BY status"
        ).fetchall()
        return {row["status"]: row["total"] for row in rows}

    def counts_by_market(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT market_key, COUNT(*) AS total FROM offers GROUP BY market_key"
        ).fetchall()
        return {row["market_key"]: row["total"] for row in rows}

    # -- el recorte geográfico de lo ya guardado (24 septiembre 2026) --------
    #
    # Lo usa `recolector/recorte.py`. Van aquí, y no allí con SQL suelto,
    # porque borrar una oferta es tocar seis tablas y dos de ellas tienen
    # herencias que no pueden quedarse colgando.

    def ubicaciones_guardadas(self, market_key: str) -> list[sqlite3.Row]:
        """Lo que hace falta para volver a juzgar la geografía de un mercado.

        `raw_json` es la respuesta cruda de la PRIMERA fuente Adzuna de la
        oferta, porque el condado no se guardaba en `offers` y ahí sí está.
        Sale NULL para las que solo llegaron por Jooble.
        """
        return self.connection.execute(
            """
            SELECT o.dedupe_key, o.status, o.zona,
                   o.location_raw, o.location_city, o.location_region,
                   (SELECT s.raw_json FROM offer_sources s
                     WHERE s.dedupe_key = o.dedupe_key AND s.source = 'adzuna'
                     ORDER BY s.first_seen_at LIMIT 1) AS raw_json
              FROM offers o
             WHERE o.market_key = ?
             ORDER BY o.dedupe_key
            """,
            (market_key,),
        ).fetchall()

    def fijar_zonas(self, zonas: dict[str, str | None]) -> int:
        """Pone la zona de cada oferta. Devuelve cuántas cambiaron. Sin commit:
        va dentro de la misma transacción que el borrado."""
        cambiadas = 0
        for clave, zona in zonas.items():
            cursor = self.connection.execute(
                "UPDATE offers SET zona = ? WHERE dedupe_key = ? AND zona IS NOT ?",
                (zona, clave, zona),
            )
            cambiadas += cursor.rowcount
        return cambiadas

    def borrar_ofertas(self, claves: Sequence[str]) -> dict[str, int]:
        """Borra esas ofertas de TODAS las tablas. Sin commit (ver arriba).

        LO DELICADO NO ES BORRAR, ES LO QUE SE QUEDA. Una vacante publicada en
        Dallas y en Houston es un grupo: se lee UNO de sus anuncios —el
        representante— y los demás copian su veredicto con `heredada_de`. Si el
        representante es el de Dallas y se borra, el de Houston se queda
        apuntando a nadie, y como el ranking solo mira representantes
        (`heredada_de IS NULL`), esa vacante desaparecería del orden SIN
        haberse borrado. Por eso se promueve a un heredero: pasa a
        representante con el mismo veredicto, y sus hermanos le apuntan a él.
        El heredero sigue con los tokens a cero, así que no se cuela en
        `lecturas_medidas`, que filtra por `tokens_salida > 0`.

        Y lo mismo en el ranking: la línea de una vacante que sigue viva se
        pasa al heredero en vez de borrarse. Solo desaparecen las líneas de
        vacantes que se van enteras, y entonces se descuentan también de
        `ofertas_disponibles` para que `desajuste_del_ranking` no las lea
        como un recorte que el orden nunca hizo.
        """
        borrar = sorted(set(claves))
        if not borrar:
            return {"ofertas": 0, "herederos": 0, "lineas_de_ranking": 0}

        self.connection.execute("CREATE TEMP TABLE IF NOT EXISTS _a_borrar (k TEXT PRIMARY KEY)")
        self.connection.execute("DELETE FROM _a_borrar")
        self.connection.executemany(
            "INSERT INTO _a_borrar (k) VALUES (?)", [(k,) for k in borrar]
        )

        herederos: dict[str, str] = {}   # representante borrado -> su heredero
        for tabla in ("clasificaciones", "triajes"):
            padres = [
                fila[0] for fila in self.connection.execute(
                    f"""
                    SELECT DISTINCT h.heredada_de FROM {tabla} h
                     WHERE h.heredada_de IN (SELECT k FROM _a_borrar)
                       AND h.dedupe_key NOT IN (SELECT k FROM _a_borrar)
                    """
                )
            ]
            for padre in padres:
                heredero = self.connection.execute(
                    f"""
                    SELECT dedupe_key FROM {tabla}
                     WHERE heredada_de = ? AND dedupe_key NOT IN (SELECT k FROM _a_borrar)
                     ORDER BY dedupe_key LIMIT 1
                    """,
                    (padre,),
                ).fetchone()[0]
                self.connection.execute(
                    f"UPDATE {tabla} SET heredada_de = NULL WHERE dedupe_key = ?",
                    (heredero,),
                )
                self.connection.execute(
                    f"UPDATE {tabla} SET heredada_de = ? WHERE heredada_de = ?",
                    (heredero, padre),
                )
                if tabla == "clasificaciones":
                    herederos[padre] = heredero

        # El ranking: la vacante que sigue viva cambia de anuncio; la que se
        # va entera se va del orden y de su recuento.
        for padre, heredero in herederos.items():
            self.connection.execute(
                "UPDATE ranking_items SET dedupe_key = ? WHERE dedupe_key = ?",
                (heredero, padre),
            )
        quitadas = self.connection.execute(
            """
            SELECT ranking_id, COUNT(*) FROM ranking_items
             WHERE dedupe_key IN (SELECT k FROM _a_borrar)
             GROUP BY ranking_id
            """
        ).fetchall()
        for ranking_id, n in quitadas:
            self.connection.execute(
                """
                UPDATE rankings
                   SET ofertas_disponibles = MAX(0, ofertas_disponibles - ?)
                 WHERE id = ? AND ofertas_disponibles > 0
                """,
                (n, ranking_id),
            )

        for tabla in ("ranking_items", "triajes", "clasificaciones",
                      "offer_queries", "offer_sources", "offers"):
            self.connection.execute(
                f"DELETE FROM {tabla} WHERE dedupe_key IN (SELECT k FROM _a_borrar)"
            )
        self.connection.execute("DELETE FROM _a_borrar")

        return {
            "ofertas": len(borrar),
            "herederos": len(herederos),
            "lineas_de_ranking": sum(n for _, n in quitadas),
        }

    # -- clasificaciones (v1) -------------------------------------------------
    #
    # A partir de aquí, lo que usa `clasificador/`. El recolector no necesita
    # nada de esto y no lo llama nunca: la capa 2 se ejecuta con su propio
    # comando, después.

    # Las columnas de `clasificaciones` que se devuelven junto a cada oferta.
    # Van con prefijo `clas_` porque `dedupe_key` y `estado` chocarían con las
    # de `offers` en un SELECT con JOIN.
    _COLUMNAS_CLASIFICACION = """
        c.estado              AS clas_estado,
        c.relevante           AS clas_relevante,
        c.motivo_descarte     AS clas_motivo_descarte,
        c.confianza           AS clas_confianza,
        c.razon               AS clas_razon,
        c.cv_id               AS clas_cv_id,
        c.idioma_anuncio      AS clas_idioma,
        c.empresa_normalizada AS clas_empresa,
        c.puesto_canonico     AS clas_puesto,
        c.es_reclutadora      AS clas_es_reclutadora,
        c.seniority_detectado AS clas_seniority,
        c.modalidad           AS clas_modalidad,
        c.necesita_criterio   AS clas_necesita_criterio,
        c.motivo_criterio     AS clas_motivo_criterio,
        c.error               AS clas_error,
        c.fichas_huella       AS clas_huella
    """

    # LO QUE DIJO EL TRIAJE, para que la bandeja pueda distinguir «el asistente
    # la vio y la dejó en el puesto 34, porque X» de «nadie ha mirado nunca
    # esta oferta». Sin estas columnas las dos se ven igual —una oferta sin
    # clasificar— y entonces el triaje sería el filtro silencioso que la regla
    # nº 1 de la casa prohíbe, reconstruido con pasos de más.
    # `triaje_heredado` va aquí y no se queda en la base porque la bandeja
    # tiene que poder decir la verdad entera: «el asistente ordenó ESTE
    # anuncio» y «el asistente ordenó esta vacante, y este es otro de sus
    # anuncios» no son la misma frase. Son tres estados y no dos, igual que en
    # el tope de lote.
    _COLUMNAS_TRIAJE = """
        t.posicion       AS triaje_posicion,
        t.merece_lectura AS triaje_merecia,
        t.seleccionada   AS triaje_seleccionada,
        t.por_que        AS triaje_por_que,
        t.triado_at      AS triaje_at,
        t.heredada_de    AS triaje_heredado
    """

    def ofertas_pendientes(
        self,
        fichas_huella: str,
        incluir_obsoletas: bool = False,
        incluir_fallidas: bool = False,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """Ofertas que hay que mandar al LLM.

        Por defecto solo las que NO tienen fila en `clasificaciones`. Esa es
        la caché: una oferta ya clasificada no se vuelve a clasificar aunque
        el recolector la encuentre veinte veces más.

        `incluir_obsoletas` añade las que se clasificaron con una versión
        anterior de las fichas. `incluir_fallidas` añade las que quedaron en
        `sin_clasificar` (la API estaba caída, por ejemplo) y merecen otro
        intento. Las dos son opt-in porque las dos cuestan dinero.
        """
        condiciones = ["c.dedupe_key IS NULL"]
        params: list[Any] = []

        if incluir_obsoletas:
            condiciones.append("c.fichas_huella <> ?")
            params.append(fichas_huella)
        if incluir_fallidas:
            condiciones.append("c.estado = 'sin_clasificar'")

        where = " OR ".join(condiciones)
        sql = f"""
            SELECT o.* FROM offers o
            LEFT JOIN clasificaciones c ON c.dedupe_key = o.dedupe_key
            WHERE {where}
            ORDER BY COALESCE(o.posted_at, o.first_seen_at) DESC
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        return self.connection.execute(sql, params).fetchall()

    def vacante_pendiente_por_clave(self, dedupe_key: str) -> list[sqlite3.Row]:
        """Una oferta concreta y sus anuncios hermanos, si siguen pendientes.

        Es lo que consume el botón «que la lea el asistente» de la bandeja: ella
        señala UNA oferta y el sistema la clasifica en el momento.

        DEVUELVE LA VACANTE, NO EL ANUNCIO, y esa diferencia cuesta dinero. Si
        ella pide que se lea uno de los cinco anuncios de "Chief Operating
        Officer" de Northline en cinco ciudades y solo se clasificara ese, los otros
        cuatro seguirían pendientes y la siguiente corrida los volvería a pagar
        —cuatro llamadas por una vacante que ya estaba decidida—. Con la vacante
        entera, el motor clasifica al representante y los hermanos heredan su
        veredicto con coste cero, que es la misma regla que aplica el triaje:
        un hueco compra la vacante, no el anuncio.

        El primero de la lista es SIEMPRE el que ella pidió. El motor clasifica
        al primero de cada grupo, así que la razón que se guarde será la del
        anuncio que ella estaba mirando.

        Devuelve `[]` si la clave no existe o si esa oferta ya tiene VEREDICTO
        —la caché no se salta desde un botón—. Los hermanos que ya tengan
        veredicto se quedan fuera por la misma razón.

        UN INTENTO FALLIDO NO ES UN VEREDICTO (7 septiembre 2026). Aquí se
        exigía `c.dedupe_key IS NULL`, o sea que bastaba con que existiera la
        fila; y una llamada que falla —API caída, cuenta sin crédito— deja
        fila, con `estado = 'sin_clasificar'` y el motivo dentro. Con aquella
        condición, un fallo sacaba la oferta de su propio botón para siempre y
        el diálogo la despedía diciendo que su veredicto estaba en la bandeja.
        No estaba: estaba el error. Se descartaba en silencio, que es la regla
        3 de la casa, y encima cobrando el turno de una lectura que no se hizo.

        Reintentar UNA que ella señala es distinto de reintentar en bloque: el
        importe se le enseña antes y lo pide ella una por una. El reintento en
        bloque sigue siendo opt-in (`ofertas_pendientes(incluir_fallidas=…)`).
        """
        fila = self.connection.execute(
            """
            SELECT o.* FROM offers o
            LEFT JOIN clasificaciones c ON c.dedupe_key = o.dedupe_key
             WHERE o.dedupe_key = ?
               AND (c.dedupe_key IS NULL OR c.estado = 'sin_clasificar')
            """,
            (dedupe_key,),
        ).fetchone()
        if fila is None:
            return []

        loose_key = fila["loose_key"]
        if not loose_key:
            # Sin clave laxa no se puede afirmar que otro anuncio sea la misma
            # vacante, y heredar un veredicto a una oferta que no lo era es
            # peor que pagar dos veces. Mismo criterio que en el motor.
            return [fila]

        hermanas = self.connection.execute(
            """
            SELECT o.* FROM offers o
            LEFT JOIN clasificaciones c ON c.dedupe_key = o.dedupe_key
             WHERE o.loose_key = ? AND o.dedupe_key <> ?
               AND (c.dedupe_key IS NULL OR c.estado = 'sin_clasificar')
             ORDER BY COALESCE(o.posted_at, o.first_seen_at) DESC
            """,
            (loose_key, dedupe_key),
        ).fetchall()
        return [fila, *hermanas]

    # -- el ranking del asistente -------------------------------------------

    def contar_ofertas_para_ranking(self) -> int:
        """Cuántas hay sin revisar, SIN traerlas ni aplicar techo.

        Es el denominador de «las 45 más recientes de 61». Va aparte de
        `ofertas_para_ranking` porque quien lo necesita —el plan, la consola y
        la bandeja— lo necesita justo cuando el techo ya recortó la lista, y
        entonces `len(filas)` ya no lo puede responder.

        Comparte el WHERE con `ofertas_para_ranking` a propósito: si los dos
        se desincronizan, el denominador deja de describir el numerador y la
        frase de la pantalla se convierte en dos números que se contradicen.
        """
        return self.connection.execute(
            """
            SELECT COUNT(*)
              FROM clasificaciones c
              JOIN offers o ON o.dedupe_key = c.dedupe_key
             WHERE c.estado = 'ok'
               AND c.heredada_de IS NULL
               AND (c.relevante = 1 OR c.necesita_criterio = 1)
               AND o.status = 'nueva'
            """
        ).fetchone()[0]

    def ofertas_para_ranking(self, limite: int | None = None) -> list[sqlite3.Row]:
        """Lo que el asistente tiene que ordenar.

        Solo lo CLASIFICADO y que sobrevivió: relevantes, más las que el
        clasificador marcó como "necesita tu criterio", que son justo las que
        ella tiene que mirar. Lo descartado no entra — el asistente ordena, no
        revisa el trabajo de la capa anterior.

        Se excluyen las heredadas: cinco anuncios de la misma vacante tienen
        que ser UNA línea del ranking, no cinco. `anuncios` dice cuántos son,
        que es información que el modelo usa (una vacante publicada en cinco
        ciudades se está buscando de verdad).

        Y SE EXCLUYE LO QUE ELLA YA DECIDIÓ. Solo entran las que siguen en
        `nueva`: si ya marcó una como «interesa» o «descartada», volver a
        ordenársela no le dice nada que no sepa, y encima cuesta.

        Sin este filtro la lista crece para siempre —a 8 clasificadas al día
        serían ~144 al mes—, el ranking se encarece con ella y deja de ser lo
        que tiene que ser: una lista corta de lo que merece su atención AHORA.
        Un orden de 144 no es «por dónde empiezo», es otro montón que revisar.
        """
        sql = """
            SELECT o.*, c.relevante, c.razon, c.confianza, c.cv_id,
                   c.empresa_normalizada, c.puesto_canonico, c.es_reclutadora,
                   c.seniority_detectado, c.modalidad, c.necesita_criterio,
                   (SELECT COUNT(*) FROM clasificaciones h
                     WHERE h.heredada_de = c.dedupe_key) + 1 AS anuncios
              FROM clasificaciones c
              JOIN offers o ON o.dedupe_key = c.dedupe_key
             WHERE c.estado = 'ok'
               AND c.heredada_de IS NULL
               AND (c.relevante = 1 OR c.necesita_criterio = 1)
               AND o.status = 'nueva'
             ORDER BY COALESCE(o.posted_at, o.first_seen_at) DESC
        """
        params: list[Any] = []
        if limite is not None:
            sql += " LIMIT ?"
            params.append(limite)
        return self.connection.execute(sql, params).fetchall()

    def guardar_ranking(self, cabecera: dict[str, Any], items: list[tuple]) -> None:
        """Guarda un ranking entero. O va completo, o no va.

        Los items se insertan en la misma transacción que la cabecera: un
        ranking a medias en la base sería un orden con huecos, que parece
        completo y esconde lo que falta.

        `ofertas_disponibles` puede faltar, y entonces se asume que el lote
        eran TODAS: es lo que era verdad en los rankings guardados antes del 4
        septiembre 2026, cuando no existía el recorte. Asumir lo contrario
        —marcarlos como recortados— pondría en pantalla un «de N» inventado
        sobre órdenes que no dejaron nada fuera.
        """
        cabecera = {
            "ofertas_disponibles": cabecera.get("ofertas_consideradas", 0),
            **cabecera,
        }
        self.connection.execute(
            """
            INSERT INTO rankings (id, creado_at, modelo, lectura, patrones,
                                  ofertas_consideradas, ofertas_disponibles,
                                  tokens_entrada, tokens_salida, coste_usd)
            VALUES (:id, :creado_at, :modelo, :lectura, :patrones,
                    :ofertas_consideradas, :ofertas_disponibles,
                    :tokens_entrada, :tokens_salida, :coste_usd)
            """,
            cabecera,
        )
        self.connection.executemany(
            """
            INSERT INTO ranking_items (ranking_id, dedupe_key, posicion, por_que, accion)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(cabecera["id"], *item) for item in items],
        )
        self.connection.commit()

    def ultimo_ranking(self) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM rankings ORDER BY creado_at DESC LIMIT 1"
        ).fetchone()

    def desajuste_del_ranking(self, ranking_id: str) -> dict[str, int]:
        """En qué se diferencia ese ranking del que saldría ahora.

        UN RANKING NO ES UNA CACHÉ: ENVEJECE, Y HASTA HOY LO HACÍA EN SILENCIO.
        Es lo único del sistema que ella lee entero, y la pantalla solo decía
        su fecha. Alguien que abre la app y lee el orden está leyendo una foto
        vieja sin saberlo, y eso es la regla nº 1 de la casa rota justo en la
        pieza más importante.

        SE MIDE CONTRA EL CONJUNTO DE CANDIDATAS, NO CONTRA LAS FECHAS. Y esa
        distinción no es sutil: contar «clasificaciones posteriores al ranking»
        daba **94** sobre esta misma base el 3 septiembre 2026, y de esas 94,
        **45 estaban ya dentro del ranking** — se releyeron después y lo que se
        movió fue su `clasificado_at`, no su contenido. La cifra de verdad era
        1. Un aviso construido sobre el reloj habría gritado noventa veces por
        nada, y ella habría dejado de leerlo.

        Salen CUATRO números porque significan cuatro cosas distintas:

        · `nuevas` .......... clasificadas después y que no están en el orden.
                              Es lo que se está perdiendo.
        · `recortadas` ...... las que el propio orden dejó fuera al ordenarse,
                              porque el lote entero no cabía en el tope por
                              acción. NO son una llegada nueva.
        · `ya_no_encajan` ... líneas que el orden SÍ enseña y que el sistema ha
                              vuelto a juzgar y ha descartado. Son las peores:
                              no faltan, sobran, y parecen recomendaciones.
        · `ya_decididas` .... líneas que ella misma ya marcó. No es un fallo del
                              orden, es que va por delante.

        Y `recortadas` ES DEL 4 SEPTIEMBRE 2026, con motivo. Hasta ese día
        `nuevas` era `candidatas - items` y de ahí se leía «clasificadas
        después». Esa deducción era CIERTA mientras el ranking ordenara siempre
        todo lo que había: si algo era candidata y no salía en el orden, solo
        podía haber llegado luego. Dejó de serlo el día que el orden se recorta
        a propósito (`llm.max_ofertas_por_ranking`), y el síntoma salió a la
        primera corrida real: la pantalla enseñaba dos cajas pegadas diciendo
        «25 ofertas clasificadas después no están en este orden» y «este orden
        no mira 25 ofertas más antiguas». **Las mismas 25**, contadas dos veces
        y con motivos opuestos — la regla nº 15 de la casa rota justo debajo de
        lo único que ella lee entero.

        SE MIDE POR EL TAMAÑO DEL CONJUNTO, no restando claves. Cuántas
        candidatas hay hoy contra cuántas había al ordenar
        (`rankings.ofertas_disponibles`). Un recorte no cambia el tamaño del
        conjunto y una llegada sí, así que esta cuenta no puede confundirlos.
        Restar claves no serviría: no se guarda cuáles se recortaron, y
        deducirlo de las fechas volvería a apoyar el aviso en el reloj, que es
        exactamente el error que este método ya corrigió una vez.

        `ofertas_disponibles = 0` significa **no consta** —los rankings
        anteriores a ese día no lo guardaban— y entonces se cuenta como antes.
        Leerlo como un cero de verdad haría que cada orden viejo declarara un
        recorte que nunca hubo.
        """
        candidatas = {
            fila["dedupe_key"] for fila in self.ofertas_para_ranking(limite=None)
        }
        items = [
            fila[0] for fila in self.connection.execute(
                "SELECT dedupe_key FROM ranking_items WHERE ranking_id = ?",
                (ranking_id,),
            )
        ]
        if not items:
            return {"total": 0, "nuevas": 0, "ya_no_encajan": 0,
                    "ya_decididas": 0, "recortadas": 0}

        marcadores = ",".join("?" * len(items))
        decididas = {
            fila[0] for fila in self.connection.execute(
                f"SELECT dedupe_key FROM offers "
                f"WHERE dedupe_key IN ({marcadores}) AND status <> 'nueva'",
                items,
            )
        }
        fuera = [clave for clave in items if clave not in candidatas]

        disponibles_entonces = self.connection.execute(
            "SELECT ofertas_disponibles FROM rankings WHERE id = ?", (ranking_id,)
        ).fetchone()
        disponibles_entonces = (disponibles_entonces or [0])[0] or 0
        recortadas = max(0, disponibles_entonces - len(items))

        sin_ordenar = len(candidatas - set(items))
        return {
            "total": len(items),
            # Lo que ha CRECIDO el conjunto desde que se ordenó. Si no consta
            # cuántas había, se cae a la cuenta de antes.
            "nuevas": (max(0, len(candidatas) - disponibles_entonces)
                       if disponibles_entonces else sin_ordenar),
            "recortadas": min(recortadas, sin_ordenar),
            "ya_decididas": sum(1 for clave in fuera if clave in decididas),
            "ya_no_encajan": sum(1 for clave in fuera if clave not in decididas),
        }

    def items_de_ranking(self, ranking_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT r.*, o.title, o.company, o.canonical_url, o.market_key,
                   c.puesto_canonico, c.empresa_normalizada
              FROM ranking_items r
              JOIN offers o ON o.dedupe_key = r.dedupe_key
              LEFT JOIN clasificaciones c ON c.dedupe_key = r.dedupe_key
             WHERE r.ranking_id = ?
             ORDER BY r.posicion
            """,
            (ranking_id,),
        ).fetchall()

    def guardar_triaje(self, filas: list[dict[str, Any]]) -> None:
        """Guarda el veredicto del triaje para CADA oferta que se le mandó.

        Se guardan todas, no solo las seleccionadas. Guardar solo el corte
        dejaría a las demás indistinguibles de las que nadie miró, y entonces
        la bandeja no podría enseñar por qué una oferta sigue sin revisar.

        Y se guardan también los ANUNCIOS HERMANOS de cada vacante, con
        `heredada_de` apuntando al que se mandó de verdad. Sin ellos la vacante
        entera volvía a entrar en la corrida siguiente por la puerta de un
        hermano sin marcar. Ver el comentario de la tabla.

        `heredada_de` se lee con `.get()` porque quien llama puede no ponerlo:
        un dict sin la clave es «esta la ordenó el modelo», que es el caso
        normal y el único que existía antes.
        """
        ahora = _now_iso()
        self.connection.executemany(
            """
            INSERT INTO triajes (dedupe_key, posicion, merece_lectura,
                                 seleccionada, por_que, modelo, triado_at,
                                 heredada_de)
            VALUES (:dedupe_key, :posicion, :merece_lectura,
                    :seleccionada, :por_que, :modelo, :triado_at,
                    :heredada_de)
            ON CONFLICT(dedupe_key) DO UPDATE SET
                posicion       = excluded.posicion,
                merece_lectura = excluded.merece_lectura,
                seleccionada   = excluded.seleccionada,
                por_que        = excluded.por_que,
                modelo         = excluded.modelo,
                triado_at      = excluded.triado_at,
                heredada_de    = excluded.heredada_de
            """,
            [
                dict(f, triado_at=ahora, heredada_de=f.get("heredada_de"))
                for f in filas
            ],
        )
        self.connection.commit()

    def guardar_corrida_triaje(self, datos: dict[str, Any]) -> None:
        """Lo que costó una llamada de triaje, para poder mirarlo después.

        Se escribe SIEMPRE que la llamada se haya hecho, aunque el triaje se
        rechace luego por venir mal numerado: la llamada se pagó igual, y un
        registro que solo guarda los éxitos miente justo el día que algo va mal
        — la misma razón por la que el libro de gastos apunta antes de guardar.
        """
        self.connection.execute(
            """
            INSERT INTO triaje_corridas (
                triado_at, vacantes, fuera_del_tope,
                tokens_entrada, tokens_salida, coste_usd,
                salida_prevista, coste_previsto, modelo, esfuerzo)
            VALUES (
                :triado_at, :vacantes, :fuera_del_tope,
                :tokens_entrada, :tokens_salida, :coste_usd,
                :salida_prevista, :coste_previsto, :modelo, :esfuerzo)
            """,
            dict(datos, triado_at=_now_iso()),
        )
        self.connection.commit()

    def corridas_de_triaje(self, limite: int = 20) -> list[sqlite3.Row]:
        """El historial, de la más reciente a la más vieja."""
        return self.connection.execute(
            "SELECT * FROM triaje_corridas ORDER BY id DESC LIMIT ?", (limite,)
        ).fetchall()

    # -- la descripción entera ----------------------------------------------

    def ofertas_sin_descripcion_completa(
        self, limite: int | None = None, solo_sin_intentar: bool = True
    ) -> list[sqlite3.Row]:
        """Las candidatas a traerles la página, y en el orden que importa.

        SE PIDE UNA VEZ POR VACANTE, NO POR ANUNCIO. Los cinco anuncios de la
        misma vacante tienen el mismo texto: traerlo cinco veces son cuatro
        peticiones regaladas a Adzuna y cuatro oportunidades de que nos corten.
        Se agrupa por `loose_key` y se pide la más reciente de cada grupo; el
        resto lo copia `heredar_descripcion_completa`, gratis. Es la misma idea
        que ya usan el clasificador y el triaje.

        `solo_sin_intentar` distingue «no se ha intentado» de «se intentó y no
        salió». Reintentar en cada corrida lo que ya falló una vez sería gastar
        peticiones en las páginas que se sabe que no las tienen — de las 28
        medidas, una no la tenía y no la va a tener mañana tampoco.
        """
        condiciones = ["o.canonical_url IS NOT NULL"]
        if solo_sin_intentar:
            condiciones.append("o.description_full_via IS NULL")
        else:
            condiciones.append("COALESCE(o.description_full, '') = ''")

        sql = f"""
            SELECT o.* FROM offers o
             WHERE {' AND '.join(condiciones)}
               AND o.rowid = (
                   SELECT h.rowid FROM offers h
                    WHERE h.loose_key = o.loose_key
                    ORDER BY COALESCE(h.posted_at, h.first_seen_at) DESC, h.rowid
                    LIMIT 1)
             ORDER BY COALESCE(o.posted_at, o.first_seen_at) DESC
        """
        params: list[Any] = []
        if limite is not None:
            sql += " LIMIT ?"
            params.append(limite)
        return self.connection.execute(sql, params).fetchall()

    def guardar_descripcion_completa(
        self, dedupe_key: str, texto: str, via: str
    ) -> None:
        """Apunta el resultado del intento, HAYA SALIDO O NO.

        `via='nada'` con texto vacío se guarda igual, y a propósito: un
        registro que solo apunta los éxitos hace que «esta página no la tiene»
        sea indistinguible de «esta no se ha mirado», y entonces se reintenta
        para siempre. Es la misma regla del libro de gastos.
        """
        self.connection.execute(
            """
            UPDATE offers
               SET description_full = ?, description_full_via = ?,
                   description_full_at = ?
             WHERE dedupe_key = ?
            """,
            (texto or None, via, _now_iso(), dedupe_key),
        )
        self.connection.commit()

    def heredar_descripcion_completa(self, dedupe_key: str) -> int:
        """Copia la descripción a los anuncios hermanos. Devuelve cuántos.

        Gratis: no hay petición, es el mismo anuncio publicado en otra ciudad.
        Sin esto, la vacante de seis publicaciones costaría seis peticiones
        para traerse seis veces el mismo texto.
        """
        cur = self.connection.execute(
            """
            UPDATE offers
               SET description_full = (
                       SELECT f.description_full FROM offers f
                        WHERE f.dedupe_key = :clave),
                   description_full_via = (
                       SELECT f.description_full_via FROM offers f
                        WHERE f.dedupe_key = :clave),
                   description_full_at = (
                       SELECT f.description_full_at FROM offers f
                        WHERE f.dedupe_key = :clave)
             WHERE loose_key = (SELECT f.loose_key FROM offers f
                                 WHERE f.dedupe_key = :clave)
               AND dedupe_key <> :clave
            """,
            {"clave": dedupe_key},
        )
        self.connection.commit()
        return cur.rowcount

    def resumen_descripciones(self) -> dict[str, int]:
        """Cuántas se trajeron y por qué vía. Para el comando y el README.

        El reparto entre `json-ld` y `html` es el que hay que vigilar: la
        primera es un formato con dueño (Adzuna lo publica para Google Jobs) y
        la segunda es una clase de CSS. Si un día `html` sube, el extractor se
        está apoyando en lo frágil y conviene enterarse antes de que falle.
        """
        fila = self.connection.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN description_full_via IS NOT NULL
                                     THEN 1 ELSE 0 END), 0) AS intentadas,
                   COALESCE(SUM(CASE WHEN description_full_via = 'json-ld'
                                     THEN 1 ELSE 0 END), 0) AS por_json_ld,
                   COALESCE(SUM(CASE WHEN description_full_via = 'html'
                                     THEN 1 ELSE 0 END), 0) AS por_html,
                   COALESCE(SUM(CASE WHEN description_full_via = 'nada'
                                     THEN 1 ELSE 0 END), 0) AS sin_suerte
              FROM offers
            """
        ).fetchone()
        return {k: int(fila[k]) for k in fila.keys()}

    def claves_ya_triadas(self) -> set[str]:
        """Las ofertas que ya pasaron por el triaje alguna vez.

        CADA OFERTA TIENE UN TURNO, Y UNO SOLO. Si las no seleccionadas
        volvieran a competir cada corrida, la cola crecería sin parar: entran
        ~21 al día y se leen 8, así que se acumularían 13 diarias y al mes el
        triaje costaría más que no tenerlo (~$0,46 por corrida sobre 390
        pendientes, frente a ~$0,03 sobre 21).

        Que el coste sea FIJO es la razón de ser de esta capa; un triaje cuyo
        precio crece con la cola no sirve para nada.

        La que no entró no se pierde: se queda en la bandeja con su puesto y
        su motivo, y se clasifica cuando ella pulse el botón.
        """
        return {
            fila["dedupe_key"]
            for fila in self.connection.execute("SELECT dedupe_key FROM triajes")
        }

    def claves_elegidas_sin_leer(self) -> set[str]:
        """Las que el triaje ELIGIÓ leer y cuya lectura no llegó a hacerse.

        EL AGUJERO QUE TAPA (medido el 9 septiembre 2026, ejecutando).
        El triaje escribe fila en `triajes` para TODAS las vacantes del lote
        —las elegidas y las que no— antes de que el motor lea ninguna. Si la
        corrida se corta después —ella pulsa «Parar», salta el tope por
        acción, se cierra la ventana—, las elegidas que no llegaron a leerse
        se quedan con su fila puesta. Y `claves_ya_triadas` las excluye de
        todo lo que viene después, así que:

            plan de la corrida siguiente ... hay_trabajo: False, $0.0000
            nota ........................... «ya tuvieron su turno»

        O sea: nunca más se leen solas. Aquella mañana quedaron seis así
        —RWE (redes eléctricas), ERCOT, Provident (data centers), Ecoplexus—,
        indistinguibles en la bandeja de las que legítimamente no pasaron el
        corte, y con la app diciéndole a la vez, por escrito:

            «NO SE HAN PERDIDO: siguen pendientes […] y la próxima corrida
             con saldo las recoge sola.»
            «Parar […] el resto queda pendiente para la próxima.» (AYUDA.md)

        Las dos frases eran falsas. Esta consulta es lo que las vuelve
        verdad: quien llama las lee ANTES que nada y SIN volver a triarlas —
        su turno ya se gastó y su selección ya se pagó; lo que faltó fue la
        lectura.

        NO se toca `claves_ya_triadas`: hacer que estas vuelvan a competir en
        el triaje sería devolverlas al sorteo, y podrían perderlo. Además el
        coste fijo del triaje depende de que cada vacante entre una sola vez.
        """
        return {
            fila["dedupe_key"]
            for fila in self.connection.execute(
                """
                SELECT t.dedupe_key
                  FROM triajes t
                  LEFT JOIN clasificaciones c ON c.dedupe_key = t.dedupe_key
                 WHERE t.seleccionada = 1
                   AND c.dedupe_key IS NULL
                """
            )
        }

    def resumen_triaje(self) -> dict[str, int]:
        """Cuántas ofertas se triaron y cuántas pasaron el corte."""
        fila = self.connection.execute(
            """
            SELECT COUNT(*)                                   AS triadas,
                   COALESCE(SUM(seleccionada), 0)             AS seleccionadas,
                   COALESCE(SUM(merece_lectura), 0)           AS merecian
              FROM triajes
            """
        ).fetchone()
        return {k: int(fila[k]) for k in ("triadas", "seleccionadas", "merecian")}

    def guardar_clasificacion(self, datos: dict[str, Any]) -> None:
        """Guarda (o pisa) la clasificación de una oferta.

        Se pasa un diccionario en lugar de veinte parámetros a propósito: el
        que arma el diccionario es `clasificador/motor.py`, y así este módulo
        no tiene que importar nada de la capa 2 — la dependencia va en un solo
        sentido, igual que con los conectores.

        `ON CONFLICT DO UPDATE` hace que reclasificar pise la fila anterior en
        vez de fallar. No se guarda histórico de clasificaciones: con una
        usuaria y 90 al mes, sería complejidad sin beneficio.
        """
        columnas = [
            "dedupe_key", "estado", "relevante", "motivo_descarte", "confianza", "razon",
            "cv_id", "idioma_anuncio", "idioma_detectado", "empresa_normalizada",
            "puesto_canonico", "es_reclutadora", "seniority_detectado", "modalidad",
            "necesita_criterio", "motivo_criterio", "error", "modelo",
            "tokens_entrada", "tokens_salida", "coste_usd", "fichas_huella",
            "heredada_de", "descripcion_via", "caracteres_descripcion",
        ]
        faltan = [c for c in columnas if c not in datos]
        if faltan:
            # Error de programación, no de datos: se lanza de verdad.
            raise ValueError(f"guardar_clasificacion: faltan claves {faltan}")

        marcadores = ", ".join("?" for _ in columnas) + ", ?"
        asignaciones = ", ".join(f"{c} = excluded.{c}" for c in columnas if c != "dedupe_key")
        valores = [datos[c] for c in columnas] + [_now_iso()]

        self.connection.execute(
            f"""
            INSERT INTO clasificaciones ({', '.join(columnas)}, clasificado_at)
            VALUES ({marcadores})
            ON CONFLICT (dedupe_key) DO UPDATE SET
                {asignaciones}, clasificado_at = excluded.clasificado_at
            """,
            valores,
        )
        self.connection.commit()

    # SQL compartido por las dos funciones de abajo, para que contar y listar
    # no puedan responder cosas distintas. Regla nº 9 de la casa: si el número
    # que se enseña y el trabajo que se hace salen de dos consultas, divergen.
    _CIEGAS_WHERE = """
        c.descripcion_via = 'recorte'
        AND o.description_full IS NOT NULL
        AND TRIM(o.description_full) <> ''
    """

    def contar_juzgadas_a_ciegas(self) -> int:
        """Veredictos hechos con el recorte de 500 teniendo hoy el anuncio
        entero guardado. Es el SEGUNDO motivo de «obsoleta».

        El primero es `fichas_huella`: cambió `config/cvs.yaml`, el criterio
        envejeció. Este es el mismo razonamiento sobre la otra entrada: cambió
        el texto del anuncio, el veredicto envejeció. En los dos casos la fila
        sigue siendo válida hasta que alguien decida pagar por rehacerla, y en
        ninguno se rehace sola — reclasificar en silencio es gasto invisible.

        Cuenta ANUNCIOS y no vacantes: los hermanos llevan la vía del padre,
        así que si el padre juzgó a ciegas ellos también cuentan. Lo que se
        pagaría son las vacantes, y eso lo dice el plan.
        """
        fila = self.connection.execute(
            f"""SELECT COUNT(*) AS total FROM clasificaciones c
                  JOIN offers o ON o.dedupe_key = c.dedupe_key
                 WHERE {self._CIEGAS_WHERE}"""
        ).fetchone()
        return fila["total"] if fila else 0

    def ofertas_juzgadas_a_ciegas(self) -> list[sqlite3.Row]:
        """TODOS los anuncios de las vacantes que se juzgaron a ciegas.

        Devuelve anuncios y no vacantes, y esa es la parte que importa. Releer
        es una llamada por vacante —`agrupar_para_ahorrar` elige quién se lee y
        los demás heredan—, pero los hermanos tienen que venir en la lista o no
        se les reescribe el veredicto. Medido el 3 septiembre 2026 sobre esta
        base: 10 filas heredadas se quedaron con el veredicto ciego mientras su
        padre ya tenía el nuevo, precisamente porque la relectura de la sesión
        pasada solo tocó a los representantes.

        Por eso el criterio es «la VACANTE se juzgó a ciegas» —algún anuncio
        suyo lleva `descripcion_via = 'recorte'`— y no «este anuncio está
        marcado ciego»: un hermano que por lo que sea quedara marcado «entera»
        seguiría necesitando que le reescriban el veredicto cuando su vacante
        se relea.

        Ordenados por lo más reciente, que es el mismo criterio que
        `ofertas_pendientes`: si el tope por acción parte el trabajo, lo
        primero que se rehace es lo que ella tiene más a mano. `Sin loose_key`
        no hay vacante que agrupar, así que ese anuncio va solo — mismo
        criterio que el motor.
        """
        return self.connection.execute(
            f"""
            SELECT o.* FROM offers o
             WHERE o.loose_key IN (
                     SELECT o2.loose_key FROM offers o2
                       JOIN clasificaciones c ON c.dedupe_key = o2.dedupe_key
                      WHERE {self._CIEGAS_WHERE.replace('o.', 'o2.')}
                        AND o2.loose_key IS NOT NULL AND o2.loose_key <> '')
                OR o.dedupe_key IN (
                     SELECT o3.dedupe_key FROM offers o3
                       JOIN clasificaciones c ON c.dedupe_key = o3.dedupe_key
                      WHERE {self._CIEGAS_WHERE.replace('o.', 'o3.')}
                        AND (o3.loose_key IS NULL OR o3.loose_key = ''))
             ORDER BY COALESCE(o.posted_at, o.first_seen_at) DESC
            """
        ).fetchall()

    # Lo que un hermano copia de su representante. Es la misma lista que arma
    # `motor._heredar`, y tiene que seguir siéndolo: si las dos se separan,
    # heredar en caliente y reparar en frío dejarían filas distintas.
    #
    # Fuera quedan, a propósito: `dedupe_key` y `heredada_de` (identidad),
    # los tokens y el coste (esta fila no costó nada y el resumen tiene que
    # decir la verdad), y `clasificado_at`, que se copia del padre porque el
    # veredicto se produjo en SU momento, no en el de la reparación.
    _COLUMNAS_HEREDABLES = (
        "estado", "relevante", "motivo_descarte", "confianza", "razon",
        "cv_id", "idioma_anuncio", "idioma_detectado", "empresa_normalizada",
        "puesto_canonico", "es_reclutadora", "seniority_detectado", "modalidad",
        "necesita_criterio", "motivo_criterio", "error", "modelo",
        "fichas_huella", "descripcion_via", "caracteres_descripcion",
        "clasificado_at",
    )

    def propagar_veredictos_heredados(self) -> int:
        """Pone al día los hermanos cuyo veredicto no coincide con su padre.
        Devuelve cuántas filas cambió. NO CUESTA NADA: es una copia.

        UN HERMANO QUE DISCREPA DE SU PADRE ES UN FALLO POR CONSTRUCCIÓN. La
        fila dice `heredada_de = X` y a la vez dice algo distinto de lo que
        dice X, así que una de las dos afirmaciones es mentira. Medido el 3
        septiembre 2026: 10 filas en ese estado, porque la relectura de la
        sesión pasada se hizo con un script suelto que solo tocó a los
        representantes.

        Va antes de releer y no después, y el orden es el ahorro: tres de las
        36 vacantes que parecían necesitar relectura solo tenían hijos
        atrasados —el padre ya había leído el anuncio entero—, y eso se
        arregla copiando en vez de pagando. Es la regla de la casa: el LLM no
        toca nada que una regla pueda resolver.
        """
        asignaciones = ", ".join(
            f"{c} = (SELECT p.{c} FROM clasificaciones p "
            f"WHERE p.dedupe_key = clasificaciones.heredada_de)"
            for c in self._COLUMNAS_HEREDABLES
        )
        # Solo las que DE VERDAD difieren: un UPDATE incondicional tocaría las
        # 21 filas heredadas cada vez y el recuento dejaría de significar nada.
        difieren = " OR ".join(
            f"COALESCE(clasificaciones.{c}, '') IS NOT "
            f"COALESCE((SELECT p.{c} FROM clasificaciones p "
            f"WHERE p.dedupe_key = clasificaciones.heredada_de), '')"
            for c in self._COLUMNAS_HEREDABLES
            if c != "clasificado_at"  # siempre difiere: no es señal de nada
        )
        cursor = self.connection.execute(
            f"""
            UPDATE clasificaciones SET {asignaciones}
             WHERE heredada_de IS NOT NULL
               AND EXISTS (SELECT 1 FROM clasificaciones p
                            WHERE p.dedupe_key = clasificaciones.heredada_de)
               AND ({difieren})
            """
        )
        self.connection.commit()
        return cursor.rowcount

    def lecturas_medidas(self) -> list[sqlite3.Row]:
        """Las lecturas que de verdad se pagaron, con lo que costaron.

        Es el espejo de `corridas_de_triaje` para el clasificador, y se apoya
        en que `clasificaciones` ya guarda tokens y coste por fila. Lo que
        faltaba no eran los datos: era `caracteres_descripcion`, sin la cual no
        se sabe QUÉ constante le tocaba a cada fila.

        Se excluyen las HEREDADAS, que tienen los tokens a cero por diseño:
        meterlas hundiría la media con lecturas que nadie hizo. Y se excluye lo
        que quedó `sin_clasificar`, que puede tener la salida truncada por un
        error y no representa una lectura completa.
        """
        return self.connection.execute(
            """
            SELECT c.dedupe_key, c.descripcion_via, c.caracteres_descripcion,
                   c.tokens_entrada, c.tokens_salida, c.coste_usd,
                   c.confianza, c.necesita_criterio, c.clasificado_at,
                   c.modelo, o.title
              FROM clasificaciones c
              JOIN offers o ON o.dedupe_key = c.dedupe_key
             WHERE c.heredada_de IS NULL
               AND c.tokens_salida > 0
               AND c.estado = 'ok'
             ORDER BY c.clasificado_at
            """
        ).fetchall()

    def ofertas_por_clave(self, claves: Sequence[str]) -> list[sqlite3.Row]:
        """Las filas de `offers` de esas claves, en el mismo orden que se piden.

        El orden importa: `agrupar_para_ahorrar` conserva el orden de llegada
        para elegir representante, así que devolver las filas barajadas haría
        que se leyera un anuncio distinto del que el plan costeó.
        """
        if not claves:
            return []
        marcadores = ", ".join("?" for _ in claves)
        filas = {
            f["dedupe_key"]: f
            for f in self.connection.execute(
                f"SELECT * FROM offers WHERE dedupe_key IN ({marcadores})",
                list(claves),
            )
        }
        return [filas[c] for c in claves if c in filas]

    def contar_obsoletas(self, fichas_huella: str) -> int:
        """Clasificaciones hechas con una versión anterior de las fichas."""
        row = self.connection.execute(
            "SELECT COUNT(*) AS total FROM clasificaciones WHERE fichas_huella <> ?",
            (fichas_huella,),
        ).fetchone()
        return row["total"] if row else 0

    def resumen_clasificaciones(self) -> dict[str, Any]:
        """Los números que imprime el comando y que pinta la bandeja."""
        fila = self.connection.execute(
            """
            SELECT
                COUNT(*)                                            AS total,
                SUM(CASE WHEN estado = 'ok' THEN 1 ELSE 0 END)      AS ok,
                SUM(CASE WHEN estado <> 'ok' THEN 1 ELSE 0 END)     AS sin_clasificar,
                SUM(CASE WHEN relevante = 1 THEN 1 ELSE 0 END)      AS relevantes,
                SUM(CASE WHEN relevante = 0 THEN 1 ELSE 0 END)      AS descartadas,
                SUM(CASE WHEN necesita_criterio = 1 THEN 1 ELSE 0 END) AS necesita_criterio,
                SUM(CASE WHEN es_reclutadora = 1 THEN 1 ELSE 0 END) AS via_reclutadora,
                COALESCE(SUM(tokens_entrada), 0)                    AS tokens_entrada,
                COALESCE(SUM(tokens_salida), 0)                     AS tokens_salida,
                COALESCE(SUM(coste_usd), 0.0)                       AS coste_usd
            FROM clasificaciones
            """
        ).fetchone()

        por_cv = {
            r["cv_id"]: r["total"]
            for r in self.connection.execute(
                "SELECT cv_id, COUNT(*) AS total FROM clasificaciones "
                "WHERE cv_id IS NOT NULL GROUP BY cv_id ORDER BY total DESC"
            )
        }
        return {**dict(fila), "por_cv": por_cv}

    def claves_de_agrupacion(self) -> list[sqlite3.Row]:
        """Lo mínimo para decidir qué ofertas son la misma vacante.

        Devuelve también `loose_key`, que ya calculaba la v0 (empresa + título
        sin ubicación) y que hasta ahora solo se usaba para MEDIR la
        sub-fusión en el reporte. Aquí pasa a ser operativa: resuelve gratis
        el caso de una misma vacante publicada en varias ciudades.
        """
        return self.connection.execute(
            f"""
            SELECT o.dedupe_key, o.loose_key, o.title, o.company, o.location_raw,
                   o.market_key, o.canonical_url, o.status,
                   {self._COLUMNAS_CLASIFICACION}
            FROM offers o
            LEFT JOIN clasificaciones c ON c.dedupe_key = o.dedupe_key
            ORDER BY o.market_key, o.company, o.title
            """
        ).fetchall()

    def listar_ofertas_con_clasificacion(
        self,
        status: str | None = None,
        market_key: str | None = None,
        relevancia: str = "todas",
        limit: int = 200,
        zona: str | None = None,
    ) -> list[sqlite3.Row]:
        """Lo que consume la bandeja: cada oferta con su clasificación al lado.

        `relevancia` filtra por lo que dijo la capa 2:
            todas        no filtra
            relevantes   solo las que el modelo dio por buenas
            descartadas  solo las que marcó como ruido
            criterio     solo las que quedaron bajo el umbral de confianza
            pendientes   todo lo que no ha pasado por el LLM (las dos de abajo)
            aplazadas    el triaje las vio, las ordenó y las dejó para después
            sin_mirar    nadie las ha mirado nunca

        LAS DOS ÚLTIMAS SON LA REGLA Nº 1 DE LA CASA. Una oferta aplazada tiene
        un puesto y un motivo escrito por el asistente; una sin mirar no tiene
        nada. Si la bandeja las enseñara igual —o peor, si solo hubiera
        `pendientes`— el triaje sería un filtro silencioso con pasos de más, y
        la frase «el asistente la vio y la dejó en el puesto 34, porque X» no se
        podría escribir en ningún sitio.
        """
        where, params = self._filtros_de_bandeja(status, market_key, relevancia, zona)
        params = [*params, limit]

        return self.connection.execute(
            f"""
            SELECT o.*, {self._COLUMNAS_CLASIFICACION}, {self._COLUMNAS_TRIAJE}
            FROM offers o
            LEFT JOIN clasificaciones c ON c.dedupe_key = o.dedupe_key
            LEFT JOIN triajes t         ON t.dedupe_key = o.dedupe_key
            {where}
            ORDER BY
                -- Primero lo que ella tiene que mirar: relevantes arriba,
                -- ruido abajo. Dentro de cada grupo, lo más reciente primero.
                CASE
                    WHEN c.necesita_criterio = 1 THEN 0
                    WHEN c.relevante = 1         THEN 1
                    WHEN c.dedupe_key IS NULL    THEN 2
                    ELSE 3
                END,
                -- Dentro de lo pendiente manda el orden que puso el triaje: el
                -- puesto 3 antes que el 34. Es información que ya se pagó, y
                -- tirarla para ordenar por fecha sería desperdiciar la única
                -- señal que tienen estas ofertas. Las que nadie miró van
                -- después, no escondidas: sin puesto, se ordenan por fecha.
                CASE WHEN c.dedupe_key IS NULL
                     THEN COALESCE(t.posicion, 999999) ELSE 0 END,
                COALESCE(o.posted_at, o.first_seen_at) DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    def _filtros_de_bandeja(
        self, status: str | None, market_key: str | None, relevancia: str,
        zona: str | None = None,
    ) -> tuple[str, list[Any]]:
        """El WHERE de la bandeja, en UN solo sitio.

        Estaba escrito dentro de la consulta que lista, y hacía falta también
        para CONTAR: la pantalla enseña un tope de publicaciones y tiene que
        poder decir cuántas se quedaron fuera, y un recuento que se calculara
        con otras condiciones diría un número que no es el de la lista. Es la
        regla nº 9 de la casa —un solo cálculo por respuesta— aplicada a un
        filtro en vez de a un importe.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if status and status != "todas":
            clauses.append("o.status = ?")
            params.append(status)
        if market_key and market_key != "todos":
            clauses.append("o.market_key = ?")
            params.append(market_key)
        # La zona solo significa algo dentro de un mercado, pero se filtra
        # igual sin él: una clave de zona es de un único mercado.
        if zona and zona != "todas":
            clauses.append("o.zona = ?")
            params.append(zona)

        if relevancia == "relevantes":
            clauses.append("c.relevante = 1 AND c.necesita_criterio = 0")
        elif relevancia == "descartadas":
            clauses.append("c.relevante = 0 AND c.necesita_criterio = 0")
        elif relevancia == "criterio":
            clauses.append("(c.necesita_criterio = 1 OR c.estado = 'sin_clasificar')")
        elif relevancia == "pendientes":
            clauses.append("c.dedupe_key IS NULL")
        elif relevancia == "aplazadas":
            clauses.append("c.dedupe_key IS NULL AND t.dedupe_key IS NOT NULL")
        elif relevancia == "sin_mirar":
            clauses.append("c.dedupe_key IS NULL AND t.dedupe_key IS NULL")

        return (f"WHERE {' AND '.join(clauses)}" if clauses else "", params)

    def contar_ofertas_de_bandeja(
        self,
        status: str | None = None,
        market_key: str | None = None,
        relevancia: str = "todas",
        zona: str | None = None,
    ) -> int:
        """Cuántos ANUNCIOS hay con ese filtro, sin tope.

        Es la mitad honesta del tope de la pantalla: sin este número, «se
        enseñan 600» no se puede convertir en «se enseñan 600 de 812», y la
        diferencia entre las dos frases es que la segunda se puede leer y la
        primera esconde lo que falta.
        """
        where, params = self._filtros_de_bandeja(status, market_key, relevancia, zona)
        return self.connection.execute(
            f"""
            SELECT COUNT(*)
            FROM offers o
            LEFT JOIN clasificaciones c ON c.dedupe_key = o.dedupe_key
            LEFT JOIN triajes t         ON t.dedupe_key = o.dedupe_key
            {where}
            """,
            params,
        ).fetchone()[0]

    def obtener_oferta_con_clasificacion(self, dedupe_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            f"""
            SELECT o.*, {self._COLUMNAS_CLASIFICACION}, {self._COLUMNAS_TRIAJE}
            FROM offers o
            LEFT JOIN clasificaciones c ON c.dedupe_key = o.dedupe_key
            LEFT JOIN triajes t         ON t.dedupe_key = o.dedupe_key
            WHERE o.dedupe_key = ?
            """,
            (dedupe_key,),
        ).fetchone()

    def resumen_pendientes(self) -> dict[str, int]:
        """Cuántas pendientes hay de cada clase, para los filtros de la bandeja.

        `aplazadas` y `sin_mirar` se cuentan por separado porque significan
        cosas opuestas: una la miró el asistente y la dejó para después con un
        motivo escrito; la otra no la ha mirado nadie. Un solo número
        «pendientes» las haría indistinguibles, que es exactamente lo que la
        tabla `triajes` existe para impedir.
        """
        fila = self.connection.execute(
            """
            SELECT
                COUNT(*) AS pendientes,
                COALESCE(SUM(CASE WHEN t.dedupe_key IS NOT NULL THEN 1 ELSE 0 END), 0)
                    AS aplazadas,
                COALESCE(SUM(CASE WHEN t.dedupe_key IS NULL THEN 1 ELSE 0 END), 0)
                    AS sin_mirar
              FROM offers o
              LEFT JOIN clasificaciones c ON c.dedupe_key = o.dedupe_key
              LEFT JOIN triajes t         ON t.dedupe_key = o.dedupe_key
             WHERE c.dedupe_key IS NULL
            """
        ).fetchone()
        return {k: int(fila[k]) for k in ("pendientes", "aplazadas", "sin_mirar")}
