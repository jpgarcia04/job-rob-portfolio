"""
El triaje — a cuáles se les presta atención completa.

Lo que se vigila aquí NO es que elija bien: eso es un juicio y no se fija en
una prueba. Lo que se vigila es que **nada desaparezca de la cola sin que se
note**, que es el único riesgo real de meter una capa que decide qué se lee.

El triaje es la pieza del sistema con más potencial de hacer daño en silencio.
Un clasificador que se equivoca deja una razón escrita que ella puede leer y
discutir; un triaje que se equivoca deja una oferta sin razón ninguna, abajo
del todo, indistinguible de las que nadie miró. Por eso casi todas estas
pruebas persiguen el mismo fallo: una oferta que se cae de la lista.

Ninguna toca la red ni gasta un céntimo: el `Triador` recibe el cliente por
parámetro, igual que `Motor` y `Asistente`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from clasificador.cliente_llm import RespuestaRanking
from clasificador.fichas import Preferencias
from clasificador.triaje import (
    OfertaTriada,
    Triador,
    Triaje,
    construir_sistema,
)
from recolector.storage import Storage

from .test_clasificador import TEXTO_ES, almacen, oferta_en_base  # noqa: F401

PREFS = Preferencias(
    criterios=["El nivel manda sobre el sector."],
    cuantas_destacar=3,
)


class TriadorFalso:
    """Devuelve el triaje que le diga la prueba, y cuenta las llamadas.

    Y APUNTA EL GASTO SI SE LE DA UN LIBRO, igual que `ClienteAnthropic`: el
    que apunta es el cliente desde el 3 septiembre 2026, así que un falso que
    no apuntara dejaría las pruebas del gasto mirando un libro vacío.
    """

    modelo = "modelo-de-prueba"

    def __init__(
        self, triaje: Triaje | None, error: str | None = None, libro=None
    ) -> None:
        self.triaje = triaje
        self.error = error
        self.llamadas = 0
        self.ultimo_lote = ""
        self.ultimo_esfuerzo: str | None = None
        self._libro = libro

    def triar(
        self, sistema: str, lote: str, esfuerzo: str | None = None
    ) -> RespuestaRanking:
        self.llamadas += 1
        self.ultimo_lote = lote
        self.ultimo_esfuerzo = esfuerzo
        respuesta = RespuestaRanking(
            ranking=self.triaje,
            tokens_entrada=2266,
            tokens_salida=700,
            modelo=self.modelo,
            error=self.error,
        )
        # ANTES DE MIRAR SI SIRVE: la llamada se pagó igual. Es la misma línea
        # y el mismo orden que en `cliente_llm._invocar`.
        if self._libro is not None:
            self._libro.registrar(respuesta.coste_usd)
        return respuesta


def triaje_de(*pares: tuple[int, bool]) -> Triaje:
    """`triaje_de((3, True), (1, False))` -> el 3 primero y merece lectura."""
    return Triaje(
        nota="Lote de prueba.",
        ofertas=[
            OfertaTriada(numero=n, merece_lectura=m, por_que=f"motivo {n}")
            for n, m in pares
        ],
    )


def sembrar(almacen: Storage, cuantas: int) -> list[str]:
    return [
        oferta_en_base(almacen, "run-1", external_id=str(i), title=f"Director {i}")
        for i in range(1, cuantas + 1)
    ]


def filas_de(almacen: Storage) -> list:
    return almacen.connection.execute("SELECT * FROM offers ORDER BY rowid").fetchall()


def todos_merecen(cuantas: int) -> Triaje:
    return triaje_de(*[(i, True) for i in range(1, cuantas + 1)])


# ---------------------------------------------------------------------------
#  Que no se pierda ninguna
# ---------------------------------------------------------------------------

class TestNoSePierdeNinguna:
    def test_todas_las_enviadas_quedan_guardadas(self, almacen):
        """Incluidas las que NO pasan el corte.

        Es la prueba central del módulo. Guardar solo las seleccionadas
        dejaría a las demás indistinguibles de las que nadie miró nunca, y la
        bandeja no podría explicarle por qué una oferta sigue sin revisar.
        """
        sembrar(almacen, 5)
        cliente = TriadorFalso(
            triaje_de((1, True), (2, True), (3, False), (4, False), (5, False))
        )
        resultado = Triador(almacen, PREFS, cliente, cuantas=2).triar(filas_de(almacen))

        assert resultado.ok
        guardadas = almacen.connection.execute(
            "SELECT COUNT(*) FROM triajes"
        ).fetchone()[0]
        assert guardadas == 5, "una oferta triada sin fila es una oferta escondida"

    def test_las_no_seleccionadas_quedan_marcadas_como_tales(self, almacen):
        """Vista pero aplazada, que no es lo mismo que descartada."""
        sembrar(almacen, 4)
        cliente = TriadorFalso(
            triaje_de((1, True), (2, True), (3, True), (4, False))
        )
        Triador(almacen, PREFS, cliente, cuantas=2).triar(filas_de(almacen))

        resumen = almacen.resumen_triaje()
        assert resumen["triadas"] == 4
        assert resumen["seleccionadas"] == 2
        assert resumen["merecian"] == 3

    def test_un_numero_repetido_invalida_el_triaje_entero(self, almacen):
        """Repetir el 1 significa que otra oferta se quedó fuera."""
        sembrar(almacen, 3)
        cliente = TriadorFalso(triaje_de((1, True), (1, True), (2, False)))
        resultado = Triador(almacen, PREFS, cliente, cuantas=2).triar(filas_de(almacen))

        assert not resultado.ok
        assert "dos veces" in resultado.error
        vacia = almacen.connection.execute("SELECT COUNT(*) FROM triajes").fetchone()[0]
        assert vacia == 0, "un triaje invalido no se guarda a medias"

    def test_una_oferta_olvidada_invalida_el_triaje_entero(self, almacen):
        sembrar(almacen, 4)
        cliente = TriadorFalso(triaje_de((1, True), (2, True), (3, False)))
        resultado = Triador(almacen, PREFS, cliente, cuantas=2).triar(filas_de(almacen))

        assert not resultado.ok
        assert "fuera" in resultado.error

    def test_un_numero_inventado_invalida_el_triaje_entero(self, almacen):
        sembrar(almacen, 3)
        cliente = TriadorFalso(triaje_de((1, True), (2, True), (47, False)))
        resultado = Triador(almacen, PREFS, cliente, cuantas=2).triar(filas_de(almacen))

        assert not resultado.ok
        assert "47" in resultado.error


# ---------------------------------------------------------------------------
#  N es un techo, no una cuota
# ---------------------------------------------------------------------------

class TestElCorte:
    def test_respeta_el_techo(self, almacen):
        sembrar(almacen, 6)
        resultado = Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(6)), cuantas=3
        ).triar(filas_de(almacen))

        assert len(resultado.seleccionadas(3)) == 3

    def test_si_pocas_merecen_lectura_se_leen_pocas(self, almacen):
        """N es un TECHO. Rellenarlo sería gastar por gastar.

        Un lote flojo tiene que costar menos, no lo mismo peor.
        """
        sembrar(almacen, 6)
        cliente = TriadorFalso(
            triaje_de((1, True), (2, True), (3, False),
                      (4, False), (5, False), (6, False))
        )
        resultado = Triador(almacen, PREFS, cliente, cuantas=4).triar(filas_de(almacen))

        assert len(resultado.seleccionadas(4)) == 2

    def test_el_orden_manda_dentro_de_las_que_merecen(self, almacen):
        claves = sembrar(almacen, 4)
        # El modelo pone la 3 primera y la 1 segunda.
        cliente = TriadorFalso(
            triaje_de((3, True), (1, True), (2, True), (4, False))
        )
        resultado = Triador(almacen, PREFS, cliente, cuantas=2).triar(filas_de(almacen))

        assert resultado.seleccionadas(2) == [claves[2], claves[0]]

    def test_no_gasta_si_caben_todas(self, almacen):
        """Triar cinco para leer ocho es pagar una llamada por nada."""
        sembrar(almacen, 5)
        cliente = TriadorFalso(triaje_de((1, True)))
        resultado = Triador(almacen, PREFS, cliente, cuantas=8).triar(filas_de(almacen))

        assert cliente.llamadas == 0
        assert len(resultado.seleccionadas(8)) == 5
        assert "caben todas" in resultado.nota


# ---------------------------------------------------------------------------
#  Lo que ve el modelo
# ---------------------------------------------------------------------------

class TestElMensaje:
    def test_el_lote_no_lleva_descripciones(self, almacen):
        """Es lo que lo hace costar ~70 tokens por oferta y no ~5.400."""
        sembrar(almacen, 10)
        cliente = TriadorFalso(todos_merecen(10))
        Triador(almacen, PREFS, cliente, cuantas=3).triar(filas_de(almacen))

        assert TEXTO_ES[:60] not in cliente.ultimo_lote

    def test_el_sistema_avisa_de_que_el_titulo_miente(self):
        """El caso FloatMe y el caso «Gerente corporativo».

        Si alguien recorta el prompt y quita esos dos ejemplos, el triaje
        vuelve a ordenar por lo bonito que suene el título, que es el fallo
        que este proyecto ya midió dos veces.
        """
        sistema = construir_sistema(PREFS, 8)
        assert "Gerente corporativo" in sistema
        assert "miente" in sistema

    def test_el_sistema_dice_que_no_se_descarta_nada(self):
        sistema = construir_sistema(PREFS, 8)
        assert "no estás descartando" in sistema.lower()

    def test_los_criterios_de_ella_van_tal_cual(self):
        sistema = construir_sistema(PREFS, 8)
        assert "El nivel manda sobre el sector." in sistema

    def test_avisa_de_las_vacantes_repetidas(self, almacen):
        """Publicada en varias ciudades = se busca de verdad. Es señal."""
        for ciudad in ("Monterrey", "Guadalajara", "Puebla"):
            oferta_en_base(
                almacen, "run-1", external_id=ciudad,
                title="Director de Operaciones", company="Acme",
                location_raw=ciudad, location_city=ciudad,
            )
        for i in range(5):
            oferta_en_base(
                almacen, "run-1", external_id=f"otra{i}",
                title=f"Gerente {i}", company=f"Empresa {i}",
            )

        filas = filas_de(almacen)
        # Se tría por VACANTE: los 3 anuncios de Acme son una sola línea, y
        # las 5 sueltas otras cinco. Por eso son 6 números, no 8.
        cliente = TriadorFalso(todos_merecen(6))
        Triador(almacen, PREFS, cliente, cuantas=2).triar(filas)

        assert "publicada en 3 sitios" in cliente.ultimo_lote
        assert cliente.ultimo_lote.count("Director de Operaciones") == 1, (
            "la misma vacante no puede ocupar tres huecos del triaje"
        )


# ---------------------------------------------------------------------------
#  Presupuesto
# ---------------------------------------------------------------------------

class GuardiaFalso:
    def __init__(self, permite: bool = True) -> None:
        self._permite = permite
        self.apuntado = 0.0
        # Por cuánto se pidió permiso. Es lo que compara
        # `TestPideLoQueVaAGastar`: un techo al que se le miente sobre el
        # importe no es un techo.
        self.pedido: list[float] = []

    def permite(self, coste: float) -> bool:
        self.pedido.append(coste)
        return self._permite

    def apuntar(self, usd: float, llamadas: int = 1) -> None:
        self.apuntado += usd


class TestPresupuesto:
    def test_la_llamada_del_triaje_se_apunta(self, almacen, tmp_path):
        """El triaje también cuesta dinero, y tiene que constar en el libro.

        QUIÉN LO APUNTA CAMBIÓ EL 3 SEPTIEMBRE 2026 y la prueba lo sigue: era
        `Triador` quien llamaba a `guardia.apuntar`, y ahora es el cliente,
        donde está el coste con el desglose de caché. Lo que se comprueba es lo
        mismo —que el dinero acaba en el libro— y por eso la prueba mira el
        LIBRO y no una capa concreta: así sobrevive al siguiente cambio de
        sitio.
        """
        from clasificador.presupuesto import LibroDeGastos

        sembrar(almacen, 10)
        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(10), libro=libro),
            cuantas=3, presupuesto=GuardiaFalso(),
        ).triar(filas_de(almacen))

        assert libro.gastado_este_mes() > 0, "el triaje también cuesta dinero"

    def test_sin_saldo_no_se_llama_y_nada_se_pierde(self, almacen):
        sembrar(almacen, 10)
        cliente = TriadorFalso(triaje_de((1, True)))
        resultado = Triador(
            almacen, PREFS, cliente, cuantas=3,
            presupuesto=GuardiaFalso(permite=False),
        ).triar(filas_de(almacen))

        assert cliente.llamadas == 0
        assert not resultado.ok
        assert "siguen pendientes" in resultado.error

    def test_se_apunta_aunque_la_respuesta_venga_mal(self, almacen, tmp_path):
        """La llamada se pagó igual.

        Un contador que solo registra los éxitos miente justo el día que algo
        va mal. Mismo criterio que en `presupuesto.py`.
        """
        from clasificador.presupuesto import LibroDeGastos

        sembrar(almacen, 10)
        libro = LibroDeGastos(tmp_path / "uso_llm.json")
        cliente = TriadorFalso(
            None, error="La API rechazó la petición.", libro=libro
        )
        Triador(
            almacen, PREFS, cliente, cuantas=3, presupuesto=GuardiaFalso(),
        ).triar(filas_de(almacen))

        assert libro.gastado_este_mes() > 0


class TestPideLoQueVaAGastar:
    """El permiso se pide por el importe de ESTA llamada, no por una constante.

    Aquí había un `permite(0.05)` escrito a mano. El triaje no tiene un tamaño
    fijo: cuesta ~$0,09 sobre 21 vacantes y ~$0,88 sobre las 228 que había
    atrasadas en la base el 2 septiembre 2026. Pedir permiso por cinco céntimos
    y gastar ochenta y ocho es dieciocho veces el número que se enseñó, y un
    techo al que se le miente sobre el importe deja de ser un techo.

    No dolía mientras la corrida la lanzaba un comando de consola con el mes
    casi entero disponible. Pasa a doler el día que el botón es suyo.
    """

    def test_el_permiso_se_pide_por_el_coste_del_lote(self, almacen):
        from clasificador.triaje import coste_estimado

        sembrar(almacen, 40)
        guardia = GuardiaFalso()
        Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(40)),
            cuantas=8, presupuesto=guardia,
        ).triar(filas_de(almacen))

        assert guardia.pedido, "el triaje tiene que pedir permiso antes de llamar"
        _, _, esperado = coste_estimado(40)
        assert abs(guardia.pedido[0] - esperado) < 1e-9

    def test_un_lote_grande_pide_mas_que_uno_pequeno(self, almacen, tmp_path):
        """La comprobación que el número fijo no podía hacer.

        Los dos van a `low`, que es lo que dice el YAML real: el esfuerzo
        cambia el permiso que se pide (a `high` la salida son 140 tokens por
        vacante y a `low` unos 50), así que probarlo sin decirlo probaría un
        camino que en producción no ocurre.
        """
        sembrar(almacen, 12)
        pequeno = GuardiaFalso()
        Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(12)),
            cuantas=8, presupuesto=pequeno, esfuerzo="low",
        ).triar(filas_de(almacen))

        with Storage(tmp_path / "otra.db") as otro:
            sembrar(otro, 90)
            grande = GuardiaFalso()
            Triador(
                otro, PREFS, TriadorFalso(todos_merecen(90)),
                cuantas=8, presupuesto=grande, esfuerzo="low",
            ).triar(filas_de(otro))

        assert grande.pedido[0] > pequeno.pedido[0] * 3, (
            "si el permiso no crece con el lote, el techo no ve venir el gasto"
        )

    def test_el_motivo_dice_cuanto_costaba(self, almacen):
        """Que ella pueda distinguir «no cabía por poco» de «no cabía por mucho»."""
        sembrar(almacen, 30)
        resultado = Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(30)),
            cuantas=8, presupuesto=GuardiaFalso(permite=False),
        ).triar(filas_de(almacen))

        assert not resultado.ok
        assert "30 vacantes" in resultado.error
        assert "siguen pendientes" in resultado.error


# ---------------------------------------------------------------------------
#  Que la cola no crezca
# ---------------------------------------------------------------------------

class TestCadaOfertaTieneUnTurno:
    """El fallo que casi se cuela: una cola que crece sin parar.

    Entran ~21 ofertas al día y se leen 8. Si las 13 que no entran volvieran a
    competir en la siguiente corrida, la cola crecería 13 diarias: al mes,
    ~390 pendientes y un triaje de ~$0,46 por corrida en vez de ~$0,03. El
    triaje acabaría costando más que no tenerlo, que es lo contrario de por
    qué existe.

    La corrección es que cada oferta se tría UNA vez. La que no entra se queda
    en la bandeja, vista y con su motivo, y espera a que ella la pida.
    """

    def test_las_no_seleccionadas_quedan_marcadas_como_ya_triadas(self, almacen):
        sembrar(almacen, 20)
        cliente = TriadorFalso(todos_merecen(20))
        Triador(almacen, PREFS, cliente, cuantas=5).triar(filas_de(almacen))

        ya = almacen.claves_ya_triadas()
        assert len(ya) == 20, (
            "las 15 que no se leyeron tienen que constar como triadas; si no, "
            "vuelven a competir mañana y la cola crece sin parar"
        )

    def test_un_triaje_fallido_no_quema_el_turno(self, almacen):
        """Si el modelo devolvió basura, esas ofertas no han tenido su turno.

        Marcarlas gastaría el turno de todas por un fallo que no es suyo.
        """
        sembrar(almacen, 20)
        cliente = TriadorFalso(triaje_de((1, True), (1, True)))
        resultado = Triador(almacen, PREFS, cliente, cuantas=5).triar(filas_de(almacen))

        assert not resultado.ok
        assert almacen.claves_ya_triadas() == set()


# ---------------------------------------------------------------------------
#  Y el turno es de la VACANTE, no del anuncio
# ---------------------------------------------------------------------------

def vacante_repetida(almacen: Storage, ciudades: list[str], titulo: str) -> list[str]:
    """La misma vacante publicada en varias ciudades. Devuelve sus claves.

    Comparten `loose_key` (empresa + título, sin ubicación), que es lo que hace
    que `agrupar_para_ahorrar` las vea como un solo puesto.
    """
    return [
        oferta_en_base(
            almacen,
            "run-1",
            external_id=f"{titulo}-{c}",
            title=titulo,
            company="Hermanos SA",
            location_raw=c,
            location_city=c,
        )
        for c in ciudades
    ]


class TestElTurnoEsDeLaVacante:
    """El agujero que hacía que la misma vacante se triara una y otra vez.

    `_guardar` escribía fila solo para el REPRESENTANTE de cada grupo. Los
    anuncios hermanos quedaban sin marcar, `claves_ya_triadas` no los veía y
    volvían a entrar — y como el representante sí estaba excluido, la corrida
    siguiente ascendía a hermano a representante y mandaba la MISMA vacante
    otra vez. Medido sobre la base real el 3 septiembre 2026: 29 vacantes con
    más de una fila, 73 filas y 44 huecos de lote (12,4%) gastados en repetir;
    un `Senior Vice President of Development & Construction` publicado en seis
    ciudades de Texas pasó por el modelo en seis corridas seguidas.

    No salía caro en lecturas —`clasificaciones.heredada_de` impide leer dos
    veces, y hay cero vacantes con dos clasificaciones propias— sino en SITIO:
    cada repetición ocupaba un hueco del lote que le tocaba a una vacante que
    no había mirado nadie.
    """

    def test_los_hermanos_quedan_marcados(self, almacen):
        vacante_repetida(almacen, ["Monterrey", "Guadalajara", "Puebla"], "Director")
        sembrar(almacen, 12)

        cliente = TriadorFalso(todos_merecen(13))
        Triador(almacen, PREFS, cliente, cuantas=5).triar(filas_de(almacen))

        ya = almacen.claves_ya_triadas()
        assert len(ya) == 15, (
            "las 3 publicaciones de la misma vacante tienen que quedar las "
            "tres marcadas, no solo la representante"
        )

    def test_la_misma_vacante_no_vuelve_a_entrar(self, almacen):
        """La prueba de regresión del fallo, tal como se daba en la base.

        Segunda corrida sobre lo que quede sin triar: si los hermanos no están
        marcados, uno de ellos asciende a representante y la vacante entera se
        vuelve a mandar al modelo.
        """
        vacante_repetida(almacen, ["Monterrey", "Guadalajara", "Puebla"], "Director")
        sembrar(almacen, 12)

        cliente = TriadorFalso(todos_merecen(13))
        Triador(almacen, PREFS, cliente, cuantas=5).triar(filas_de(almacen))

        ya = almacen.claves_ya_triadas()
        pendientes = [f for f in filas_de(almacen) if f["dedupe_key"] not in ya]
        assert pendientes == [], (
            "tras triar la vacante entera no puede quedar ningún anuncio suyo "
            "sin marcar: el que quede la hace volver a competir mañana"
        )

    def test_el_hermano_dice_de_quien_heredo_el_puesto(self, almacen):
        """Marcarlo no basta: tiene que quedar claro que no lo ordenó el modelo.

        Si el hermano se marcara como una fila normal, aparecería en «Sin
        revisar todavía» con un puesto que ningún modelo le dio — que es
        exactamente lo que el tope de lote tiene prohibido. La columna evita
        tener que elegir entre marcarlo y decir la verdad.
        """
        claves = vacante_repetida(almacen, ["Monterrey", "Guadalajara"], "Director")
        sembrar(almacen, 12)

        cliente = TriadorFalso(todos_merecen(13))
        Triador(almacen, PREFS, cliente, cuantas=5).triar(filas_de(almacen))

        filas = {
            f["dedupe_key"]: f
            for f in almacen.connection.execute("SELECT * FROM triajes")
        }
        propias = [k for k in claves if filas[k]["heredada_de"] is None]
        heredadas = [k for k in claves if filas[k]["heredada_de"] is not None]

        assert len(propias) == 1, "solo una de las dos se le mandó al modelo"
        assert len(heredadas) == 1
        assert filas[heredadas[0]]["heredada_de"] == propias[0]
        assert filas[heredadas[0]]["posicion"] == filas[propias[0]]["posicion"], (
            "el hermano hereda el puesto de su representante, no otro"
        )

    def test_el_hermano_nunca_se_selecciona_para_lectura(self, almacen):
        """La que se lee entera es la representante; el hermano hereda gratis.

        Si el hermano saliera `seleccionada`, el clasificador pagaría dos
        llamadas por la misma vacante — justo lo que la agrupación evita.
        """
        vacante_repetida(almacen, ["Monterrey", "Guadalajara", "Puebla"], "Director")
        sembrar(almacen, 12)

        cliente = TriadorFalso(todos_merecen(13))
        Triador(almacen, PREFS, cliente, cuantas=5).triar(filas_de(almacen))

        heredadas = almacen.connection.execute(
            "SELECT seleccionada FROM triajes WHERE heredada_de IS NOT NULL"
        ).fetchall()
        assert heredadas, "la prueba no vale si no hay ningún hermano marcado"
        assert all(f["seleccionada"] == 0 for f in heredadas)



# ---------------------------------------------------------------------------
#  Que la respuesta quepa en la llamada
# ---------------------------------------------------------------------------

class TestCabeEnUnaLlamada:
    """El techo que nadie había elegido.

    El triaje escribe una línea por vacante y piensa por el camino, y las dos
    cosas cuentan dentro de `max_tokens`. Pasarse no da un error barato: la
    llamada se paga ENTERA y no devuelve ni un triaje.

    El 3 septiembre 2026 había 273 vacantes sin triar y nada comprobaba esto.
    El plan anunciaba 38.340 tokens de salida contra un techo de 8.000 y
    terminaba con «El plan CABE en lo que queda del mes».
    """

    def test_un_lote_que_no_cabe_no_llega_a_llamar(self, almacen):
        sembrar(almacen, 400)
        cliente = TriadorFalso(todos_merecen(400))
        guardia = GuardiaFalso()

        resultado = Triador(
            almacen, PREFS, cliente, cuantas=8,
            presupuesto=guardia, esfuerzo="low",
        ).triar(filas_de(almacen))

        assert cliente.llamadas == 0, "una llamada que se va a cortar no se hace"
        assert not resultado.ok
        assert resultado.causa == "no_cabe"

    def test_no_le_pide_permiso_al_presupuesto(self, almacen):
        """El orden importa, y por eso se fija.

        Al techo del mes esta llamada le cabe de sobra —son céntimos—, así que
        preguntarle primero daría permiso para una llamada que se va a cortar.
        La comprobación de que cabe va ANTES.
        """
        sembrar(almacen, 400)
        guardia = GuardiaFalso()

        Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(400)), cuantas=8,
            presupuesto=guardia, esfuerzo="low",
        ).triar(filas_de(almacen))

        assert not guardia.pedido, (
            "no se le pide permiso al mes por una llamada que no cabe"
        )

    def test_no_se_pierde_ninguna_oferta(self, almacen):
        """Negarse es seguro; cortarse no. Las ofertas siguen pendientes."""
        sembrar(almacen, 400)

        Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(400)), cuantas=8,
            esfuerzo="low",
        ).triar(filas_de(almacen))

        triadas = almacen.connection.execute(
            "SELECT COUNT(*) FROM triajes"
        ).fetchone()[0]
        assert triadas == 0, (
            "si no se tría, no se marca nada como triado: cada oferta tiene un "
            "turno y no se gasta sin haberla mirado"
        )

    def test_el_motivo_trae_el_numero_accionable(self, almacen):
        """«No cabe» sin decir cuántas caben no se puede accionar."""
        from clasificador.triaje import cabida

        sitio = cabida(400, "low")
        assert not sitio.cabe
        assert f"~{sitio.caben} vacantes" in sitio.explicacion(400)
        assert sitio.caben > 0

    def test_el_esfuerzo_cambia_cuantas_caben(self):
        """A `high` cabe menos de la mitad que a `low`, y hay que verlo venir.

        Es la razón de que `cabida()` reciba el esfuerzo en vez de llevar un
        número fijo: subir la perilla del YAML cambia el techo real de golpe.
        """
        from clasificador.triaje import cabida

        assert cabida(1, "low").caben > cabida(1, "high").caben * 2

    def test_un_lote_normal_si_cabe(self, almacen):
        """La guarda no puede estorbar en el caso de todos los días."""
        from clasificador.triaje import cabida

        assert cabida(25, "low").cabe, "un día normal son ~25 vacantes"
        assert cabida(60, "low").cabe, "y un lunes con atraso, unas 60"


# ---------------------------------------------------------------------------
#  Qué le llega a la pantalla cuando NO se tría
# ---------------------------------------------------------------------------

def config_de_prueba(
    cuantas: int = 8, esfuerzo: str = "low", tope: int | None = 60
) -> SimpleNamespace:
    """Solo lo que `_aplicar_triaje` mira del config."""
    return SimpleNamespace(
        llm=SimpleNamespace(
            triaje=SimpleNamespace(
                activo=True,
                ofertas_por_corrida=cuantas,
                max_vacantes_por_triaje=tope,
                esfuerzo=esfuerzo,
            )
        )
    )


class TestElCodigoDeSalidaNoMiente:
    """El 3 significa «no había saldo» en todo el proyecto.

    `bandeja/trabajos.py` lo traduce literalmente a `sin_saldo` y la interfaz
    lo pinta como algo que NO es un fallo. Hasta el 3 septiembre 2026, TODOS
    los finales sin triaje devolvían 3: «no hay nada nuevo que triar» —que es
    el estado normal en cuanto la cola está al día—, «falta la API key» y «el
    lote no cabe en la llamada» se le presentaban a la clienta las tres como si
    se hubiera quedado sin dinero.

    Es la regla nº 1 de la casa aplicada a la pantalla: dos cosas distintas
    tienen que poder distinguirse. Y esta de más: mentir en un número pequeño
    es lo que hace que se deje de creer en los grandes.
    """

    def test_no_hay_nada_nuevo_no_es_sin_saldo(self, almacen):
        """El caso de todos los días una vez la cola está triada."""
        from clasificador.cli import _aplicar_triaje

        claves = sembrar(almacen, 5)
        almacen.guardar_triaje([
            {
                "dedupe_key": c, "posicion": i, "merece_lectura": 0,
                "seleccionada": 0, "por_que": "ya tuvo su turno",
                "modelo": "modelo-de-prueba",
            }
            for i, c in enumerate(claves, 1)
        ])

        filas, triaje, codigo = _aplicar_triaje(
            almacen, config_de_prueba(), filas_de(almacen), GuardiaFalso(),
            cliente=TriadorFalso(None),
        )

        assert filas == []
        assert codigo == 0, "«ya están todas triadas» no es quedarse sin dinero"

    def test_un_lote_que_no_cabe_no_es_sin_saldo(self, almacen):
        """El tope de lote hace que este caso ya casi no se dé, pero no lo borra.

        Con un tope sensato el lote se recorta y la llamada cabe siempre; hay
        que pedir un tope de 400 para llegar hasta aquí. Se prueba igualmente
        porque el tope es configurable y nadie impide ponerlo alto: la guarda
        es la red, y una red que solo funciona si otro número está bien puesto
        no es una red. Lo que se fija es que siga siendo un 2 —un problema de
        tamaño— y no un 3, que la bandeja pinta como «sin saldo».
        """
        from clasificador.cli import _aplicar_triaje

        sembrar(almacen, 400)
        cliente = TriadorFalso(todos_merecen(400))

        filas, triaje, codigo = _aplicar_triaje(
            almacen, config_de_prueba(tope=400), filas_de(almacen), GuardiaFalso(),
            cliente=cliente,
        )

        assert cliente.llamadas == 0
        assert codigo == 2, (
            "un lote que no cabe es un problema de tamaño, no de dinero"
        )
        assert triaje.causa == "no_cabe"

    def test_sin_saldo_si_de_verdad_no_hay_saldo(self, almacen):
        """Y el 3 se sigue devolviendo cuando SÍ es eso. Sin esto, el arreglo
        habría cambiado una mentira por otra."""
        from clasificador.cli import _aplicar_triaje

        sembrar(almacen, 30)

        filas, triaje, codigo = _aplicar_triaje(
            almacen, config_de_prueba(), filas_de(almacen),
            GuardiaFalso(permite=False), cliente=TriadorFalso(todos_merecen(30)),
        )

        assert codigo == 3
        assert triaje.causa == "presupuesto"

    def test_caben_todas_sin_gastar_el_triaje(self, almacen):
        """Menos vacantes que huecos: no se llama, y no es un fallo."""
        from clasificador.cli import _aplicar_triaje

        sembrar(almacen, 3)
        cliente = TriadorFalso(None)

        filas, triaje, codigo = _aplicar_triaje(
            almacen, config_de_prueba(cuantas=8), filas_de(almacen),
            GuardiaFalso(), cliente=cliente,
        )

        assert cliente.llamadas == 0
        assert len(filas) == 3, "se leen las tres, sin gastar la llamada del triaje"
        assert codigo == 0

# ---------------------------------------------------------------------------
#  Lo que dice que va a costar
# ---------------------------------------------------------------------------

class TestEstimacion:
    def test_reproduce_la_medicion_real(self):
        """Medido con `count_tokens` el 3 septiembre 2026, mandando el esquema
        de salida y `thinking` igual que la llamada de verdad.

        La cifra anterior de esta prueba —2.266 sobre 21 ofertas— se midió SIN
        el esquema y con el prompt de sistema más corto, y por eso fijaba una
        estimación que se quedaba 1.435 tokens corta. Una prueba puede fijar un
        número equivocado durante semanas si el número se midió mal: lo que la
        hace útil es la fecha y el método escritos al lado.
        """
        from clasificador.triaje import coste_estimado

        for cuantas, medido in ((21, 3705), (75, 7099), (273, 19150)):
            entrada, _, _ = coste_estimado(cuantas)
            assert abs(entrada - medido) < 120, (
                f"sobre {cuantas} vacantes la estimación dice {entrada} y "
                f"`count_tokens` midió {medido}"
            )

    def test_el_esfuerzo_cambia_lo_que_cuesta(self):
        """Hallazgo del 3 septiembre 2026.

        El estimador cobraba todo a 140 tokens de salida por vacante, que es la
        cifra de `high` —el valor que aplica la API por defecto—, mientras el
        triaje corría en `low`. Sobre 21 vacantes decía $0,0878 donde lo medido
        fueron $0,045. El error caía del lado seguro, pero el número que se le
        enseña a la clienta en la bandeja tiene que parecerse al que se le
        cobra.
        """
        from clasificador.triaje import coste_estimado

        _, _, barato = coste_estimado(21, "low")
        _, _, caro = coste_estimado(21, "high")
        assert barato < caro, "si `low` no sale más barato, la perilla no llega"

        # Contra los dos costes MEDIDOS el 2 septiembre 2026 a esfuerzo `low`.
        for cuantas, medido in ((21, 0.045), (25, 0.054)):
            _, _, estimado = coste_estimado(cuantas, "low")
            assert estimado == pytest.approx(medido, rel=0.15), (
                f"sobre {cuantas} vacantes se estima ${estimado:.4f} y se "
                f"midieron ${medido:.4f}"
            )
            assert estimado >= medido, (
                "y cuando se falle, que sea por arriba: quedarse corto deja a "
                "la clienta a media corrida"
            )

    def test_sin_esfuerzo_se_asume_el_caro(self):
        """Quien llame sin saber el esfuerzo tiene que llevarse la cifra alta.

        `high` es lo que aplica la API si no se manda `output_config.effort`,
        así que además de ser el lado seguro es lo que de verdad pasaría.
        """
        from clasificador.triaje import coste_estimado

        assert coste_estimado(30)[2] == coste_estimado(30, "high")[2]

    def test_crece_con_el_lote(self):
        from clasificador.triaje import coste_estimado

        _, _, poco = coste_estimado(10)
        _, _, mucho = coste_estimado(60)
        assert mucho > poco

    def test_triar_un_lote_cuesta_menos_que_leerlo_entero(self):
        """La única comparación que justifica esta capa.

        OJO CON LA INTUICIÓN: triar no es gratis. Comparar veinte anuncios
        genera mucho pensamiento, y el pensamiento se factura como salida — es
        el 80% del coste del triaje.

        Aquí ponía que triar 20 costaba como leer SEIS enteras y que a partir
        de `ofertas_por_corrida: 15` el triaje dejaba de compensar. Las dos
        cifras venían del estimador que cobraba el triaje a esfuerzo `high`
        cuando corre en `low`; corregidas el 3 septiembre 2026, son TRES
        lecturas y ~22. La prueba no cambia porque nunca dependió de esos
        números: fija el único extremo que siempre tiene que cumplirse, y por
        eso sobrevivió a que el estimador estuviera mal.
        """
        from clasificador.cliente_llm import coste_estimado_de_llamada
        from clasificador.triaje import coste_estimado

        _, _, triar_veinte = coste_estimado(20)
        leer_una = coste_estimado_de_llamada(850)[2]
        assert triar_veinte < leer_una * 20, (
            "triar el lote entero tiene que costar menos que leerlo entero, "
            "o esta capa no tiene ningún sentido"
        )

    def test_el_esfuerzo_llega_al_cliente(self, almacen):
        """Es la perilla que baja el coste del triaje un 44% (medido)."""
        sembrar(almacen, 20)
        cliente = TriadorFalso(todos_merecen(20))
        Triador(almacen, PREFS, cliente, cuantas=5, esfuerzo="low").triar(
            filas_de(almacen)
        )
        assert cliente.ultimo_esfuerzo == "low"


# ---------------------------------------------------------------------------
#  El tope de lote
# ---------------------------------------------------------------------------

class TestElTopeDeLote:
    """Lo que no entra en la llamada CONSERVA SU TURNO ENTERO.

    Es la distinción que hay que defender aquí, y es más fina que la de
    `TestCadaOfertaTieneUnTurno`. Hay tres estados, no dos:

        leída          se clasificó
        aplazada       el modelo la vio, la puso en un puesto y escribió por
                       qué. Gastó su turno: no vuelve a competir sola
        fuera de tope  nadie la ha mirado. NO gastó su turno

    El tercero es el nuevo, y el riesgo de meterlo es confundirlo con el
    segundo: si una oferta que el tope dejó fuera se marcara en `triajes`,
    saldría de la cola para siempre sin que ningún modelo la hubiera visto —un
    descarte silencioso con dos pasos de más, que es la regla nº 1 rota de la
    peor manera posible, porque además parecería que funciona.

    Por eso casi todas estas pruebas miran lo mismo: qué hay y qué NO hay en
    `triajes` después de una corrida con tope.
    """

    def test_solo_se_mandan_las_primeras_n(self, almacen):
        sembrar(almacen, 20)
        cliente = TriadorFalso(todos_merecen(5))
        resultado = Triador(
            almacen, PREFS, cliente, cuantas=3, tope_lote=5
        ).triar(filas_de(almacen))

        assert resultado.ok
        assert resultado.consideradas == 5
        assert resultado.fuera_del_tope == 15

    def test_las_que_no_entran_no_se_marcan(self, almacen):
        """LA PRUEBA IMPORTANTE. Sin esto, el tope es un filtro silencioso."""
        claves = sembrar(almacen, 20)
        cliente = TriadorFalso(todos_merecen(5))
        Triador(almacen, PREFS, cliente, cuantas=3, tope_lote=5).triar(
            filas_de(almacen)
        )

        ya = almacen.claves_ya_triadas()
        assert len(ya) == 5, (
            "solo las 5 que el modelo llegó a ver pueden constar como triadas; "
            "marcar las otras 15 las sacaría de la cola sin que nadie las mirara"
        )
        assert set(claves[5:]).isdisjoint(ya)

    def test_la_corrida_siguiente_las_coge_enteras(self, almacen):
        """Dos corridas seguidas: la cola drena, no se vara ni se pierde nada.

        Es lo que `--limite N` no consigue. Ese recorta por RECENCIA sobre
        anuncios pendientes —antes de quitar las ya triadas y antes de
        agrupar—, así que las nuevas reocupan la ventana y el atraso viejo se
        queda fuera para siempre. El tope se aplica después de las dos cosas.
        """
        sembrar(almacen, 20)

        vistas = almacen.claves_ya_triadas()
        primera = [f for f in filas_de(almacen) if f["dedupe_key"] not in vistas]
        Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(5)), cuantas=3, tope_lote=5
        ).triar(primera)

        vistas = almacen.claves_ya_triadas()
        segunda = [f for f in filas_de(almacen) if f["dedupe_key"] not in vistas]
        assert len(segunda) == 15, "las 15 que no entraron siguen esperando enteras"

        resultado = Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(5)), cuantas=3, tope_lote=5
        ).triar(segunda)
        assert resultado.ok
        assert len(almacen.claves_ya_triadas()) == 10, "la cola drena de 5 en 5"

    def test_el_tope_cuenta_vacantes_y_no_anuncios(self, almacen):
        """Tres anuncios de la misma vacante ocupan UN sitio del tope.

        El tope se aplica después de agrupar, igual que el corte de lecturas.
        Si contara anuncios, una empresa que publica en cinco ciudades se
        comería el lote y desplazaría vacantes distintas a la corrida siguiente.
        """
        for i in range(1, 4):
            oferta_en_base(
                almacen, "run-1", external_id=f"dcs-{i}",
                title="Chief Operating Officer", company="Northline Asset Services",
            )
        for i in range(1, 4):
            oferta_en_base(
                almacen, "run-1", external_id=f"otra-{i}", title=f"Director {i}",
            )

        cliente = TriadorFalso(todos_merecen(2))
        resultado = Triador(
            almacen, PREFS, cliente, cuantas=1, tope_lote=2
        ).triar(filas_de(almacen))

        assert resultado.ok
        assert resultado.consideradas == 2
        # 4 vacantes distintas (Northline + 3 directores): 2 entran, 2 esperan.
        assert resultado.fuera_del_tope == 2

    def test_el_tope_desbloquea_un_lote_que_no_cabria(self, almacen):
        """El caso que dejó la app parada el 3 septiembre 2026.

        Sin tope, un lote por encima de `cabida()` se niega entero —y hace bien,
        porque la llamada se pagaría y no devolvería ningún triaje—, pero la
        corrida siguiente se encuentra exactamente el mismo lote y se vuelve a
        negar. La guarda sola no tiene salida; el tope es la otra mitad.
        """
        from clasificador.triaje import cabida

        cuantas_no_caben = cabida(1, "low").caben + 20
        sembrar(almacen, cuantas_no_caben)
        filas = filas_de(almacen)

        sin_tope = Triador(
            almacen, PREFS, TriadorFalso(None), cuantas=8, esfuerzo="low"
        ).triar(filas)
        assert sin_tope.causa == "no_cabe"
        assert almacen.claves_ya_triadas() == set()

        con_tope = Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(60)),
            cuantas=8, esfuerzo="low", tope_lote=60,
        ).triar(filas)
        assert con_tope.ok, "con el tope puesto la corrida tiene que salir adelante"
        assert len(almacen.claves_ya_triadas()) == 60

    def test_el_permiso_se_pide_por_el_lote_recortado(self, almacen):
        """Se le pide al techo lo que se va a gastar, no lo que hay en cola.

        Es la misma regla que arregló el 0,05 escrito a mano, aplicada al otro
        lado: ahora que el lote se recorta, pedir permiso por la cola entera
        reservaría de más y podría negar una corrida que sí cabía.
        """
        from clasificador.triaje import coste_estimado

        sembrar(almacen, 100)
        guardia = GuardiaFalso()
        Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(20)),
            cuantas=8, esfuerzo="low", presupuesto=guardia, tope_lote=20,
        ).triar(filas_de(almacen))

        _, _, por_veinte = coste_estimado(20, "low")
        _, _, por_cien = coste_estimado(100, "low")
        assert guardia.pedido[-1] == pytest.approx(por_veinte), (
            "pidió permiso por la cola entera y no por el lote que manda"
        )
        assert guardia.pedido[-1] < por_cien

    def test_sin_tope_se_manda_todo_como_antes(self, almacen):
        """`None` deja el comportamiento anterior intacto."""
        sembrar(almacen, 20)
        resultado = Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(20)), cuantas=3, tope_lote=None
        ).triar(filas_de(almacen))
        assert resultado.consideradas == 20
        assert resultado.fuera_del_tope == 0


class TestCuantasCorridasTarda:
    """`corridas_para_absorber`, y el error que corrige.

    Dividir la cola entre el tope es la cuenta de una cola congelada. Sobre las
    273 vacantes reales del 3 septiembre 2026 con tope 60 eso daba 5 corridas
    —273/60— y son 8, porque cada día entran ~25 nuevas que también hay que
    triar. El número sale en pantalla, así que equivocarlo es prometerle a la
    clienta que el atraso se va en cinco días.
    """

    def test_cuenta_las_llegadas(self):
        from clasificador.plan import corridas_para_absorber

        assert corridas_para_absorber(273, 60, llegadas=25) == 8
        assert corridas_para_absorber(273, 60, llegadas=0) == 5

    def test_una_cola_que_ya_cabe_es_una_corrida(self):
        from clasificador.plan import corridas_para_absorber

        assert corridas_para_absorber(40, 60, llegadas=25) == 1

    def test_un_tope_por_debajo_de_las_llegadas_no_drena(self):
        """Y se dice, en vez de devolver un número que no va a cumplirse."""
        from clasificador.plan import corridas_para_absorber

        assert corridas_para_absorber(273, 20, llegadas=25) == 0


# ---------------------------------------------------------------------------
#  La configuración del tope
# ---------------------------------------------------------------------------

class TestLaConfiguracionDelTope:
    def test_el_defecto_es_tener_tope(self):
        """Al revés que `activo`, y a propósito.

        `activo` viene apagado porque encendido decide a cuántas ofertas se les
        presta atención completa. Aquí el defecto peligroso es el contrario: sin
        tope, un atraso grande hace que la corrida se niegue entera y no se tríe
        ninguna oferta.
        """
        from recolector.config import _parse_triaje

        assert _parse_triaje({}, "prueba").max_vacantes_por_triaje >= 1

    def test_un_tope_menor_que_las_lecturas_falla_al_arrancar(self):
        """Las dos perillas se confunden con facilidad y el síntoma no se ve.

        Con el tope por debajo de `ofertas_por_corrida` se leerían menos
        ofertas de las pedidas y el triaje no llegaría a dispararse nunca, sin
        que nada lo dijera. Mismo patrón que la pausa de Adzuna contra su
        límite por minuto: se valida al arrancar y se dice qué número tocar.
        """
        from recolector.config import ConfigError, _parse_triaje

        with pytest.raises(ConfigError) as exc:
            _parse_triaje(
                {"ofertas_por_corrida": 8, "max_vacantes_por_triaje": 5}, "prueba"
            )
        assert "max_vacantes_por_triaje" in str(exc.value)
        assert "ofertas_por_corrida" in str(exc.value)

    def test_el_yaml_real_manda_un_lote_que_cabe(self):
        """Contra la configuración que se envía, como las listas de títulos.

        Es la comprobación que no puede vivir en `recolector/config.py`: la
        aritmética de `cabida()` es del clasificador y la configuración no
        importa hacia arriba. Así que se fija aquí, que es donde el proyecto ya
        prueba el AFINADO contra el YAML real y no contra datos inventados.
        """
        from clasificador.plan import LOTE_TIPICO
        from clasificador.triaje import cabida
        from recolector.config import load_config

        triaje = load_config().llm.triaje
        tope = triaje.max_vacantes_por_triaje

        sitio = cabida(tope, triaje.esfuerzo)
        assert sitio.cabe, (
            f"el YAML manda lotes de {tope} vacantes y a esfuerzo "
            f"'{triaje.esfuerzo}' solo caben {sitio.caben}: la corrida se "
            "negaría a sí misma y la app volvería a quedarse bloqueada"
        )
        assert tope > LOTE_TIPICO, (
            f"el tope ({tope}) tiene que ser mayor que las vacantes que entran "
            f"al día (~{LOTE_TIPICO}) o la cola no bajaría nunca, y el tope "
            "habría dejado de ser temporal para volverse un filtro permanente"
        )


# ---------------------------------------------------------------------------
#  Que los tokens del triaje se guarden en algún sitio
# ---------------------------------------------------------------------------

class TestSeApuntaLaCorrida:
    """La causa raíz de los tres errores de estimación de este proyecto.

    La constante de salida por vacante llevaba tres sesiones desviándose sin
    que nadie lo notara, y no por descuido: es que no había DÓNDE notarlo.
    `triajes` no tiene columnas de tokens y `uso_llm.json` solo agrega dólares
    por día, así que los tokens de la llamada vivían un instante en la salida de
    consola y se perdían con el scrollback.

    Volvió a pasar el 3 septiembre 2026 —una corrida real de 60 vacantes cuyo
    número hubo que reconstruir restando el coste de las lecturas al gasto del
    día— y de ahí sale `triaje_corridas`.
    """

    def test_una_corrida_deja_su_fila(self, almacen):
        sembrar(almacen, 20)
        Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(20)),
            cuantas=5, esfuerzo="low",
        ).triar(filas_de(almacen))

        corridas = almacen.corridas_de_triaje()
        assert len(corridas) == 1
        fila = corridas[0]
        assert fila["vacantes"] == 20
        assert fila["tokens_salida"] == 700
        assert fila["esfuerzo"] == "low"

    def test_guarda_lo_previsto_al_lado_de_lo_medido(self, almacen):
        """Sin las dos cifras no se puede responder «cuánto se desvió».

        Y esa es la pregunta útil, no «cuánto costó»: el número que importa es
        el que se le enseñó a la clienta antes de pulsar.
        """
        sembrar(almacen, 20)
        Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(20)),
            cuantas=5, esfuerzo="low",
        ).triar(filas_de(almacen))

        fila = almacen.corridas_de_triaje()[0]
        assert fila["salida_prevista"] > 0
        assert fila["coste_previsto"] > 0
        assert fila["coste_usd"] > 0

    def test_el_tope_queda_apuntado(self, almacen):
        """Para poder leer después una salida por vacante que cuadre.

        El denominador es lo que fue EN LA LLAMADA. Sin `fuera_del_tope` no se
        distingue una corrida de 60 sobre una cola de 60 de una de 60 sobre una
        cola de 273, y la segunda dice algo del atraso que la primera no.
        """
        sembrar(almacen, 50)
        Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(10)),
            cuantas=5, esfuerzo="low", tope_lote=10,
        ).triar(filas_de(almacen))

        fila = almacen.corridas_de_triaje()[0]
        assert fila["vacantes"] == 10
        assert fila["fuera_del_tope"] == 40

    def test_una_llamada_pagada_que_sale_mal_tambien_se_apunta(self, almacen):
        """Un registro que solo guarda los éxitos miente el día que algo falla.

        Es la misma regla que el libro de gastos, que apunta ANTES de guardar
        la clasificación. Si el modelo devuelve un triaje mal numerado, las
        ofertas no gastan su turno —eso ya se prueba aparte— pero la llamada se
        pagó, y es justo la que hay que poder mirar después.
        """
        sembrar(almacen, 20)
        # Numeración repetida: el triaje se rechaza entero.
        cliente = TriadorFalso(triaje_de((1, True), (1, True)))
        resultado = Triador(
            almacen, PREFS, cliente, cuantas=5, esfuerzo="low"
        ).triar(filas_de(almacen))

        assert not resultado.ok
        assert almacen.claves_ya_triadas() == set(), "nadie gasta su turno"
        assert len(almacen.corridas_de_triaje()) == 1, (
            "la llamada se pagó: tiene que constar aunque el triaje se rechace"
        )

    def test_una_corrida_que_no_llego_a_llamar_no_deja_fila(self, almacen):
        """Sin llamada no hay nada que apuntar, y una fila a cero mentiría."""
        sembrar(almacen, 5)
        Triador(
            almacen, PREFS, TriadorFalso(todos_merecen(5)), cuantas=8
        ).triar(filas_de(almacen))

        assert almacen.corridas_de_triaje() == []
