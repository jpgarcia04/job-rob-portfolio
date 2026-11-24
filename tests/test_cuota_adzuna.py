"""
La cuota de Adzuna, con los límites REALES de su plan.

Hasta el 3 septiembre 2026 el proyecto no fingía conocerlos: los medía y
avisaba, pero nunca se plantaba. Ahora están leídos del panel de la key y son
CUATRO, no uno:

     25 hit / minute     <- ritmo, y es el que de verdad apretaba
    250 hit / day
   1000 hit / week
   2500 hit / month

Lo que se vigila aquí es lo que sale caro si falla:

  - que la ventana SEMANAL exista y sea móvil. Es la que faltaba del todo, y
    la que no se parece ni al día ni al mes;
  - que el programa se NIEGUE antes de pasarse, en vez de descubrirlo con un
    429 a mitad de corrida y un reporte incompleto;
  - que el ritmo se valide al arrancar, porque un límite por minuto no se
    controla contando después: se controla yendo más despacio.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recolector.quota import ApiBudgetExceededError, WindowedUsage

# Los números del plan Trial, escritos aquí para que estas pruebas fallen si
# alguien los cambia en el YAML sin haber vuelto a mirar el panel.
POR_DIA, POR_SEMANA, POR_MES = 250, 1000, 2500

# CUÁNTAS PETICIONES CUESTA UNA CORRIDA COMPLETA, contra el YAML que se envía.
#
# YA NO ES UNA MULTIPLICACIÓN, y ese es el cambio del 17 septiembre 2026: cada
# mercado pregunta lo suyo (`combinaciones_por_mercado`), así que son
#
#     13 en Alemania  +  21 en México  +  13 en Texas  =  47
#
# HISTORIAL, porque los tres números salen en los comentarios del YAML:
#
#   46  hasta el 4 sept 2026 ... 23 combinaciones × 2 mercados
#   26  del 4 al 17 sept ....... 13 × 2, al salir México con las 10 en español
#   47  desde el 17 sept ....... 3 mercados, cada uno con lo que le sirve
#
# Sin el reparto, esos tres mercados con las 23 combinaciones serían 69 por
# corrida —2.070 al mes, el 83% de la cuota— y 22 de ellas están medidas como
# muertas. Ver `data/reportes/2026-09-17-mexico/medicion.md`.
UNA_CORRIDA = 47


def contador(tmp_path: Path, por_dia: dict[int, int] | None = None) -> WindowedUsage:
    """Un contador con consumo repartido: `{días_atrás: peticiones}`."""
    ruta = tmp_path / "uso_adzuna.json"
    hoy = datetime.now(timezone.utc).date()
    by_day = {
        (hoy - timedelta(days=n)).strftime("%Y-%m-%d"): v
        for n, v in (por_dia or {}).items()
    }
    ruta.write_text(
        json.dumps({"by_day": by_day, "total": sum(by_day.values())}),
        encoding="utf-8",
    )
    return WindowedUsage(ruta, "adzuna")


# ---------------------------------------------------------------------------
#  La ventana semanal
# ---------------------------------------------------------------------------

class TestLaSemana:
    def test_suma_los_ultimos_siete_dias(self, tmp_path):
        uso = contador(tmp_path, {0: 46, 3: 46, 6: 46})
        assert uso.used_this_week() == 138

    def test_lo_de_hace_ocho_dias_ya_no_cuenta(self, tmp_path):
        uso = contador(tmp_path, {0: 46, 7: 500, 8: 500})
        assert uso.used_this_week() == 46

    def test_es_movil_y_no_semana_natural(self, tmp_path):
        """La lectura conservadora, y por qué.

        Adzuna dice «1000 hit / week» sin aclarar si empieza el lunes. Con
        semanas naturales, seis corridas el domingo y seis el lunes son doce en
        24 horas repartidas en dos semanas distintas — y su contador, sea cual
        sea, las vería juntas. Una ventana móvil nunca cuenta menos que una
        natural, así que si cabe aquí cabe en la suya.
        """
        uso = contador(tmp_path, {n: 100 for n in range(7)})
        assert uso.used_this_week() == 700
        assert uso.used_today() == 100, "el día sigue siendo el día"


# ---------------------------------------------------------------------------
#  Que se niegue antes de pasarse
# ---------------------------------------------------------------------------

class TestSeNiegaAntes:
    def _correr(self, uso: WindowedUsage, previstas: int = UNA_CORRIDA, **extra):
        return uso.check_before_run(
            planned=previstas,
            daily_budget=POR_DIA,
            weekly_budget=POR_SEMANA,
            monthly_budget=POR_MES,
            warn_daily_above=100,
            **extra,
        )

    def test_la_corrida_diaria_cabe_de_sobra(self, tmp_path):
        """26 al día son el 10% del tope diario y el 31% del mensual.

        Eran 46, el 18% y el 55%, hasta que se fueron las combinaciones en
        español con México (4 septiembre 2026). Ver `UNA_CORRIDA`.
        """
        uso = contador(tmp_path, {n: UNA_CORRIDA for n in range(1, 7)})
        assert self._correr(uso) == []

    def test_se_planta_al_pasar_la_semana(self, tmp_path):
        """La ventana que faltaba. Sin ella esto habría sido un 429."""
        uso = contador(tmp_path, {n: 160 for n in range(7)})   # 1120 en 7 días

        with pytest.raises(ApiBudgetExceededError) as fallo:
            self._correr(uso)
        assert "SEMANAL" in str(fallo.value)

    def test_se_planta_al_pasar_el_dia(self, tmp_path):
        uso = contador(tmp_path, {0: 240})

        with pytest.raises(ApiBudgetExceededError) as fallo:
            self._correr(uso)
        assert "DIARIO" in str(fallo.value)

    def test_force_avisa_pero_deja_pasar(self, tmp_path):
        """Deliberadamente incómodo, igual que en Jooble."""
        uso = contador(tmp_path, {0: 240})
        avisos = self._correr(uso, force=True)

        assert avisos and "--force" in avisos[0]

    def test_avisa_del_despiste_aunque_quepa(self, tmp_path):
        """Caben nueve corridas al día; varias seguidas no son normales.

        El tope diario nunca salta en la operación normal, así que si esta rama
        se apagara al declarar los números reales, se habría perdido justo el
        aviso que sirve para el día a día: «hoy van 104» significa que algo se
        corrió de más.

        CUÁNTAS CORRIDAS PREVIAS HACEN FALTA SE DERIVA DEL UMBRAL, y no se
        escribe. Con 46 por corrida bastaban dos (92 + 46 = 138); con 26 hacen
        falta tres (78 + 26 = 104). Un número fijo aquí volvería a caerse en
        cuanto cambien las combinaciones, y se caería diciendo que el aviso
        está roto cuando lo que cambió fue el tamaño de la corrida.
        """
        avisar_al_pasar = 100
        previas = -(-avisar_al_pasar // UNA_CORRIDA) - 1   # techo, menos la de ahora
        uso = contador(tmp_path, {0: UNA_CORRIDA * previas})
        avisos = self._correr(uso)

        assert UNA_CORRIDA * (previas + 1) > avisar_al_pasar, (
            "la fixture ya no pasa del umbral: esta prueba dejó de probar nada"
        )
        assert avisos, (
            f"{UNA_CORRIDA * previas} + {UNA_CORRIDA} pasa del aviso de "
            f"{avisar_al_pasar} y hay que decirlo"
        )
        assert "se corrió de más" in avisos[0]
        assert "Cabe en el tope diario" in avisos[0]

    def test_sin_topes_declarados_no_se_niega(self, tmp_path):
        """El proyecto no inventa un límite que no conoce.

        Es lo que pasaba antes de leer el panel: se cuenta y se avisa, pero
        negarse por un número que nadie ha verificado impediría correr sin
        motivo.
        """
        uso = contador(tmp_path, {0: 5000})
        avisos = uso.check_before_run(
            planned=UNA_CORRIDA,
            daily_budget=None,
            weekly_budget=None,
            monthly_budget=None,
            warn_daily_above=100,
        )
        assert avisos and "no me puedo negar" in avisos[0]


# ---------------------------------------------------------------------------
#  El ritmo, que es el que apretaba
# ---------------------------------------------------------------------------

class TestElRitmo:
    """25 peticiones por minuto no es un tope de volumen y no se cuenta.

    Con la pausa anterior de 1,0 s una corrida disparaba ~45 peticiones en el
    primer minuto: HTTP 429 a mitad de faena, reporte incompleto, y sin forma
    de distinguir «no hay mercado» de «me cortaron» — que es la conclusión
    falsa más cara del proyecto.
    """

    def test_el_yaml_real_va_a_un_ritmo_que_cabe(self):
        """Contra la configuración que se envía, no contra datos inventados."""
        from recolector.config import load_config

        adzuna = load_config().adzuna
        assert adzuna.max_calls_per_minute, "el límite de ritmo tiene que estar escrito"

        por_minuto = 60.0 / adzuna.sleep_seconds_between_calls
        assert por_minuto <= adzuna.max_calls_per_minute

    def test_una_pausa_demasiado_corta_no_deja_arrancar(self, tmp_path):
        """Se valida al cargar, no se descubre con un 429.

        Los dos números viven en el mismo bloque del YAML y los edita la misma
        persona: desincronizarlos es fácil y no da ningún síntoma hasta que la
        corrida se cae por la mitad.
        """
        import yaml

        from recolector.config import ConfigError, load_config

        datos = yaml.safe_load(
            Path("config/search_profile.yaml").read_text(encoding="utf-8")
        )
        datos["sources"]["adzuna"]["sleep_seconds_between_calls"] = 1.0
        ruta = tmp_path / "apurado.yaml"
        ruta.write_text(yaml.safe_dump(datos, allow_unicode=True), encoding="utf-8")

        with pytest.raises(ConfigError) as fallo:
            load_config(ruta)
        assert "2.40" in str(fallo.value), "tiene que decir el número que hay que escribir"


# ---------------------------------------------------------------------------
#  La aritmética de la cadencia, fijada
# ---------------------------------------------------------------------------

class TestLaCadenciaCabe:
    """La pregunta de la tarea 4, respondida con los números del panel.

    Si alguien sube `max_pages` o mete otra corrida diaria, esta prueba es la
    que avisa de que ya no cabe — antes de que lo diga la API.
    """

    def test_una_corrida_al_dia_cabe_en_el_mes(self):
        assert UNA_CORRIDA * 30 == 1410
        assert UNA_CORRIDA * 30 < POR_MES

    def test_dos_corridas_al_dia_siguen_cabiendo_pero_por_poco(self):
        """Con 46 no cabían (2.760). Con 26 sobraban (1.560). Con 47 son 2.820.

        O SEA QUE VOLVIERON A NO CABER el 17 septiembre 2026, y conviene que
        esté escrito: la holgura para dos corridas diarias era del cambio del 4
        de septiembre y se fue con la vuelta de México.

        No es un problema hoy —la cadencia es UNA al día y deja el 56%— pero sí
        lo sería el día que alguien quiera correr por la mañana y por la tarde.
        Ahí hay que pagar el plan, no apretar.
        """
        assert UNA_CORRIDA * 2 * 30 == 2820
        assert UNA_CORRIDA * 2 * 30 > POR_MES

    def test_el_perfil_real_sigue_costando_una_corrida_de_47(self):
        """Contra el YAML que se envía. Si crecen las combinaciones, salta.

        SE PREGUNTA POR `planned_queries` Y NO POR UNA MULTIPLICACIÓN. Desde el
        17 septiembre 2026 los mercados no preguntan lo mismo, así que
        `combinaciones × mercados` da un número que no es el que se cobra — y
        esta prueba existe justo para que el número que se cobra sea el que se
        cree.
        """
        from recolector.config import load_config

        config = load_config()
        perfil = config.profile(None)
        peticiones = perfil.planned_queries() * config.adzuna.max_pages

        assert peticiones == UNA_CORRIDA
        assert peticiones * 30 < POR_MES, (
            "la cadencia diaria dejó de caber en el tope mensual de Adzuna"
        )

    def test_el_reparto_por_mercado_es_el_que_se_decidio_midiendo(self):
        """Los tres números de la medición del 17 septiembre 2026.

        Si alguien mueve una combinación de sitio, esto dice cuál y a dónde, en
        vez de dejar que la cuota se entere sola a fin de mes.
        """
        from recolector.config import load_config

        perfil = load_config().profile(None)
        assert {k: len(perfil.combinations_for(k)) for k in perfil.market_keys} == {
            "alemania": 13,
            "mexico": 21,
            "texas_us": 13,
        }

    def test_sin_el_reparto_serian_69_y_el_83_por_ciento(self):
        """Lo que se ahorra, fijado: 22 peticiones por corrida, 660 al mes.

        Es el motivo entero de que `combinaciones_por_mercado` exista. Si un día
        alguien lo quita por simplificar, esta prueba dice lo que cuesta.
        """
        from recolector.config import load_config

        config = load_config()
        perfil = config.profile(None)
        todas = len({c for k in perfil.market_keys for c in perfil.combinations_for(k)})
        sin_reparto = todas * len(perfil.market_keys) * config.adzuna.max_pages

        assert todas == 23
        assert sin_reparto == 69
        assert sin_reparto - UNA_CORRIDA == 22
        assert sin_reparto * 30 / POR_MES > 0.80
