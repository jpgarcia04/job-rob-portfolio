"""
Releer lo que se juzgó a ciegas.

EL RIESGO DE ESTA CAPA NO ES QUE NO FUNCIONE, ES QUE FUNCIONE DE MÁS. Releer
una oferta ya clasificada toca tres cosas que el proyecto se ha pasado tres
sesiones protegiendo:

  1. el TURNO del triaje — cada oferta tiene uno y solo uno, y devolvérselo a
     33 vacantes las resucita en una cola que drena a 35 por corrida;
  2. el TOPE POR ACCIÓN — releer 33 no cabe en $0,60, y la salida es partirlo,
     nunca subirlo;
  3. los HERMANOS — un veredicto que se rehace y no se propaga deja filas que
     dicen `heredada_de = X` afirmando algo distinto de lo que dice X. Pasó de
     verdad: 10 filas así el 3 septiembre 2026.

Así que casi todas estas pruebas persiguen un daño colateral, no un resultado.
Ninguna toca la red: el `Motor` recibe el cliente por parámetro.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clasificador.motor import (
    VIA_ENTERA,
    VIA_RECORTE,
    Motor,
    descripcion_via_de,
)
from clasificador.plan import plan_de_relectura
from recolector.storage import Storage

from .test_clasificador import (  # noqa: F401
    TEXTO_ES,
    ClienteFalso,
    almacen,
    fichas_de_prueba,
    oferta_en_base,
    respuesta_ok,
)

# Un anuncio entero de verdad mide miles de caracteres; el recorte del plan
# Trial mide 500 exactos. Se usan longitudes realistas porque el estimador
# cobra por ellas y el troceo depende del importe.
ENTERA = "Dirección de operaciones en logística energética. " * 120


def _clasificar(almacen, fichas, filas, cliente=None):
    cliente = cliente or ClienteFalso(respuesta_ok())
    return Motor(almacen, fichas, cliente).clasificar(filas)


def _via_en_base(almacen, clave: str) -> str | None:
    fila = almacen.connection.execute(
        "SELECT descripcion_via FROM clasificaciones WHERE dedupe_key = ?",
        (clave,),
    ).fetchone()
    return fila["descripcion_via"] if fila else None


# ---------------------------------------------------------------------------
#  1a. La fila recuerda con qué texto juzgó
# ---------------------------------------------------------------------------

class TestLaFilaRecuerdaConQueJuzgo:
    """Sin esto, «obsoleta» solo puede significar «cambiaron las fichas»."""

    def test_con_el_recorte_lo_dice(self, almacen):
        fichas = fichas_de_prueba()
        clave = oferta_en_base(almacen, "run-1", description=TEXTO_ES)
        filas = almacen.ofertas_pendientes(fichas_huella=fichas.huella)

        _clasificar(almacen, fichas, filas)

        assert _via_en_base(almacen, clave) == VIA_RECORTE

    def test_con_el_anuncio_entero_lo_dice(self, almacen):
        fichas = fichas_de_prueba()
        clave = oferta_en_base(almacen, "run-1", description=TEXTO_ES)
        almacen.guardar_descripcion_completa(clave, ENTERA, "json-ld")
        filas = almacen.ofertas_pendientes(fichas_huella=fichas.huella)

        _clasificar(almacen, fichas, filas)

        assert _via_en_base(almacen, clave) == VIA_ENTERA

    def test_guarda_los_caracteres_que_de_verdad_entraron(self, almacen):
        """La MEDIDA, aparte del significado. Es lo que permite reconstruir
        qué predijo el estimador en esta fila."""
        fichas = fichas_de_prueba()
        clave = oferta_en_base(almacen, "run-1", description=TEXTO_ES)
        almacen.guardar_descripcion_completa(clave, ENTERA, "json-ld")
        filas = almacen.ofertas_pendientes(fichas_huella=fichas.huella)

        _clasificar(almacen, fichas, filas)

        fila = almacen.connection.execute(
            "SELECT caracteres_descripcion FROM clasificaciones WHERE dedupe_key = ?",
            (clave,),
        ).fetchone()
        assert fila["caracteres_descripcion"] == len(ENTERA.strip())

    def test_las_dos_lecturas_no_pueden_discrepar(self, almacen):
        """`descripcion_de` y `descripcion_via_de` salen de la MISMA decisión.

        Si fueran dos condiciones escritas por separado, la fila podría decir
        que juzgó con el anuncio entero mientras el prompt llevaba el recorte.
        """
        from clasificador.motor import descripcion_de

        clave = oferta_en_base(almacen, "run-1", description=TEXTO_ES)
        fila = almacen.get_offer(clave)
        assert descripcion_via_de(fila) == VIA_RECORTE
        assert descripcion_de(fila) == TEXTO_ES.strip()

        almacen.guardar_descripcion_completa(clave, ENTERA, "json-ld")
        fila = almacen.get_offer(clave)
        assert descripcion_via_de(fila) == VIA_ENTERA
        assert descripcion_de(fila) == ENTERA.strip()

    def test_un_texto_entero_vacio_no_cuenta_como_entero(self, almacen):
        """`description_full_via = 'nada'` guarda texto vacío: se intentó y la
        página no lo tiene. Eso NO es haber juzgado con el anuncio entero."""
        clave = oferta_en_base(almacen, "run-1", description=TEXTO_ES)
        almacen.guardar_descripcion_completa(clave, "", "nada")

        assert descripcion_via_de(almacen.get_offer(clave)) == VIA_RECORTE


# ---------------------------------------------------------------------------
#  1b. Existe un camino para releer, y encuentra lo que hay que releer
# ---------------------------------------------------------------------------

class TestQueSeRelee:

    def test_una_juzgada_con_el_recorte_entra_cuando_llega_el_texto(self, almacen):
        fichas = fichas_de_prueba()
        clave = oferta_en_base(almacen, "run-1", description=TEXTO_ES)
        _clasificar(almacen, fichas, almacen.ofertas_pendientes(fichas.huella))

        # Todavía no hay anuncio entero: no hay nada que releer.
        assert almacen.contar_juzgadas_a_ciegas() == 0

        almacen.guardar_descripcion_completa(clave, ENTERA, "json-ld")

        assert almacen.contar_juzgadas_a_ciegas() == 1
        assert [f["dedupe_key"] for f in almacen.ofertas_juzgadas_a_ciegas()] == [clave]

    def test_una_ya_juzgada_con_el_entero_no_se_vuelve_a_pagar(self, almacen):
        """La caché sigue siendo la caché. Releer no es reclasificar todo."""
        fichas = fichas_de_prueba()
        clave = oferta_en_base(almacen, "run-1", description=TEXTO_ES)
        almacen.guardar_descripcion_completa(clave, ENTERA, "json-ld")
        _clasificar(almacen, fichas, almacen.ofertas_pendientes(fichas.huella))

        assert almacen.contar_juzgadas_a_ciegas() == 0
        assert almacen.ofertas_juzgadas_a_ciegas() == []

    def test_sin_anuncio_entero_no_hay_nada_que_releer(self, almacen):
        """Las 4 de la base real que no tienen texto: releerlas daría el mismo
        veredicto y costaría lo mismo. No entran."""
        fichas = fichas_de_prueba()
        oferta_en_base(almacen, "run-1", description=TEXTO_ES)
        _clasificar(almacen, fichas, almacen.ofertas_pendientes(fichas.huella))

        assert almacen.contar_juzgadas_a_ciegas() == 0


# ---------------------------------------------------------------------------
#  1c. Y no se lleva por delante el triaje
# ---------------------------------------------------------------------------

class TestNoTocaElTriaje:
    """La regla que el prompt pidió por escrito: si sale natural «y de paso
    les devuelvo el turno», eso resucita 33 vacantes en la cola."""

    def test_releer_no_escribe_ni_una_fila_en_triajes(self, almacen):
        fichas = fichas_de_prueba()
        clave = oferta_en_base(almacen, "run-1", description=TEXTO_ES)
        _clasificar(almacen, fichas, almacen.ofertas_pendientes(fichas.huella))
        almacen.guardar_descripcion_completa(clave, ENTERA, "json-ld")

        antes = almacen.connection.execute(
            "SELECT COUNT(*) AS n FROM triajes"
        ).fetchone()["n"]

        _clasificar(almacen, fichas, almacen.ofertas_juzgadas_a_ciegas())

        despues = almacen.connection.execute(
            "SELECT COUNT(*) AS n FROM triajes"
        ).fetchone()["n"]
        assert despues == antes

    def test_no_devuelve_el_turno_gastado(self, almacen):
        """Una oferta que ya tuvo su turno lo sigue teniendo gastado después
        de releerla. Si se lo devolviéramos, volvería a competir por un hueco
        del lote que le toca a una vacante que no ha mirado nadie."""
        fichas = fichas_de_prueba()
        clave = oferta_en_base(almacen, "run-1", description=TEXTO_ES)
        almacen.guardar_triaje([{
            "dedupe_key": clave, "posicion": 1, "merece_lectura": 1,
            "seleccionada": 1, "por_que": "encaja", "modelo": "falso",
        }])
        _clasificar(almacen, fichas, almacen.ofertas_pendientes(fichas.huella))
        almacen.guardar_descripcion_completa(clave, ENTERA, "json-ld")

        assert clave in almacen.claves_ya_triadas()
        _clasificar(almacen, fichas, almacen.ofertas_juzgadas_a_ciegas())
        assert clave in almacen.claves_ya_triadas()

    def test_lo_que_se_relee_no_pasa_por_claves_ya_triadas(self, almacen):
        """LA PRUEBA DE QUE EL CAMINO SIRVE PARA ALGO.

        Es el fallo medido del 3 septiembre 2026: con `--reclasificar`, de 295
        pendientes solo 1 llegaba al motor porque `claves_ya_triadas` excluía
        todo lo demás. Si releer volviera a pasar por ahí, esta lista saldría
        vacía y el comando no haría nada mientras dice que sí.
        """
        fichas = fichas_de_prueba()
        clave = oferta_en_base(almacen, "run-1", description=TEXTO_ES)
        almacen.guardar_triaje([{
            "dedupe_key": clave, "posicion": 1, "merece_lectura": 1,
            "seleccionada": 1, "por_que": "encaja", "modelo": "falso",
        }])
        _clasificar(almacen, fichas, almacen.ofertas_pendientes(fichas.huella))
        almacen.guardar_descripcion_completa(clave, ENTERA, "json-ld")

        # La oferta ESTÁ en las ya triadas...
        assert clave in almacen.claves_ya_triadas()
        # ...y aun así la relectura la ve.
        assert [f["dedupe_key"] for f in almacen.ofertas_juzgadas_a_ciegas()] == [clave]


# ---------------------------------------------------------------------------
#  Los hermanos: el fallo de las 10 filas huérfanas
# ---------------------------------------------------------------------------

class TestLosHermanosSeQuedanAlDia:

    def _vacante_de_dos(self, almacen, fichas):
        """Dos anuncios de la misma vacante: misma empresa y título, otra
        ciudad. `agrupar_para_ahorrar` los une por `loose_key`.

        Devuelve (representante, heredero) preguntándoselo a la BASE y no
        suponiéndolo: a quién le toca representar lo decide el orden de
        `ofertas_pendientes`, no el orden en que esta prueba los creó.
        """
        oferta_en_base(
            almacen, "run-1", external_id="1",
            location_raw="Ciudad de México", location_city="Ciudad de México",
            description=TEXTO_ES,
        )
        oferta_en_base(
            almacen, "run-1", external_id="2",
            location_raw="Monterrey", location_city="Monterrey",
            description=TEXTO_ES,
        )
        _clasificar(almacen, fichas, almacen.ofertas_pendientes(fichas.huella))

        filas = almacen.connection.execute(
            "SELECT dedupe_key, heredada_de FROM clasificaciones"
        ).fetchall()
        representante = next(f["dedupe_key"] for f in filas if f["heredada_de"] is None)
        heredero = next(f["dedupe_key"] for f in filas if f["heredada_de"] is not None)
        assert heredero != representante
        return representante, heredero

    def test_al_releer_la_vacante_el_hermano_se_reescribe(self, almacen):
        """Si solo se reescribiera al representante, el hermano quedaría
        diciendo `heredada_de = padre` y a la vez otra cosa que el padre."""
        fichas = fichas_de_prueba()
        padre, hijo = self._vacante_de_dos(almacen, fichas)
        for clave in (padre, hijo):
            almacen.guardar_descripcion_completa(clave, ENTERA, "json-ld")

        filas = almacen.ofertas_juzgadas_a_ciegas()
        assert {f["dedupe_key"] for f in filas} == {padre, hijo}, (
            "el hermano tiene que venir en la lista o no se le reescribe"
        )

        cliente = ClienteFalso(respuesta_ok(confianza=0.95))
        _clasificar(almacen, fichas, filas, cliente=cliente)

        assert cliente.llamadas == 1, "una llamada por VACANTE, no por anuncio"
        for clave in (padre, hijo):
            assert _via_en_base(almacen, clave) == VIA_ENTERA

    def test_propagar_pone_al_dia_a_un_hermano_atrasado(self, almacen):
        """El caso real: el padre se releyó con un script suelto y el hijo se
        quedó con el veredicto viejo."""
        fichas = fichas_de_prueba()
        padre, hijo = self._vacante_de_dos(almacen, fichas)

        # Se rehace SOLO al padre, como hizo el script de la sesión pasada.
        almacen.guardar_descripcion_completa(padre, ENTERA, "json-ld")
        _clasificar(
            almacen, fichas, [almacen.get_offer(padre)],
            cliente=ClienteFalso(respuesta_ok(confianza=0.95)),
        )

        def confianza(clave):
            return almacen.connection.execute(
                "SELECT confianza FROM clasificaciones WHERE dedupe_key = ?",
                (clave,),
            ).fetchone()["confianza"]

        assert confianza(padre) == pytest.approx(0.95)
        assert confianza(hijo) == pytest.approx(0.9), "el hijo se quedó atrás"

        assert almacen.propagar_veredictos_heredados() == 1
        assert confianza(hijo) == pytest.approx(0.95)

    def test_propagar_es_idempotente(self, almacen):
        """Un segundo pase no toca nada. Si tocara, el recuento que se le
        enseña dejaría de significar «esto estaba roto»."""
        fichas = fichas_de_prueba()
        padre, _ = self._vacante_de_dos(almacen, fichas)
        almacen.guardar_descripcion_completa(padre, ENTERA, "json-ld")
        _clasificar(
            almacen, fichas, [almacen.get_offer(padre)],
            cliente=ClienteFalso(respuesta_ok(confianza=0.95)),
        )

        assert almacen.propagar_veredictos_heredados() == 1
        assert almacen.propagar_veredictos_heredados() == 0

    def test_propagar_no_inventa_gasto(self, almacen):
        """Una heredada no costó nada y el resumen tiene que decir la verdad."""
        fichas = fichas_de_prueba()
        padre, hijo = self._vacante_de_dos(almacen, fichas)
        almacen.guardar_descripcion_completa(padre, ENTERA, "json-ld")
        _clasificar(
            almacen, fichas, [almacen.get_offer(padre)],
            cliente=ClienteFalso(respuesta_ok(confianza=0.95)),
        )
        almacen.propagar_veredictos_heredados()

        fila = almacen.connection.execute(
            "SELECT tokens_entrada, tokens_salida, coste_usd, heredada_de "
            "FROM clasificaciones WHERE dedupe_key = ?", (hijo,),
        ).fetchone()
        assert fila["tokens_entrada"] == 0
        assert fila["tokens_salida"] == 0
        assert fila["coste_usd"] == 0
        assert fila["heredada_de"] == padre


# ---------------------------------------------------------------------------
#  El dinero: se parte, no se sube el tope
# ---------------------------------------------------------------------------

class TestElTopePorAccionParteElTrabajo:

    def _muchas_ciegas(self, almacen, fichas, cuantas: int):
        """`cuantas` VACANTES, cada una con DOS anuncios.

        Los hermanos no son decorado: sin ellos, recortar por anuncios y
        recortar por vacantes dan lo mismo y `test_limite_cuenta_vacantes_y_no
        _anuncios` no probaría nada. Es justo el fallo que dejó a `--limite N`
        sin servir para el atraso del triaje.
        """
        claves = []
        for i in range(cuantas):
            for ciudad in ("Ciudad de México", "Monterrey"):
                claves.append(oferta_en_base(
                    almacen, "run-1", external_id=f"{i}-{ciudad}",
                    title=f"Director de Operaciones {i}",
                    location_raw=ciudad, location_city=ciudad,
                    description=TEXTO_ES,
                ))
        _clasificar(almacen, fichas, almacen.ofertas_pendientes(fichas.huella))
        for clave in claves:
            almacen.guardar_descripcion_completa(clave, ENTERA, "json-ld")
        return claves

    def test_lo_que_no_cabe_se_queda_para_la_siguiente(self, almacen):
        fichas = fichas_de_prueba()
        self._muchas_ciegas(almacen, fichas, 20)

        plan = plan_de_relectura(almacen, fichas, tope_por_accion_usd=0.10)

        assert plan.vacantes == 20
        assert 0 < plan.esta_accion < 20
        assert plan.fuera_de_esta_accion == 20 - plan.esta_accion
        assert plan.coste_usd <= 0.10

    def test_el_plan_nunca_se_pasa_del_tope(self, almacen):
        """La promesa: ningún botón se lleva una cantidad que no se vio."""
        fichas = fichas_de_prueba()
        self._muchas_ciegas(almacen, fichas, 20)

        for tope in (0.05, 0.10, 0.25, 0.50):
            plan = plan_de_relectura(almacen, fichas, tope_por_accion_usd=tope)
            if plan.esta_accion > 1:
                assert plan.coste_usd <= tope, f"se pasó del tope de ${tope}"

    def test_una_sola_vacante_pasa_aunque_no_quepa(self, almacen):
        """Si ni una cabe, se ofrece una igualmente: el guardia la rechazará y
        dirá por qué. Devolver cero sería dejar la app bloqueada sin decirlo,
        que es el fallo que ya costó un HTTP 409 en bucle."""
        fichas = fichas_de_prueba()
        self._muchas_ciegas(almacen, fichas, 3)

        plan = plan_de_relectura(almacen, fichas, tope_por_accion_usd=0.0001)

        assert plan.esta_accion == 1
        assert plan.fuera_de_esta_accion == 2

    def test_limite_cuenta_vacantes_y_no_anuncios(self, almacen):
        """`--limite` sobre anuncios es lo que dejó a `--limite N` sin servir
        para el atraso del triaje: la ventana se llenaba de hermanos."""
        fichas = fichas_de_prueba()
        self._muchas_ciegas(almacen, fichas, 10)

        plan = plan_de_relectura(almacen, fichas, tope_por_accion_usd=99.0, limite=4)

        assert plan.vacantes == 10
        assert plan.esta_accion == 4, "4 VACANTES, no los 4 primeros anuncios"
        # 4 vacantes de dos anuncios cada una: los hermanos vienen incluidos
        # porque también hay que reescribirles el veredicto.
        assert len(plan.claves) == 8

    def test_el_plan_dice_exactamente_que_filas_costeo(self, almacen):
        """El comando lee ESAS y no vuelve a consultar: si eligiera otras, el
        importe confirmado y el cobrado dejarían de ser el mismo."""
        fichas = fichas_de_prueba()
        self._muchas_ciegas(almacen, fichas, 6)

        plan = plan_de_relectura(almacen, fichas, tope_por_accion_usd=99.0, limite=3)

        # 3 vacantes de dos anuncios cada una: representantes Y hermanos.
        assert plan.esta_accion == 3
        assert len(plan.claves) == 6
        filas = almacen.ofertas_por_clave(plan.claves)
        assert [f["dedupe_key"] for f in filas] == list(plan.claves)

    def test_sin_nada_que_releer_no_hay_trabajo(self, almacen):
        fichas = fichas_de_prueba()
        plan = plan_de_relectura(almacen, fichas, tope_por_accion_usd=0.60)

        assert plan.vacantes == 0
        assert not plan.hay_trabajo
        assert plan.coste_usd == 0.0


# ---------------------------------------------------------------------------
#  La derivación de las filas viejas
# ---------------------------------------------------------------------------

class TestDerivarLoQueYaEstabaEscrito:
    """Las 86 filas de la base real se escribieron sin estas columnas. No se
    rellenan adivinando: la fecha lo decide sin margen."""

    def test_juzgada_antes_de_que_llegara_el_texto_es_recorte(self, tmp_path: Path):
        with Storage(tmp_path / "p.db") as db:
            fichas = fichas_de_prueba()
            clave = oferta_en_base(db, "run-1", description=TEXTO_ES)
            _clasificar(db, fichas, db.ofertas_pendientes(fichas.huella))
            db.guardar_descripcion_completa(clave, ENTERA, "json-ld")
            # Se borra la columna, como si la fila fuera de antes.
            db.connection.execute(
                "UPDATE clasificaciones SET descripcion_via = NULL, "
                "caracteres_descripcion = NULL"
            )
            db.connection.commit()

        # Al reabrir, la migración deriva.
        with Storage(tmp_path / "p.db") as db:
            assert _via_en_base(db, clave) == VIA_RECORTE

    def test_el_mismo_instante_no_prueba_que_lo_leyera(self, tmp_path: Path):
        """Empate de marcas de tiempo: se deriva RECORTE, no ENTERA.

        POR QUÉ EXISTE ESTA PRUEBA (7 septiembre 2026). La comparación era
        `clasificado_at >= description_full_at`, así que un empate se leía como
        «lo leyó entero». En Windows el reloj de `datetime.now()` avanza a
        saltos de milisegundos, y la prueba de aquí arriba —que clasifica y
        acto seguido guarda el texto— empataba de vez en cuando y se ponía roja
        sola. Una prueba que falla una vez de cada tantas se acaba ignorando, y
        entonces deja de proteger nada.

        El empate además no es solo un problema de pruebas: significa que no se
        sabe cuál de las dos cosas pasó primero, y de las dos lecturas posibles
        la que no inventa evidencia es la de recorte. Mismo criterio que
        `test_no_se_deriva_lo_que_no_se_puede`.
        """
        with Storage(tmp_path / "p.db") as db:
            fichas = fichas_de_prueba()
            clave = oferta_en_base(db, "run-1", description=TEXTO_ES)
            _clasificar(db, fichas, db.ofertas_pendientes(fichas.huella))
            db.guardar_descripcion_completa(clave, ENTERA, "json-ld")
            # El empate, forzado: las dos marcas exactamente iguales.
            db.connection.execute(
                "UPDATE clasificaciones SET descripcion_via = NULL, "
                "caracteres_descripcion = NULL, "
                "clasificado_at = (SELECT description_full_at FROM offers "
                "                   WHERE dedupe_key = clasificaciones.dedupe_key)"
            )
            db.connection.commit()

        with Storage(tmp_path / "p.db") as db:
            assert _via_en_base(db, clave) == VIA_RECORTE

    def test_no_se_deriva_lo_que_no_se_puede(self, tmp_path: Path):
        """Texto entero sin fecha de llegada: no hay evidencia. Se deja NULL,
        porque una procedencia equivocada se lee como un dato."""
        with Storage(tmp_path / "p.db") as db:
            fichas = fichas_de_prueba()
            clave = oferta_en_base(db, "run-1", description=TEXTO_ES)
            _clasificar(db, fichas, db.ofertas_pendientes(fichas.huella))
            db.guardar_descripcion_completa(clave, ENTERA, "json-ld")
            db.connection.execute(
                "UPDATE clasificaciones SET descripcion_via = NULL, "
                "caracteres_descripcion = NULL"
            )
            db.connection.execute(
                "UPDATE offers SET description_full_at = NULL"
            )
            db.connection.commit()

        with Storage(tmp_path / "p.db") as db:
            assert _via_en_base(db, clave) is None


# ---------------------------------------------------------------------------
#  El espejo del estimador de lecturas
# ---------------------------------------------------------------------------

class TestElEspejoDeLasLecturas:
    """`triaje_corridas` existe porque la constante del triaje se desvió tres
    sesiones sin que hubiera dónde notarlo. Esto es lo mismo para las lecturas,
    y se apoya en `caracteres_descripcion`: sin ella no se sabe qué constante
    le tocaba a cada fila."""

    def test_una_lectura_pagada_aparece_con_su_via(self, almacen):
        fichas = fichas_de_prueba()
        clave = oferta_en_base(almacen, "run-1", description=TEXTO_ES)
        almacen.guardar_descripcion_completa(clave, ENTERA, "json-ld")
        _clasificar(almacen, fichas, almacen.ofertas_pendientes(fichas.huella))

        medidas = almacen.lecturas_medidas()

        assert len(medidas) == 1
        assert medidas[0]["descripcion_via"] == VIA_ENTERA
        assert medidas[0]["tokens_salida"] == 400
        assert medidas[0]["caracteres_descripcion"] == len(ENTERA.strip())

    def test_las_heredadas_no_hunden_la_media(self, almacen):
        """Tienen los tokens a cero POR DISEÑO, porque nadie las leyó. Si
        entraran, la media por lectura bajaría con cada anuncio hermano y la
        constante se 'corregiría' hacia un número inventado."""
        fichas = fichas_de_prueba()
        for ciudad in ("Ciudad de México", "Monterrey", "Guadalajara"):
            clave = oferta_en_base(
                almacen, "run-1", external_id=ciudad,
                location_raw=ciudad, location_city=ciudad,
                description=TEXTO_ES,
            )
            almacen.guardar_descripcion_completa(clave, ENTERA, "json-ld")
        _clasificar(almacen, fichas, almacen.ofertas_pendientes(fichas.huella))

        total = almacen.connection.execute(
            "SELECT COUNT(*) AS n FROM clasificaciones"
        ).fetchone()["n"]
        assert total == 3, "tres anuncios, una sola vacante"

        medidas = almacen.lecturas_medidas()
        assert len(medidas) == 1, "solo la que se pagó de verdad"
        assert all(m["tokens_salida"] > 0 for m in medidas)

    def test_una_lectura_fallida_no_cuenta_como_medida(self, almacen):
        """`sin_clasificar` puede traer la salida truncada por un error, y eso
        no es una lectura completa: promediarla desviaría la constante."""
        from clasificador.cliente_llm import RespuestaLLM

        fichas = fichas_de_prueba()
        clave = oferta_en_base(almacen, "run-1", description=TEXTO_ES)
        almacen.guardar_descripcion_completa(clave, ENTERA, "json-ld")
        rota = RespuestaLLM(
            clasificacion=None, tokens_entrada=900, tokens_salida=12,
            modelo="modelo-de-prueba", error="la respuesta vino cortada",
        )
        _clasificar(
            almacen, fichas, almacen.ofertas_pendientes(fichas.huella),
            cliente=ClienteFalso(rota),
        )

        assert almacen.lecturas_medidas() == []

    def test_sin_lecturas_no_se_inventa_una_media(self, almacen):
        assert almacen.lecturas_medidas() == []
