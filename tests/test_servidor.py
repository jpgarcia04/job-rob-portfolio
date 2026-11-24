"""
Los endpoints de la bandeja.

Se llaman como funciones de Python, no por HTTP. No es un atajo: el
enrutamiento es cosa de FastAPI y ya está probado por FastAPI, mientras que lo
que aquí puede salir caro —negarse cuando no cabe, no dejar lanzar dos
corridas, distinguir aplazada de nunca mirada— vive en el cuerpo de esas
funciones. Probarlo así además evita meter `httpx` en un proyecto que mantiene
la lista de dependencias corta a propósito.

Nada toca la red, ni la base real, ni el libro de gastos real: `load_config` y
`_abrir_almacen` se sustituyen por versiones que apuntan a un directorio
temporal, y el registro de trabajos recibe un lanzador falso.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from bandeja import server
from bandeja.trabajos import Registro
from recolector.config import LlmBudgetConfig, TriajeConfig
from recolector.storage import Storage

from .test_clasificador import TEXTO_ES, oferta_en_base  # noqa: F401
from .test_trabajos import ProcesoFalso, esperar, lanzador_que_devuelve
from .test_triaje import PREFS, TriadorFalso, filas_de, todos_merecen


def config_falsa(
    tmp_path: Path,
    techo: float = 12.0,
    racion: float = 10.0,
    modo: str = "parar",
    tope_accion: float = 0.60,
):
    """Lo que `server.py` mira del config, y nada más."""
    return SimpleNamespace(
        database_path=tmp_path / "ofertas.db",
        llm=LlmBudgetConfig(
            presupuesto_mensual_usd=techo,
            al_agotarse=modo,
            racion_automatica_mensual_usd=racion,
            avisar_al_superar_pct=75,
            tope_por_accion_usd=tope_accion,
            triaje=TriajeConfig(
                activo=True,
                ofertas_por_corrida=8,
                max_vacantes_por_triaje=60,
                esfuerzo="low",
            ),
        ),
        market=lambda clave: SimpleNamespace(label=f"Mercado {clave}"),
    )


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """Servidor apuntando a una base y un libro de gastos de usar y tirar."""
    config = config_falsa(tmp_path)
    almacen = Storage(config.database_path)

    monkeypatch.setattr(server, "load_config", lambda: config)
    # Se devuelve SIEMPRE la misma conexión y no se cierra entre llamadas: en
    # producción se abre una por petición, pero aquí interesa poder mirar la
    # base desde la prueba después de llamar al endpoint.
    monkeypatch.setattr(server, "_abrir_almacen", lambda: almacen)
    monkeypatch.setattr(almacen, "close", lambda: None)
    monkeypatch.setattr(server, "_cargar_fichas", _fichas)

    registro = Registro(lanzador=lanzador_que_devuelve(ProcesoFalso(["listo"])))
    monkeypatch.setattr(server, "trabajos", registro)

    yield SimpleNamespace(
        config=config, almacen=almacen, registro=registro, tmp_path=tmp_path
    )
    almacen.connection.close()


def _fichas():
    from .test_clasificador import fichas_de_prueba

    return fichas_de_prueba()


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
#  Aplazada NO es lo mismo que nunca mirada
# ---------------------------------------------------------------------------

class TestLoQueVioElTriaje:
    """La tarea que da sentido al triaje entero.

    «El asistente la vio y la dejó en el puesto 34, porque X» y «nadie ha
    mirado nunca esta oferta» son dos frases distintas, y si la bandeja no
    puede escribir las dos, el triaje es un filtro silencioso con pasos de más.
    """

    def test_el_resumen_cuenta_las_dos_por_separado(self, entorno):
        from clasificador.triaje import Triador

        sembrar(entorno.almacen, 20)
        Triador(entorno.almacen, PREFS, TriadorFalso(todos_merecen(20)), cuantas=5).triar(
            filas_de(entorno.almacen)
        )
        sembrar(entorno.almacen, 7, empresa="Nueva")

        datos = server.resumen()

        assert datos["triaje"]["aplazadas"] == 20
        assert datos["triaje"]["sin_mirar"] == 7
        assert datos["clasificacion"]["pendientes"] == 27

    def test_las_aplazadas_llegan_con_su_puesto_y_su_motivo(self, entorno):
        from clasificador.triaje import Triador

        sembrar(entorno.almacen, 12)
        Triador(entorno.almacen, PREFS, TriadorFalso(todos_merecen(12)), cuantas=3).triar(
            filas_de(entorno.almacen)
        )

        datos = server.listar_ofertas(relevancia="aplazadas")

        assert datos["total"] > 0
        for oferta in datos["ofertas"]:
            assert oferta["triaje_posicion"] is not None
            assert oferta["triaje_por_que"], (
                "sin motivo escrito, aplazar es indistinguible de esconder"
            )

    def test_las_nunca_miradas_no_traen_puesto_inventado(self, entorno):
        sembrar(entorno.almacen, 4)
        datos = server.listar_ofertas(relevancia="sin_mirar")

        assert datos["total"] == 4
        for oferta in datos["ofertas"]:
            assert oferta["triaje_posicion"] is None

    def test_los_dos_filtros_no_se_solapan(self, entorno):
        from clasificador.triaje import Triador

        sembrar(entorno.almacen, 10)
        Triador(entorno.almacen, PREFS, TriadorFalso(todos_merecen(10)), cuantas=3).triar(
            filas_de(entorno.almacen)
        )
        sembrar(entorno.almacen, 5, empresa="Nueva")

        aplazadas = {
            o["dedupe_key"] for o in server.listar_ofertas(relevancia="aplazadas")["ofertas"]
        }
        sin_mirar = {
            o["dedupe_key"] for o in server.listar_ofertas(relevancia="sin_mirar")["ofertas"]
        }

        assert not (aplazadas & sin_mirar)
        assert len(aplazadas) + len(sin_mirar) == 15

    def test_un_filtro_inventado_se_rechaza(self, entorno):
        with pytest.raises(HTTPException) as fallo:
            server.listar_ofertas(relevancia="las_buenas")
        assert fallo.value.status_code == 400


# ---------------------------------------------------------------------------
#  El número antes de gastar
# ---------------------------------------------------------------------------

class TestElPlanSeEnsenaAntes:
    def test_pedir_el_plan_no_lanza_nada(self, entorno):
        sembrar(entorno.almacen, 30)
        datos = server.plan("clasificar")

        assert datos["coste_usd"] > 0
        assert entorno.registro.en_curso() is None, "pedir el plan no gasta"

    def test_el_plan_dice_si_cabe(self, entorno):
        sembrar(entorno.almacen, 30)
        assert server.plan("clasificar")["cabe"]

    def test_sin_saldo_el_plan_dice_que_no_cabe(self, entorno):
        sembrar(entorno.almacen, 30)
        server._libro().registrar(11.99)

        datos = server.plan("clasificar")
        assert not datos["cabe"]
        assert datos["disponible_usd"] < 0.05

    def test_el_plan_de_una_oferta_trae_su_titulo(self, entorno):
        clave = sembrar(entorno.almacen, 1)[0]
        datos = server.plan_de_leer_una(clave)

        assert datos["hay_trabajo"]
        assert datos["titulo"]
        assert datos["coste_usd"] > 0

    def test_una_accion_inventada_se_rechaza(self, entorno):
        with pytest.raises(HTTPException) as fallo:
            server.plan("borrar_todo")
        assert fallo.value.status_code == 400


# ---------------------------------------------------------------------------
#  El servidor se niega, no solo la interfaz
# ---------------------------------------------------------------------------

class TestElServidorSeNiega:
    """Quien tiene que negarse es quien gasta.

    Que el navegador enseñara un número no prueba nada: el cliente puede estar
    desfasado, o alguien puede llamar al endpoint a mano.
    """

    def test_no_lanza_si_no_cabe_en_el_presupuesto(self, entorno):
        sembrar(entorno.almacen, 30)
        server._libro().registrar(11.99)

        with pytest.raises(HTTPException) as fallo:
            server.lanzar_trabajo(server.LanzarTrabajo(accion="clasificar"))

        assert fallo.value.status_code == 402
        assert "no se ha perdido ninguna" in fallo.value.detail.lower()
        assert entorno.registro.en_curso() is None

    def test_no_lanza_si_la_accion_pasa_del_tope_por_accion(self, entorno, monkeypatch):
        """EL FRENO QUE SE LE PROMETE, comprobado donde gasta.

        Hasta el 3 sept 2026 el servidor solo miraba si cabía en el MES. Eso
        protege de gastar mucho en treinta días, pero no de que un botón se
        lleve un pellizco grande de una vez — y con el techo mensual en modo
        aviso, no quedaría ningún freno delante de sus botones.

        Se simula bajando el tope, no inflando la base: lo que se prueba es el
        freno, y hacerlo depender de cuántas ofertas haya sembradas lo ataría a
        la aritmética del estimador.
        """
        sembrar(entorno.almacen, 30)
        monkeypatch.setattr(
            server, "load_config",
            lambda *a, **k: config_falsa(entorno.tmp_path, tope_accion=0.001),
        )

        with pytest.raises(HTTPException) as fallo:
            server.lanzar_trabajo(server.LanzarTrabajo(accion="clasificar"))

        assert fallo.value.status_code == 402
        assert "tope por acción" in fallo.value.detail
        assert "no se ha perdido ninguna oferta" in fallo.value.detail
        assert entorno.registro.en_curso() is None

    def test_con_el_mes_en_modo_aviso_el_boton_sigue_funcionando(self, entorno, monkeypatch):
        """La otra mitad, y es la que hace falta que no se rompa.

        Con `al_agotarse: avisar`, pasarse del techo del mes NO puede impedirle
        pulsar: es su API key y su recarga. Si esta prueba cayera, la app se le
        apagaría el día 17 exactamente igual que antes y todo el cambio no
        habría servido de nada.
        """
        sembrar(entorno.almacen, 30)
        monkeypatch.setattr(
            server, "load_config",
            lambda *a, **k: config_falsa(entorno.tmp_path, modo="avisar"),
        )
        server._libro().registrar(11.99)

        trabajo = server.lanzar_trabajo(server.LanzarTrabajo(accion="clasificar"))
        assert trabajo["estado"] in ("en_curso", "terminado")

    def test_no_lanza_si_no_hay_nada_que_hacer(self, entorno):
        with pytest.raises(HTTPException) as fallo:
            server.lanzar_trabajo(server.LanzarTrabajo(accion="clasificar"))

        assert fallo.value.status_code == 409
        assert entorno.registro.en_curso() is None

    def test_no_lanza_si_el_coste_subio_desde_que_se_enseno(self, entorno):
        """Abre el diálogo, se va a comer, entran 200 ofertas, vuelve y acepta.

        Sin esta comprobación habría confirmado un número y gastado otro, que
        es exactamente lo que la regla de la casa prohíbe.
        """
        sembrar(entorno.almacen, 200)

        with pytest.raises(HTTPException) as fallo:
            server.lanzar_trabajo(
                server.LanzarTrabajo(accion="clasificar", coste_confirmado_usd=0.05)
            )

        assert fallo.value.status_code == 409
        assert "ha subido" in fallo.value.detail
        assert entorno.registro.en_curso() is None

    def test_si_el_coste_no_cambio_se_lanza(self, entorno):
        sembrar(entorno.almacen, 30)
        previsto = server.plan("clasificar")["coste_usd"]

        trabajo = server.lanzar_trabajo(
            server.LanzarTrabajo(accion="clasificar", coste_confirmado_usd=previsto)
        )
        assert trabajo["estado"] in ("corriendo", "terminado")

    def test_leer_una_ya_clasificada_no_se_lanza(self, entorno):
        clave = sembrar(entorno.almacen, 1)[0]
        entorno.almacen.guardar_clasificacion({
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

        with pytest.raises(HTTPException) as fallo:
            server.lanzar_trabajo(
                server.LanzarTrabajo(accion="leer_una", clave=clave)
            )
        assert fallo.value.status_code == 409

    def test_leer_una_sin_clave_se_rechaza(self, entorno):
        with pytest.raises(HTTPException) as fallo:
            server.lanzar_trabajo(server.LanzarTrabajo(accion="leer_una"))
        assert fallo.value.status_code == 400


# ---------------------------------------------------------------------------
#  Uno cada vez, también desde la API
# ---------------------------------------------------------------------------

class TestUnoCadaVez:
    def test_el_segundo_boton_devuelve_409(self, entorno, monkeypatch):
        puerta = threading.Event()
        monkeypatch.setattr(
            server, "trabajos",
            Registro(lanzador=lanzador_que_devuelve(ProcesoFalso([], bloquear=puerta))),
        )
        sembrar(entorno.almacen, 30)

        server.lanzar_trabajo(server.LanzarTrabajo(accion="clasificar"))
        with pytest.raises(HTTPException) as fallo:
            server.lanzar_trabajo(server.LanzarTrabajo(accion="clasificar"))

        assert fallo.value.status_code == 409
        assert "en curso" in fallo.value.detail
        puerta.set()


# ---------------------------------------------------------------------------
#  Seguir una corrida
# ---------------------------------------------------------------------------

class TestSeguirLaCorrida:
    def test_el_trabajo_en_curso_sobrevive_a_recargar_la_pagina(self, entorno, monkeypatch):
        """El trabajo vive en el servidor, no en la pestaña."""
        puerta = threading.Event()
        registro = Registro(
            lanzador=lanzador_que_devuelve(ProcesoFalso([], bloquear=puerta))
        )
        monkeypatch.setattr(server, "trabajos", registro)
        sembrar(entorno.almacen, 30)

        lanzado = server.lanzar_trabajo(server.LanzarTrabajo(accion="clasificar"))
        assert server.listar_trabajos()["en_curso"]["id"] == lanzado["id"]

        puerta.set()

    def test_un_trabajo_inexistente_da_404(self, entorno):
        with pytest.raises(HTTPException) as fallo:
            server.ver_trabajo("no-existe")
        assert fallo.value.status_code == 404

    def test_cancelar_uno_terminado_da_409(self, entorno):
        sembrar(entorno.almacen, 30)
        lanzado = server.lanzar_trabajo(server.LanzarTrabajo(accion="clasificar"))
        esperar(SimpleNamespace(id=lanzado["id"]), entorno.registro)

        with pytest.raises(HTTPException) as fallo:
            server.cancelar_trabajo(lanzado["id"])
        assert fallo.value.status_code == 409

    def test_el_coste_que_se_enseno_viaja_con_el_trabajo(self, entorno):
        sembrar(entorno.almacen, 30)
        previsto = server.plan("clasificar")["coste_usd"]
        trabajo = server.lanzar_trabajo(server.LanzarTrabajo(accion="clasificar"))

        assert trabajo["coste_estimado_usd"] == pytest.approx(previsto, abs=0.0001)


# ---------------------------------------------------------------------------
#  El presupuesto
# ---------------------------------------------------------------------------

class TestPresupuesto:
    def test_ensena_las_dos_vistas(self, entorno):
        """La automática y la suya. La diferencia es la ración de las automáticas."""
        datos = server.presupuesto()

        assert datos["techo_mensual"] == 12.0
        assert datos["racion_automatica"] == 10.0
        assert datos["automatico"]["techo"] == 10.0
        assert datos["suyo"]["techo"] == 12.0
        assert datos["suyo"]["disponible"] > datos["automatico"]["disponible"]

    def test_el_tope_por_accion_viaja_a_la_pantalla(self, entorno):
        """Es el número que se le promete, así que tiene que poder enseñarse.

        Si la interfaz solo recibiera el techo del mes, la pantalla explicaría
        un freno que ya no es el que frena.
        """
        assert server.presupuesto()["tope_por_accion"] == 0.60

    def test_lo_gastado_se_ve(self, entorno):
        server._libro().registrar(3.5, llamadas=7)
        datos = server.presupuesto()

        assert datos["gastado_mes"] == pytest.approx(3.5)
        assert datos["llamadas_mes"] == 7

    def test_un_boton_suyo_puede_gastar_la_reserva(self, entorno):
        """Es la razón de ser de `racion_automatica_mensual_usd`.

        La tarea programada se para a los $10 para que su botón no se encuentre
        nunca la caja vacía.
        """
        server._libro().registrar(10.5)
        datos = server.presupuesto()

        assert datos["automatico"]["agotado"]
        assert not datos["suyo"]["agotado"]


# ---------------------------------------------------------------------------
#  El ranking
# ---------------------------------------------------------------------------

class TestRanking:
    def test_sin_ranking_no_se_inventa_uno(self, entorno):
        assert server.ranking() == {"hay": False}

    def test_devuelve_el_ultimo_guardado(self, entorno):
        claves = sembrar(entorno.almacen, 2)
        entorno.almacen.guardar_ranking(
            {
                "id": "r1", "creado_at": "2026-09-02T10:00:00+00:00",
                "modelo": "m", "lectura": "Empieza por la primera.",
                "patrones": '["Mucha logística esta semana."]',
                "ofertas_consideradas": 2, "tokens_entrada": 10,
                "tokens_salida": 5, "coste_usd": 0.05,
            },
            [(claves[0], 1, "Es la más cercana a su terreno.", "postular_ya"),
             (claves[1], 2, "Sector adyacente.", "revisar")],
        )

        datos = server.ranking()
        assert datos["hay"]
        assert datos["lectura"] == "Empieza por la primera."
        assert datos["patrones"] == ["Mucha logística esta semana."]
        assert [i["posicion"] for i in datos["items"]] == [1, 2]
        assert datos["items"][0]["accion"] == "postular_ya"


# ---------------------------------------------------------------------------
#  El orden envejece, y la pantalla lo dice
# ---------------------------------------------------------------------------

class TestElRankingEnvejece:
    """Un ranking NO es una caché, y hasta hoy envejecía en silencio.

    Es lo único del sistema que ella lee entero, y la pantalla solo decía su
    fecha. Alguien que abre la app y lee el orden está leyendo una foto vieja
    sin saberlo: la regla nº 1 de la casa rota justo en la pieza más importante.

    LA PARTE QUE HAY QUE FIJAR ES CÓMO SE MIDE. Contar «clasificaciones
    posteriores al ranking» parece lo natural y es lo que estaba mal: sobre la
    base real del 3 septiembre 2026 daba 94, y de esas 94 **45 estaban ya
    dentro del ranking** —se releyeron después y lo que se movió fue su
    `clasificado_at`, no su contenido—. La cifra de verdad era 1. Un aviso
    construido sobre el reloj habría gritado noventa veces por nada, y ella
    habría dejado de leerlo a la segunda.
    """

    def _con_ranking(self, entorno, claves_en_el_orden, disponibles=...):
        """`disponibles` = cuántas candidatas había cuando se ordenó.

        Por omisión, las mismas que se ordenaron: un orden sin recorte. `None`
        simula los rankings de antes del 4 septiembre 2026, que no guardaban el
        dato y traen 0 — «no consta», no «ninguna».
        """
        if disponibles is ...:
            disponibles = len(claves_en_el_orden)
        entorno.almacen.guardar_ranking(
            {
                "id": "r1", "creado_at": "2026-09-02T10:00:00+00:00",
                "modelo": "m", "lectura": "Empieza por la primera.",
                "patrones": "[]", "ofertas_consideradas": len(claves_en_el_orden),
                "ofertas_disponibles": 0 if disponibles is None else disponibles,
                "tokens_entrada": 10, "tokens_salida": 5, "coste_usd": 0.05,
            },
            [(clave, i + 1, "porque sí", "revisar")
             for i, clave in enumerate(claves_en_el_orden)],
        )

    def _clasificar(self, almacen, clave, *, relevante=True, criterio=False):
        almacen.guardar_clasificacion({
            "dedupe_key": clave, "estado": "ok", "relevante": relevante,
            "motivo_descarte": None, "confianza": 0.9, "razon": "r",
            "cv_id": "ejecutivo_es", "idioma_anuncio": "es",
            "idioma_detectado": "es", "empresa_normalizada": "Acme",
            "puesto_canonico": "Director", "es_reclutadora": False,
            "seniority_detectado": "direccion", "modalidad": "presencial",
            "necesita_criterio": criterio, "motivo_criterio": None,
            "error": None, "modelo": "m", "tokens_entrada": 1,
            "tokens_salida": 1, "coste_usd": 0.01,
            "fichas_huella": _fichas().huella, "heredada_de": None,
            "descripcion_via": "entera", "caracteres_descripcion": 900,
        })

    def test_un_orden_recien_hecho_no_avisa_de_nada(self, entorno):
        claves = sembrar(entorno.almacen, 2)
        for clave in claves:
            self._clasificar(entorno.almacen, clave)
        self._con_ranking(entorno, claves)

        desajuste = server.ranking()["desajuste"]
        assert desajuste == {"total": 2, "nuevas": 0, "ya_no_encajan": 0,
                             "ya_decididas": 0, "recortadas": 0}

    def test_una_clasificada_despues_cuenta_como_NUEVA(self, entorno):
        claves = sembrar(entorno.almacen, 3)
        for clave in claves:
            self._clasificar(entorno.almacen, clave)
        self._con_ranking(entorno, claves[:2])

        assert server.ranking()["desajuste"]["nuevas"] == 1

    def test_lo_que_el_orden_RECORTO_no_se_cuenta_como_nuevo(self, entorno):
        """Los dos avisos decían el mismo 25, y uno de los dos mentía.

        Salió al pulsar «Ponme al día» de verdad el 4 septiembre 2026. El orden
        se recortó a 20 de 45 —lo dijo antes de gastar— y la pantalla quedó con
        dos cajas de aviso pegadas:

            «Este orden se ha quedado atrás: 25 ofertas clasificadas después
             no están en este orden.»
            «Este orden no mira 25 ofertas más antiguas que tienes sin revisar.»

        Son las MISMAS 25, contadas dos veces y con motivos opuestos. La regla
        nº 15 de la casa —dos números de la misma pantalla no pueden
        contradecirse— rota justo debajo de lo único que ella lee entero.

        DE DÓNDE VENÍA. `nuevas` era `candidatas - items`, y de ahí se deducía
        «clasificadas después». Esa deducción era CIERTA mientras el ranking
        ordenara siempre todo lo que había: si algo era candidata y no estaba
        en el orden, solo podía haber llegado después. Dejó de serlo el día que
        el orden se recorta a propósito.

        LO QUE SE CUENTA AHORA es el crecimiento del conjunto: cuántas
        candidatas hay hoy contra cuántas había cuando se ordenó
        (`rankings.ofertas_disponibles`). Eso no puede confundir un recorte con
        una llegada, porque el recorte no cambia el tamaño del conjunto.
        """
        claves = sembrar(entorno.almacen, 5)
        for clave in claves:
            self._clasificar(entorno.almacen, clave)
        # Se ordenan 2 de las 5: las otras 3 se recortaron, no llegaron después.
        self._con_ranking(entorno, claves[:2], disponibles=5)

        desajuste = server.ranking()["desajuste"]
        assert desajuste["recortadas"] == 3
        assert desajuste["nuevas"] == 0, (
            "las 3 que el propio orden dejó fuera se están contando como "
            "llegadas nuevas: es el mismo número dicho de dos formas opuestas"
        )

    def test_una_que_llega_DESPUES_de_un_orden_recortado_si_es_nueva(self, entorno):
        """La prueba que se cae si el arreglo se pasa de frenada.

        Callar las llegadas de verdad para no repetir el recorte sería esconder
        ofertas para no dar un aviso feo, que es peor que el aviso.
        """
        claves = sembrar(entorno.almacen, 6)
        for clave in claves[:5]:
            self._clasificar(entorno.almacen, clave)
        self._con_ranking(entorno, claves[:2], disponibles=5)

        # La sexta se clasifica DESPUÉS del orden: el conjunto crece de 5 a 6.
        self._clasificar(entorno.almacen, claves[5])

        desajuste = server.ranking()["desajuste"]
        assert desajuste["nuevas"] == 1
        assert desajuste["recortadas"] == 3

    def test_un_orden_viejo_sin_el_dato_no_inventa_un_recorte(self, entorno):
        """Los rankings de antes del 4 septiembre 2026 traen `disponibles` a 0.

        Cero ahí significa «no consta», no «no había ninguna». Si se leyera como
        un número, cada orden viejo diría que recortó todo lo que hoy no está
        dentro, y eso es inventarse una procedencia — justo lo que este proyecto
        no hace en `descripcion_via` ni en `heredada_de`.
        """
        claves = sembrar(entorno.almacen, 3)
        for clave in claves:
            self._clasificar(entorno.almacen, clave)
        self._con_ranking(entorno, claves[:2], disponibles=None)

        desajuste = server.ranking()["desajuste"]
        assert desajuste["recortadas"] == 0
        assert desajuste["nuevas"] == 1, (
            "sin el dato hay que seguir contando como antes: la tercera no "
            "estaba en el orden y no consta que se recortara"
        )

    def test_una_que_el_sistema_descarto_DESPUES_cuenta_como_que_ya_no_encaja(
        self, entorno
    ):
        """Es la peor de las tres: no falta, SOBRA.

        Sigue en la lista, con su puesto y su motivo, y se lee como una
        recomendación del sistema cuando el propio sistema ya la ha descartado.
        """
        claves = sembrar(entorno.almacen, 2)
        for clave in claves:
            self._clasificar(entorno.almacen, clave)
        self._con_ranking(entorno, claves)

        # El sistema la vuelve a leer y cambia de opinión.
        self._clasificar(entorno.almacen, claves[1], relevante=False)

        desajuste = server.ranking()["desajuste"]
        assert desajuste["ya_no_encajan"] == 1
        assert desajuste["ya_decididas"] == 0, (
            "que el sistema cambie de opinión no es que ella haya decidido"
        )

    def test_las_que_ella_ya_marco_se_cuentan_APARTE(self, entorno):
        """No es un fallo del orden: es que ella va por delante.

        Meterlo en el mismo saco que «el sistema las descartó» haría que el
        aviso dijera que el orden está mal cuando lo que pasa es que ella ya
        trabajó.
        """
        claves = sembrar(entorno.almacen, 2)
        for clave in claves:
            self._clasificar(entorno.almacen, clave)
        self._con_ranking(entorno, claves)

        server.cambiar_estado(claves[0], server.CambioEstado(estado="postulada"))

        desajuste = server.ranking()["desajuste"]
        assert desajuste["ya_decididas"] == 1
        assert desajuste["ya_no_encajan"] == 0

    def test_releer_una_que_YA_ESTABA_en_el_orden_no_cuenta_como_nueva(self, entorno):
        """LA PRUEBA QUE FIJA EL ERROR DE MEDICIÓN.

        Reintroducirlo es contar `clasificaciones` con `clasificado_at`
        posterior a `rankings.creado_at`: esta oferta lo cumple —se releyó
        después— y NO es nueva, porque está dentro del orden.
        """
        claves = sembrar(entorno.almacen, 2)
        for clave in claves:
            self._clasificar(entorno.almacen, clave)
        self._con_ranking(entorno, claves)

        # Se relee, con el mismo veredicto: solo cambia `clasificado_at`.
        for clave in claves:
            self._clasificar(entorno.almacen, clave)

        assert server.ranking()["desajuste"]["nuevas"] == 0


# ---------------------------------------------------------------------------
#  El cuarto estado
# ---------------------------------------------------------------------------

class TestYaMePostule:
    """Con tres estados, «me interesa» era un callejón sin salida.

    Su trabajo de verdad tiene ese cuarto estado. El día que la app no lo
    tenga, ella lo lleva en una hoja aparte — y ese día la bandeja deja de ser
    donde trabaja, que es lo único que este proyecto no puede permitirse.
    """

    def test_se_puede_marcar(self, entorno):
        clave = sembrar(entorno.almacen, 1)[0]
        assert server.cambiar_estado(
            clave, server.CambioEstado(estado="postulada")
        )["estado"] == "postulada"
        assert entorno.almacen.counts_by_status()["postulada"] == 1

    def test_sale_del_orden_del_asistente(self, entorno):
        """Por el mismo motivo que «interesa» y «descartada»: el asistente
        ordena lo que ella todavía NO ha decidido, y postularse es decidir."""
        claves = sembrar(entorno.almacen, 2)
        for clave in claves:
            TestElRankingEnvejece()._clasificar(entorno.almacen, clave)
        assert len(entorno.almacen.ofertas_para_ranking()) == 2

        server.cambiar_estado(claves[0], server.CambioEstado(estado="postulada"))
        assert len(entorno.almacen.ofertas_para_ranking()) == 1

    def test_un_estado_inventado_se_sigue_rechazando(self, entorno):
        clave = sembrar(entorno.almacen, 1)[0]
        with pytest.raises(HTTPException) as fallo:
            server.cambiar_estado(clave, server.CambioEstado(estado="quizas"))
        assert fallo.value.status_code == 400

    def test_el_resumen_lo_cuenta(self, entorno):
        clave = sembrar(entorno.almacen, 1)[0]
        server.cambiar_estado(clave, server.CambioEstado(estado="postulada"))
        assert server.resumen()["por_estado"]["postulada"] == 1


# ---------------------------------------------------------------------------
#  La pantalla tiene un tope, y tiene que decirlo
# ---------------------------------------------------------------------------

class TestLaVistaNoEsconde:
    """Lo que no cabe en la pantalla se DICE. Regla nº 1 de la casa.

    La bandeja siempre trajo un máximo de anuncios por vista y nunca lo dijo.
    Medido el 3 septiembre 2026 contra la base real, con el filtro que sale al
    abrir la app: 379 anuncios en estado «nueva», 200 llegaban a la pantalla, y
    de sus 307 vacantes se veían 168. **139 vacantes no existían para ella**, y
    no había ninguna forma de enterarse ni de llegar a ellas.

    Son dos pruebas y hacen falta las dos, porque el fallo tenía dos mitades:
    que el tope era demasiado bajo para su base, y que era mudo. Subirlo sin
    decirlo solo mueve el día en que vuelve a pasar.
    """

    def test_una_bandeja_normal_se_enseña_entera(self, entorno):
        """250 anuncios —más de lo que ella acumula en una semana— caben."""
        sembrar(entorno.almacen, 250)
        respuesta = server.listar_ofertas()
        assert respuesta["anuncios"] == 250
        assert respuesta["hay_mas"] is False
        assert respuesta["anuncios_totales"] == 250

    def test_cuando_se_corta_lo_dice_y_dice_cuántas_faltan(self, entorno, monkeypatch):
        """Con el tope bajado a 10 para no sembrar 601 ofertas en una prueba.

        Lo que se fija no es el número —ese es una perilla— sino que la
        respuesta lleve las dos cifras: las que se enseñan y las que hay.
        """
        monkeypatch.setattr(server, "TOPE_DE_ANUNCIOS_POR_VISTA", 10)
        sembrar(entorno.almacen, 25)

        respuesta = server.listar_ofertas()
        assert respuesta["anuncios"] == 10
        assert respuesta["hay_mas"] is True
        assert respuesta["anuncios_totales"] == 25
        assert respuesta["tope"] == 10

    def test_el_recuento_usa_el_mismo_filtro_que_la_lista(self, entorno, monkeypatch):
        """Si contara sin filtrar, el aviso diría que faltan ofertas de otro
        estado y ella buscaría algo que no está. El WHERE vive en un solo
        sitio (`Storage._filtros_de_bandeja`) justo por esto."""
        monkeypatch.setattr(server, "TOPE_DE_ANUNCIOS_POR_VISTA", 5)
        claves = sembrar(entorno.almacen, 20)
        for clave in claves[:8]:
            server.cambiar_estado(clave, server.CambioEstado(estado="descartada"))

        respuesta = server.listar_ofertas(estado="nueva")
        assert respuesta["anuncios_totales"] == 12  # 20 − 8 descartadas
        assert server.listar_ofertas(estado="descartada")["anuncios_totales"] == 8


# ---------------------------------------------------------------------------
#  Las pastillas cuentan lo que la rejilla va a enseñar
# ---------------------------------------------------------------------------

class TestLosContadoresNoContradicenLaPantalla:
    """Dos números de la misma pantalla que no cuadraban entre sí.

    Medido el 4 septiembre 2026 sobre la base real, con México elegido en el
    desplegable: la pastilla decía «Relevantes 59» y la rejilla enseñaba **3
    tarjetas**. Las dos cifras eran ciertas y ninguna respondía a lo que ella
    estaba mirando — la pastilla contaba ANUNCIOS de toda la base (los dos
    mercados, los cuatro estados) y la rejilla enseña VACANTES del filtro
    puesto.

    Es la regla nº 14 de la casa —contar vacantes, no anuncios— en el sitio
    donde más se nota, porque un número que no describe lo que hay debajo se
    lee como un fallo del programa.
    """

    def _vacante_en_tres_ciudades(self, almacen, empresa="Acme", titulo="Director de Operaciones"):
        """Un mismo puesto publicado tres veces: 3 anuncios, 1 vacante."""
        return [
            oferta_en_base(
                almacen, "run-1", external_id=f"{empresa}-{ciudad}",
                title=titulo, company=empresa,
                location_raw=ciudad, location_city=ciudad,
            )
            for ciudad in ("Monterrey", "Guadalajara", "Puebla")
        ]

    def test_cuenta_vacantes_y_no_anuncios(self, entorno):
        self._vacante_en_tres_ciudades(entorno.almacen)

        resumen = server.resumen()
        rejilla = server.listar_ofertas()

        assert rejilla["anuncios"] == 3
        assert resumen["por_estado"]["nueva"] == 1 == rejilla["total"], (
            "la pastilla tiene que decir lo que se ve: una tarjeta, no tres anuncios"
        )

    def test_respeta_el_mercado_elegido(self, entorno):
        self._vacante_en_tres_ciudades(entorno.almacen)
        oferta_en_base(
            entorno.almacen, "run-1", external_id="tx-1", title="Chief Operating Officer",
            company="Otra", market_key="texas_us", location_raw="Austin", location_city="Austin",
        )

        assert server.resumen(mercado="mexico")["por_estado"]["nueva"] == 1
        assert server.resumen(mercado="texas_us")["por_estado"]["nueva"] == 1
        assert server.resumen()["por_estado"]["nueva"] == 2

    def test_la_lectura_del_sistema_cuenta_dentro_del_estado_que_se_mira(self, entorno):
        """«Nadie las ha mirado» de las descartadas no es la de las nuevas."""
        claves = self._vacante_en_tres_ciudades(entorno.almacen)
        otra = oferta_en_base(
            entorno.almacen, "run-1", external_id="otra-1",
            title="Director de Planta", company="Otra",
        )

        assert server.resumen()["vistas"]["sin_mirar"] == 2
        server.cambiar_estado(otra, server.CambioEstado(estado="descartada"))

        assert server.resumen(estado="nueva")["vistas"]["sin_mirar"] == 1
        assert server.resumen(estado="descartada")["vistas"]["sin_mirar"] == 1
        assert claves  # las tres siguen siendo una sola vacante nueva


# ---------------------------------------------------------------------------
#  La base sobrevive a la configuración
# ---------------------------------------------------------------------------

class TestUnMercadoQueYaNoEstaEnElYaml:
    """Quitar un mercado del YAML no puede dejarla sin abrir la app.

    Medido el 4 septiembre 2026, el día que la clienta preguntó si se podía
    cambiar un mercado por otro: con 147 ofertas de México en la base, quitar
    `mexico` de `search_profile.yaml` hacía que `/api/resumen` y `/api/ofertas`
    reventaran los dos con `ConfigError` — o sea, **la app no abría**.

    `config.market()` hace bien en gritar: RECOLECTAR de un mercado que no está
    definido es un error de configuración. Pero la bandeja no recolecta, enseña
    lo que ya está guardado, y ese histórico no desaparece porque hoy se busque
    en otro sitio. Importa más de lo que parece porque el ajuste de geografía es
    justo lo que puede salir de su sesión con el headhunter, y para entonces el
    desarrollador está a 9.000 km.
    """

    def _sin_el_mercado(self, entorno, clave: str, monkeypatch):
        config = entorno.config
        oferta_en_base(entorno.almacen, "run-1", external_id="tx-1",
                       market_key="texas_us", title="Chief Operating Officer")
        oferta_en_base(entorno.almacen, "run-1", external_id="mx-1",
                       market_key=clave, title="Director de Operaciones")

        # `config_falsa` resuelve cualquier clave; aquí se le enseña a negarse,
        # que es lo que hace el de verdad.
        def solo_texas(k):
            if k != "texas_us":
                raise ValueError(f"El mercado '{k}' no está definido.")
            return SimpleNamespace(label="Texas, EE.UU.")

        monkeypatch.setattr(config, "market", solo_texas)

    def test_el_resumen_sigue_abriendo(self, entorno, monkeypatch):
        self._sin_el_mercado(entorno, "mexico", monkeypatch)

        mercados = {m["clave"]: m["etiqueta"] for m in server.resumen()["mercados"]}

        assert mercados["texas_us"] == "Texas, EE.UU."
        assert mercados["mexico"] == "mexico", "sin etiqueta, pero enseñado"

    def test_las_ofertas_siguen_saliendo(self, entorno, monkeypatch):
        self._sin_el_mercado(entorno, "mexico", monkeypatch)

        ofertas = server.listar_ofertas()["ofertas"]

        assert len(ofertas) == 2
        assert {o["mercado_etiqueta"] for o in ofertas} == {"Texas, EE.UU.", "mexico"}


class TestElDocumentoDeAyuda:
    """`AYUDA.md`: el único documento escrito para ella, y el único `.md` que
    viaja en el clon.

    El README es el cuaderno de ingeniería y se quedó fuera del árbol el 7
    septiembre 2026, así que esto no es «un archivo más de documentación»: es
    lo único que ella puede consultar sin escribir a España. Si falta, el
    botón «Dudas y ayuda» abre un panel sin nada dentro.
    """

    def test_el_archivo_existe_y_no_esta_vacio(self):
        assert server.AYUDA_MD.exists(), (
            f"falta {server.AYUDA_MD.name}: es el único documento que viaja "
            "en el clon y el botón «Dudas y ayuda» se queda sin contenido"
        )
        assert server.AYUDA_MD.read_text(encoding="utf-8").strip()

    def test_el_endpoint_lo_devuelve_entero(self):
        # Entero y sin tocar: el mismo archivo se imprime en papel y se pinta
        # en la pantalla, y dos versiones del mismo texto son dos textos.
        respuesta = server.ayuda()
        assert respuesta["hay"] is True
        assert respuesta["texto"] == server.AYUDA_MD.read_text(encoding="utf-8")

    def test_sin_el_archivo_la_app_sigue_abriendo(self, monkeypatch, tmp_path):
        """UN DOCUMENTO QUE FALTA NO PUEDE TIRAR UNA VENTANA.

        La app tiene que abrir siempre; que no encuentre la ayuda es un
        problema del desarrollador y se dice en pantalla, no una excepción que
        deje la bandeja a medias.
        """
        monkeypatch.setattr(server, "AYUDA_MD", tmp_path / "no-esta.md")
        respuesta = server.ayuda()
        assert respuesta == {"hay": False, "texto": ""}

    def test_los_botones_que_nombra_siguen_existiendo(self):
        """LO QUE MÁS SE ROMPE DE UN MANUAL ES QUE LA PANTALLA CAMBIE.

        El documento manda pulsar botones por su nombre —«Parar», «Buscar el
        original», «Todos los mercados»—. Renombrar uno en la interfaz sin
        tocar el texto deja instrucciones que no se pueden seguir, y desde
        otro continente eso no se ve. Aquí se cae.
        """
        import re

        # SOLO `index.html`, Y SIN SUS COMENTARIOS. Buscar también en
        # `app.js` hacía la prueba inútil, y está medido: renombrando el
        # botón «Buscar el original» seguía saliendo en verde, porque ese
        # nombre aparece además en un mensaje y en un comentario del
        # JavaScript. Un nombre que sobrevive en un comentario no es un
        # botón que ella pueda pulsar.
        crudo = (Path(server.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
        pantalla = re.sub(r"<!--.*?-->", "", crudo, flags=re.DOTALL)
        texto = server.AYUDA_MD.read_text(encoding="utf-8")

        nombrados = {
            " ".join(n.split())                       # el archivo parte líneas
            for n in re.findall(r"«([^»]+)»", texto)
        }
        assert nombrados, "el documento ya no nombra ningún botón: revisa esto"

        perdidos = [n for n in sorted(nombrados) if n not in pantalla]
        assert not perdidos, (
            f"AYUDA.md manda pulsar {perdidos} y ese texto ya no está en la "
            "pantalla: o se renombró el botón o se renombró en el documento"
        )


# ---------------------------------------------------------------------------
#  Una decisión es de la VACANTE, no del anuncio que hace de portada
# ---------------------------------------------------------------------------

class TestMarcarLaVacanteEntera:
    """EL FALLO, MEDIDO USANDO LA APP EL 9 SEPTIEMBRE 2026.

    La bandeja pinta una tarjeta por vacante y dentro dice «la misma vacante
    aparece 10 veces». Al pulsar «Descartar» solo cambiaba el anuncio
    representante: los otros nueve seguían en «nueva». La tarjeta se iba de la
    pantalla —la quita el navegador— y volvía en el siguiente refresco como
    «9 ANUNCIOS». Descartarla de verdad costaba diez clics.

    Y no era raro: de las 272 vacantes de esa mañana, 32 tenían más de un
    anuncio y dos tenían diez.

    Peor todavía, cuesta dinero: las hermanas seguían PENDIENTES, así que la
    corrida siguiente pagaba por releer una vacante que ella ya había
    decidido — medido, $0,0398 y con un veredicto distinto del primero.
    """

    def _vacante_de(self, almacen, cuantos: int) -> list[str]:
        """`cuantos` anuncios del mismo puesto en ciudades distintas.

        Misma empresa y mismo título: es lo que hace que compartan
        `loose_key` y que `agrupar` los una en una sola tarjeta.
        """
        return [
            oferta_en_base(
                almacen, "run-1",
                external_id=f"dcs-{i}",
                title="Chief Operating Officer",
                company="Northline Asset Services",
                location_raw=f"Ciudad {i}",
                location_city=f"Ciudad {i}",
            )
            for i in range(cuantos)
        ]

    def test_descartar_se_lleva_los_diez_anuncios(self, entorno):
        claves = self._vacante_de(entorno.almacen, 10)
        tarjeta = server.listar_ofertas()["ofertas"][0]
        assert tarjeta["grupo"]["tamano"] == 10, "una tarjeta, diez anuncios"

        respuesta = server.cambiar_estado(
            tarjeta["dedupe_key"],
            server.CambioEstado(
                estado="descartada",
                hermanas=[m["clave"] for m in tarjeta["grupo"]["miembros"]],
            ),
        )

        assert respuesta["anuncios"] == 10
        cuentas = entorno.almacen.counts_by_status()
        assert cuentas.get("nueva", 0) == 0, (
            "si queda uno en «nueva», la vacante vuelve en el siguiente refresco"
        )
        assert cuentas["descartada"] == 10
        assert len(claves) == 10

    def test_no_reaparece_al_refrescar(self, entorno):
        """La prueba tal como ella la vive: marcar, recargar, y que no vuelva."""
        self._vacante_de(entorno.almacen, 4)
        tarjeta = server.listar_ofertas()["ofertas"][0]

        server.cambiar_estado(
            tarjeta["dedupe_key"],
            server.CambioEstado(
                estado="interesa",
                hermanas=[m["clave"] for m in tarjeta["grupo"]["miembros"]],
            ),
        )

        assert server.listar_ofertas(estado="nueva")["ofertas"] == []
        assert len(server.listar_ofertas(estado="interesa")["ofertas"]) == 1

    def test_las_cuentas_no_la_ponen_en_dos_sitios(self, entorno):
        """Contaba a la vez en «Sin revisar» y en «Interesan»."""
        self._vacante_de(entorno.almacen, 3)
        tarjeta = server.listar_ofertas()["ofertas"][0]

        server.cambiar_estado(
            tarjeta["dedupe_key"],
            server.CambioEstado(
                estado="interesa",
                hermanas=[m["clave"] for m in tarjeta["grupo"]["miembros"]],
            ),
        )

        por_estado = server.resumen()["por_estado"]
        assert por_estado["nueva"] == 0
        assert por_estado["interesa"] == 1

    def test_sin_hermanas_sigue_marcando_la_suya(self, entorno):
        """Compatibilidad: un cliente viejo, o una oferta sin grupo."""
        clave = sembrar(entorno.almacen, 1)[0]
        respuesta = server.cambiar_estado(clave, server.CambioEstado(estado="postulada"))
        assert respuesta["anuncios"] == 1
        assert entorno.almacen.counts_by_status()["postulada"] == 1

    def test_una_hermana_que_ya_no_existe_no_rompe_la_decision(self, entorno):
        """La base puede cambiar entre que se pinta la ficha y ella pulsa.

        Perder la decisión entera por una clave que ya no está sería castigarla
        por una carrera que no ha provocado.
        """
        claves = self._vacante_de(entorno.almacen, 2)
        respuesta = server.cambiar_estado(
            claves[0],
            server.CambioEstado(
                estado="descartada",
                hermanas=[claves[1], "una-clave-que-no-existe"],
            ),
        )
        assert respuesta["anuncios"] == 2
        assert entorno.almacen.counts_by_status()["descartada"] == 2

    def test_la_portada_que_no_existe_sigue_siendo_404(self, entorno):
        with pytest.raises(HTTPException) as fallo:
            server.cambiar_estado(
                "no-existe", server.CambioEstado(estado="descartada", hermanas=[])
            )
        assert fallo.value.status_code == 404
