"""
Pruebas de la capa 2 — el ruteo de CVs.

Todas offline: ni red, ni API key, ni un céntimo gastado. Eso es posible
porque `Motor` recibe el cliente por parámetro, así que aquí se le pasa uno
falso que devuelve lo que la prueba quiera.

Lo que se prueba es lo que, si falla en silencio, hace que el sistema mande
el CV equivocado o le enseñe a la clienta ruido como si fuera una oportunidad:

  - detección de idioma (es la que decide el CV)
  - el umbral de confianza y el contraste entre las dos señales
  - que un fallo de la API acabe en "sin clasificar", nunca en una
    clasificación inventada
  - la caché: una oferta ya clasificada no se vuelve a mandar
  - la agrupación de la misma vacante publicada varias veces
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clasificador.agrupador import agrupar, grupos_con_repeticion
from clasificador.cliente_llm import RespuestaLLM, coste_de
from clasificador.esquema import Clasificacion
from clasificador.fichas import Ficha, Fichas, FichasError, cargar_fichas
from clasificador.idioma import _normalizar, detectar_idioma
from clasificador.motor import CV_SUPLENTE, Motor
from clasificador.prompt import construir_oferta, construir_sistema
from recolector.models import JobOffer
from recolector.dedupe import assign_keys
from recolector.storage import Storage

from .fixtures import adzuna_alemania_coo_logistics

# ---------------------------------------------------------------------------
#  Andamiaje
# ---------------------------------------------------------------------------

TEXTO_ES = (
    "Buscamos un Director de Operaciones para dirigir la operación integral de "
    "almacenes, transporte y mantenimiento de una red de distribución. El puesto "
    "reporta al consejo de administración y tiene a su cargo el presupuesto de "
    "logística y los equipos de distribución."
)

TEXTO_EN = (
    "We are seeking a Chief Operating Officer to lead the asset maintenance "
    "business nationwide. The role reports to the CEO and is accountable for "
    "profitability, operational excellence and safety across roadway and "
    "logistics maintenance contracts in the United States."
)


# El anuncio alemán NO se escribe aquí: se saca del mismo fixture que usa el
# pipeline, para que no puedan divergir. Es el primero de los cinco de
# `adzuna_alemania_coo_logistics`, y está redactado en alemán a propósito
# (ver el docstring del fixture: el 11% del inventario alemán real lo está).
_ANUNCIO_DE = adzuna_alemania_coo_logistics()["results"][0]
TITULO_DE = "Chief Operating Officer (COO) - Logistics"
TEXTO_DE = _ANUNCIO_DE["description"]

# El recorte que devuelve la API de Adzuna: 500 caracteres y punto. Es el
# estado de CUALQUIER anuncio recién recolectado, antes de que corra
# `recolector descripciones`, y por tanto el texto con el que se clasifica si
# ella pulsa "Ponme al día" y el tramo de descripciones se queda fuera del tope.
TEXTO_DE_RECORTE = (
    "Wir sind ein führendes Unternehmen der Logistikinfrastruktur und suchen "
    "einen Chief Operating Officer für den Betrieb unserer Anlagen. Sie "
    "verantworten die operative Steuerung, die Weiterentwicklung der "
    "Organisation und die Zusammenarbeit mit den Behörden. Ihre Aufgaben "
    "umfassen die Planung und die Umsetzung der Betriebsstrategie."
)


def ficha(cv_id: str, idioma: str) -> Ficha:
    """Ficha inventada. Se prueba el ruteo SIN manejar documentos reales:
    esa es una de las cuatro razones por las que el LLM lee la ficha y no el PDF.
    """
    return Ficha(
        id=cv_id,
        nombre=f"CV de prueba {cv_id}",
        archivo=Path("data/cvs/inventado.docx"),
        idioma=idioma,
        sector="Logística y manufactura",
        puestos_objetivo=["Director de Operaciones", "COO"],
        palabras_clave=["logística", "manufactura"],
        seniority="C-level, 15+ años",
        no_aplica=["Puestos técnicos sin dirección"],
    )


def fichas_de_prueba(umbral: float = 0.70) -> Fichas:
    return Fichas(
        umbral_confianza=umbral,
        lista=[ficha("cv_es", "es"), ficha("cv_en", "en")],
        huella="huella_de_prueba",
    )


def clasificacion(**overrides) -> Clasificacion:
    datos = dict(
        relevante=True,
        motivo_descarte=None,
        confianza=0.9,
        razon="Pide dirección de operaciones en logística, que es su perfil.",
        cv_id="cv_es",
        idioma_anuncio="es",
        empresa_normalizada="Acme",
        puesto_canonico="Director de Operaciones",
        es_reclutadora=False,
        seniority_detectado="c_level",
        modalidad="presencial",
    )
    datos.update(overrides)
    return Clasificacion(**datos)


class ClienteFalso:
    """Devuelve siempre lo mismo y cuenta cuántas veces lo llamaron.

    El contador es la prueba de la caché: si una oferta ya clasificada se
    volviera a mandar, este número subiría.

    Y APUNTA EL GASTO SI SE LE DA UN LIBRO, igual que `ClienteAnthropic` desde
    el 3 septiembre 2026. No es adorno de la prueba: el que apunta es el
    cliente, así que un falso que no apuntara haría que las pruebas del techo
    de gasto pasaran sobre un libro siempre vacío — es decir, no probarían
    nada. Ver `cliente_llm.ClienteAnthropic`.
    """

    modelo = "modelo-de-prueba"

    def __init__(self, respuesta: RespuestaLLM, libro=None) -> None:
        self.respuesta = respuesta
        self.llamadas = 0
        self._libro = libro

    def clasificar(self, sistema: str, oferta: str) -> RespuestaLLM:
        self.llamadas += 1
        if self._libro is not None:
            self._libro.registrar(self.respuesta.coste_usd)
        return self.respuesta


def respuesta_ok(**overrides) -> RespuestaLLM:
    return RespuestaLLM(
        clasificacion=clasificacion(**overrides),
        tokens_entrada=900,
        tokens_salida=400,
        modelo="modelo-de-prueba",
    )


def oferta_en_base(almacen: Storage, run_id: str, **overrides) -> str:
    """Mete una oferta en la base y devuelve su clave."""
    datos = dict(
        source="adzuna",
        market_key="mexico",
        query_label="Director de Operaciones + Manufactura",
        external_id="1",
        title="Director de Operaciones",
        company="Acme",
        location_raw="Ciudad de México",
        location_city="Ciudad de México",
        location_region=None,
        location_country="Mexico",
        url="https://example.test/1",
        description=TEXTO_ES,
    )
    datos.update(overrides)
    oferta = assign_keys(JobOffer(**datos))
    almacen.save_offer(oferta, run_id)
    almacen.commit()
    return oferta.dedupe_key


@pytest.fixture()
def almacen(tmp_path: Path):
    with Storage(tmp_path / "prueba.db") as db:
        yield db


# ---------------------------------------------------------------------------
#  Detección de idioma — la que decide el CV
# ---------------------------------------------------------------------------

def test_detecta_espanol():
    d = detectar_idioma("Director de Operaciones", TEXTO_ES)
    assert d.idioma == "es"
    assert d.es_fiable


def test_detecta_ingles():
    d = detectar_idioma("Chief Operating Officer", TEXTO_EN)
    assert d.idioma == "en"
    assert d.es_fiable


def test_texto_corto_no_afirma_idioma():
    """Dos palabras no son evidencia de nada. Preferimos no saber."""
    d = detectar_idioma("COO", "")
    assert d.idioma == "desconocido"
    assert not d.es_fiable


def test_nombre_propio_en_ingles_no_desvia_un_anuncio_espanol():
    """'Instituto Global de Recursos' dentro de un anuncio en español.

    Es un caso real de la base: si el nombre de la empresa contara, el
    sistema mandaría el CV en inglés a una oferta mexicana escrita en español.
    """
    d = detectar_idioma(
        "Dirección de Manufactura",
        "El Programa de Manufactura de Instituto Global de Recursos México trabaja con "
        "gobiernos, empresas e instituciones financieras para acelerar la "
        "transición energética mediante investigación rigurosa y colaboración.",
    )
    assert d.idioma == "es"


def test_detecta_aleman():
    """El mismo anuncio que usa el pipeline, leído por el detector.

    Antes del 5 septiembre 2026 esto devolvía **`es` con 0,75 de confianza**:
    el anuncio alemán que la v12 metió en las fixtures se habría llevado la
    ficha en ESPAÑOL, y ninguna prueba lo veía.
    """
    d = detectar_idioma(TITULO_DE, TEXTO_DE)
    assert d.idioma == "de"
    assert d.es_fiable


def test_el_recorte_de_la_api_de_un_anuncio_aleman_no_se_lee_como_espanol():
    """500 caracteres es el estado de cualquier anuncio recién recolectado.

    Medido sobre las 36 alemanas de la base el 5 septiembre 2026: con el
    recorte, 4 salían `es`, y a esas se les habría adjuntado la ficha en
    español. Con el anuncio entero salían `en` —el CV correcto— pero por
    casualidad, no por criterio.
    """
    d = detectar_idioma(TITULO_DE, TEXTO_DE_RECORTE)
    assert d.idioma == "de"
    assert d.es_fiable


def test_un_anuncio_aleman_sin_dieresis_tambien_se_reconoce():
    """Sin ä, sin ö y sin ß: aquí solo pueden decidir las palabras.

    Existe porque las otras pruebas de alemán no se caían al vaciar
    `MARCADORES_DE`: se apoyaban enteras en el empujón de los caracteres, y
    una prueba que pasa con la lista vacía no está probando la lista. Hay
    alemán corriente sin un solo diacrítico, y es el caso que quedaba
    descubierto.
    """
    d = detectar_idioma(
        "Chief Operating Officer",
        "Wir sind ein Unternehmen der Logistikinfrastruktur und suchen einen "
        "Chief Operating Officer. Sie verantworten den Betrieb der Anlagen, "
        "die operative Steuerung und die Weiterentwicklung der Organisation. "
        "Das Team besteht aus 200 Mitarbeitenden und berichtet an die "
        "Geschaeftsleitung.",
    )
    assert d.idioma == "de"
    assert d.es_fiable


def test_una_lista_de_requisitos_en_aleman_se_reconoce_por_las_terminaciones():
    """El caso para el que existen los sufijos, y que ninguna prueba sujetaba.

    Lo dice el comentario de `SUFIJOS_*` en `idioma.py`: un anuncio que solo
    enumera requisitos puede no tener ni un "der" ni un "und". Lo que sí tiene
    es media docena de palabras acabadas en -ung y en -keit, que no existen en
    ninguno de los otros dos idiomas.
    """
    d = detectar_idioma(
        "Chief Operating Officer",
        "Abgeschlossenes Studium, mehrjaehrige Berufserfahrung, "
        "Fuehrungserfahrung, ausgepraegte Kommunikationsfaehigkeit, "
        "Verantwortung, Weiterentwicklung, unternehmerisches Denken, "
        "technisches Verstaendnis, Zuverlaessigkeit, Belastbarkeit",
    )
    assert d.idioma == "de"
    assert d.es_fiable


def test_la_dieresis_sola_no_puntua_para_el_espanol():
    """La 'ü' la comparten los dos idiomas, así que no puede valer 6 puntos
    para uno de ellos.

    Era el fallo entero: tres de los cuatro anuncios alemanes que salían como
    españoles no tenían NI UNA palabra funcional española. Sus seis puntos
    eran la diéresis y nada más.
    """
    d = detectar_idioma(TITULO_DE, TEXTO_DE)
    assert "caracteres propios del español" not in d.motivo


def test_la_eszett_no_fabrica_un_marcador_de_espanol():
    """`ß` no es una 's' con adorno: NFD no la descompone, así que `[a-z]+`
    partía la palabra en dos y el trozo de la derecha podía ser un marcador
    de otro idioma. "großes" salía como ["gro", "es"], y ese "es" contaba
    como el verbo español.
    """
    assert _normalizar("Großes Team") == ["grosses", "team"]


def test_el_motivo_de_la_deteccion_es_legible():
    """La decisión que elige el CV no puede ser una caja negra."""
    d = detectar_idioma("Director de Operaciones", TEXTO_ES)
    assert "español" in d.motivo and "inglés" in d.motivo


# ---------------------------------------------------------------------------
#  Fichas
# ---------------------------------------------------------------------------

def test_fichas_reales_del_proyecto_cargan():
    fichas = cargar_fichas()
    assert len(fichas.lista) >= 1
    assert 0.0 < fichas.umbral_confianza < 1.0


def test_archivo_de_cv_inexistente_falla_ruidosamente(tmp_path: Path):
    yaml = tmp_path / "cvs.yaml"
    yaml.write_text(
        "version: 1\n"
        "umbral_confianza: 0.7\n"
        "cvs:\n"
        "  - id: uno\n"
        "    nombre: Uno\n"
        "    archivo: data/cvs/no_existe_seguro.pdf\n"
        "    idioma: es\n"
        "    sector: Logística\n"
        "    puestos_objetivo: ['COO']\n"
        "    seniority: C-level\n",
        encoding="utf-8",
    )
    with pytest.raises(FichasError) as exc:
        cargar_fichas(yaml)
    assert "no_existe_seguro" in str(exc.value)


def test_umbral_fuera_de_rango_falla(tmp_path: Path):
    yaml = tmp_path / "cvs.yaml"
    yaml.write_text(
        "version: 1\numbral_confianza: 70\ncvs:\n  - id: uno\n", encoding="utf-8"
    )
    with pytest.raises(FichasError) as exc:
        cargar_fichas(yaml)
    assert "fuera de rango" in str(exc.value)


def test_ids_repetidos_fallan(tmp_path: Path):
    """Si se repite un id, la segunda ficha nunca se usaría. Silencioso y caro."""
    real = Path("data/cvs")
    archivos = list(real.glob("*")) if real.exists() else []
    if not archivos:
        pytest.skip("No hay CVs en data/cvs/ en este clon")

    yaml = tmp_path / "cvs.yaml"
    bloque = (
        f"  - id: repetido\n"
        f"    nombre: Uno\n"
        f"    archivo: {archivos[0].as_posix()}\n"
        f"    idioma: es\n"
        f"    sector: Logística\n"
        f"    puestos_objetivo: ['COO']\n"
        f"    seniority: C-level\n"
    )
    yaml.write_text(f"version: 1\ncvs:\n{bloque}{bloque}", encoding="utf-8")
    with pytest.raises(FichasError) as exc:
        cargar_fichas(yaml)
    assert "repetido" in str(exc.value)


# ---------------------------------------------------------------------------
#  Prompt
# ---------------------------------------------------------------------------

def test_el_sistema_lleva_las_fichas_y_los_ids_validos():
    texto = construir_sistema(fichas_de_prueba())
    assert "cv_es" in texto and "cv_en" in texto
    assert "No inventes otros" in texto


def test_se_avisa_de_la_descripcion_truncada():
    """19 de las 21 ofertas reales vienen cortadas por el portal. El modelo
    tiene que saberlo para bajar su confianza en vez de rellenar el hueco.
    """
    texto = construir_oferta(
        titulo="COO", empresa="Acme", ubicacion="Austin", mercado="texas_us",
        descripcion="Una descripción larga que el portal cortó a la mitad y que termina así…",
        idioma_detectado="en",
    )
    assert "CORTADA" in texto


def test_se_avisa_de_la_descripcion_casi_vacia():
    texto = construir_oferta(
        titulo="COO", empresa="Acme", ubicacion="Austin", mercado="texas_us",
        descripcion="Muy corta.", idioma_detectado="en",
    )
    assert "muy poca información" in texto.lower()


# ---------------------------------------------------------------------------
#  Motor: umbral, contraste y fallos
# ---------------------------------------------------------------------------

def test_el_cv_sale_del_idioma_no_de_lo_que_diga_el_modelo(almacen: Storage):
    """El anuncio está en español; el modelo dice 'cv_en'. Manda el idioma,
    y la discrepancia se marca para que la revise ella.
    """
    run = almacen.start_run("prueba", ["adzuna"], ["mexico"])
    oferta_en_base(almacen, run)

    cliente = ClienteFalso(respuesta_ok(cv_id="cv_en", idioma_anuncio="en"))
    motor = Motor(almacen, fichas_de_prueba(), cliente)
    resumen = motor.clasificar(motor.pendientes())

    resultado = resumen.resultados[0]
    assert resultado.cv_final == "cv_es"
    assert resultado.necesita_criterio
    assert "Discrepancia" in (resultado.motivo_criterio or "")


def test_un_anuncio_aleman_lleva_el_cv_en_ingles(almacen: Storage):
    """La decisión de la clienta, ejecutada: no hay ficha en alemán y no la va
    a haber, así que un anuncio en alemán lleva la inglesa.

    Y NO se marca como duda. Es una decisión escrita en `CV_SUPLENTE`, no un
    empate: pedirle criterio sobre algo ya resuelto, en todos los anuncios
    alemanes y siempre con la misma respuesta, es enseñarle a no leer los
    avisos.
    """
    run = almacen.start_run("prueba", ["adzuna"], ["alemania"])
    oferta_en_base(almacen, run, title=TITULO_DE, description=TEXTO_DE)

    cliente = ClienteFalso(respuesta_ok(cv_id="cv_en", idioma_anuncio="de"))
    motor = Motor(almacen, fichas_de_prueba(), cliente)
    resumen = motor.clasificar(motor.pendientes())

    resultado = resumen.resultados[0]
    assert resultado.cv_final == "cv_en"
    assert not resultado.necesita_criterio
    assert CV_SUPLENTE["de"] == "en"


def test_si_el_modelo_manda_otro_cv_a_un_anuncio_aleman_se_marca(almacen: Storage):
    """El suplente manda, pero la discrepancia no se traga.

    Que el modelo diga `cv_es` sobre un anuncio alemán es exactamente la señal
    de que una de las dos lecturas está mal, y es la que hasta hoy no podía
    saltar nunca: con `Idioma = Literal["es", "en"]` el modelo no tenía forma
    de decir "de" ni queriendo.
    """
    run = almacen.start_run("prueba", ["adzuna"], ["alemania"])
    oferta_en_base(almacen, run, title=TITULO_DE, description=TEXTO_DE)

    cliente = ClienteFalso(respuesta_ok(cv_id="cv_es", idioma_anuncio="es"))
    motor = Motor(almacen, fichas_de_prueba(), cliente)
    resumen = motor.clasificar(motor.pendientes())

    resultado = resumen.resultados[0]
    assert resultado.cv_final == "cv_en"
    assert resultado.necesita_criterio
    assert "cv_es" in (resultado.motivo_criterio or "")


def test_confianza_baja_pide_criterio(almacen: Storage):
    run = almacen.start_run("prueba", ["adzuna"], ["mexico"])
    oferta_en_base(almacen, run)

    cliente = ClienteFalso(respuesta_ok(confianza=0.55))
    motor = Motor(almacen, fichas_de_prueba(umbral=0.70), cliente)
    resumen = motor.clasificar(motor.pendientes())

    assert resumen.necesitan_criterio == 1
    assert "por debajo del umbral" in (resumen.resultados[0].motivo_criterio or "")


def test_confianza_alta_no_pide_criterio(almacen: Storage):
    run = almacen.start_run("prueba", ["adzuna"], ["mexico"])
    oferta_en_base(almacen, run)

    motor = Motor(almacen, fichas_de_prueba(), ClienteFalso(respuesta_ok(confianza=0.92)))
    resumen = motor.clasificar(motor.pendientes())

    assert resumen.necesitan_criterio == 0
    assert resumen.relevantes == 1


def test_un_fallo_de_la_api_no_inventa_una_clasificacion(almacen: Storage):
    """La regla que más importa de todo el módulo: si no se puede clasificar,
    se dice, y se dice por qué. Nunca un cv_id a boleo.
    """
    run = almacen.start_run("prueba", ["adzuna"], ["mexico"])
    clave = oferta_en_base(almacen, run)

    fallo = RespuestaLLM(
        clasificacion=None, tokens_entrada=0, tokens_salida=0,
        modelo="modelo-de-prueba", error="No se pudo conectar con la API.",
    )
    motor = Motor(almacen, fichas_de_prueba(), ClienteFalso(fallo))
    resumen = motor.clasificar(motor.pendientes())

    assert resumen.fallidas == 1
    assert resumen.resultados[0].cv_final is None

    fila = almacen.obtener_oferta_con_clasificacion(clave)
    assert fila["clas_estado"] == "sin_clasificar"
    assert fila["clas_cv_id"] is None
    assert "API" in fila["clas_error"]


def test_un_cv_id_inventado_por_el_modelo_no_se_guarda(almacen: Storage):
    """Si el modelo devuelve un id que no existe y encima no se sabe el
    idioma, no hay CV que valga: se marca y decide ella.
    """
    run = almacen.start_run("prueba", ["adzuna"], ["mexico"])
    oferta_en_base(almacen, run, title="COO", description="")

    cliente = ClienteFalso(respuesta_ok(cv_id="cv_que_no_existe"))
    motor = Motor(almacen, fichas_de_prueba(), cliente)
    resumen = motor.clasificar(motor.pendientes())

    assert resumen.resultados[0].cv_final is None
    assert resumen.resultados[0].necesita_criterio


def test_la_cache_evita_reclasificar(almacen: Storage):
    """Sin esto, cada corrida del recolector reclasificaría todo el histórico.
    Es un requisito, no una optimización.
    """
    run = almacen.start_run("prueba", ["adzuna"], ["mexico"])
    oferta_en_base(almacen, run)

    fichas = fichas_de_prueba()
    cliente = ClienteFalso(respuesta_ok())
    motor = Motor(almacen, fichas, cliente)

    motor.clasificar(motor.pendientes())
    assert cliente.llamadas == 1

    # Segunda pasada: no debe quedar nada pendiente ni gastarse un token.
    assert motor.pendientes() == []
    motor.clasificar(motor.pendientes())
    assert cliente.llamadas == 1


def test_cambiar_las_fichas_marca_obsoleto_pero_no_reclasifica_solo(almacen: Storage):
    run = almacen.start_run("prueba", ["adzuna"], ["mexico"])
    oferta_en_base(almacen, run)

    viejas = fichas_de_prueba()
    cliente = ClienteFalso(respuesta_ok())
    Motor(almacen, viejas, cliente).clasificar(
        Motor(almacen, viejas, cliente).pendientes()
    )

    nuevas = Fichas(
        umbral_confianza=viejas.umbral_confianza,
        lista=viejas.lista,
        huella="otra_huella",
    )
    motor_nuevo = Motor(almacen, nuevas, cliente)

    assert almacen.contar_obsoletas("otra_huella") == 1
    # Por defecto NO se rehace: reclasificar en silencio es gasto invisible.
    assert motor_nuevo.pendientes() == []
    assert len(motor_nuevo.pendientes(incluir_obsoletas=True)) == 1


def test_el_coste_se_calcula_sobre_los_tokens_reales():
    # 1M de entrada a $5 y 1M de salida a $25.
    assert coste_de(1_000_000, 0) == pytest.approx(5.00)
    assert coste_de(0, 1_000_000) == pytest.approx(25.00)


# ---------------------------------------------------------------------------
#  Agrupación: la misma vacante publicada varias veces
# ---------------------------------------------------------------------------

def _fila(clave: str, loose: str, titulo: str, empresa: str, ciudad: str, **clas):
    base = {
        "dedupe_key": clave,
        "loose_key": loose,
        "title": titulo,
        "company": empresa,
        "location_raw": ciudad,
        "market_key": "texas_us",
        "canonical_url": f"https://example.test/{clave}",
        "status": "nueva",
        "clas_empresa": None,
        "clas_puesto": None,
    }
    base.update(clas)
    return base


def test_misma_vacante_en_varias_ciudades_se_agrupa_gratis():
    """Cinco anuncios idénticos en cinco ciudades: los junta la clave laxa
    que la v0 ya calculaba. Cero tokens.
    """
    filas = [
        _fila(f"k{i}", "laxa-northline", "Chief Operating Officer", "Northline Asset Services", ciudad)
        for i, ciudad in enumerate(["Austin", "Dallas", "Houston", "Fort Worth", "San Antonio"])
    ]
    grupos = grupos_con_repeticion(agrupar(filas))
    assert len(grupos) == 1
    assert grupos[0].tamano == 5
    assert len(grupos[0].ubicaciones) == 5


def test_dos_nombres_de_empresa_distintos_se_juntan_por_lo_que_normalizo_el_modelo():
    """El caso de Harlan / Northline: la clave laxa NO los junta porque cambia el
    nombre de la empresa y el título. Solo los junta lo que devolvió el LLM.
    """
    filas = [
        _fila(
            "k1", "laxa-a", "Chief Operating Officer", "Northline Asset Services", "Dallas",
            clas_empresa="Harlan Contracting Group",
            clas_puesto="Chief Operating Officer",
        ),
        _fila(
            "k2", "laxa-b", "COO — National Logistics Growth Leader",
            "Harlan Contracting Group, LLC", "Austin",
            clas_empresa="Harlan Contracting Group, LLC",
            clas_puesto="Chief Operating Officer",
        ),
    ]
    grupos = grupos_con_repeticion(agrupar(filas))
    assert len(grupos) == 1
    assert grupos[0].tamano == 2


def test_la_agrupacion_es_transitiva():
    """A y B se juntan por la clave laxa; B y C por lo que normalizó el modelo.
    El resultado tiene que ser UN grupo de tres, no dos grupos sueltos. Es el
    caso real de las siete ofertas de Northline.
    """
    filas = [
        _fila("kA", "laxa-1", "Chief Operating Officer", "Northline", "Austin"),
        _fila(
            "kB", "laxa-1", "Chief Operating Officer", "Northline", "Dallas",
            clas_empresa="Harlan Contracting Group", clas_puesto="Chief Operating Officer",
        ),
        _fila(
            "kC", "laxa-2", "COO — National Logistics", "Harlan Contracting Group, LLC",
            "Houston",
            clas_empresa="Harlan Contracting Group", clas_puesto="Chief Operating Officer",
        ),
    ]
    grupos = grupos_con_repeticion(agrupar(filas))
    assert len(grupos) == 1
    assert grupos[0].tamano == 3


def test_ofertas_distintas_no_se_agrupan():
    filas = [
        _fila("k1", "laxa-1", "Chief Operating Officer", "Acme", "Austin"),
        _fila("k2", "laxa-2", "Senior Software Developer", "Pixelforge", "College Station"),
    ]
    assert grupos_con_repeticion(agrupar(filas)) == []


def test_el_representante_es_el_titulo_mas_informativo():
    filas = [
        _fila("k1", "laxa", "COO", "Acme", "Austin"),
        _fila("k2", "laxa", "COO — National Logistics Growth Leader", "Acme", "Dallas"),
    ]
    grupo = agrupar(filas)[0]
    assert "National Logistics" in grupo.representante.titulo


# ---------------------------------------------------------------------------
#  La lista negra de títulos — el filtro que le ahorra dinero
# ---------------------------------------------------------------------------
#  Se prueba contra el YAML REAL que se envía, no contra una lista inventada.
#  Si alguien añade un término demasiado ancho ("director", "manager"), estas
#  pruebas se ponen rojas ANTES de que el filtro se coma una oportunidad.

def _terminos_excluidos() -> list[str]:
    import yaml

    ruta = Path(__file__).resolve().parent.parent / "config" / "search_profile.yaml"
    datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    perfil = datos["profiles"][datos["active_profile"]]
    return perfil["hard_filters"]["exclude_title_terms"]


# Títulos REALES de la corrida del 27 agosto 2026 que eran ruido.
RUIDO_REAL = [
    "Business Development Manager",
    "Investment Management Analyst",
    "Lead Business Aligned Financial Analyst",
    "Senior Credit Analyst Monterrey",
    "Senior Software Developer",
    "Sr Business Strategy Associate",
    "Sr. Business & Strategy Analyst (Data Analytics)",
]

# Títulos REALES de la misma corrida que sí le servían. Ninguno puede caer.
BUENAS_REALES = [
    "COO — National Logistics Growth Leader (Austin)",
    "COO, Asset Maintenance - Strategic Growth Leader (Dallas)",
    "Chief Operating Officer",
    "Chief Operating Officer COO",
    "Dirección de Manufactura",
    "Director General",
    "Director de logística",
    "Gerente corporativo",
    "Scale-Driven COO for Marketplace Platform (Austin)",
]


@pytest.mark.parametrize("titulo", RUIDO_REAL)
def test_la_lista_negra_mata_el_ruido_conocido(titulo: str):
    from recolector.textnorm import contains_any_term

    assert contains_any_term(titulo, _terminos_excluidos()) is not None, (
        f"«{titulo}» era ruido en la corrida real y la lista negra ya no lo mata. "
        "Cada uno que se escape es una llamada al LLM que se paga de más."
    )


@pytest.mark.parametrize("titulo", BUENAS_REALES)
def test_la_lista_negra_no_mata_ninguna_buena(titulo: str):
    from recolector.textnorm import contains_any_term

    culpable = contains_any_term(titulo, _terminos_excluidos())
    assert culpable is None, (
        f"«{titulo}» es una oferta BUENA y la lista negra la descarta por «{culpable}». "
        "Un término demasiado ancho aquí le esconde una oportunidad sin que se entere."
    )


def test_la_lista_negra_compara_por_palabra_completa():
    """El riesgo real de una lista negra: matar por coincidencia parcial.

    «engineer» NO puede matar «Director of Engineering», ni «analyst» puede
    matar un puesto de «Analytics». Si esto se rompiera, el filtro empezaría
    a comerse ofertas buenas en silencio, que es el peor fallo posible aquí.
    """
    from recolector.textnorm import contains_any_term

    terminos = _terminos_excluidos()
    assert contains_any_term("Director of Engineering", terminos) is None
    assert contains_any_term("Head of Analytics and Operations", terminos) is None
    assert contains_any_term("Chief Operating Officer", terminos) is None


# ---------------------------------------------------------------------------
#  Agrupar ANTES de llamar — una vacante, una llamada
# ---------------------------------------------------------------------------

def _misma_vacante_en(almacen: Storage, ciudades: list[str]) -> list[str]:
    """La misma vacante publicada en varias ciudades, como hizo Northline en Texas."""
    claves = []
    for i, ciudad in enumerate(ciudades):
        claves.append(
            oferta_en_base(
                almacen,
                "run-1",
                external_id=f"dcs-{i}",
                title="Chief Operating Officer",
                company="Northline Asset Services",
                market_key="texas_us",
                location_raw=ciudad,
                location_city=ciudad,
                location_country="US",
                url=f"https://example.test/dcs-{i}",
                description=TEXTO_EN,
            )
        )
    return claves


def test_la_misma_vacante_en_cinco_ciudades_cuesta_una_sola_llamada(almacen: Storage):
    _misma_vacante_en(almacen, ["Austin", "Dallas", "Houston", "San Antonio", "Fort Worth"])

    cliente = ClienteFalso(respuesta_ok())
    motor = Motor(almacen, fichas_de_prueba(), cliente)
    filas = motor.pendientes()
    assert len(filas) == 5

    resumen = motor.clasificar(filas)

    assert cliente.llamadas == 1
    assert resumen.llamadas == 1
    assert resumen.heredadas == 4
    assert resumen.total == 5


def test_la_clasificacion_heredada_no_suma_coste(almacen: Storage):
    """Las copias van a cero tokens: el resumen de coste no puede mentir."""
    _misma_vacante_en(almacen, ["Austin", "Dallas", "Houston"])

    motor = Motor(almacen, fichas_de_prueba(), ClienteFalso(respuesta_ok()))
    resumen = motor.clasificar(motor.pendientes())

    heredadas = [r for r in resumen.resultados if r.heredada_de]
    assert len(heredadas) == 2
    assert all(r.tokens_entrada == 0 and r.tokens_salida == 0 for r in heredadas)

    # El coste total es el de UNA llamada, no el de tres.
    assert resumen.coste_usd == pytest.approx(coste_de(900, 400))


def test_la_heredada_guarda_de_quien_copio(almacen: Storage):
    """Sin esta huella, una copia parece una decisión propia del modelo."""
    _misma_vacante_en(almacen, ["Austin", "Dallas"])

    motor = Motor(almacen, fichas_de_prueba(), ClienteFalso(respuesta_ok()))
    resumen = motor.clasificar(motor.pendientes())

    pagada = [r for r in resumen.resultados if r.heredada_de is None][0]
    copia = [r for r in resumen.resultados if r.heredada_de][0]

    assert copia.heredada_de == pagada.dedupe_key
    assert copia.cv_final == pagada.cv_final
    assert copia.clasificacion is pagada.clasificacion


def test_las_copias_quedan_guardadas_en_la_base(almacen: Storage):
    """Heredar no puede dejar filas sin clasificar: si la bandeja filtra por
    'relevantes', los cinco anuncios tienen que tener fila, no solo el pagado.
    """
    _misma_vacante_en(almacen, ["Austin", "Dallas", "Houston", "San Antonio", "Fort Worth"])

    motor = Motor(almacen, fichas_de_prueba(), ClienteFalso(respuesta_ok()))
    motor.clasificar(motor.pendientes())

    assert motor.pendientes() == []
    assert almacen.resumen_clasificaciones()["total"] == 5


def test_vacantes_distintas_no_heredan_entre_si(almacen: Storage):
    """Lo peligroso sería lo contrario: copiar un veredicto a otra vacante."""
    _misma_vacante_en(almacen, ["Austin", "Dallas"])
    oferta_en_base(
        almacen, "run-1",
        external_id="otra",
        title="Dirección de Manufactura",
        company="Instituto Global de Recursos",
        url="https://example.test/otra",
    )

    cliente = ClienteFalso(respuesta_ok())
    motor = Motor(almacen, fichas_de_prueba(), cliente)
    resumen = motor.clasificar(motor.pendientes())

    # Dos llamadas: una por vacante. La tercera oferta NO copia de las otras.
    assert cliente.llamadas == 2
    assert resumen.llamadas == 2
    assert resumen.heredadas == 1


def test_el_dry_run_no_promete_mas_llamadas_de_las_que_hara(almacen: Storage):
    """El plan y la corrida tienen que contar lo mismo. Si `--dry-run` cobrara
    por los cinco anuncios y luego se hicieran dos llamadas, la estimación
    dejaría de servir para decidir si lanzar.
    """
    _misma_vacante_en(almacen, ["Austin", "Dallas", "Houston"])
    oferta_en_base(almacen, "run-1", external_id="otra", url="https://example.test/otra")

    cliente = ClienteFalso(respuesta_ok())
    motor = Motor(almacen, fichas_de_prueba(), cliente)
    filas = motor.pendientes()

    previstas = motor.llamadas_previstas(filas)
    assert previstas == 2

    motor.clasificar(filas)
    assert cliente.llamadas == previstas
