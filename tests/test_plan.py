"""
El plan — el número que se enseña ANTES de gastar.

La regla de la casa es que la app nunca gasta un dólar sin haber enseñado
antes el número. Lo que se vigila aquí no es que la estimación acierte —eso
lo miden las pruebas de coste— sino que **el plan describa la corrida que
viene detrás**. Un plan que estima sobre un conjunto de ofertas distinto del
que se va a procesar no falla nunca y miente siempre.

Es el fallo que ya se había colado: el `--dry-run` estimaba el triaje sobre
TODAS las pendientes, sin quitar las que ya tuvieron su turno ni agrupar los
anuncios que son la misma vacante. Sobre la base del 2 septiembre 2026, 302
contra 228.

Ninguna prueba llama a nada: `plan.py` solo usa fórmulas.
"""

from __future__ import annotations

from types import SimpleNamespace

from clasificador.motor import agrupar_para_ahorrar
from clasificador.plan import (
    ClienteInerte,
    plan_de_clasificacion,
    plan_de_ranking,
    plan_de_una_lectura,
)
from clasificador.triaje import Triador
from recolector.storage import Storage

from .test_clasificador import (  # noqa: F401
    almacen,
    fichas_de_prueba,
    oferta_en_base,
)
from .test_triaje import PREFS, TriadorFalso, filas_de, todos_merecen


def config_de_prueba(
    activo: bool = True,
    cuantas: int = 8,
    esfuerzo: str = "low",
    tope: int | None = None,
) -> SimpleNamespace:
    """Solo lo que `plan.py` mira del config. Nada más, para que la prueba no
    dependa de lo que hoy diga `config/search_profile.yaml`.

    `esfuerzo` viene con el mismo valor que el YAML real porque decide dos
    cosas del plan: lo que estima que va a costar, y cuántas vacantes caben en
    una llamada. Un config de prueba sin él probaría un camino que en
    producción no ocurre nunca.
    """
    return SimpleNamespace(
        llm=SimpleNamespace(
            triaje=SimpleNamespace(
                activo=activo,
                ofertas_por_corrida=cuantas,
                max_vacantes_por_triaje=tope,
                esfuerzo=esfuerzo,
            )
        )
    )


def clasificar_en_base(almacen: Storage, clave: str) -> None:
    """Deja una oferta como LEÍDA, que es lo que hace una corrida completa.

    Hace falta para distinguir dos estados que antes se escribían igual y no
    lo son: «el triaje la eligió y el motor la leyó» —corrida terminada— y
    «el triaje la eligió y la corrida se cortó antes de leerla». Ver
    `Storage.claves_elegidas_sin_leer`.
    """
    almacen.guardar_clasificacion({
        "dedupe_key": clave, "estado": "ok", "relevante": 1,
        "motivo_descarte": None, "confianza": 0.9, "razon": "Encaja.",
        "cv_id": "cv_es", "idioma_anuncio": "es", "idioma_detectado": "es",
        "empresa_normalizada": "Acme", "puesto_canonico": "Director",
        "es_reclutadora": 0, "seniority_detectado": "c_level",
        "modalidad": "presencial", "necesita_criterio": 0,
        "motivo_criterio": None, "error": None, "modelo": "m",
        "tokens_entrada": 1, "tokens_salida": 1, "coste_usd": 0.01,
        "fichas_huella": "huella_de_prueba", "heredada_de": None,
        "descripcion_via": "entera", "caracteres_descripcion": 5000,
    })


def sembrar(almacen: Storage, cuantas: int, empresa: str = "Acme") -> list[str]:
    return [
        oferta_en_base(
            almacen, "run-1",
            external_id=f"{empresa}-{i}",
            title=f"Director {i}",
            company=empresa,
        )
        for i in range(1, cuantas + 1)
    ]


# ---------------------------------------------------------------------------
#  El plan describe la corrida real
# ---------------------------------------------------------------------------

class TestElPlanDescribeLaCorrida:
    def test_no_cuenta_las_que_ya_tuvieron_su_turno(self, almacen):
        """La prueba central. Cada oferta se tría una vez.

        Si el plan cuenta las ya triadas, promete una llamada de triaje más
        cara que la que se va a hacer. El error cae del lado seguro y por eso
        no se nota, que es exactamente lo que lo hace peligroso.
        """
        sembrar(almacen, 30)
        # Triaje real, con cliente falso: 30 ofertas gastan su turno.
        resultado = Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(30)), cuantas=8
        ).triar(filas_de(almacen))
        # Y LA CORRIDA TERMINA: las 8 elegidas se leen. Sin esta parte, lo que
        # se estaría montando no es «todas tuvieron su turno» sino una corrida
        # cortada a medias, que desde el 9 septiembre 2026 es otro caso y
        # tiene su propia prueba (ver `TestLoQueUnCorteDejoAMedias`).
        for clave in resultado.seleccionadas(8):
            clasificar_en_base(almacen, clave)
        # Y entran diez nuevas que nadie ha mirado.
        sembrar(almacen, 10, empresa="Nueva")

        plan = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())

        assert plan.pendientes == 32, "las 8 leídas ya no están pendientes"
        assert plan.ya_triadas == 22
        assert plan.sin_triar == 10
        assert plan.vacantes == 10, "las 30 ya vistas no vuelven a competir"

    def test_agrupa_antes_de_estimar(self, almacen):
        """Cinco anuncios de la misma vacante son una llamada, no cinco."""
        for i in range(5):
            oferta_en_base(
                almacen, "run-1",
                external_id=f"dcs-{i}",
                title="Chief Operating Officer",
                company="Northline Asset Services",
                location_raw=f"Ciudad {i}",
                location_city=f"Ciudad {i}",
            )

        plan = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())

        assert plan.pendientes == 5
        assert plan.vacantes == 1, "agrupar es gratis y el plan tiene que contarlo"
        assert plan.llamadas == 1

    def test_el_triaje_solo_entra_si_no_caben_todas(self, almacen):
        """Es un techo, no una cuota: con 5 pendientes y 8 huecos no se tría."""
        sembrar(almacen, 5)
        plan = plan_de_clasificacion(almacen, config_de_prueba(cuantas=8), fichas_de_prueba())

        assert not plan.hay_triaje
        assert plan.coste_triaje_usd == 0.0
        assert plan.lecturas == 5
        assert "caben todas" in plan.nota

    def test_con_mas_pendientes_que_huecos_si_se_tria(self, almacen):
        sembrar(almacen, 30)
        plan = plan_de_clasificacion(almacen, config_de_prueba(cuantas=8), fichas_de_prueba())

        assert plan.hay_triaje
        assert plan.coste_triaje_usd > 0
        assert plan.lecturas == 8
        assert plan.llamadas == 9, "ocho lecturas más la llamada del triaje"

    def test_el_coste_del_triaje_crece_con_el_lote(self, almacen, tmp_path):
        sembrar(almacen, 20)
        pequeno = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())

        with Storage(tmp_path / "grande.db") as otro:
            sembrar(otro, 180)
            grande = plan_de_clasificacion(otro, config_de_prueba(), fichas_de_prueba())

        assert grande.coste_triaje_usd > pequeno.coste_triaje_usd * 5
        assert grande.lecturas == pequeno.lecturas, (
            "lo que se lee está topado; lo que crece es lo que se tría"
        )

    def test_un_lote_que_no_cabe_en_la_llamada_no_se_planifica(self, tmp_path):
        """El plan no puede prometer una corrida que se va a cortar.

        El 3 septiembre 2026 había 273 vacantes sin triar y el plan las mandaba
        en una sola llamada: anunciaba 38.340 tokens de salida contra un techo
        de 8.000 y terminaba diciendo «El plan CABE en lo que queda del mes».
        Cabía en el mes. No cabía en la llamada, y la corrida habría pagado la
        respuesta entera sin triar una sola oferta.
        """
        with Storage(tmp_path / "atraso.db") as otro:
            sembrar(otro, 400)
            plan = plan_de_clasificacion(
                otro, config_de_prueba(), fichas_de_prueba()
            )

        assert not plan.hay_trabajo, (
            "un lote que no cabe en la llamada no se puede confirmar"
        )
        assert plan.coste_usd == 0.0, "no se anuncia gasto de algo que no se hará"
        assert "no caben en una sola llamada" in plan.nota
        assert "Caben ~" in plan.nota, (
            "el motivo tiene que traer el número accionable, no solo la negativa"
        )
        assert plan.vacantes == 400, (
            "los recuentos se conservan: «no cabe» y «no hay ofertas» no se "
            "pueden ver igual en pantalla"
        )


# ---------------------------------------------------------------------------
#  Cuando lanzar no haría nada
# ---------------------------------------------------------------------------

class TestCuandoNoHayTrabajo:
    def test_base_vacia(self, almacen):
        plan = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())

        assert not plan.hay_trabajo
        assert plan.coste_usd == 0.0

    def test_todas_ya_triadas_no_es_lo_mismo_que_no_haber_ofertas(self, almacen):
        """La regla nº 1 aplicada al botón.

        Con todo triado y nada seleccionado, lanzar la corrida no volvería a
        mirar nada: esas ofertas ya gastaron su turno. El plan tiene que
        decirlo, y decir cuál es su vía — el botón por oferta— en vez de
        enseñar un cero que parece «no hay ofertas».
        """
        sembrar(almacen, 20)
        resultado = Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(20)), cuantas=5
        ).triar(filas_de(almacen))
        # La corrida TERMINÓ: las 5 elegidas se leyeron. Es lo que hace que a
        # las otras 15 no les quede nada automático por delante.
        for clave in resultado.seleccionadas(5):
            clasificar_en_base(almacen, clave)

        plan = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())

        assert not plan.hay_trabajo
        assert plan.pendientes == 15, "siguen ahí; lo que no queda es turno"
        assert plan.ya_triadas == 15
        assert "que la lea el asistente" in plan.nota


# ---------------------------------------------------------------------------
#  Sin triaje
# ---------------------------------------------------------------------------

class TestSinTriaje:
    def test_lee_todas_y_lo_dice(self, almacen):
        sembrar(almacen, 30)
        plan = plan_de_clasificacion(
            almacen, config_de_prueba(), fichas_de_prueba(), sin_triaje=True
        )

        assert not plan.hay_triaje
        assert plan.lecturas == 30
        assert plan.llamadas == 30
        assert "Sin triaje" in plan.nota

    def test_sale_mas_caro_que_con_triaje(self, almacen):
        """La comparación que justifica la capa, dicha por el propio plan."""
        sembrar(almacen, 30)
        con = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())
        sin = plan_de_clasificacion(
            almacen, config_de_prueba(), fichas_de_prueba(), sin_triaje=True
        )

        assert sin.coste_usd > con.coste_usd

    def test_ignora_los_turnos_gastados(self, almacen):
        """`--sin-triaje` lee todo lo pendiente, tenga turno o no."""
        sembrar(almacen, 20)
        Triador(almacen, PREFS, TriadorFalso(todos_merecen(20)), cuantas=5).triar(
            filas_de(almacen)
        )

        plan = plan_de_clasificacion(
            almacen, config_de_prueba(), fichas_de_prueba(), sin_triaje=True
        )
        assert plan.lecturas == 20


# ---------------------------------------------------------------------------
#  Una sola lectura: el botón de la bandeja
# ---------------------------------------------------------------------------

class TestUnaLectura:
    def test_oferta_inexistente(self, almacen):
        plan = plan_de_una_lectura(almacen, fichas_de_prueba(), "no-existe")
        assert not plan.existe
        assert not plan.se_puede

    def test_oferta_pendiente(self, almacen):
        clave = sembrar(almacen, 1)[0]
        plan = plan_de_una_lectura(almacen, fichas_de_prueba(), clave)

        assert plan.se_puede
        assert plan.coste_usd > 0
        assert plan.titulo

    def test_una_lectura_suelta_cuesta_mas_que_una_de_corrida(self, almacen):
        """No es un recargo: es que una llamada sola ESCRIBE la caché.

        Las de una corrida la leen a un décimo. Se enseña el número de verdad
        aunque parezca alto; explicarlo es trabajo de la interfaz, no de la
        estimación.
        """
        claves = sembrar(almacen, 10)
        suelta = plan_de_una_lectura(almacen, fichas_de_prueba(), claves[0])
        corrida = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())

        media_de_corrida = corrida.coste_lecturas_usd / corrida.lecturas
        assert suelta.coste_usd > media_de_corrida

    def test_una_ya_clasificada_no_se_vuelve_a_pagar(self, almacen):
        """La caché no es una optimización, es un requisito."""
        clave = sembrar(almacen, 1)[0]
        almacen.guardar_clasificacion({
            "dedupe_key": clave, "estado": "ok", "relevante": 1,
            "motivo_descarte": None, "confianza": 0.9, "razon": "Encaja.",
            "cv_id": "cv_es", "idioma_anuncio": "es", "idioma_detectado": "es",
            "empresa_normalizada": "Acme", "puesto_canonico": "Director",
            "es_reclutadora": 0, "seniority_detectado": "c_level",
            "modalidad": "presencial", "necesita_criterio": 0,
            "motivo_criterio": None, "error": None, "modelo": "m",
            "tokens_entrada": 1, "tokens_salida": 1, "coste_usd": 0.01,
            "fichas_huella": "huella_de_prueba", "heredada_de": None,
            "descripcion_via": "entera", "caracteres_descripcion": 5000,
        })

        plan = plan_de_una_lectura(almacen, fichas_de_prueba(), clave)
        assert plan.ya_clasificada
        assert not plan.se_puede
        assert plan.coste_usd == 0.0


# ---------------------------------------------------------------------------
#  El ranking
# ---------------------------------------------------------------------------

class TestPlanDeRanking:
    def test_sin_nada_clasificado_no_hay_trabajo(self, almacen):
        plan = plan_de_ranking(almacen, PREFS)
        assert not plan.hay_trabajo
        assert plan.coste_usd == 0.0


# ---------------------------------------------------------------------------
#  El cliente inerte
# ---------------------------------------------------------------------------

class TestClienteInerte:
    def test_revienta_si_alguien_intenta_usarlo(self):
        """Un cliente que devolviera respuestas vacías en silencio dejaría
        ofertas sin clasificar con cara de estar clasificadas.
        """
        import pytest

        for llamada in (
            lambda c: c.clasificar("sistema", "oferta"),
            lambda c: c.ordenar("sistema", "lote"),
            lambda c: c.triar("sistema", "lote"),
        ):
            with pytest.raises(RuntimeError):
                llamada(ClienteInerte())


# ---------------------------------------------------------------------------
#  Que el plan y la corrida no se separen
# ---------------------------------------------------------------------------

def test_las_vacantes_del_plan_son_las_que_recibe_el_triaje(almacen):
    """El contrato entre `plan.py` y `triaje.py`, fijado.

    Si alguien cambia cómo se agrupa o cómo se filtran los turnos en un sitio
    y no en el otro, el número que se le enseña deja de ser el que se cobra.
    Esta prueba es la costura.
    """
    sembrar(almacen, 25)
    for i in range(3):
        oferta_en_base(
            almacen, "run-1",
            external_id=f"rep-{i}", title="Site Superintendent",
            company="SmartTech", location_raw=f"Ciudad {i}",
            location_city=f"Ciudad {i}",
        )

    plan = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())

    fichas = fichas_de_prueba()
    filas = almacen.ofertas_pendientes(fichas_huella=fichas.huella)
    vistas = almacen.claves_ya_triadas()
    nuevas = [f for f in filas if f["dedupe_key"] not in vistas]
    representantes = agrupar_para_ahorrar(nuevas)

    assert plan.vacantes == len(representantes) == 26


def test_el_plan_manda_el_mismo_lote_que_la_corrida_con_tope(almacen):
    """La costura del tope, que es donde se rompería primero.

    El plan y la corrida aplican el recorte por caminos distintos —uno para
    enseñar el número, el otro para gastarlo— y si se separaran, la bandeja
    enseñaría el coste de un lote y se cobraría el de otro. Es la misma
    divergencia que ya se coló una vez con la agrupación y los turnos.

    Se comprueba contra lo que el `Triador` le manda AL CLIENTE, no contra otro
    cálculo: el lote que viaja es la única verdad.
    """
    sembrar(almacen, 30)

    plan = plan_de_clasificacion(
        almacen, config_de_prueba(tope=10), fichas_de_prueba()
    )

    assert plan.vacantes == 30, "la cola entera se sigue diciendo"
    assert plan.vacantes_esta_corrida == 10
    assert plan.fuera_del_tope == 20

    fichas = fichas_de_prueba()
    filas = almacen.ofertas_pendientes(fichas_huella=fichas.huella)
    cliente = TriadorFalso(todos_merecen(10))
    resultado = Triador(
        almacen, PREFS, cliente, cuantas=8, esfuerzo="low", tope_lote=10
    ).triar(filas)

    assert resultado.consideradas == plan.vacantes_esta_corrida
    assert resultado.fuera_del_tope == plan.fuera_del_tope
    assert cliente.ultimo_lote.count("\n") + 1 >= 10


def test_el_plan_dice_cuantas_esperan_y_no_las_esconde(almacen):
    """Enseñar solo lo que entra sería apretar el coste escondiendo la cola."""
    sembrar(almacen, 30)

    plan = plan_de_clasificacion(
        almacen, config_de_prueba(tope=10), fichas_de_prueba()
    )

    assert "30" in plan.nota, "la cola entera tiene que salir en la nota"
    assert "20" in plan.nota, "y cuántas esperan a la siguiente corrida"
    assert "turno" in plan.nota, (
        "y con qué palabras: conservan su turno, que no es lo mismo que estar "
        "aplazadas ni que estar descartadas"
    )


def test_sin_tope_el_plan_no_habla_de_tope(almacen):
    """Lo que no ocurre no se menciona: una línea de más también confunde."""
    sembrar(almacen, 30)

    plan = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())

    assert plan.fuera_del_tope == 0
    assert plan.vacantes_esta_corrida == plan.vacantes == 30


# ---------------------------------------------------------------------------
#  Lo que un corte dejó a medias
# ---------------------------------------------------------------------------
#  EL AGUJERO, MEDIDO EJECUTANDO LA APP EL 9 SEPTIEMBRE 2026.
#
#  El triaje apunta en `triajes` TODAS las vacantes del lote antes de que el
#  motor lea ninguna. Si la corrida se corta después —«Parar», el tope por
#  acción, la ventana cerrada— las ELEGIDAS que no llegaron a leerse quedan
#  con su fila puesta, y `claves_ya_triadas` las escondía de todo lo que
#  venía detrás. Aquella mañana quedaron seis así (RWE, ERCOT, Provident,
#  Ecoplexus) con el plan diciendo:
#
#      hay_trabajo: False   coste: $0.0000
#      «ya tuvieron su turno […] lanzar la corrida no las volvería a mirar»
#
#  mientras la corrida que las había perdido le había prometido por escrito
#  «NO SE HAN PERDIDO […] la próxima corrida con saldo las recoge sola», y
#  AYUDA.md le prometía «el resto queda pendiente para la próxima».
#
#  Las 607 pruebas de entonces pasaban todas. Estas son las que faltaban.

class TestLoQueUnCorteDejoAMedias:
    def test_una_lectura_elegida_y_no_hecha_sigue_siendo_trabajo(self, almacen):
        """La prueba central: el botón no puede decir que no hay nada."""
        sembrar(almacen, 20)
        Triador(almacen, PREFS, TriadorFalso(todos_merecen(20)), cuantas=5).triar(
            filas_de(almacen)
        )
        # No se clasifica ninguna: es una corrida cortada justo después del
        # triaje, que es el caso de «Parar» y el del tope por acción.

        plan = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())

        assert plan.hay_trabajo, "las 5 elegidas y sin leer son trabajo pendiente"
        assert plan.lecturas == 5
        assert "sin leer" in plan.nota

    def test_no_vuelven_a_pasar_por_el_triaje(self, almacen):
        """Su turno ya se gastó y su selección ya se pagó.

        Devolverlas al sorteo sería poder perderlo otra vez, y rompería que el
        coste del triaje sea fijo — que es la razón de ser de esa capa.
        """
        sembrar(almacen, 20)
        Triador(almacen, PREFS, TriadorFalso(todos_merecen(20)), cuantas=5).triar(
            filas_de(almacen)
        )

        plan = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())

        assert plan.sin_triar == 0
        assert not plan.hay_triaje, "no se paga otra llamada de triaje por ellas"
        assert plan.coste_triaje_usd == 0.0

    def test_ocupan_hueco_en_vez_de_sumarse(self, almacen):
        """Una corrida detrás de otra cortada no puede costar el doble.

        Se leen hasta `ofertas_por_corrida` vacantes y todo el modelo de coste
        cuelga de eso. Si los rescates se sumaran a las que elija el triaje,
        el importe del botón dejaría de describir la corrida que viene detrás.
        """
        sembrar(almacen, 40)
        # Con cuantas=8 el triaje elige 8 y ninguna se lee: 8 rescates, cero
        # huecos libres.
        Triador(almacen, PREFS, TriadorFalso(todos_merecen(40)), cuantas=8).triar(
            filas_de(almacen)
        )
        sembrar(almacen, 20, empresa="Nueva")

        plan = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())

        assert plan.lecturas == 8, "8 rescates llenan la corrida; no son 8 + 8"
        assert not plan.hay_triaje, "sin huecos libres, no se tría nada nuevo"

    def test_con_huecos_libres_se_reparte(self, almacen):
        """Dos rescates dejan seis huecos, y el triaje llena esos seis."""
        sembrar(almacen, 10)
        Triador(almacen, PREFS, TriadorFalso(todos_merecen(10)), cuantas=2).triar(
            filas_de(almacen)
        )
        sembrar(almacen, 40, empresa="Nueva")

        plan = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())

        assert plan.hay_triaje, "quedan huecos y hay 40 sin triar"
        assert plan.lecturas == 8, "2 rescatadas + 6 que elige el triaje"

    def test_leerlas_las_saca_de_la_cola(self, almacen):
        """Que no se queden rescatándose para siempre."""
        sembrar(almacen, 20)
        resultado = Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(20)), cuantas=5
        ).triar(filas_de(almacen))
        for clave in resultado.seleccionadas(5):
            clasificar_en_base(almacen, clave)

        plan = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())

        assert not plan.hay_trabajo


# ---------------------------------------------------------------------------
#  El importe describe la corrida, no la cola
# ---------------------------------------------------------------------------

class TestElImporteCubreLoQueElTriajeElija:
    """MEDIDO EL 9 SEPTIEMBRE 2026, comparando el diálogo con la corrida.

    El plan cobraba por las N PRIMERAS de la cola —ordenada por fecha— y la
    corrida leía las N que eligiera el triaje, que no son las mismas:

        el diálogo dijo ..... Paystand, Wells Fargo, FloatMe, CNA…
        la corrida leyó ..... Honeywell, Fluence, TAK Broadband…
        enseñado $0,1742  ->  cobrado $0,2200

    Los anuncios miden de 500 a 9.000 caracteres, así que coger seis
    cualesquiera es una lotería, y puede caer por debajo. Por debajo es el
    único lado que este proyecto no se permite.
    """

    def _con_anuncios_desiguales(self, almacen):
        """Diez cortas primero y diez larguísimas después.

        Ordenadas así a propósito: el plan cogía las primeras, que son
        justo las baratas.
        """
        for i in range(10):
            oferta_en_base(
                almacen, "run-1", external_id=f"corta-{i}",
                title=f"Director Corto {i}", company=f"Corta {i}",
                description="x" * 400,
            )
        for i in range(10):
            oferta_en_base(
                almacen, "run-1", external_id=f"larga-{i}",
                title=f"Director Largo {i}", company=f"Larga {i}",
                description="y" * 12000,
            )

    def test_cobra_por_las_mas_largas_que_el_triaje_podria_elegir(self, almacen):
        self._con_anuncios_desiguales(almacen)

        plan = plan_de_clasificacion(
            almacen, config_de_prueba(cuantas=5), fichas_de_prueba()
        )

        assert plan.hay_triaje
        # Si estimara sobre las cinco primeras (cortas) saldría una fracción
        # de esto. Lo que se cobra es el peor caso que el triaje puede elegir.
        solo_cortas = 5 * 0.01
        assert plan.coste_lecturas_usd > solo_cortas, (
            "estimar sobre las primeras de la cola deja el importe por debajo "
            "de lo que la corrida puede gastar"
        )

    def test_no_promete_una_lista_que_el_triaje_no_ha_elegido(self, almacen):
        """La ficha pinta estos títulos y ella los lee como una promesa."""
        self._con_anuncios_desiguales(almacen)

        plan = plan_de_clasificacion(
            almacen, config_de_prueba(cuantas=5), fichas_de_prueba()
        )

        assert plan.hay_triaje
        assert plan.muestra == (), (
            "con triaje por delante, los nombres concretos todavía no existen"
        )
        assert "cuáles se sabrá al hacerlo" in plan.nota

    def test_sin_triaje_si_se_sabe_cuales_y_se_dicen(self, almacen):
        """Cuando caben todas, la lista es exacta y hay que enseñarla."""
        sembrar(almacen, 3)

        plan = plan_de_clasificacion(
            almacen, config_de_prueba(cuantas=8), fichas_de_prueba()
        )

        assert not plan.hay_triaje
        assert len(plan.muestra) == 3

    def test_los_rescates_si_se_dicen_por_nombre(self, almacen):
        """Esos ya están decididos: se leen sí o sí."""
        sembrar(almacen, 20)
        Triador(almacen, PREFS, TriadorFalso(todos_merecen(20)), cuantas=2).triar(
            filas_de(almacen)
        )
        sembrar(almacen, 40, empresa="Nueva")

        plan = plan_de_clasificacion(almacen, config_de_prueba(), fichas_de_prueba())

        assert plan.hay_triaje
        assert len(plan.muestra) == 2, "los rescates sí se saben; el resto no"
