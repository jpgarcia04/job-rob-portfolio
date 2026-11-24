"""
El coste: que lo que se apunta sea lo que se gastó.

POR QUÉ EXISTE ESTE ARCHIVO
---------------------------
El 2 septiembre 2026, la primera vez que se clasificó de verdad, el coste
estaba mal contado en dos sitios y en direcciones opuestas, lo que hizo que
durante semanas ninguno de los dos se notara:

  - `--dry-run` se quedaba CORTO por 2,4x, porque contaba el prompt de
    sistema y el texto del anuncio pero no el esquema de salida, que el SDK
    serializa y manda en cada llamada sin que aparezca en ningún string de
    este repositorio.

  - El gasto apuntado se PASABA por 3,25x, porque la API devuelve la entrada
    partida en tres campos —nueva, leída de caché, escrita en caché— y se
    sumaban los tres para cobrarlos todos a $5/millón. Una lectura de caché
    cuesta la décima parte.

El segundo es el caro. Quien decide con ese número es el techo de
`presupuesto.py`: con el gasto inflado, el techo del mes se dispara habiendo
gastado un tercio y deja ofertas sin clasificar. Y la regla de la casa dice
que lo que no se hace por falta de saldo se queda pendiente — pero aquí ni
siquiera faltaba saldo.

Todas las cifras de aquí salen de una medición real contra `usage`, no de
números inventados. Ninguna prueba toca la red.
"""

from __future__ import annotations

import pytest

from clasificador.cliente_llm import (
    CARACTERES_POR_TOKEN,
    MULTIPLICADOR_CACHE_ESCRITURA,
    MULTIPLICADOR_CACHE_LECTURA,
    PRECIO_ENTRADA_USD_POR_MILLON,
    PRECIO_SALIDA_USD_POR_MILLON,
    TOKENS_PREFIJO_CACHEADO,
    RespuestaLLM,
    coste_de,
    coste_estimado_de_llamada,
    coste_medido,
)

# Forma de una llamada real, medida el 2 septiembre 2026 sobre claude-opus-5.
ENTRADA_NUEVA, CACHE_LEIDA, SALIDA = 307, 5110, 245
COSTE_REAL = 0.01022


class TestCosteMedido:
    def test_reproduce_la_llamada_real(self):
        assert coste_medido(ENTRADA_NUEVA, CACHE_LEIDA, 0, SALIDA) == pytest.approx(
            COSTE_REAL, abs=1e-5
        )

    def test_la_formula_plana_se_pasa_por_tres(self):
        """La regresión concreta: sumar los tres campos y cobrarlos igual.

        No es una diferencia de céntimos. Si alguien vuelve a hacer esto, el
        techo del mes se dispara con un tercio del gasto real.
        """
        plano = coste_de(ENTRADA_NUEVA + CACHE_LEIDA, SALIDA)
        medido = coste_medido(ENTRADA_NUEVA, CACHE_LEIDA, 0, SALIDA)
        assert plano > medido * 3

    def test_leer_de_cache_cuesta_una_decima_parte(self):
        solo_lectura = coste_medido(0, 1_000_000, 0, 0)
        solo_nueva = coste_medido(1_000_000, 0, 0, 0)
        assert solo_lectura == pytest.approx(solo_nueva * MULTIPLICADOR_CACHE_LECTURA)

    def test_escribir_la_cache_cuesta_un_25_por_ciento_mas(self):
        solo_escritura = coste_medido(0, 0, 1_000_000, 0)
        solo_nueva = coste_medido(1_000_000, 0, 0, 0)
        assert solo_escritura == pytest.approx(solo_nueva * MULTIPLICADOR_CACHE_ESCRITURA)

    def test_los_precios_base_no_se_han_movido(self):
        """Si alguien toca las tarifas, que se entere de que hay pruebas atadas."""
        assert PRECIO_ENTRADA_USD_POR_MILLON == 5.00
        assert PRECIO_SALIDA_USD_POR_MILLON == 25.00


class TestElCosteMedidoLlegaAlLibro:
    """`RespuestaLLM.coste_usd` es lo que `Motor` apunta en el libro de gastos."""

    def test_prefiere_el_medido_cuando_lo_hay(self):
        r = RespuestaLLM(
            clasificacion=None,
            tokens_entrada=ENTRADA_NUEVA + CACHE_LEIDA,
            tokens_salida=SALIDA,
            modelo="claude-opus-5",
            coste_medido_usd=COSTE_REAL,
        )
        assert r.coste_usd == COSTE_REAL

    def test_sin_desglose_cae_a_la_formula_plana(self):
        """Un cliente falso de los tests no tiene `usage`, y no debe reventar."""
        r = RespuestaLLM(
            clasificacion=None,
            tokens_entrada=900,
            tokens_salida=400,
            modelo="falso",
        )
        assert r.coste_usd == pytest.approx(coste_de(900, 400))


class TestEstimacion:
    def test_la_estimacion_se_parece_al_gasto_real(self):
        """Lo que `--dry-run` promete tiene que parecerse a lo que se cobra.

        Es la regla de la casa: la app nunca gasta un dólar sin haber
        enseñado antes el número. Un número que se equivoca por 2,4x no es
        haberlo enseñado.
        """
        caracteres = int(ENTRADA_NUEVA * CARACTERES_POR_TOKEN)
        _, _, estimado = coste_estimado_de_llamada(caracteres)
        # Se estima con 400 tokens de salida y la real trajo 245, así que se
        # pasa por arriba a propósito. Lo que no puede es doblarse.
        assert estimado == pytest.approx(COSTE_REAL, rel=0.65)
        assert estimado > COSTE_REAL, "estimar por debajo deja a la clienta a medias"

    def test_la_primera_llamada_cuesta_mas_que_las_siguientes(self):
        """Escribir la caché son x1,25; leerla, x0,10."""
        _, _, primera = coste_estimado_de_llamada(850, primera_de_la_corrida=True)
        _, _, siguiente = coste_estimado_de_llamada(850)
        assert primera > siguiente

    def test_cuenta_el_esquema_invisible(self):
        """El fallo original: estimar solo con lo que se ve en los strings.

        El esquema de salida no aparece en ningún texto de este repositorio
        pero viaja en cada llamada. Si `TOKENS_PREFIJO_CACHEADO` desaparece
        de la cuenta, la estimación vuelve a quedarse a menos de la mitad.
        """
        entrada, _, _ = coste_estimado_de_llamada(850)
        assert entrada >= TOKENS_PREFIJO_CACHEADO


# ---------------------------------------------------------------------------
#  EL TOTAL DE LA CORRIDA ES LO COBRADO, NO LA FÓRMULA PLANA (3 sept 2026)
# ---------------------------------------------------------------------------

from .test_clasificador import almacen  # noqa: F401  (fixture, se usa por nombre)


class TestElResumenNoRecalculaElCoste:
    """El error de la caché se arregló en `cliente_llm.py` y se quedó aquí.

    `ResumenCorrida.coste_usd` sumaba los tokens de todas las llamadas y les
    aplicaba la tarifa completa. Esa fórmula es la correcta para una
    ESTIMACIÓN —el `--dry-run` no sabe todavía qué parte vendrá de caché— pero
    este número es lo que la consola imprime como «esta corrida costó X» y lo
    que la bandeja le enseña a ella.

    Medido en la reclasificación real de 14 ofertas del 3 septiembre 2026:
    **$0,7378 anunciados contra $0,4453 cobrados**, un 66% de más.

    No lo cazó ninguna prueba porque los clientes falsos no devolvían
    `coste_medido_usd`: sin desglose de caché, la fórmula plana y la buena dan
    el mismo número y el fallo es invisible. Por eso este cliente falso SÍ trae
    desglose — una prueba que no puede distinguir las dos fórmulas no prueba
    cuál se está usando.
    """

    def _motor_con_cache(self, almacen, coste_medido):
        from clasificador.cliente_llm import RespuestaLLM
        from clasificador.motor import Motor
        from .test_clasificador import (
            ClienteFalso, clasificacion, fichas_de_prueba,
        )

        respuesta = RespuestaLLM(
            clasificacion=clasificacion(),
            tokens_entrada=7_000,   # casi todo servido de caché
            tokens_salida=680,
            modelo="modelo-de-prueba",
            coste_medido_usd=coste_medido,
        )
        return Motor(almacen, fichas_de_prueba(), ClienteFalso(respuesta)), respuesta

    def test_el_total_suma_lo_medido_y_no_lo_recalcula(self, almacen):
        from .test_presupuesto import _sembrar

        _sembrar(almacen, 3)
        motor, respuesta = self._motor_con_cache(almacen, coste_medido=0.0318)
        resumen = motor.clasificar(motor.pendientes())

        assert resumen.clasificadas == 3
        assert resumen.coste_usd == pytest.approx(0.0318 * 3)

    def test_y_ese_total_NO_es_el_que_daria_la_tarifa_plana(self, almacen):
        """La mitad que hace que la prueba anterior signifique algo.

        Si las dos fórmulas dieran lo mismo sobre estos datos, la de arriba
        pasaría con el fallo puesto. Aquí se fija que se diferencian de largo:
        con 7.000 tokens de entrada casi todos cacheados, la plana cobra más
        del doble.
        """
        from clasificador.cliente_llm import coste_de
        from .test_presupuesto import _sembrar

        _sembrar(almacen, 3)
        motor, respuesta = self._motor_con_cache(almacen, coste_medido=0.0318)
        resumen = motor.clasificar(motor.pendientes())

        plana = coste_de(resumen.tokens_entrada, resumen.tokens_salida)
        assert plana > resumen.coste_usd * 1.5, (
            "los datos de la prueba no distinguen las dos fórmulas"
        )

    def test_las_heredadas_no_suman_al_total(self, almacen):
        """Copiar un veredicto es gratis, y el total tiene que decirlo.

        Con la fórmula plana sí sumaban: los tokens del representante se
        contaban una vez por cada hermano que llevara sus tokens copiados.
        """
        from .test_clasificador import oferta_en_base

        run = almacen.start_run("prueba", ["adzuna"], ["mexico"])
        for i, ciudad in enumerate(["Monterrey", "Guadalajara", "Puebla"]):
            oferta_en_base(
                almacen, run, external_id=str(i),
                location_raw=ciudad, location_city=ciudad,
                url=f"https://example.test/{i}",
            )

        motor, _ = self._motor_con_cache(almacen, coste_medido=0.0318)
        resumen = motor.clasificar(motor.pendientes())

        assert resumen.total == 3
        assert resumen.heredadas == 2
        assert resumen.coste_usd == pytest.approx(0.0318), (
            "una llamada, un cobro: los hermanos heredan gratis"
        )


# ---------------------------------------------------------------------------
#  LA SALIDA CON EL ANUNCIO ENTERO (3 septiembre 2026)
# ---------------------------------------------------------------------------

class TestLaSalidaDependeDeSiElAnuncioVieneEntero:
    """El estimador cobraba 400 tokens de salida para todo.

    Con el recorte de 500 caracteres eso estaba medido (245-353). Con el
    anuncio entero se queda corto en **23 de 28** lecturas reales: la mediana
    medida es 586 y la media 656. El plan salía un ~12% por debajo de la
    factura el mismo día que se encendieron las descripciones — que es el lado
    malo del error, porque deja a la clienta a media corrida.

    Y HAY UNA CORRECCIÓN DE DIAGNÓSTICO DETRÁS. El proyecto tenía escrito que
    «la salida crece cuando crece la entrada», deducido de tres pares. Con 28
    medidas la correlación entre caracteres del anuncio y tokens de salida es
    −0,12: no hay pendiente, hay un escalón. Por eso siguen siendo constantes
    y por eso son dos, no una fórmula proporcional.
    """

    def test_un_anuncio_recortado_estima_la_salida_del_recorte(self):
        from clasificador.cliente_llm import TOKENS_SALIDA_RECORTE, salida_tipica

        assert salida_tipica(863, caracteres_descripcion=500) == TOKENS_SALIDA_RECORTE

    def test_un_anuncio_entero_estima_bastante_mas(self):
        from clasificador.cliente_llm import TOKENS_SALIDA_ENTERA, salida_tipica

        assert salida_tipica(6315, caracteres_descripcion=5817) == TOKENS_SALIDA_ENTERA
        assert TOKENS_SALIDA_ENTERA > 550, (
            "la mediana medida sobre 28 lecturas reales fue 586 y la media 656"
        )

    def test_sin_la_descripcion_se_deduce_del_texto_entero(self):
        """La red para quien no tiene la descripción suelta a mano.

        Los dos grupos no se tocan sobre los datos reales —el recorte llega a
        la llamada midiendo como mucho 863 caracteres y el entero como poco
        1.222—, así que el umbral de 1.000 los separa. Es un apaño y depende de
        lo que mida el envoltorio del prompt; el camino bueno es pasar
        `caracteres_descripcion`, que es lo que hace el motor.
        """
        from clasificador.cliente_llm import (
            TOKENS_SALIDA_ENTERA, TOKENS_SALIDA_RECORTE, salida_tipica,
        )

        assert salida_tipica(863) == TOKENS_SALIDA_RECORTE
        assert salida_tipica(1222) == TOKENS_SALIDA_ENTERA

    def test_el_coste_estimado_sube_al_pasar_a_entera(self):
        """No es solo la entrada: la salida es la que se cobra a $25 el millón.

        Si esta prueba cayera, el plan volvería a decir un número un 12% por
        debajo del cobrado en cuanto haya descripciones enteras.
        """
        from clasificador.cliente_llm import coste_estimado_de_llamada

        _, salida_corta, corta = coste_estimado_de_llamada(
            863, caracteres_descripcion=500
        )
        _, salida_entera, entera = coste_estimado_de_llamada(
            6315, caracteres_descripcion=5817
        )
        assert salida_entera > salida_corta
        assert entera > corta

    def test_el_envoltorio_no_convierte_un_recorte_en_entero(self):
        """El fallo que este parámetro existe para evitar.

        El envoltorio del prompt añade ~350 caracteres, así que un anuncio
        recortado llega a la llamada midiendo ~860. Con el umbral aplicado al
        texto entero y sin la descripción, un umbral mal puesto le cobraría un
        75% de salida de más sin que fallara nada.
        """
        from clasificador.cliente_llm import TOKENS_SALIDA_RECORTE, salida_tipica

        for envoltorio in (300, 350, 400):
            assert (
                salida_tipica(500 + envoltorio, caracteres_descripcion=500)
                == TOKENS_SALIDA_RECORTE
            )
