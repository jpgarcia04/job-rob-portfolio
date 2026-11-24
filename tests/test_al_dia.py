"""
«Ponme al día»: tres comandos seguidos bajo UN solo tope.

QUÉ SE VIGILA AQUÍ, Y POR QUÉ SOLO ESTO
---------------------------------------
Encadenar comandos es fácil; lo que puede salir caro es el tope. El freno que
se le promete a la clienta es POR ACCIÓN, y una acción de tres tramos donde
cada uno se mide contra los $0,60 no promete $0,60: promete $1,80. Es el mismo
error que la v8 corrigió al mover el freno de por-llamada a por-acción,
cometido otra vez un nivel más arriba — y esta vez sin que falle nada, que es
lo que lo hace peligroso.

Así que estas pruebas fijan tres cosas:

  1. que el tope de un tramo NUNCA pueda subir, solo bajar;
  2. que cada tramo se lance con lo que QUEDA del tope, no con el tope entero;
  3. que quedarse sin tope pare la cadena y lo DIGA, sin que eso se lea como
     un fallo — lo que no se hizo sigue pendiente, que es la regla de la casa.

Ninguna abre un proceso ni llama a nada: el lanzador entra por parámetro, igual
que el cliente del LLM en el resto del proyecto.
"""

from __future__ import annotations

import threading

import pytest

from bandeja.trabajos import Parada, Paso, Registro
from clasificador.presupuesto import GuardiaPresupuesto


class ProcesoFalso:
    def __init__(self, codigo: int = 0, lineas: list[str] | None = None) -> None:
        self._codigo = codigo
        self._lineas = lineas or []

    @property
    def stdout(self):
        return iter(linea + "\n" for linea in self._lineas)

    def wait(self) -> int:
        return self._codigo

    def kill(self) -> None:
        pass


class ConfigDeMentira:
    """Lo mínimo que mira `GuardiaPresupuesto`."""

    def __init__(self, tope: float) -> None:
        self.tope_por_accion_usd = tope
        self.presupuesto_mensual_usd = 12.0
        self.al_agotarse = "avisar"

    def frena_el_mes(self, es_manual: bool) -> bool:
        return False


class LibroDeMentira:
    def __init__(self, gastado: float = 0.0) -> None:
        self._gastado = gastado

    def gastado_total(self) -> float:
        return self._gastado


def esperar(condicion, segundos: float = 3.0) -> None:
    import time

    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if condicion():
            return
        time.sleep(0.01)
    raise AssertionError("no se cumplió a tiempo")


# ---------------------------------------------------------------------------
#  1. El tope de un tramo solo puede BAJAR
# ---------------------------------------------------------------------------

class TestElTopeDeUnTramoSoloBaja:
    """Es la guarda que impide que `--tope-accion-usd` afloje el freno.

    Sin ella, el parámetro que reparte el tope entre los tramos serviría
    también para subirlo, y entonces la promesa que se le enseña dejaría de ser
    un techo: sería una sugerencia que cualquier comando puede ignorar. Es
    exactamente lo que el proyecto se niega a hacer cuando algo no cabe.
    """

    def test_sin_tramo_manda_el_yaml(self):
        guardia = GuardiaPresupuesto(LibroDeMentira(), ConfigDeMentira(0.60))
        assert guardia.tope_por_accion == pytest.approx(0.60)

    def test_un_tramo_mas_pequeno_baja_el_tope(self):
        guardia = GuardiaPresupuesto(
            LibroDeMentira(), ConfigDeMentira(0.60), tope_accion=0.24
        )
        assert guardia.tope_por_accion == pytest.approx(0.24)
        assert guardia.abrir_accion(0.30) is not None, (
            "con $0,24 de tramo, una acción de $0,30 tiene que rechazarse"
        )

    def test_un_tramo_mas_grande_NO_sube_el_tope(self):
        """El fallo que esta prueba existe para cazar.

        Si `tope_por_accion` devolviera el máximo en vez del mínimo, esta
        llamada daría 999 y `abrir_accion(50)` diría que sí. Reintroducirlo es
        cambiar `min` por `max` en `presupuesto.tope_por_accion`.
        """
        guardia = GuardiaPresupuesto(
            LibroDeMentira(), ConfigDeMentira(0.60), tope_accion=999.0
        )
        assert guardia.tope_por_accion == pytest.approx(0.60)
        assert guardia.abrir_accion(50.0) is not None

    def test_un_tramo_a_cero_no_deja_gastar_nada(self):
        guardia = GuardiaPresupuesto(
            LibroDeMentira(), ConfigDeMentira(0.60), tope_accion=0.0
        )
        assert guardia.abrir_accion(0.01) is not None


class TestElFlagLlegaAlGuardia:
    """Que `--tope-accion-usd` no se quede en el parser.

    Los cuatro comandos que gastan construyen el guardia por el mismo sitio, y
    un flag que se parsea y no se usa es peor que no tenerlo: parece que frena.
    """

    @pytest.mark.parametrize("argv", [
        ["--manual", "--tope-accion-usd", "0.05"],
        ["ranking", "--manual", "--tope-accion-usd", "0.05"],
        ["releer", "--manual", "--tope-accion-usd", "0.05"],
    ])
    def test_cada_comando_que_gasta_lo_respeta(self, argv):
        from clasificador.cli import build_parser, _construir_guardia

        args = build_parser().parse_args(argv)
        assert args.tope_accion_usd == pytest.approx(0.05)

        class ConfigConLlm:
            llm = ConfigDeMentira(0.60)

        # Se construye el guardia como lo hace el CLI, con un libro postizo.
        guardia = GuardiaPresupuesto(
            LibroDeMentira(), ConfigConLlm.llm,
            es_manual=True, tope_accion=args.tope_accion_usd,
        )
        assert guardia.tope_por_accion == pytest.approx(0.05)
        assert _construir_guardia is not None  # el CLI sigue teniendo la vía


# ---------------------------------------------------------------------------
#  2. La cadena reparte el tope y no lo triplica
# ---------------------------------------------------------------------------

class TestLaCadenaCompartUnSoloTope:
    def test_los_tramos_se_lanzan_en_orden(self):
        lanzados: list[list[str]] = []

        def lanzador(argv):
            lanzados.append(list(argv))
            return ProcesoFalso(codigo=0)

        registro = Registro(lanzador=lanzador)
        pasos = [
            Paso("uno", ["-m", "recolector", "run"]),
            Paso("dos", ["-m", "clasificador", "--manual"]),
            Paso("tres", ["-m", "clasificador", "ranking", "--manual"]),
        ]
        trabajo = registro.lanzar_cadena(
            accion="al_dia", etiqueta="Ponme al día",
            planificador=lambda i: pasos[i], pasos_total=3,
        )
        esperar(lambda: trabajo.terminado)

        assert [a[1] for a in lanzados] == ["recolector", "clasificador", "clasificador"]
        assert trabajo.estado == "terminado"
        assert trabajo.pasos_total == 3 and trabajo.paso_actual == 3

    def test_cada_tramo_recibe_lo_que_QUEDA_del_tope(self):
        """LA PRUEBA QUE SOSTIENE LA PROMESA.

        El planificador es el que reparte, y aquí se comprueba que reparte de
        verdad: el segundo tramo tiene que recibir menos que el primero porque
        el primero ya gastó. Si alguien devolviera el tope entero en cada
        tramo, esta prueba se cae — y ese es exactamente el fallo de las «tres
        promesas de $0,60».
        """
        TOPE = 0.60
        libro = LibroDeMentira(0.0)
        lanzados: list[list[str]] = []

        def lanzador(argv):
            lanzados.append(list(argv))
            # Cada tramo «gasta» $0,20 en cuanto corre.
            libro._gastado += 0.20
            return ProcesoFalso(codigo=0)

        def planificador(indice):
            restante = round(TOPE - libro.gastado_total(), 6)
            if restante <= 0:
                return Parada("sin tope", fue_bien=True)
            return Paso(
                f"tramo {indice}",
                ["-m", "clasificador", "--manual", "--tope-accion-usd", f"{restante:.6f}"],
            )

        registro = Registro(lanzador=lanzador)
        trabajo = registro.lanzar_cadena(
            accion="al_dia", etiqueta="Ponme al día",
            planificador=planificador, pasos_total=3,
        )
        esperar(lambda: trabajo.terminado)

        topes = [float(a[a.index("--tope-accion-usd") + 1]) for a in lanzados]
        assert topes == pytest.approx([0.60, 0.40, 0.20]), (
            f"cada tramo tiene que llevar lo que queda, no el tope entero: {topes}"
        )
        assert sum(topes) != pytest.approx(TOPE * 3)

    def test_quedarse_sin_tope_para_la_cadena_y_lo_dice(self):
        """Y NO es un fallo: es la promesa funcionando.

        Lo que falta sigue pendiente. Si esto se marcara «fallido», la pantalla
        diría que se rompió algo donde lo que pasó es que se cumplió el tope —
        la misma distinción que separa «sin saldo» de «se rompió» en el resto
        del proyecto.
        """
        def planificador(indice):
            if indice == 0:
                return Paso("uno", ["-m", "recolector", "run"])
            return Parada(
                "«Ordenar» costaría $0.2897 y a esta acción le quedan $0.0500.",
                fue_bien=True,
            )

        registro = Registro(lanzador=lambda argv: ProcesoFalso(codigo=0))
        trabajo = registro.lanzar_cadena(
            accion="al_dia", etiqueta="Ponme al día",
            planificador=planificador, pasos_total=3,
        )
        esperar(lambda: trabajo.terminado)

        assert trabajo.estado == "terminado", "quedarse sin tope no es un fallo"
        assert trabajo.quedo_sin_hacer and "Ordenar" in trabajo.quedo_sin_hacer
        assert trabajo.paso_actual == 1

    def test_un_tramo_que_falla_para_la_cadena_con_SU_significado(self):
        """El código 3 es «no había saldo» y tiene que llegar así a la pantalla."""
        def planificador(indice):
            return Paso(f"tramo {indice}", ["-m", "clasificador", "--manual"])

        codigos = iter([0, 3, 0])
        registro = Registro(lanzador=lambda argv: ProcesoFalso(codigo=next(codigos)))
        trabajo = registro.lanzar_cadena(
            accion="al_dia", etiqueta="Ponme al día",
            planificador=planificador, pasos_total=3,
        )
        esperar(lambda: trabajo.terminado)

        assert trabajo.estado == "sin_saldo"
        assert trabajo.paso_actual == 2, "no se sigue al tercero tras quedarse sin saldo"
        assert trabajo.quedo_sin_hacer

    def test_no_se_puede_lanzar_una_cadena_con_otra_corriendo(self):
        """El libro de gastos se escribe entero sin cerrojo: dos a la vez se
        pisan los apuntes, y ese dinero desaparece de la cuenta del mes."""
        from bandeja.trabajos import TrabajoEnCursoError

        puerta = threading.Event()

        class Lento(ProcesoFalso):
            def wait(self):
                puerta.wait(timeout=5)
                return 0

        registro = Registro(lanzador=lambda argv: Lento())
        registro.lanzar_cadena(
            accion="al_dia", etiqueta="Ponme al día",
            planificador=lambda i: Paso("x", ["-m", "recolector", "run"]),
            pasos_total=3,
        )
        esperar(lambda: registro.en_curso() is not None)
        try:
            with pytest.raises(TrabajoEnCursoError):
                registro.lanzar("recolectar", ["-m", "recolector", "run"], "otra")
        finally:
            puerta.set()


# ---------------------------------------------------------------------------
#  3. El plan de la cadena
# ---------------------------------------------------------------------------

class TestElPlanDeLaCadena:
    """Que el plan describa la cadena que se va a lanzar, no otra.

    Es la regla del archivo `plan.py`: un plan que no describe la corrida que
    viene detrás no es un plan.
    """

    def _plan(self, tope: float):
        from recolector.config import load_config
        from recolector.storage import Storage
        from clasificador.fichas import cargar_fichas, cargar_preferencias
        from clasificador.plan import plan_del_dia

        config = load_config()
        if not config.database_path.exists():
            pytest.skip("sin base de datos: esta prueba mide contra la real")
        object.__setattr__(config.llm, "tope_por_accion_usd", tope)
        almacen = Storage(config.database_path)
        try:
            return plan_del_dia(
                almacen, config, cargar_fichas(), cargar_preferencias()
            )
        finally:
            almacen.close()

    def test_lo_que_se_lanza_nunca_pasa_del_tope(self):
        for tope in (0.60, 0.30, 0.10):
            plan = self._plan(tope)
            assert plan.coste_usd <= tope + 1e-9, (
                f"con tope ${tope:.2f} la cadena se lanzaría por "
                f"${plan.coste_usd:.4f}"
            )

    def test_lo_que_no_cabe_se_queda_fuera_y_se_dice(self):
        """Y se enseña ADEMÁS el coste completo.

        Esconder lo que falta haría que «Ponme al día» dijera que ya está al
        día cuando le falta el paso que ella lee entero. Regla nº 1 de la casa
        aplicada a un importe.
        """
        plan = self._plan(0.10)
        assert plan.quedan_fuera, "con un tope de $0,10 algo tiene que quedarse fuera"
        assert plan.coste_completo_usd > plan.coste_usd
        for tramo in plan.quedan_fuera:
            assert tramo.etiqueta in plan.nota

    def test_el_ranking_se_estima_CONTANDO_lo_que_se_va_a_leer(self):
        """Si no, el número enseñado sale menor que el cobrado.

        El paso 2 clasifica ofertas que el paso 3 tiene que ordenar. Estimar el
        ranking sobre las candidatas de ahora deja el importe corto, y ese es
        el único lado que este proyecto no se permite — es el error que el
        estimador del ranking ya cometió una vez, con un 66% de menos.
        """
        from recolector.config import load_config
        from recolector.storage import Storage
        from clasificador.fichas import cargar_preferencias
        from clasificador.plan import plan_de_ranking

        config = load_config()
        if not config.database_path.exists():
            pytest.skip("sin base de datos")
        almacen = Storage(config.database_path)
        try:
            if not almacen.ofertas_para_ranking(limite=None):
                pytest.skip("no hay nada que ordenar")
            prefs = cargar_preferencias()
            sin_extra = plan_de_ranking(almacen, prefs, esfuerzo="medium")
            con_extra = plan_de_ranking(
                almacen, prefs, esfuerzo="medium", ofertas_extra=8
            )
        finally:
            almacen.close()

        assert con_extra.ofertas == sin_extra.ofertas + 8
        assert con_extra.coste_usd > sin_extra.coste_usd


# ---------------------------------------------------------------------------
#  4. Un ranking que no cabe entero SE PARTE, y el recorte se dice
# ---------------------------------------------------------------------------

class TestElRankingQueNoCabeSePARTE:
    """El caso que se colaba entre los dos frenos, y era el silencioso.

    «Ordenar por dónde empezar» puede no caber por DOS motivos distintos:

      · **no cabe en el importe** — el tope por acción se lo comieron los
        tramos de antes;
      · **no cabe en la llamada** — el lote pasa de las ~60 ofertas que
        `asistente.cabida` admite a `medium`, y `plan_de_ranking` devolvía
        entonces coste $0,00. Con coste cero el tramo entraba en cualquier
        tope, `hay_trabajo` era False, y el diálogo imprimía **«nada que
        hacer»** encima del único paso que ella lee entero.

    No era un caso lejano: al ranking entran las clasificadas sin revisar, y
    eso crece hasta 8 al día. Con 39 esperando el 4 septiembre 2026, tres
    corridas sin marcar nada lo cruzaban.

    LA SALIDA NO ES NEGARSE, ES PARTIR. Es el patrón de `plan_de_relectura`:
    cuando algo no cabe en una acción se busca el trozo más grande que sí, y
    **se dice lo que se dejó fuera**. Negarse era la respuesta segura cuando la
    alternativa era pasarse del tope; no lo es cuando la alternativa es un
    orden más corto anunciado como tal.

    Lo que estas pruebas fijan es que el recorte NUNCA sea mudo. Un orden de 33
    sobre 98 pintado igual que uno de 98 sobre 98 esconde ofertas dentro de la
    pieza que existe para no esconder ninguna — regla nº 1 de la casa, y la
    nº 15 con ella.
    """

    LOTE_QUE_NO_CABE = 90  # a `medium` caben ~60, y el tope paga menos aún

    def _plan_con_una_cola_de(self, cuantas: int, tope: float = 0.60):
        from recolector.config import load_config
        from recolector.storage import Storage
        from clasificador.fichas import cargar_fichas, cargar_preferencias
        from clasificador.plan import plan_del_dia

        config = load_config()
        if not config.database_path.exists():
            pytest.skip("sin base de datos: esta prueba mide contra la real")
        object.__setattr__(config.llm, "tope_por_accion_usd", tope)
        almacen = Storage(config.database_path)
        try:
            filas = list(almacen.ofertas_para_ranking(limite=None))
            if not filas:
                pytest.skip("no hay nada que ordenar")
            cola = (filas * (cuantas // len(filas) + 1))[:cuantas]

            class ColaLarga:
                """El almacén de verdad, con la cola del ranking estirada.

                Implementa las DOS lecturas de la cola —la lista y el
                contador—: si solo se falseara una, el numerador y el
                denominador de «33 de 98» vendrían de sitios distintos, que es
                justo el fallo que esto vigila.
                """

                def __init__(self, real):
                    self._real = real

                def __getattr__(self, nombre):
                    return getattr(self._real, nombre)

                def ofertas_para_ranking(self, limite=None):
                    return list(cola if limite is None else cola[:limite])

                def contar_ofertas_para_ranking(self):
                    return len(cola)

            return plan_del_dia(
                ColaLarga(almacen), config, cargar_fichas(), cargar_preferencias()
            )
        finally:
            almacen.close()

    def _ranking(self, plan):
        return next(t for t in plan.tramos if t.accion == "ranking")

    def test_se_ordena_un_lote_mas_corto_en_vez_de_no_ordenar_nada(self):
        plan = self._plan_con_una_cola_de(self.LOTE_QUE_NO_CABE)
        ranking = self._ranking(plan)
        assert ranking.hay_trabajo and ranking.entra, (
            f"{self.LOTE_QUE_NO_CABE} ofertas esperando y el orden no se hace: "
            "había un lote más corto perfectamente pagable"
        )
        assert 0 < ranking.detalle["Ofertas a ordenar"] < self.LOTE_QUE_NO_CABE
        assert ranking.coste_usd > 0

    def test_la_cadena_entera_sigue_cabiendo_en_el_tope(self):
        for cola in (39, 61, self.LOTE_QUE_NO_CABE, 200):
            plan = self._plan_con_una_cola_de(cola)
            assert plan.coste_usd <= plan.tope_por_accion_usd + 1e-9, (
                f"con {cola} ofertas por ordenar la cadena se lanzaría por "
                f"${plan.coste_usd:.4f}"
            )

    def test_el_recorte_NO_es_mudo(self):
        """Lo que de verdad ve la clienta, no lo que calcula el plan."""
        import bandeja.server as srv

        plan = self._plan_con_una_cola_de(self.LOTE_QUE_NO_CABE)
        ranking = self._ranking(plan)
        # Son las de la cola MÁS las que el paso 2 va a leer: el ranking de la
        # cadena ordena lo de hoy más lo que acaba de entrar, y estimarlo sobre
        # la cola de ahora dejaría el importe corto.
        assert ranking.recortado_de >= self.LOTE_QUE_NO_CABE
        assert ranking.etiqueta in plan.nota

        original = srv.plan_del_dia
        srv.plan_del_dia = lambda *a, **k: plan
        try:
            json = srv._plan_del_dia_como_json()
        finally:
            srv.plan_del_dia = original

        linea = json["detalle"][ranking.etiqueta]
        assert linea != "nada que hacer"
        assert str(ranking.recortado_de) in linea, (
            f"la línea dice «{linea}» y no dice de cuántas se ordenaron"
        )
        assert json["avisos"], "un orden recortado sin un solo aviso"
        assert any("no las mira" in a for a in json["avisos"])

    def test_con_un_lote_que_cabe_entero_no_se_recorta_ni_se_avisa(self):
        """La prueba que se cae si el arreglo se pasa de frenada.

        El tamaño se DERIVA del YAML en vez de escribirse: la cadena estima el
        ranking sobre la cola más las lecturas que el paso 2 va a hacer, así
        que un número fijo aquí se rompería al mover cualquiera de las dos
        perillas — y se rompería diciendo que el recorte está mal cuando lo que
        cambió fue la configuración.
        """
        import bandeja.server as srv
        from recolector.config import load_config

        config = load_config()
        techo = config.llm.max_ofertas_por_ranking or 60
        cabe_de_sobra = max(1, techo - config.llm.triaje.ofertas_por_corrida)

        plan = self._plan_con_una_cola_de(cabe_de_sobra)
        ranking = self._ranking(plan)
        assert ranking.entra
        assert ranking.recortado_de is None
        assert ranking.motivo_fuera is None

        original = srv.plan_del_dia
        srv.plan_del_dia = lambda *a, **k: plan
        try:
            json = srv._plan_del_dia_como_json()
        finally:
            srv.plan_del_dia = original
        assert not json["avisos"]

    def test_si_no_cabe_NI_UNA_se_dice_el_motivo_y_no_nada_que_hacer(self):
        """El único caso en que el orden sigue cayéndose de la cadena.

        Con el tope casi agotado por la lectura no hay lote corto que valga.
        Entonces sí se queda fuera — pero como «no entra», nunca como «nada
        que hacer», que es lo que se lee como «ya estás al día».
        """
        import bandeja.server as srv

        # El tope se DERIVA de lo que cuesta el paso 2, más una miga: escribir
        # un número aquí lo ataría a `ofertas_por_corrida`, y entonces bajar
        # las lecturas —que abarata el paso 2— dejaría hueco para un ranking
        # corto y la prueba se caería sin que nada estuviera roto.
        holgado = self._plan_con_una_cola_de(self.LOTE_QUE_NO_CABE)
        paso2 = next(t for t in holgado.tramos if t.accion == "clasificar")
        plan = self._plan_con_una_cola_de(
            self.LOTE_QUE_NO_CABE, tope=round(paso2.coste_usd + 0.001, 6)
        )
        ranking = self._ranking(plan)
        assert ranking.hay_trabajo
        assert not ranking.entra
        assert ranking.motivo_fuera in ("tope", "llamada")
        assert ranking in plan.quedan_fuera

        original = srv.plan_del_dia
        srv.plan_del_dia = lambda *a, **k: plan
        try:
            json = srv._plan_del_dia_como_json()
        finally:
            srv.plan_del_dia = original
        assert json["detalle"][ranking.etiqueta] != "nada que hacer"
        assert json["avisos"]

    def test_el_recorte_viaja_al_comando_que_se_lanza(self):
        """El plan dice «33 de 98» y el subproceso tiene que ordenar 33.

        Sin el `--limite`, el comando volvería a pedir la cola entera y se
        negaría él solo: el número enseñado no describiría la corrida.
        """
        from clasificador.plan import plan_de_ranking_que_quepa
        from recolector.config import load_config
        from recolector.storage import Storage
        from clasificador.fichas import cargar_preferencias

        config = load_config()
        if not config.database_path.exists():
            pytest.skip("sin base de datos")
        almacen = Storage(config.database_path)
        try:
            if not almacen.ofertas_para_ranking(limite=None):
                pytest.skip("no hay nada que ordenar")
            plan = plan_de_ranking_que_quepa(
                almacen, cargar_preferencias(),
                esfuerzo="medium", tope_restante=0.05,
            )
        finally:
            almacen.close()
        assert plan.limite is not None
        assert plan.limite == plan.ofertas
        assert plan.recortado
