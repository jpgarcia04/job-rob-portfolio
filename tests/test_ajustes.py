"""
Sus criterios, y el reloj que corre sola.

LO QUE SE VIGILA AQUÍ, Y POR QUÉ ES LO QUE MÁS DUELE SI SE ROMPE
-----------------------------------------------------------------
1. **Que guardar sus criterios no le corte las actualizaciones.** `cvs.yaml`
   está versionado y el actualizador se niega a actualizar una copia con
   cambios locales. Escribir ahí sus preferencias le costaría el único camino
   que existe hasta su portátil, a 9.000 km, en silencio y para siempre.

2. **Que apagar la corrida automática quite el reloj de verdad.** Dejar
   corriendo una tarea que ella apagó es gastar su dinero contra su decisión,
   que es el peor fallo que puede tener esta pieza.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clasificador import ajustes as aj
from clasificador.fichas import cargar_preferencias

RAIZ = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
#  Dónde se guardan
# ---------------------------------------------------------------------------

class TestNoTocanElArchivoVersionado:
    def test_se_guardan_fuera_de_git(self):
        """LA PRUEBA QUE PROTEGE EL CAMINO A SU MÁQUINA.

        `data/` está en .gitignore; `config/` no. Si esta ruta se moviera al
        YAML, la primera vez que ella tocara un criterio su árbol quedaría
        sucio y `actualizar.ps1` no volvería a actualizar nunca — sin fallar,
        sin avisar, y sin que nadie lo vea desde España.
        """
        assert aj.DEFAULT_AJUSTES_PATH.parent.name == "data"
        gitignore = (RAIZ / ".gitignore").read_text(encoding="utf-8")
        assert "data/" in gitignore

    def test_el_yaml_sigue_siendo_la_semilla(self, tmp_path: Path):
        """Sin archivo suyo, manda lo que trae la app de fábrica."""
        prefs = cargar_preferencias(ajustes=tmp_path / "no-existe.json")
        assert prefs.criterios, "el YAML trae criterios de ejemplo"

    def test_lo_suyo_manda_sobre_la_semilla(self, tmp_path: Path):
        destino = tmp_path / "preferencias.json"
        aj.guardar(
            aj.Ajustes(criterios=["Solo remoto."], cuantas_destacar=7), destino
        )

        prefs = cargar_preferencias(ajustes=destino)

        assert prefs.criterios == ["Solo remoto."]
        assert prefs.cuantas_destacar == 7

    def test_un_archivo_a_medias_no_borra_lo_que_no_trae(self, tmp_path: Path):
        """Guardar solo la hora no puede dejarla sin criterios."""
        destino = tmp_path / "preferencias.json"
        destino.write_text(json.dumps({"automatica": {"activa": True}}), encoding="utf-8")

        prefs = cargar_preferencias(ajustes=destino)

        assert prefs.criterios, "los del YAML siguen ahí"

    def test_un_archivo_ilegible_no_impide_abrir(self, tmp_path: Path):
        """Y NO SE BORRA: se cae a los de fábrica y el archivo se queda para
        poder mirarlo. Perder sus criterios en silencio es peor que ignorarlos
        una vez."""
        destino = tmp_path / "preferencias.json"
        destino.write_text("{esto no es json", encoding="utf-8")

        prefs = cargar_preferencias(ajustes=destino)

        assert prefs.criterios
        assert destino.exists()


# ---------------------------------------------------------------------------
#  Lo que ella escribe, comprobado
# ---------------------------------------------------------------------------

class TestLoQueEllaEscribe:
    def test_se_limpian_los_espacios_y_las_lineas_vacias(self):
        salida = aj.validar(
            {"criterios": ["  Solo   remoto.  ", "", "   ", "Nada de agencias."]},
            semilla=aj.Ajustes(),
        )
        assert salida.criterios == ["Solo remoto.", "Nada de agencias."]

    def test_hay_un_tope_de_criterios_y_dice_por_que(self):
        """Cada criterio viaja en el bloque de sistema de CADA llamada del
        asistente. Una caja de texto sin tope es una factura sin tope."""
        with pytest.raises(aj.AjustesError) as fallo:
            aj.validar({"criterios": ["x"] * 13}, semilla=aj.Ajustes())
        assert "12" in str(fallo.value)

    def test_un_criterio_kilometrico_se_rechaza(self):
        with pytest.raises(aj.AjustesError):
            aj.validar({"criterios": ["x" * 400]}, semilla=aj.Ajustes())

    def test_destacar_cero_no_es_un_orden(self):
        with pytest.raises(aj.AjustesError) as fallo:
            aj.validar({"cuantas_destacar": 0}, semilla=aj.Ajustes())
        assert "al menos una" in str(fallo.value)

    @pytest.mark.parametrize("mala", ["", "7", "25:00", "07:99", "siete"])
    def test_una_hora_que_no_existe_se_rechaza(self, mala):
        with pytest.raises(aj.AjustesError):
            aj.validar({"automatica": {"hora": mala}}, semilla=aj.Ajustes())

    def test_la_hora_se_normaliza(self):
        salida = aj.validar({"automatica": {"hora": "7:5"}}, semilla=aj.Ajustes())
        assert salida.automatica.hora == "07:05"

    def test_lo_que_no_viene_se_queda_como_estaba(self):
        """La pantalla manda solo lo que cambió."""
        semilla = aj.Ajustes(criterios=["Uno."], cuantas_destacar=4)
        salida = aj.validar({"automatica": {"activa": True}}, semilla=semilla)
        assert salida.criterios == ["Uno."]
        assert salida.cuantas_destacar == 4


# ---------------------------------------------------------------------------
#  El reloj
# ---------------------------------------------------------------------------

class TestLaCorridaAutomatica:
    def test_viene_apagada_de_fabrica(self):
        """Encenderla es gastar su dinero sin que esté delante. Eso lo decide
        ella con el importe a la vista, no una actualización que le llega sin
        avisar."""
        assert aj.Ajustes().automatica.activa is False

    def test_apagarla_quita_la_tarea(self, monkeypatch):
        """EL PEOR FALLO POSIBLE DE ESTA PIEZA sería dejar corriendo un reloj
        que ella apagó."""
        from bandeja import programada

        quitadas = []
        monkeypatch.setattr(programada, "quitar", lambda n=None: quitadas.append(n) or True)
        monkeypatch.setattr(programada, "poner", lambda *a, **k: pytest.fail("no toca poner"))
        monkeypatch.setattr(programada.sys, "platform", "win32")

        programada.asegurar(False, "07:00")

        assert quitadas, "con la casilla apagada hay que borrar la tarea"

    def test_encenderla_la_pone_a_su_hora(self, monkeypatch):
        from bandeja import programada

        puestas = []
        monkeypatch.setattr(programada, "poner",
                            lambda hora, *a, **k: puestas.append(hora) or None)
        monkeypatch.setattr(programada.sys, "platform", "win32")

        aviso = programada.asegurar(True, "08:15")

        assert puestas == ["08:15"]
        assert "08:15" in aviso

    def test_si_windows_se_niega_se_dice(self, monkeypatch):
        """Encender la casilla y que la tarea no se cree no puede pasar en
        silencio: ella se quedaría esperando un trabajo que nadie hace."""
        from bandeja import programada

        monkeypatch.setattr(programada, "poner", lambda *a, **k: "acceso denegado")
        monkeypatch.setattr(programada.sys, "platform", "win32")

        aviso = programada.asegurar(True, "07:00")

        assert "no se pudo programar" in aviso
        assert "acceso denegado" in aviso

    def test_el_xml_trae_lo_que_hace_que_corra_en_un_portatil(self):
        """Tres valores que no se pueden poner con banderas de `schtasks`, y
        sin los cuales la tarea no corre nunca en un portátil:

        `DisallowStartIfOnBatteries` viene en `true` por defecto en Windows —
        a las siete de la mañana un portátil está con mucha probabilidad con
        la batería, y con el defecto la tarea NO SE EJECUTA y nadie se entera.
        """
        from bandeja import programada

        xml = programada.PLANTILLA
        assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
        assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
        assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
        assert "<LogonType>InteractiveToken</LogonType>" in xml, (
            "corre solo con su sesión abierta y sin guardar ninguna contraseña"
        )

    def test_la_corrida_diaria_no_lleva_manual(self):
        """`--manual` marca lo que pide ELLA, y eso se rige por el tope por
        acción. Una corrida que decide un reloj con ella dormida tiene que ir
        contra la ración mensual de las automáticas, que sí frena en seco."""
        import importlib

        diaria = importlib.import_module("herramientas.corrida_diaria")

        for _, argv in diaria.PASOS:
            assert "--manual" not in argv

    def test_los_tres_pasos_van_en_el_orden_que_toca(self):
        import importlib

        diaria = importlib.import_module("herramientas.corrida_diaria")
        etiquetas = [e for e, _ in diaria.PASOS]

        assert etiquetas == [
            "Buscar ofertas nuevas",
            "Que el asistente lea lo nuevo",
            "Ordenar por dónde empezar",
        ], "buscar trae material, leer solo trabaja sobre lo que ya está, y el orden solo vale si incluye lo leído"
