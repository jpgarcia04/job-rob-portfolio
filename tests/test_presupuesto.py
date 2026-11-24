"""
El techo de gasto del LLM.

Un guardia de presupuesto que no funciona es PEOR que no tener ninguno: da
permiso para dejar la app corriendo sola en la máquina de la clienta creyendo
que hay una red debajo. Por eso estas pruebas no se conforman con comprobar
que el contador suma.

Las cinco que de verdad importan:

  1. Que al pararse, lo no clasificado SIGA PENDIENTE — sin fila en
     `clasificaciones`. Si el guardia parara dejando las ofertas marcadas como
     hechas, se le esconderían para siempre y nadie se enteraría.
  2. Que un libro de gastos ilegible NO se interprete como "cero gastado".
  3. Que la ausencia del bloque `llm:` en el YAML no signifique "sin techo".
  4. Que el TOPE POR ACCIÓN frene de verdad, y no solo a la entrada: una
     estimación se queda corta el día que tocan ocho anuncios largos.
  5. Que el tope escrito en el YAML QUEPA con las perillas escritas en ese
     mismo YAML. Si no cabe, su botón se niega y no hay nada roto que mirar.

Las cinco son fallos que no dan ningún síntoma: la app parece funcionar
perfectamente mientras gasta de más, esconde ofertas o se niega sin motivo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clasificador.motor import Motor
from clasificador.presupuesto import (
    reparar_libro_de_la_entrega,
    GuardiaPresupuesto,
    LibroCorruptoError,
    LibroDeGastos,
)
from recolector.config import ConfigError, LlmBudgetConfig, _parse_llm_budget
from recolector.storage import Storage

from .test_clasificador import (
    ClienteFalso,
    almacen,          # noqa: F401  (fixture de pytest, se usa por nombre)
    fichas_de_prueba,
    oferta_en_base,
    respuesta_ok,
)


def config_llm(
    techo: float = 1.0,
    modo: str = "parar",
    racion: float | None = None,
    avisar_pct: int = 75,
    tope_accion: float = 0.0,
) -> LlmBudgetConfig:
    """`racion` por defecto = el techo entero: las automáticas no se recortan.

    `tope_accion` por defecto = 0, que significa «sin tope por acción». Las
    pruebas que van del techo MENSUAL no tienen que montar el otro freno, igual
    que las que no van de presupuesto pasan `presupuesto=None` al motor.
    """
    return LlmBudgetConfig(
        presupuesto_mensual_usd=techo,
        al_agotarse=modo,
        racion_automatica_mensual_usd=techo if racion is None else racion,
        avisar_al_superar_pct=avisar_pct,
        tope_por_accion_usd=tope_accion,
    )


def guardia(tmp_path: Path, **kwargs) -> GuardiaPresupuesto:
    es_manual = kwargs.pop("es_manual", False)
    libro = LibroDeGastos(tmp_path / "uso_llm.json")
    return GuardiaPresupuesto(libro, config_llm(**kwargs), es_manual=es_manual)


def motor_con_techo(almacen, tmp_path: Path, **kwargs):
    """Un motor con guardia y con un cliente que APUNTA en el mismo libro.

    UN SOLO LIBRO POR PROCESO, y aquí está el porqué en miniatura: si el
    cliente abriera el suyo, el guardia leería un saldo congelado y el freno
    no frenaría nunca — el fallo se vería como «no se paró», que es
    exactamente lo que estas pruebas existen para cazar.
    """
    g = guardia(tmp_path, **kwargs)
    cliente = ClienteFalso(respuesta_ok(), libro=g.libro)
    return Motor(almacen, fichas_de_prueba(), cliente, presupuesto=g), cliente, g


# ---------------------------------------------------------------------------
#  El libro de gastos
# ---------------------------------------------------------------------------

def test_libro_nuevo_empieza_en_cero(tmp_path: Path):
    libro = LibroDeGastos(tmp_path / "uso_llm.json")
    assert libro.gastado_este_mes() == 0.0
    assert libro.llamadas_este_mes() == 0


def test_el_gasto_se_escribe_a_disco_en_cada_apunte(tmp_path: Path):
    """No al final de la corrida: a cada llamada.

    Si el proceso muere a mitad —Ctrl+C, corte de luz, la laptop se cierra—
    lo gastado hasta ese momento tiene que estar contado. Un contador que
    solo persiste al terminar regala presupuesto cada vez que algo falla.
    """
    ruta = tmp_path / "uso_llm.json"
    LibroDeGastos(ruta).registrar(0.25)

    # Otra instancia, como si fuera otro proceso: tiene que ver el gasto.
    assert LibroDeGastos(ruta).gastado_este_mes() == pytest.approx(0.25)


def test_el_libro_acumula_llamadas_y_dolares(tmp_path: Path):
    libro = LibroDeGastos(tmp_path / "uso_llm.json")
    libro.registrar(0.10)
    libro.registrar(0.05)
    assert libro.gastado_este_mes() == pytest.approx(0.15)
    assert libro.llamadas_este_mes() == 2
    assert libro.gastado_total() == pytest.approx(0.15)


def test_un_libro_ilegible_no_se_interpreta_como_cero(tmp_path: Path):
    """El fallo más caro posible de este módulo.

    `WindowedUsage` sí puede reiniciarse solo cuando su archivo se corrompe:
    la cuota de Adzuna se regenera y perder la cuenta cuesta un 429. Aquí
    perder la cuenta cuesta dinero de otra persona, así que se falla
    ruidosamente y que lo decida un humano.
    """
    ruta = tmp_path / "uso_llm.json"
    ruta.write_text("{esto no es json", encoding="utf-8")

    with pytest.raises(LibroCorruptoError) as exc:
        LibroDeGastos(ruta)

    assert "no se va a asumir" in str(exc.value).lower()


# ---------------------------------------------------------------------------
#  El estado: reserva, disponible, avisos
# ---------------------------------------------------------------------------

def test_lo_disponible_descuenta_lo_gastado(tmp_path: Path):
    g = guardia(tmp_path, techo=8.0)
    g.apuntar(3.0)
    assert g.estado().disponible == pytest.approx(5.0)


def test_la_racion_de_las_automaticas_no_alcanza_a_lo_que_lanza_ella(tmp_path: Path):
    """Es lo que hace que el botón «buscar ahora» sea creíble.

    Sin una ración aparte, la tarea programada de las 7 de la mañana puede
    gastarse el mes entero y dejarla sin poder pedir una búsqueda a mediodía.

    Antes esto se escribía al revés —`reserva_manual_usd: 2` sobre un techo de
    8, o sea 6 implícitos para las automáticas— y el número que se leía en el
    YAML no era el que mandaba. Ahora la ración se escribe entera.
    """
    libro = LibroDeGastos(tmp_path / "uso_llm.json")
    config = config_llm(techo=8.0, racion=6.0)

    automatica = GuardiaPresupuesto(libro, config, es_manual=False)
    suya = GuardiaPresupuesto(libro, config, es_manual=True)

    assert automatica.estado().disponible == pytest.approx(6.0)
    assert suya.estado().disponible == pytest.approx(8.0)

    # La automática se gasta TODO lo que puede.
    libro.registrar(6.0)
    assert automatica.estado().agotado
    assert not suya.estado().agotado
    assert suya.estado().disponible == pytest.approx(2.0)


def test_avisa_antes_de_agotarse_no_despues(tmp_path: Path):
    g = guardia(tmp_path, techo=10.0, avisar_pct=75)
    g.apuntar(7.0)
    assert not g.estado().conviene_avisar
    g.apuntar(0.6)
    assert g.estado().conviene_avisar
    assert not g.estado().agotado


def test_el_modo_avisar_no_para_a_ELLA_pero_si_a_las_automaticas(tmp_path: Path):
    """La asimetría que se introdujo el 3 septiembre 2026.

    `avisar` significa que el techo MENSUAL deja de frenar sus acciones: es su
    API key y su recarga automática, y apagarle la herramienta el día 17 no la
    protege de nada. A las corridas automáticas las sigue parando su ración,
    porque ahí no hay nadie delante para decidir.

    Si esto se rompiera, la asimetría desaparecería en la dirección peligrosa:
    una tarea programada gastando sin freno en su máquina.
    """
    libro = LibroDeGastos(tmp_path / "uso_llm.json")
    config = config_llm(techo=1.0, modo="avisar", racion=1.0)
    libro.registrar(5.0)

    suya = GuardiaPresupuesto(libro, config, es_manual=True)
    automatica = GuardiaPresupuesto(libro, config, es_manual=False)

    assert suya.estado().agotado, "el número sigue diciendo la verdad"
    assert suya.permite(1.0), "pero a ella no la frena"
    assert not automatica.permite(1.0), "a la automática sí"


# ---------------------------------------------------------------------------
#  El motor: pararse de verdad, y no perder nada al hacerlo
# ---------------------------------------------------------------------------

def _sembrar(almacen: Storage, cuantas: int) -> None:
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


def test_se_para_al_agotarse_y_lo_demas_sigue_pendiente(almacen: Storage, tmp_path: Path):
    """LA PRUEBA QUE JUSTIFICA TODO EL MÓDULO.

    Se siembran 10 ofertas y un techo que solo da para unas pocas. Lo que se
    comprueba no es que pare —eso es lo fácil— sino que lo que no clasificó
    SIGA SIENDO PENDIENTE: sin fila en `clasificaciones`, de forma que la
    próxima corrida con saldo lo recoja sola.

    Si esto se rompiera, quedarse sin presupuesto sería indistinguible de
    haber revisado el mercado entero, y esas ofertas no se volverían a mirar
    NUNCA.
    """
    _sembrar(almacen, 10)

    # Cada llamada falsa devuelve 900 de entrada y 400 de salida: $0.0145.
    # Con techo de $0.05 caben 3 y sobra para menos de una cuarta.
    motor, cliente, g = motor_con_techo(almacen, tmp_path, techo=0.05)

    pendientes_antes = motor.pendientes()
    assert len(pendientes_antes) == 10

    resumen = motor.clasificar(pendientes_antes)

    assert resumen.se_detuvo_por_presupuesto
    assert cliente.llamadas < 10, "no puede haberlas clasificado todas"
    assert resumen.clasificadas == cliente.llamadas

    # Lo importante: siguen pendientes, no marcadas ni perdidas.
    quedan = motor.pendientes()
    assert len(quedan) == 10 - resumen.clasificadas
    assert resumen.detenidas_por_presupuesto == len(quedan)


def test_la_siguiente_corrida_con_saldo_recoge_lo_que_quedo(almacen: Storage, tmp_path: Path):
    """La otra mitad de lo anterior: pararse no puede ser un callejón sin salida."""
    _sembrar(almacen, 6)

    motor, _, _ = motor_con_techo(almacen, tmp_path, techo=0.03)
    primera = motor.clasificar(motor.pendientes())
    assert primera.se_detuvo_por_presupuesto

    # Mes siguiente / techo subido: se usa un libro limpio para simular
    # el cambio de mes sin tener que viajar en el tiempo.
    holgado = GuardiaPresupuesto(
        LibroDeGastos(tmp_path / "otro.json"), config_llm(techo=5.0)
    )
    motor2 = Motor(
        almacen, fichas_de_prueba(),
        ClienteFalso(respuesta_ok(), libro=holgado.libro),
        presupuesto=holgado,
    )
    segunda = motor2.clasificar(motor2.pendientes())

    assert not segunda.se_detuvo_por_presupuesto
    assert motor2.pendientes() == []
    assert primera.clasificadas + segunda.clasificadas == 6


def test_sin_guardia_no_hay_techo(almacen: Storage):
    """`presupuesto=None` sigue funcionando: es lo que usan las otras pruebas.

    Se fija como comportamiento para que nadie lo convierta en obligatorio sin
    darse cuenta de que rompería las 48 pruebas de la capa 2.
    """
    _sembrar(almacen, 4)
    cliente = ClienteFalso(respuesta_ok())
    motor = Motor(almacen, fichas_de_prueba(), cliente)
    resumen = motor.clasificar(motor.pendientes())

    assert resumen.clasificadas == 4
    assert not resumen.se_detuvo_por_presupuesto
    assert resumen.estado_presupuesto is None


def test_el_gasto_apuntado_coincide_con_el_de_la_corrida(almacen: Storage, tmp_path: Path):
    """El libro y el resumen tienen que contar lo mismo.

    Son dos caminos distintos hasta el mismo número —uno suma tokens de la
    base, el otro apunta dólares en un archivo— y si divergen, el techo estaría
    guardando un presupuesto que no es el que se está gastando.
    """
    _sembrar(almacen, 3)
    motor, _, g = motor_con_techo(almacen, tmp_path, techo=5.0)

    resumen = motor.clasificar(motor.pendientes())

    assert not resumen.se_detuvo_por_presupuesto
    assert g.libro.gastado_este_mes() == pytest.approx(resumen.coste_usd)
    assert g.libro.llamadas_este_mes() == resumen.llamadas


def test_las_heredadas_no_gastan_presupuesto(almacen: Storage, tmp_path: Path):
    """Cinco anuncios de la misma vacante son UNA llamada, y un apunte."""
    run = almacen.start_run("prueba", ["adzuna"], ["mexico"])
    for i, ciudad in enumerate(["Monterrey", "Guadalajara", "Puebla"]):
        oferta_en_base(
            almacen,
            run,
            external_id=str(i),
            location_raw=ciudad,
            location_city=ciudad,
            url=f"https://example.test/{i}",
        )

    motor, cliente, g = motor_con_techo(almacen, tmp_path, techo=5.0)
    resumen = motor.clasificar(motor.pendientes())

    assert resumen.total == 3
    assert resumen.heredadas == 2
    assert cliente.llamadas == 1
    # UN apunte, no tres. Lo garantiza que quien apunta sea el cliente y que a
    # los hermanos no se les llame: heredar es gratis y tiene que verse en el
    # libro, no solo en el resumen.
    assert g.libro.llamadas_este_mes() == 1


# ---------------------------------------------------------------------------
#  La configuración: que el defecto sea el seguro
# ---------------------------------------------------------------------------

def test_sin_bloque_llm_hay_techo_de_todos_modos():
    """Un proyecto que gasta dinero ajeno no puede tener «ilimitado» por defecto.

    Si alguien borra el bloque `llm:` del YAML, o lo escribe mal el día que
    añada un perfil nuevo, el fallo por defecto tiene que ser el seguro.
    """
    config = _parse_llm_budget(None, "prueba")
    assert config.presupuesto_mensual_usd > 0
    assert config.detiene_al_agotarse


def test_al_agotarse_invalido_falla_al_arrancar():
    with pytest.raises(ConfigError) as exc:
        _parse_llm_budget({"al_agotarse": "seguir_gastando"}, "prueba")
    assert "al_agotarse" in str(exc.value)


def test_una_racion_mayor_que_el_techo_falla_al_arrancar():
    """Las automáticas no pueden correr con más margen que el número que se le
    enseña a ella en la barra de gasto. Sería al revés de lo que se pretende.
    """
    with pytest.raises(ConfigError):
        _parse_llm_budget(
            {"presupuesto_mensual_usd": 2.0, "racion_automatica_mensual_usd": 5.0},
            "prueba",
        )


def test_la_clave_vieja_de_la_reserva_no_se_ignora_en_silencio():
    """`reserva_manual_usd` cambió de nombre Y de sentido el 3 sept 2026.

    Dejarla pasar sin decir nada sería lo peor de las tres opciones: las
    automáticas se quedarían con la ración por defecto —el techo entero— y
    nadie se enteraría, porque no hay ningún síntoma visible en que una
    corrida automática gaste de más. Y traducirla sola sería adivinar: un 2 no
    dice si la ración es 2 o es 10.

    El mensaje trae la cuenta hecha, que es lo que lo hace útil.
    """
    with pytest.raises(ConfigError) as exc:
        _parse_llm_budget(
            {"presupuesto_mensual_usd": 12.0, "reserva_manual_usd": 2.0}, "prueba"
        )
    assert "racion_automatica_mensual_usd: 10.00" in str(exc.value)


def test_un_tope_por_accion_mayor_que_el_mes_falla_al_arrancar():
    """Un tope que no puede dispararse nunca no es un tope, es un adorno."""
    with pytest.raises(ConfigError) as exc:
        _parse_llm_budget(
            {"presupuesto_mensual_usd": 5.0, "tope_por_accion_usd": 9.0}, "prueba"
        )
    assert "tope_por_accion_usd" in str(exc.value)


def test_sin_tope_escrito_hay_tope_de_todos_modos():
    """Mismo criterio que el techo del mes: el defecto tiene que ser el seguro.

    Si alguien escribe el bloque `llm:` a mano y se olvida del tope, la app no
    puede quedarse sin freno — que es lo que pasaría si el defecto fuera 0.
    """
    assert _parse_llm_budget({"presupuesto_mensual_usd": 12.0}, "prueba").tope_por_accion_usd > 0


def test_el_yaml_real_declara_un_freno():
    """Contra la configuración que se envía, como las pruebas de las listas.

    Lo que se comprueba cambió el 3 septiembre 2026 y el motivo importa. Antes
    se exigía `al_agotarse: parar`, porque el techo mensual era el único freno
    y dejarlo en `avisar` habría sido gastar sin límite en su máquina. Ahora el
    freno es el tope POR ACCIÓN, así que lo que no puede faltar es ese —y la
    ración de las automáticas, que son las que corren sin nadie delante—.
    """
    from recolector.config import load_config

    llm = load_config().llm
    assert llm.presupuesto_mensual_usd > 0
    assert llm.tope_por_accion_usd > 0, (
        "El YAML que se envía no declara `tope_por_accion_usd`: ninguna acción "
        "tendría freno."
    )
    assert 0 < llm.racion_automatica_mensual_usd <= llm.presupuesto_mensual_usd
    assert llm.frena_el_mes(es_manual=False), (
        "Las corridas automáticas tienen que tener freno mensual pase lo que "
        "pase: son las que corren sin nadie delante."
    )


# ---------------------------------------------------------------------------
#  EL TOPE POR ACCIÓN — el freno que se le promete (3 septiembre 2026)
# ---------------------------------------------------------------------------

class TestElTopePorAccion:
    """Antes del 3 sept 2026 el único freno era mensual, y no promete lo mismo.

    «No pasarás de $12 este mes» y «ningún botón se te llevará más de $0,60»
    son promesas distintas, y la que ella puede comprobar mirando la pantalla
    es la segunda: es la que se enseña al lado del botón antes de pulsarlo.
    """

    def test_una_accion_por_debajo_del_tope_arranca(self, tmp_path: Path):
        g = guardia(tmp_path, techo=12.0, tope_accion=0.60)
        assert g.abrir_accion(0.35) is None

    def test_una_accion_por_encima_se_niega_con_los_dos_numeros(self, tmp_path: Path):
        """El motivo lleva dentro el importe y el tope.

        Un «no cabe» sin números obliga a ir a buscar el YAML, y quien lo lee
        es ella.
        """
        g = guardia(tmp_path, techo=12.0, tope_accion=0.60)
        motivo = g.abrir_accion(1.06)

        assert motivo is not None
        assert "1.06" in motivo and "0.60" in motivo
        assert "no se ha perdido ninguna oferta" in motivo

    def test_el_tope_no_depende_de_cuanto_quede_del_mes(self, tmp_path: Path):
        """Es un freno POR ACCIÓN, no una segunda forma del techo mensual.

        Con el mes intacto, una acción cara se sigue negando. Si dependiera del
        saldo, el día 1 dejaría pasar cualquier cosa — que es justo el caso que
        este proyecto vivió: el triaje de 273 vacantes a ~$1,06 pasó porque
        cabía en el mes.
        """
        g = guardia(tmp_path, techo=12.0, tope_accion=0.60)
        assert g.estado().disponible == pytest.approx(12.0)
        assert g.abrir_accion(1.06) is not None

    def test_un_tope_de_cero_significa_sin_tope(self, tmp_path: Path):
        """Es lo que usan las pruebas que van del techo mensual."""
        g = guardia(tmp_path, techo=12.0, tope_accion=0.0)
        assert g.abrir_accion(99.0) is None

    def test_la_accion_tampoco_lo_rebasa_MIENTRAS_corre(self, almacen, tmp_path: Path):
        """LA MITAD QUE NO ES OBVIA, y sin ella el tope es una intención.

        `abrir_accion` mira una ESTIMACIÓN. Si los anuncios salen más largos de
        lo previsto, la acción arrancaría por debajo del tope y acabaría por
        encima, y el número que ella confirmó no sería el que se cobró. Así que
        `permite()` compara también contra lo que la acción lleva gastado DE
        VERDAD, llamada a llamada.

        Cada llamada falsa cuesta $0,0145. Con el tope en $0,05 caben tres y la
        cuarta no.
        """
        _sembrar(almacen, 10)
        motor, cliente, g = motor_con_techo(
            almacen, tmp_path, techo=100.0, tope_accion=0.05
        )

        resumen = motor.clasificar(motor.pendientes())

        assert resumen.se_detuvo_por_presupuesto
        assert cliente.llamadas == 3
        assert g.gastado_en_esta_accion() <= 0.05
        # Y lo que no se hizo sigue pendiente, que es la regla de siempre.
        assert len(motor.pendientes()) == 10 - resumen.clasificadas

    def test_la_siguiente_se_mide_con_margen(self, tmp_path: Path):
        """EL RESQUICIO POR EL QUE SE ESCAPÓ $0,0485 (medido el 9 sept 2026).

        La guarda comparaba lo gastado DE VERDAD contra lo ESTIMADO de la
        siguiente, que es lo correcto — salvo que la estimación se quede
        corta. Ese día se forzó al modelo a escribir seis veces lo medido:
        cada llamada cabía por estimación, costaba el doble al volver, y la
        acción cerró en $0,6485 con el tope en $0,60.

        Ahora la siguiente se mide con un 40% de margen, que es el mismo que
        usa `cabe_la_respuesta` y por el mismo motivo.
        """
        g = guardia(tmp_path, techo=12.0, tope_accion=0.60)
        g.abrir_accion(0.50)
        g.apuntar(0.50)

        # Quedan $0,10 de los $0,60. Una llamada estimada en $0,08 cabría
        # justa... y basta con que salga un 25% más cara para pasarse.
        assert not g.permite(0.08)
        assert g.permite(0.05)

    def test_el_margen_no_frena_lo_que_cabe_de_sobra(self, tmp_path: Path):
        """Negarse cuesta cero, pero negarse SIEMPRE convierte el tope en otro
        número más bajo y sin decirlo."""
        g = guardia(tmp_path, techo=12.0, tope_accion=0.60)
        g.abrir_accion(0.30)

        assert g.permite(0.30), "una acción entera que cabe tiene que arrancar"

    def test_el_motivo_y_la_decision_usan_la_misma_cuenta(self, tmp_path: Path):
        """Si divergen, el mensaje cuenta una historia distinta del frenazo."""
        g = guardia(tmp_path, techo=12.0, tope_accion=0.60)
        g.abrir_accion(0.50)
        g.apuntar(0.50)

        assert not g.permite(0.08)
        assert "tope de $0.60" in g.motivo_de_parada(0.08), (
            "lo que paró fue la acción, no el mes"
        )

    def test_el_motivo_distingue_topar_de_quedarse_sin_mes(self, almacen, tmp_path: Path):
        """«Esta acción llegó a su tope» y «se acabó el mes» no son lo mismo.

        Lo primero se arregla volviendo a pulsar; lo segundo no. Decir las dos
        cosas igual es la regla nº 1 de la casa rota en la pantalla.
        """
        _sembrar(almacen, 10)

        # Con el mes de sobra y el tope apretado, el frenazo es del tope.
        motor, _, g = motor_con_techo(
            almacen, tmp_path, techo=100.0, tope_accion=0.05
        )
        resumen = motor.clasificar(motor.pendientes())
        assert "tope" in resumen.avisos[0]
        assert "mes" not in resumen.avisos[0]

        # Con el mes agotado y sin tope, es del mes.
        motor2, _, _ = motor_con_techo(almacen, tmp_path, techo=0.02)
        resumen2 = motor2.clasificar(motor2.pendientes())
        assert "mes" in resumen2.avisos[0]
        assert "tope" not in resumen2.avisos[0]

    def test_el_gasto_de_otra_accion_anterior_no_cuenta(self, tmp_path: Path):
        """La cuenta empieza cuando se construye el guardia, no en el mes.

        Si contara el mes entero, la segunda acción del día se encontraría el
        tope ya gastado y la app se apagaría sola sin que nada estuviera mal.
        """
        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        libro.registrar(5.0)

        g = GuardiaPresupuesto(libro, config_llm(techo=12.0, tope_accion=0.60))
        assert g.gastado_en_esta_accion() == pytest.approx(0.0)
        assert g.permite(0.3)


class TestElTopeCabeConLasPerillasDeHoy:
    """Contra el YAML REAL y las longitudes REALES de la base.

    ES LA PRUEBA QUE IMPIDE QUE EL NÚMERO SE PUDRA. El tope tiene que quedar
    por encima de lo que las otras perillas ya permiten gastar en una acción, y
    lo que decide ese techo son dos cosas que no están en el mismo archivo:
    `ofertas_por_corrida` y `max_vacantes_por_triaje` (en el YAML) y cuánto
    miden los anuncios (en la base). Subir cualquiera de las dos perillas deja
    el tope por debajo, y el síntoma sería que su botón empieza a negarse sin
    que haya nada roto que mirar.

    Mismo patrón que `test_cuota_adzuna.py`: la aritmética se fija contra la
    configuración que se envía, no contra números inventados.
    """

    def _techo_de_clasificar(self):
        from recolector.config import load_config
        from recolector.storage import Storage as _Storage
        from clasificador.fichas import cargar_fichas
        from clasificador.motor import Motor as _Motor, descripcion_de
        from clasificador.plan import ClienteInerte
        from clasificador.triaje import coste_estimado as coste_triaje

        config = load_config()
        if not config.database_path.exists():
            pytest.skip("sin base de datos: esta prueba mide contra la real")

        fichas = cargar_fichas()
        almacen = _Storage(config.database_path)
        try:
            filas = list(almacen.ofertas_pendientes(fichas_huella=fichas.huella))
            if not filas:
                pytest.skip("no hay pendientes contra las que medir")
            # El caso peor que las perillas de hoy permiten: la llamada del
            # triaje a tope, más las lecturas más largas que hay en la base.
            _, _, coste_triaje_usd = coste_triaje(
                config.llm.triaje.max_vacantes_por_triaje, config.llm.triaje.esfuerzo
            )
            peores = sorted(filas, key=lambda f: -len(descripcion_de(f)))[
                : config.llm.triaje.ofertas_por_corrida
            ]
            motor = _Motor(almacen, fichas, cliente=ClienteInerte())
            return config, coste_triaje_usd + motor.coste_estimado(peores)[2]
        finally:
            almacen.close()

    def test_la_accion_mas_cara_que_permiten_las_perillas_cabe_en_el_tope(self):
        config, techo = self._techo_de_clasificar()
        assert techo <= config.llm.tope_por_accion_usd, (
            f"La acción `clasificar` puede costar ${techo:.4f} con las perillas "
            f"de hoy (triaje de {config.llm.triaje.max_vacantes_por_triaje} + "
            f"{config.llm.triaje.ofertas_por_corrida} lecturas) y el tope son "
            f"${config.llm.tope_por_accion_usd:.2f}. Su botón se negaría sin que "
            "nada esté roto. Sube `llm.tope_por_accion_usd` o baja las perillas."
        )

    def test_y_el_tope_no_es_tan_alto_que_no_frene_nada(self):
        """La otra mitad: un tope que nunca se dispara no es un freno.

        Tiene que quedar por DEBAJO del caso real que este proyecto vivió —el
        triaje de 273 vacantes— o no habría servido de nada tenerlo puesto.
        """
        from recolector.config import load_config
        from clasificador.triaje import coste_estimado as coste_triaje

        config = load_config()
        _, _, el_caso_real = coste_triaje(273, config.llm.triaje.esfuerzo)
        _, techo = self._techo_de_clasificar()
        assert techo < config.llm.tope_por_accion_usd < el_caso_real * 2, (
            "El tope tiene que estar entre lo que una corrida normal cuesta y "
            "lo que costó el atraso de 273 vacantes que lo justificó."
        )

    def test_el_ranking_al_maximo_tambien_cabe(self):
        """El ranking es la otra acción con techo propio, y hoy es la más cara.

        `cabida` limita el lote a 60 ofertas a `medium`. Ese es su peor caso, y
        también tiene que caber en el tope.
        """
        from recolector.config import load_config
        from clasificador.asistente import Asistente, cabida
        from clasificador.fichas import cargar_preferencias
        from clasificador.plan import ClienteInerte
        from recolector.storage import Storage as _Storage

        config = load_config()
        if not config.database_path.exists():
            pytest.skip("sin base de datos")
        almacen = _Storage(config.database_path)
        try:
            base = list(almacen.ofertas_para_ranking(limite=None))
        finally:
            almacen.close()
        if not base:
            pytest.skip("no hay nada que ordenar")

        caben = cabida(10_000, config.llm.esfuerzo_ranking).caben
        lote = (base * (caben // len(base) + 1))[:caben]
        asistente = Asistente(
            cliente=ClienteInerte(),
            preferencias=cargar_preferencias(),
            esfuerzo=config.llm.esfuerzo_ranking,
        )
        coste = asistente.coste_estimado(lote)[2]
        assert coste <= config.llm.tope_por_accion_usd, (
            f"El ranking al máximo que admite `cabida` ({caben} ofertas) cuesta "
            f"${coste:.4f} y el tope son ${config.llm.tope_por_accion_usd:.2f}."
        )


# ---------------------------------------------------------------------------
#  EL AJUSTE A MANO — corregir el libro sin inventar una corrida
# ---------------------------------------------------------------------------

class TestLosAjustes:
    """Las dos salidas obvias eran malas las dos, así que se marca.

    Dejar los $0,1587 fuera deja el libro mintiendo; apuntarlos como gasto
    normal lo deja correcto pero inventa llamadas que el programa no hizo. Se
    apunta Y se deja escrito de dónde salió, que es lo que este proyecto ya
    hace en `heredada_de` y en `description_full_via`.
    """

    def test_el_ajuste_suma_al_gasto_del_mes(self, tmp_path: Path):
        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        libro.registrar(1.0)
        libro.ajustar(0.1587, "un A/B que no pasó por el guardia")

        assert libro.gastado_este_mes() == pytest.approx(1.1587)

    def test_el_ajuste_no_inventa_llamadas(self, tmp_path: Path):
        """El importe es real; las llamadas del programa, no.

        Inflar el contador de llamadas para cuadrar el de dólares sería cambiar
        una mentira por otra, y `llamadas_mes` es lo que se enseña al lado.
        """
        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        libro.registrar(1.0)
        libro.ajustar(0.1587, "un A/B que no pasó por el guardia")

        assert libro.llamadas_este_mes() == 1

    def test_el_ajuste_deja_escrito_el_motivo_y_sobrevive_al_disco(self, tmp_path: Path):
        """Un número corregido sin explicación es peor que el número malo.

        Dentro de un mes, «¿por qué el libro no cuadra con las clasificaciones
        de la base?» tiene que tener respuesta en el propio archivo.
        """
        ruta = tmp_path / "uso_llm.json"
        LibroDeGastos(ruta).ajustar(0.1587, "el A/B de tres reclasificaciones")

        ajustes = LibroDeGastos(ruta).ajustes()
        assert len(ajustes) == 1
        assert ajustes[0]["usd"] == pytest.approx(0.1587)
        assert "A/B" in ajustes[0]["motivo"]
        assert ajustes[0]["fecha"]

    def test_un_libro_sin_ajustes_no_los_inventa(self, tmp_path: Path):
        assert LibroDeGastos(tmp_path / "uso_llm.json").ajustes() == []


# ---------------------------------------------------------------------------
#  QUIÉN APUNTA: el cliente, y aunque la respuesta venga mal
# ---------------------------------------------------------------------------

class TestElClienteApunta:
    """El agujero de los $0,1587, cerrado y con prueba.

    Un script de medición construyó `ClienteAnthropic` a mano, llamó a la API
    sin pasar por `GuardiaPresupuesto` y su gasto no entró en el libro. No era
    un fallo del libro: era que apuntar dependía de que quien usara el cliente
    se acordara, y eran tres capas acordándose.

    Estas pruebas ejercitan el cliente DE VERDAD —con el SDK sustituido, sin
    red y sin API key válida— porque lo que hay que fijar es que apunte él, no
    que apunte alguien.
    """

    # Una clasificación válida, en el formato en que la API la devuelve: un
    # bloque de texto con el JSON dentro. Antes esto era un `parsed_output` ya
    # montado, y eso escondía justo el paso que fallaba — la validación.
    CLASIFICACION_BUENA = (
        '{"relevante": true, "motivo_descarte": null, "confianza": 0.9, '
        '"razon": "Encaja.", "cv_id": "ejecutivo_es", "idioma_anuncio": "es", '
        '"empresa_normalizada": "Acme", "puesto_canonico": "Director", '
        '"es_reclutadora": false, "seniority_detectado": "direccion", '
        '"modalidad": "remoto"}'
    )

    def _cliente_con_respuesta(
        self, libro, *, stop_reason="end_turn", texto=None
    ):
        from types import SimpleNamespace
        from clasificador.cliente_llm import ClienteAnthropic

        cliente = ClienteAnthropic(api_key="clave-de-prueba", libro=libro)
        if texto is None:
            texto = self.CLASIFICACION_BUENA
        respuesta = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=500,
                cache_read_input_tokens=5000,
                cache_creation_input_tokens=0,
                output_tokens=400,
            ),
            stop_reason=stop_reason,
            stop_details=SimpleNamespace(category="ninguna"),
            content=[SimpleNamespace(type="text", text=texto)],
        )
        cliente._cliente = SimpleNamespace(
            messages=SimpleNamespace(create=lambda **kwargs: respuesta)
        )
        return cliente

    def test_el_cliente_apunta_sin_que_nadie_se_lo_pida(self, tmp_path: Path):
        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        self._cliente_con_respuesta(libro).clasificar("sistema", "oferta")

        assert libro.gastado_este_mes() > 0
        assert libro.llamadas_este_mes() == 1

    def test_apunta_aunque_la_respuesta_no_sirva(self, tmp_path: Path):
        """La llamada se pagó igual. Es la regla del libro, ahora en un sitio.

        Un `max_tokens` cuesta exactamente lo mismo que una respuesta buena, y
        es justo el día en que hace falta que el libro no mienta.
        """
        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        respuesta = self._cliente_con_respuesta(
            libro, stop_reason="max_tokens"
        ).clasificar("sistema", "oferta")

        assert not respuesta.ok
        assert libro.gastado_este_mes() > 0

    def test_apunta_aunque_la_respuesta_no_VALIDE(self, tmp_path: Path):
        """EL AGUJERO GEMELO, MEDIDO EL 9 SEPTIEMBRE 2026 CONTRA LA APP.

        `max_tokens` ya estaba cubierto por la prueba de arriba. Lo que no lo
        estaba es una API que contesta 200 con texto que no encaja con el
        esquema: la validación vivía DENTRO de `messages.parse`, la excepción
        saltaba por encima del apunte, y el libro se quedaba en $0,0000 con la
        llamada pagada. Medido: 15 llamadas hechas, 14 apuntadas.

        Cuesta exactamente lo mismo que una respuesta buena.
        """
        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        respuesta = self._cliente_con_respuesta(
            libro, texto="Claro, aquí tienes la clasificación: {relevante: sí"
        ).clasificar("sistema", "oferta")

        assert not respuesta.ok
        assert libro.gastado_este_mes() > 0, (
            "la llamada se pagó: si no está en el libro, el techo del mes miente"
        )
        assert libro.llamadas_este_mes() == 1

    def test_lo_que_le_dice_cuando_no_valida(self, tmp_path: Path):
        """Ella veía `ValidationError: 1 validation error for Clasificacion`.

        Un nombre de clase de Python en su pantalla no le dice si tiene que
        hacer algo, ni si eso le ha costado dinero. Las dos cosas se dicen.
        """
        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        respuesta = self._cliente_con_respuesta(
            libro, texto="{}"
        ).clasificar("sistema", "oferta")

        assert "ValidationError" not in respuesta.error
        assert "sigue pendiente" in respuesta.error
        assert "se ha contado" in respuesta.error, (
            "tiene que saber que eso ha entrado en la cuenta del mes"
        )

    def test_una_respuesta_sin_texto_tampoco_se_regala(self, tmp_path: Path):
        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        respuesta = self._cliente_con_respuesta(libro, texto="").clasificar(
            "sistema", "oferta"
        )

        assert not respuesta.ok
        assert libro.gastado_este_mes() > 0

    def test_el_esquema_es_el_que_manda_el_sdk(self):
        """El esquema viaja por `output_config.format`, como hacía `parse`.

        Se comprueba contra la transformación del propio SDK: si algún día ese
        módulo interno desaparece, esto falla AQUÍ —en la máquina del
        desarrollador, con las pruebas— y no en su casa con un 400.
        """
        from anthropic.lib._parse._transform import transform_schema
        from clasificador.cliente_llm import _esquema_json
        from clasificador.esquema import Clasificacion

        assert _esquema_json(Clasificacion) == transform_schema(Clasificacion)

    def test_el_esquema_llega_a_la_peticion(self, tmp_path: Path):
        """Que no se quede en una constante que nadie manda."""
        from types import SimpleNamespace
        from clasificador.cliente_llm import ClienteAnthropic

        visto = {}

        def espiar(**kwargs):
            visto.update(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=1, cache_read_input_tokens=0,
                    cache_creation_input_tokens=0, output_tokens=1,
                ),
                stop_reason="end_turn",
                stop_details=None,
                content=[SimpleNamespace(type="text", text=self.CLASIFICACION_BUENA)],
            )

        cliente = ClienteAnthropic(api_key="clave-de-prueba", libro=False)
        cliente._cliente = SimpleNamespace(messages=SimpleNamespace(create=espiar))
        cliente.clasificar("sistema", "oferta")

        formato = visto["output_config"]["format"]
        assert formato["type"] == "json_schema"
        assert "relevante" in formato["schema"]["properties"], (
            "sin esquema en la petición, la API deja de estar obligada a nada"
        )

    def test_apunta_una_sola_vez_por_llamada(self, tmp_path: Path):
        """La trampa del cambio: si alguna capa vuelve a apuntar, se cuenta doble.

        El gasto del mes se duplicaría y el techo se dispararía a mitad de mes
        sin que nada fallara. Los tres `apuntar` de `motor.py`, `triaje.py` y
        `cli.py` se quitaron en el mismo cambio; esto lo fija.
        """
        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        cliente = self._cliente_con_respuesta(libro)
        una = cliente.clasificar("sistema", "oferta")
        cliente.clasificar("sistema", "otra")

        assert libro.llamadas_este_mes() == 2
        assert libro.gastado_este_mes() == pytest.approx(una.coste_usd * 2)

    def test_no_apunta_lo_que_no_llego_a_llamarse(self, tmp_path: Path):
        """Un error de red no gasta, así que no puede aparecer en el libro."""
        from types import SimpleNamespace
        import anthropic
        from clasificador.cliente_llm import ClienteAnthropic

        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        cliente = ClienteAnthropic(api_key="clave-de-prueba", libro=libro)

        def reventar(**kwargs):
            raise anthropic.APIConnectionError(request=None)

        cliente._cliente = SimpleNamespace(
            messages=SimpleNamespace(create=reventar)
        )
        respuesta = cliente.clasificar("sistema", "oferta")

        assert not respuesta.ok
        assert libro.gastado_este_mes() == 0.0

    def test_el_cliente_y_el_guardia_comparten_UN_libro(self, tmp_path: Path):
        """Dos instancias del libro se pisan, y el síntoma es que el freno falla.

        `LibroDeGastos` guarda el archivo ENTERO desde su copia en memoria. Con
        dos abiertos, el del guardia no ve lo que apunta el del cliente, así que
        `permite()` compara siempre contra el saldo del arranque y no para
        nunca. Por eso el cliente recibe el libro del guardia.
        """
        g = guardia(tmp_path, techo=12.0, tope_accion=0.60)
        cliente = self._cliente_con_respuesta(g.libro)
        cliente.clasificar("sistema", "oferta")

        assert g.gastado_en_esta_accion() > 0, (
            "el guardia no ve lo que apuntó el cliente: hay dos libros"
        )


# ---------------------------------------------------------------------------
#  Un plantón de la API no se lee como «no hay red»
# ---------------------------------------------------------------------------

class TestCuandoLaApiNoContesta:
    """Lo que pasa cuando la llamada se queda colgada, que en su casa pasará.

    El SDK espera **10 minutos** por llamada y reintenta dos veces, así que sin
    tocar nada una sola llamada colgada puede tener la corrida media hora
    parada, con la barra de progreso quieta y sin decir por qué. El cliente
    pone su propio reloj, y cuando salta, lo dice con sus palabras: un plantón
    y una red caída se arreglan distinto, así que no pueden leerse igual.
    """

    def _cliente_que_falla(self, libro, excepcion):
        from types import SimpleNamespace
        from clasificador.cliente_llm import ClienteAnthropic

        def estallar(**kwargs):
            raise excepcion

        cliente = ClienteAnthropic(api_key="clave-de-prueba", libro=libro)
        cliente._cliente = SimpleNamespace(
            messages=SimpleNamespace(create=estallar)
        )
        return cliente

    def test_el_cliente_no_espera_diez_minutos(self, tmp_path: Path):
        """El reloj es del proyecto, no el de fábrica del SDK."""
        from clasificador.cliente_llm import ClienteAnthropic

        cliente = ClienteAnthropic(api_key="clave-de-prueba", libro=False)
        assert cliente._cliente.timeout == 240.0

    def test_un_planton_se_cuenta_como_planton(self, tmp_path: Path):
        import anthropic
        import httpx2

        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        fallo = anthropic.APITimeoutError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        respuesta = self._cliente_que_falla(libro, fallo).clasificar("sistema", "oferta")

        assert not respuesta.ok
        assert "tardó demasiado" in respuesta.error
        # Y NO se apunta gasto: una llamada que no llegó a contestar no se
        # cobró, y el libro solo cuenta lo que se pagó.
        assert libro.gastado_este_mes() == 0

    def test_y_sigue_distinguiendose_de_quedarse_sin_red(self, tmp_path: Path):
        import anthropic
        import httpx2

        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        fallo = anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        respuesta = self._cliente_que_falla(libro, fallo).clasificar("sistema", "oferta")

        assert "¿Hay red?" in respuesta.error


# ---------------------------------------------------------------------------
#  La entrega que no se pudo preparar
# ---------------------------------------------------------------------------

class TestRepararElLibroDeLaEntrega:
    """10 septiembre 2026: el portátil se entregó sin correr
    `preparar_entrega.py`, así que el libro viajó con $4,6244 y 149 llamadas
    del desarrollador dentro.

    No rompe nada —el mes es una cuenta, no un límite— pero deja el único
    número que ella mira para saber lo que le cuesta la herramienta diciendo
    una cifra que no es suya. Y ya no había forma de entrar en esa máquina:
    `data/` no viaja en git, así que la reparación tiene que ir en el código.
    """

    LIBRO_DEL_DESARROLLADOR = {
        "by_day": {
            "2026-09-02": {"usd": 0.73167625, "llamadas": 22},
            "2026-09-03": {"usd": 3.3253595, "llamadas": 115},
            "2026-09-04": {"usd": 0.41563125, "llamadas": 8},
            "2026-09-08": {"usd": 0.15169375, "llamadas": 4},
        },
        "total_usd": 4.62436075,
        "total_llamadas": 149,
        "last_used": "2026-09-08T08:10:17.449439+00:00",
        "ajustes": [{"fecha": "2026-09-03", "usd": 0.1587, "motivo": "A/B…"}],
    }

    def _libro(self, tmp_path: Path, datos: dict) -> Path:
        ruta = tmp_path / "uso_llm.json"
        ruta.write_text(json.dumps(datos), encoding="utf-8")
        return ruta

    def test_quita_el_gasto_que_no_es_suyo(self, tmp_path: Path):
        ruta = self._libro(tmp_path, self.LIBRO_DEL_DESARROLLADOR)

        hecho = reparar_libro_de_la_entrega(ruta, entrega="2026-09-10")

        assert hecho and "4.6244" in hecho
        libro = LibroDeGastos(ruta)
        assert libro.gastado_total() == 0.0
        assert libro.llamadas_este_mes() == 0

    def test_NO_toca_lo_que_ella_ya_gasto(self, tmp_path: Path):
        """LA PRUEBA QUE DECIDE EL DISEÑO.

        Para cuando esto llegue a su máquina puede haber usado ya la app. Ese
        gasto es suyo y cuenta. Borrar el libro entero —que era lo fácil— se
        lo llevaría por delante.
        """
        # Fechas del mes en curso: `llamadas_este_mes` mira el reloj, y con
        # fechas fijas la prueba caducaba sola al cambiar de mes.
        from datetime import date
        hoy = date.today()
        d1, d2 = hoy.replace(day=1).isoformat(), hoy.replace(day=2).isoformat()
        datos = json.loads(json.dumps(self.LIBRO_DEL_DESARROLLADOR))
        datos["by_day"][d1] = {"usd": 0.4231, "llamadas": 9}
        datos["by_day"][d2] = {"usd": 0.1902, "llamadas": 4}
        datos["total_usd"] += 0.6133
        datos["total_llamadas"] += 13
        ruta = self._libro(tmp_path, datos)

        reparar_libro_de_la_entrega(ruta, entrega=d1)

        libro = LibroDeGastos(ruta)
        assert libro.gastado_total() == pytest.approx(0.6133)
        assert libro.llamadas_este_mes() == 13

    def test_guarda_el_original_antes_de_tocarlo(self, tmp_path: Path):
        ruta = self._libro(tmp_path, self.LIBRO_DEL_DESARROLLADOR)

        reparar_libro_de_la_entrega(ruta, entrega="2026-09-10")

        copias = list(tmp_path.glob("*antes-de-la-entrega*"))
        assert len(copias) == 1
        guardado = json.loads(copias[0].read_text(encoding="utf-8"))
        assert guardado["total_usd"] == pytest.approx(4.62436075), (
            "la copia tiene que traer el libro ENTERO, sin tocar"
        )

    def test_deja_escrito_de_donde_venia(self, tmp_path: Path):
        """Cuando hay que quitar algo, se guarda la procedencia. Regla de la casa."""
        ruta = self._libro(tmp_path, self.LIBRO_DEL_DESARROLLADOR)

        reparar_libro_de_la_entrega(ruta, entrega="2026-09-10")

        ajustes = LibroDeGastos(ruta).ajustes()
        assert len(ajustes) == 1, "el ajuste viejo se va con el dinero que explicaba"
        assert "4.6244" in ajustes[-1]["motivo"]
        assert "149 llamadas" in ajustes[-1]["motivo"]
        assert "antes-de-la-entrega" in ajustes[-1]["motivo"]

    def test_la_segunda_vez_no_hace_nada(self, tmp_path: Path):
        """Corre en CADA arranque: tiene que apagarse sola."""
        ruta = self._libro(tmp_path, self.LIBRO_DEL_DESARROLLADOR)

        assert reparar_libro_de_la_entrega(ruta, entrega="2026-09-10")
        antes = ruta.read_text(encoding="utf-8")

        assert reparar_libro_de_la_entrega(ruta, entrega="2026-09-10") is None
        assert ruta.read_text(encoding="utf-8") == antes
        assert len(list(tmp_path.glob("*antes-de-la-entrega*"))) == 1

    def test_un_libro_limpio_se_queda_como_esta(self, tmp_path: Path):
        """El de una instalación bien preparada. No se toca."""
        ruta = self._libro(tmp_path, {
            "by_day": {"2026-09-11": {"usd": 0.31, "llamadas": 7}},
            "total_usd": 0.31, "total_llamadas": 7, "last_used": None, "ajustes": [],
        })
        antes = ruta.read_text(encoding="utf-8")

        assert reparar_libro_de_la_entrega(ruta, entrega="2026-09-10") is None
        assert ruta.read_text(encoding="utf-8") == antes

    def test_sin_libro_no_pasa_nada(self, tmp_path: Path):
        assert reparar_libro_de_la_entrega(tmp_path / "no-existe.json") is None

    def test_un_libro_ilegible_no_impide_arrancar(self, tmp_path: Path):
        """No lanza NUNCA: esto corre antes de levantar el servidor."""
        ruta = tmp_path / "uso_llm.json"
        ruta.write_text("{esto no es json", encoding="utf-8")

        aviso = reparar_libro_de_la_entrega(ruta)

        assert aviso and "no se pudo reparar" in aviso

    def test_la_repara_el_SERVIDOR_y_no_la_cascara(self):
        """DÓNDE SE PONE DECIDE CUÁNDO LLEGA (medido el 11 septiembre 2026).

        Primero se puso en `abrir.py` y no reparó nada: la cáscara ya está
        cargada en memoria cuando el actualizador cambia los archivos, así que
        ese proceso sigue corriendo la versión ANTERIOR y lo que se ponga allí
        tarda DOS aperturas en hacer efecto. El servidor arranca después del
        `checkout`, o sea que es código nuevo desde la primera.

        Con la clienta a 9.000 km y sin forma de entrar en su máquina, esa
        diferencia es la que decide si la reparación llega hoy o mañana.
        """
        import inspect

        from bandeja import server

        fuente = inspect.getsource(server.main)
        assert "reparar_libro_de_la_entrega" in fuente, (
            "una reparación que nadie ejecuta no repara nada"
        )
        antes = fuente.split("reparar_libro_de_la_entrega", 1)[0]
        assert "uvicorn.run" not in antes, (
            "tiene que repararse con el libro quieto, antes de servir nada"
        )
