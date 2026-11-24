"""
El asistente — la pieza que ordena en vez de filtrar.

Lo que se vigila aquí no es que ordene "bien": eso es un juicio y no se puede
fijar en una prueba. Lo que se vigila es que el ANDAMIAJE alrededor del juicio
no pierda nada por el camino.

El fallo que persiguen casi todas estas pruebas es el mismo: **una oferta que
desaparece sin que nadie lo note.** El modelo devuelve números del 1 al N; si
repite uno, se salta otro o se inventa el 47, la traducción a claves de la
base produciría un ranking que PARECE completo y esconde vacantes. Con dos o
tres ofertas relevantes al día, esconder una es esconder una semana de
oportunidades.
"""

from __future__ import annotations

from pathlib import Path

from clasificador.asistente import (
    Asistente,
    OfertaOrdenada,
    Ranking,
    construir_lote,
    construir_sistema,
    patrones_a_json,
    patrones_desde_json,
)
from clasificador.cliente_llm import RespuestaRanking
from clasificador.fichas import Preferencias, cargar_fichas, cargar_preferencias
from clasificador.motor import Motor
from recolector.storage import Storage

from .test_clasificador import (
    ClienteFalso,
    almacen,          # noqa: F401  (fixture de pytest)
    fichas_de_prueba,
    oferta_en_base,
    respuesta_ok,
)

PREFS = Preferencias(
    criterios=[
        "El nivel manda sobre el sector.",
        "Empleador directo antes que agencia.",
    ],
    cuantas_destacar=3,
)


class OrdenadorFalso:
    """Devuelve el ranking que le diga la prueba, y cuenta las llamadas."""

    modelo = "modelo-de-prueba"

    def __init__(self, ranking: Ranking | None, error: str | None = None) -> None:
        self.ranking = ranking
        self.error = error
        self.llamadas = 0
        self.ultimo_lote = ""
        self.ultimo_esfuerzo: str | None = None

    def ordenar(
        self, sistema: str, lote: str, esfuerzo: str | None = None
    ) -> RespuestaRanking:
        self.llamadas += 1
        self.ultimo_lote = lote
        self.ultimo_esfuerzo = esfuerzo
        return RespuestaRanking(
            ranking=self.ranking,
            tokens_entrada=2500,
            tokens_salida=1200,
            modelo=self.modelo,
            error=self.error,
        )


def ranking_de(*numeros: int, lectura: str = "Semana floja.") -> Ranking:
    return Ranking(
        lectura=lectura,
        ofertas=[
            OfertaOrdenada(numero=n, por_que=f"razón {n}", accion="revisar")
            for n in numeros
        ],
        patrones=[],
    )


def _clasificar_todo(almacen: Storage, cuantas: int, **overrides) -> None:
    """Siembra ofertas y las clasifica con un cliente falso.

    El asistente trabaja sobre lo YA clasificado, así que las pruebas tienen
    que pasar antes por el motor.
    """
    run = almacen.start_run("prueba", ["adzuna"], ["mexico"])
    for i in range(cuantas):
        oferta_en_base(
            almacen,
            run,
            external_id=str(i),
            title=f"Director de Operaciones {i}",
            company=f"Empresa {i}",
            url=f"https://example.test/{i}",
        )
    motor = Motor(almacen, fichas_de_prueba(), ClienteFalso(respuesta_ok(**overrides)))
    motor.clasificar(motor.pendientes())


# ---------------------------------------------------------------------------
#  El mensaje: números, y nada de datos personales
# ---------------------------------------------------------------------------

def test_el_lote_va_numerado_del_uno_a_ene(almacen: Storage):
    _clasificar_todo(almacen, 3)
    filas = almacen.ofertas_para_ranking()
    lote = construir_lote(filas)

    assert "1. " in lote and "2. " in lote and "3. " in lote
    assert "4. " not in lote


def test_al_modelo_no_se_le_manda_la_descripcion_ni_el_cv(almacen: Storage):
    """Misma regla que el clasificador: se le manda lo destilado, no el texto crudo.

    Es más barato —una descripción son ~500 caracteres por oferta, y son
    veinte— y mantiene la promesa de que el documento de la clienta no sale de
    su máquina.
    """
    _clasificar_todo(almacen, 2)
    filas = almacen.ofertas_para_ranking()
    lote = construir_lote(filas)

    # El texto completo del anuncio no aparece.
    assert "reporta al consejo de administración y tiene a su cargo" not in lote
    # Pero sí lo que el clasificador ya destiló.
    assert "Director de Operaciones" in lote


def test_los_criterios_de_ella_van_tal_cual_al_prompt():
    """No se reformulan ni se convierten en pesos numéricos.

    Reducir «el nivel manda sobre el sector» a un número es precisión falsa:
    el resultado deja de parecerse a lo que ella pidió y ya no puede ajustarlo
    editando una frase.
    """
    sistema = construir_sistema(PREFS, cuantas=3)
    for criterio in PREFS.criterios:
        assert criterio in sistema


def test_sin_criterios_declarados_sigue_funcionando():
    sistema = construir_sistema(Preferencias(criterios=[], cuantas_destacar=2), cuantas=2)
    assert "No ha declarado criterios propios" in sistema


# ---------------------------------------------------------------------------
#  La traducción número -> clave: donde se pierden las ofertas
# ---------------------------------------------------------------------------

def test_traduce_los_numeros_a_claves_en_orden(almacen: Storage):
    _clasificar_todo(almacen, 3)
    filas = almacen.ofertas_para_ranking()

    # El modelo pone la tercera primero.
    cliente = OrdenadorFalso(ranking_de(3, 1, 2))
    resultado = Asistente(cliente, PREFS).ordenar(filas)

    assert resultado.ok
    assert [pos for _, pos, _, _ in resultado.items] == [1, 2, 3]
    assert resultado.items[0][0] == filas[2]["dedupe_key"]
    assert resultado.items[1][0] == filas[0]["dedupe_key"]


def test_un_numero_repetido_invalida_el_ranking(almacen: Storage):
    _clasificar_todo(almacen, 3)
    filas = almacen.ofertas_para_ranking()

    resultado = Asistente(OrdenadorFalso(ranking_de(1, 1, 2)), PREFS).ordenar(filas)

    assert not resultado.ok
    assert "dos veces" in (resultado.error or "")
    assert resultado.items == []


def test_un_numero_inventado_invalida_el_ranking(almacen: Storage):
    _clasificar_todo(almacen, 3)
    filas = almacen.ofertas_para_ranking()

    resultado = Asistente(OrdenadorFalso(ranking_de(1, 2, 47)), PREFS).ordenar(filas)

    assert not resultado.ok
    assert "47" in (resultado.error or "")


def test_un_ranking_incompleto_no_se_acepta(almacen: Storage):
    """LA PRUEBA QUE JUSTIFICA LA VALIDACIÓN ENTERA.

    Si el modelo devuelve 4 de 5 ofertas, el ranking se vería perfectamente
    normal: cuatro líneas ordenadas, con su razón cada una. La quinta
    simplemente no estaría, y nadie tendría forma de notarlo.

    Por eso se exige que estén TODAS, y por eso un ranking incompleto no se
    guarda: un orden con huecos es peor que no tener orden.
    """
    _clasificar_todo(almacen, 5)
    filas = almacen.ofertas_para_ranking()

    resultado = Asistente(OrdenadorFalso(ranking_de(1, 2, 3, 4)), PREFS).ordenar(filas)

    assert not resultado.ok
    assert "dejó fuera" in (resultado.error or "")
    assert resultado.items == []


def test_un_error_de_la_api_no_produce_un_ranking_vacio(almacen: Storage):
    _clasificar_todo(almacen, 3)
    filas = almacen.ofertas_para_ranking()

    cliente = OrdenadorFalso(None, error="No se pudo conectar con la API.")
    resultado = Asistente(cliente, PREFS).ordenar(filas)

    assert not resultado.ok
    assert resultado.items == []
    # El coste sí se conoce: la llamada se pagó aunque fallara.
    assert resultado.tokens_entrada > 0


# ---------------------------------------------------------------------------
#  Una llamada por lote — que es todo el argumento económico
# ---------------------------------------------------------------------------

def test_veinte_ofertas_son_una_sola_llamada(almacen: Storage):
    _clasificar_todo(almacen, 20)
    filas = almacen.ofertas_para_ranking()
    assert len(filas) == 20

    cliente = OrdenadorFalso(ranking_de(*range(1, 21)))
    resultado = Asistente(cliente, PREFS).ordenar(filas)

    assert resultado.ok
    assert cliente.llamadas == 1, (
        "Si esto sube, el asistente dejó de ser una llamada por lote y su coste "
        "pasó a crecer con el número de ofertas. Ese es todo el argumento."
    )


def test_ordenar_cuesta_menos_que_clasificar(almacen: Storage):
    """Fija la relación de coste que justifica el asistente.

    Clasificar 20 ofertas son 20 llamadas; ordenarlas es una.

    EL UMBRAL ERA `/3` Y ESTABA CALIBRADO CONTRA UN ESTIMADOR ROTO. Hasta el
    3 septiembre 2026 `Asistente.coste_estimado` no contaba el esquema de
    salida y estimaba el pensamiento en 60 tokens por oferta cuando son 188 a
    `medium`: daba un tercio del coste real. Con los números medidos, ordenar
    20 ofertas cuesta ~el 44% de clasificarlas, no menos de un tercio.

    Ordenar sigue siendo la parte barata —y sigue siendo lo que más le sirve a
    ella—, pero la ventaja es la mitad de grande de lo que este archivo
    afirmaba. Se corrige el número y se deja escrito de dónde venía el viejo.
    """
    _clasificar_todo(almacen, 20)
    filas = almacen.ofertas_para_ranking()

    # Con el esfuerzo que dice el YAML. Sin pasarlo se estimaría a `high`, que
    # es el defecto de la API y no lo que corre.
    _, entrada_ranking, coste_ranking = Asistente(
        OrdenadorFalso(None), PREFS, esfuerzo="medium"
    ).coste_estimado(filas)
    _, _, coste_clasificar = Motor(
        almacen, fichas_de_prueba(), ClienteFalso(respuesta_ok())
    ).coste_estimado(filas)

    assert coste_ranking < coste_clasificar / 2, (
        "una llamada para todo el lote tiene que seguir costando bastante "
        "menos que una por oferta"
    )


def test_el_ranking_no_manda_descripciones(almacen: Storage):
    """Lo que la prueba anterior creía estar vigilando.

    Al asistente se le manda lo que el clasificador ya destiló —puesto,
    empresa, nivel, razón—, nunca la descripción del anuncio. Si alguien la
    metiera, la entrada por oferta se dispararía y el coste con ella. Vigilarlo
    por la RELACIÓN de costes era indirecto; se mide donde de verdad pasaría.
    """
    _clasificar_todo(almacen, 20)
    filas = almacen.ofertas_para_ranking()

    entrada, _, _ = Asistente(
        OrdenadorFalso(None), PREFS, esfuerzo="medium"
    ).coste_estimado(filas)
    fijo = 2106            # el prefijo, medido: esquema + prompt de sistema
    por_oferta = (entrada - fijo) / len(filas)

    assert por_oferta < 400, (
        f"{por_oferta:.0f} tokens por oferta en el lote del ranking. Medido el "
        "3 sept 2026 eran 237; una descripción entera son ~1.250. Si esto "
        "sube así, alguien ha metido el anuncio completo en el lote"
    )



# ---------------------------------------------------------------------------
#  Que la respuesta quepa en la llamada
# ---------------------------------------------------------------------------

class TestCabeEnUnaLlamada:
    """Mismo modo de fallo que el triaje, y aquí es peor.

    El ranking escribe una línea razonada por oferta —188 tokens a `medium`—,
    así que el techo llega antes de lo que parece: ~60 ofertas. Y un ranking
    cortado no se degrada, se rechaza entero: `_traducir` valida que estén
    todas y tira el orden si falta alguna. O sea que pasarse cuesta la llamada
    completa a cambio de nada.

    No es un caso lejano: al ranking entran las clasificadas que ella todavía
    no ha revisado, y eso crece a 8 al día menos lo que marque.
    """

    def test_un_lote_que_no_cabe_no_llega_a_llamar(self, almacen: Storage):
        _clasificar_todo(almacen, 80)
        filas = almacen.ofertas_para_ranking()
        cliente = OrdenadorFalso(None)

        resultado = Asistente(cliente, PREFS, esfuerzo="medium").ordenar(filas)

        assert cliente.llamadas == 0, "una llamada que se va a cortar no se hace"
        assert not resultado.ok
        assert "no caben en una sola llamada" in resultado.error
        assert "Caben ~" in resultado.error, (
            "el motivo tiene que traer el número accionable"
        )

    def test_el_lote_de_hoy_si_cabe(self):
        """La guarda no puede estorbar en el caso normal."""
        from clasificador.asistente import cabida

        assert cabida(11, "medium").cabe, "hoy hay 11 ofertas por ordenar"
        assert cabida(40, "medium").cabe, "y un mes flojo de revisiones, 40"

    def test_el_esfuerzo_mueve_el_techo(self):
        """Subir `esfuerzo_ranking` reduce a la mitad lo que cabe, y hay que
        poder verlo venir desde el YAML."""
        from clasificador.asistente import cabida

        assert cabida(1, "low").caben > cabida(1, "high").caben * 2

    def test_el_plan_no_deja_confirmar_lo_que_no_cabe(self, almacen: Storage):
        """La bandeja se apoya en `hay_trabajo` para negarse con un 409."""
        from clasificador.plan import plan_de_ranking

        _clasificar_todo(almacen, 80)
        plan = plan_de_ranking(almacen, PREFS, esfuerzo="medium")

        assert plan.ofertas == 80, "los recuentos se conservan"
        assert not plan.cabe
        assert not plan.hay_trabajo
        assert plan.coste_usd == 0.0, "no se anuncia gasto de algo que no se hará"

# ---------------------------------------------------------------------------
#  Qué entra en el ranking y qué no
# ---------------------------------------------------------------------------

def test_lo_descartado_no_entra_al_ranking(almacen: Storage):
    """El asistente ordena, no revisa el trabajo de la capa anterior."""
    _clasificar_todo(almacen, 3, relevante=False, motivo_descarte="Sector equivocado.")
    assert almacen.ofertas_para_ranking() == []


def test_lo_que_necesita_criterio_si_entra(almacen: Storage):
    """Son justo las que ella tiene que mirar: no pueden quedarse fuera."""
    _clasificar_todo(almacen, 3, confianza=0.40)
    filas = almacen.ofertas_para_ranking()

    assert len(filas) == 3
    assert all(f["necesita_criterio"] for f in filas)


def test_cinco_anuncios_de_la_misma_vacante_son_una_linea(almacen: Storage):
    """Y el modelo se entera de que son cinco, porque eso es una señal.

    Una vacante publicada en cinco ciudades a la vez se está buscando de
    verdad. Que sea UNA línea del ranking es para no hacerle revisar cinco
    veces lo mismo; que diga "publicada en 5 sitios" es para que pueda subirla.
    """
    run = almacen.start_run("prueba", ["adzuna"], ["mexico"])
    for i, ciudad in enumerate(["Monterrey", "Guadalajara", "Puebla", "León", "Mérida"]):
        oferta_en_base(
            almacen, run, external_id=str(i),
            location_raw=ciudad, location_city=ciudad,
            url=f"https://example.test/{i}",
        )
    motor = Motor(almacen, fichas_de_prueba(), ClienteFalso(respuesta_ok()))
    motor.clasificar(motor.pendientes())

    filas = almacen.ofertas_para_ranking()
    assert len(filas) == 1
    assert filas[0]["anuncios"] == 5
    assert "publicada en 5 sitios" in construir_lote(filas)


# ---------------------------------------------------------------------------
#  Persistencia
# ---------------------------------------------------------------------------

def test_el_ranking_se_guarda_y_se_relee(almacen: Storage):
    _clasificar_todo(almacen, 3)
    filas = almacen.ofertas_para_ranking()
    resultado = Asistente(OrdenadorFalso(ranking_de(2, 3, 1)), PREFS).ordenar(filas)

    almacen.guardar_ranking(
        {
            "id": "abc123",
            "creado_at": "2026-08-31T12:00:00+00:00",
            "modelo": "modelo-de-prueba",
            "lectura": resultado.lectura,
            "patrones": patrones_a_json(["tres son de la misma agencia"]),
            "ofertas_consideradas": resultado.consideradas,
            "tokens_entrada": resultado.tokens_entrada,
            "tokens_salida": resultado.tokens_salida,
            "coste_usd": resultado.coste_usd,
        },
        resultado.items,
    )

    cabecera = almacen.ultimo_ranking()
    assert cabecera is not None
    assert cabecera["id"] == "abc123"
    assert patrones_desde_json(cabecera["patrones"]) == ["tres son de la misma agencia"]

    items = almacen.items_de_ranking("abc123")
    assert [i["posicion"] for i in items] == [1, 2, 3]
    assert items[0]["dedupe_key"] == filas[1]["dedupe_key"]


def test_patrones_desde_json_aguanta_basura():
    assert patrones_desde_json(None) == []
    assert patrones_desde_json("no soy json") == []
    assert patrones_desde_json('{"no": "una lista"}') == []


# ---------------------------------------------------------------------------
#  Las preferencias no pueden provocar gasto
# ---------------------------------------------------------------------------

def test_cambiar_las_preferencias_no_marca_las_clasificaciones_como_obsoletas():
    """La razón por la que las preferencias se cargan aparte de las fichas.

    La huella de las fichas marca las clasificaciones como obsoletas, y
    rehacerlas cuesta dinero. Reordenar la lista de prioridades de la clienta
    NO cambia el veredicto de ninguna oferta, así que no puede disparar una
    reclasificación de la base entera.

    Si alguien mete `preferencias` dentro de `_calcular_huella`, esta prueba
    se pone roja antes de que a ella le llegue una factura por haber cambiado
    de opinión sobre qué mirar primero.
    """
    import yaml

    ruta = Path(__file__).resolve().parent.parent / "config" / "cvs.yaml"
    datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))

    assert "preferencias" in datos, "el YAML real ya no declara preferencias"

    huella_original = cargar_fichas().huella

    # Se escribe un YAML con las preferencias cambiadas y se comprueba que la
    # huella de las fichas no se mueve.
    import tempfile

    datos["preferencias"]["criterios"] = ["Otra cosa completamente distinta."]
    datos["preferencias"]["cuantas_destacar"] = 9

    with tempfile.TemporaryDirectory() as tmp:
        otro = Path(tmp) / "cvs.yaml"
        # Las rutas de los CVs son relativas a la raíz, así que se copia el
        # archivo entero y solo cambia el bloque de preferencias.
        otro.write_text(
            yaml.safe_dump(datos, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        assert cargar_fichas(otro).huella == huella_original
        assert cargar_preferencias(otro).cuantas_destacar == 9


def test_el_yaml_real_trae_preferencias_utilizables():
    prefs = cargar_preferencias()
    assert prefs.hay_criterios, (
        "config/cvs.yaml no declara criterios: el asistente ordenaría sin el "
        "criterio de la clienta, que es justo lo que lo hace valioso."
    )
    assert prefs.cuantas_destacar >= 1


# ---------------------------------------------------------------------------
#  Que la lista no crezca para siempre
# ---------------------------------------------------------------------------

class TestSoloLoQueSigueVivo:
    """El ranking ordena lo que ella NO ha decidido todavía.

    Sin esto la lista crece sin parar: a 8 clasificadas al día serían ~144 al
    mes, el ranking se encarece con ella, y deja de ser «por dónde empiezo»
    para convertirse en otro montón que revisar. Que es justo lo contrario de
    para lo que existe esta capa.
    """

    def test_lo_que_ella_ya_decidio_no_vuelve_a_ordenarse(self, almacen):
        clave = oferta_en_base(almacen, "run-1", external_id="1")
        otra = oferta_en_base(almacen, "run-1", external_id="2", title="Director General")
        for k in (clave, otra):
            almacen.guardar_clasificacion(_clasificacion_relevante(k))

        assert len(almacen.ofertas_para_ranking()) == 2

        almacen.set_status(clave, "descartada")
        quedan = almacen.ofertas_para_ranking()
        assert [f["dedupe_key"] for f in quedan] == [otra]

    def test_lo_que_le_interesa_tampoco_se_reordena(self, almacen):
        """Ya decidió que le interesa: reordenárselo no le dice nada nuevo."""
        clave = oferta_en_base(almacen, "run-1", external_id="1")
        almacen.guardar_clasificacion(_clasificacion_relevante(clave))
        almacen.set_status(clave, "interesa")

        assert almacen.ofertas_para_ranking() == []


def _clasificacion_relevante(clave: str) -> dict:
    return {
        "dedupe_key": clave, "estado": "ok", "relevante": 1,
        "motivo_descarte": None, "confianza": 0.9, "razon": "encaja",
        "cv_id": "ejecutivo_es", "idioma_anuncio": "es", "idioma_detectado": "es",
        "empresa_normalizada": "Acme", "puesto_canonico": "Director",
        "es_reclutadora": 0, "seniority_detectado": "direccion",
        "modalidad": "presencial", "necesita_criterio": 0, "motivo_criterio": None,
        "error": None, "modelo": "falso", "tokens_entrada": 900,
        "tokens_salida": 400, "coste_usd": 0.01,
        "fichas_huella": "huella", "heredada_de": None,
        "descripcion_via": "entera", "caracteres_descripcion": 5000,
    }
