"""
Pruebas de las piezas sueltas: normalización, deduplicación, geografía,
salario y parseo de fechas.

Son las cuatro cosas que, si fallan en silencio, hacen que el reporte final
mienta. Ninguna toca la red.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from recolector.config import SalaryFloor
from recolector.dedupe import loose_key, strict_key
from recolector.models import JobOffer
from recolector.salary import BELOW, MEETS, UNKNOWN, evaluate
from recolector.textnorm import (
    contains_any_term,
    normalize_company,
    normalize_location,
    normalize_title,
    strip_accents,
    strip_html,
)


def make_offer(**overrides) -> JobOffer:
    """Oferta mínima para las pruebas. Los campos que no interesan van vacíos."""
    defaults = dict(
        source="adzuna",
        market_key="mexico",
        query_label="COO + Logistics",
        external_id="1",
        title="Chief Operating Officer",
        company="Acme",
        location_raw="Ciudad de México",
        location_city=None,
        location_region=None,
        location_country="Mexico",
        url="https://example.test/1",
        description="",
    )
    defaults.update(overrides)
    return JobOffer(**defaults)


# ---------------------------------------------------------------------------
#  Normalización de texto
# ---------------------------------------------------------------------------

class TestNormalizacion:
    def test_quita_acentos(self):
        assert strip_accents("México Manufactura Ñ") == "Mexico Manufactura N"

    def test_quita_html_y_entidades(self):
        assert strip_html("<strong>COO</strong> &ndash; Infra") == "COO – Infra"

    @pytest.mark.parametrize(
        "entrada, esperado",
        [
            ("Grupo Energético del Norte S.A. de C.V.", "grupo energetico del norte"),
            ("Logística Sustentable SAPI de CV", "logistica sustentable"),
            ("Desarrollos Ávila, S. de R.L. de C.V.", "desarrollos avila"),
            ("Midwest Distribution Group Inc", "midwest distribution group"),
            ("Nationwide Grid Services LLC", "nationwide grid services"),
            ("Acme Corp Inc", "acme"),
        ],
    )
    def test_quita_formas_juridicas(self, entrada, esperado):
        assert normalize_company(entrada) == esperado

    def test_empresa_sin_forma_juridica_no_se_toca(self):
        assert normalize_company("Lone Star Logistics Partners") == (
            "lone star logistics partners"
        )

    @pytest.mark.parametrize(
        "entrada, esperado",
        [
            ("Director of Operations (m/f/d)", "director of operations"),
            ("COO [Remote] #40912", "coo"),
            ("VP Operations REQ-88213", "vp operations"),
            ("Chief Operating Officer (COO) &ndash; Logistics",
             "chief operating officer logistics"),
        ],
    )
    def test_limpia_titulos(self, entrada, esperado):
        assert normalize_title(entrada) == esperado

    @pytest.mark.parametrize(
        "alias",
        ["Ciudad de México", "CDMX", "Mexico City", "Distrito Federal"],
    )
    def test_unifica_alias_de_cdmx(self, alias):
        assert normalize_location(alias) == "ciudad de mexico"

    def test_termino_excluido_coincide_por_palabra_completa(self):
        # "junior" sí, pero no dentro de otra palabra.
        assert contains_any_term("Junior Analyst", ["junior"]) == "junior"
        assert contains_any_term("Juniores del Norte", ["junior"]) is None

    def test_termino_excluido_ignora_acentos_y_mayusculas(self):
        assert contains_any_term("PRÁCTICAS profesionales", ["prácticas"]) == "prácticas"


# ---------------------------------------------------------------------------
#  Deduplicación
# ---------------------------------------------------------------------------

class TestDeduplicacion:
    def test_misma_oferta_escrita_distinto_produce_la_misma_clave(self):
        """El caso que justifica toda la capa de normalización."""
        adzuna = make_offer(
            title="Chief Operating Officer (COO) &ndash; Logistics",
            company="Grupo Energético del Norte S.A. de C.V.",
            location_city="Ciudad de México",
        )
        jooble = make_offer(
            source="jooble",
            title="Chief Operating Officer (COO) - Logistics",
            company="Grupo Energetico del Norte",
            location_city="CDMX",
        )
        assert strict_key(adzuna) == strict_key(jooble)

    def test_distinta_ciudad_separa_la_clave_estricta_pero_no_la_laxa(self):
        """El caso de sub-fusión que el reporte mide y avisa.

        Adzuna devuelve la alcaldía ("Cuauhtémoc"), Jooble la ciudad
        ("Ciudad de México"). Son la misma oferta y la clave estricta no lo ve.
        """
        adzuna = make_offer(company="Acme", location_city="Cuauhtémoc")
        jooble = make_offer(source="jooble", company="Acme", location_city="Ciudad de México")

        assert strict_key(adzuna) != strict_key(jooble)
        assert loose_key(adzuna) == loose_key(jooble)

    def test_mismo_puesto_en_dos_ciudades_no_se_fusiona(self):
        """Y esto es correcto: son dos oportunidades reales, no un duplicado."""
        monterrey = make_offer(location_city="Monterrey")
        guadalajara = make_offer(location_city="Guadalajara")
        assert strict_key(monterrey) != strict_key(guadalajara)


# ---------------------------------------------------------------------------
#  Filtro geográfico
# ---------------------------------------------------------------------------

class TestGeografia:
    @pytest.fixture
    def texas(self):
        from recolector.config import load_config

        return load_config().market("texas_us")

    @pytest.fixture
    def alemania(self):
        from recolector.config import load_config

        return load_config().market("alemania")

    def test_alemania_acepta_todo_el_pais(self, alemania):
        """`geo_filter.mode: none` acepta cualquier ciudad del índice.

        Ocupa el sitio que tenía México, que era el otro mercado sin filtro
        regional y se borró el 4 septiembre 2026.
        """
        from recolector.geo import ACCEPTED, classify

        offer = make_offer(
            market_key="alemania",
            location_raw="Bremerhaven, Bremen", location_region="Bremen",
        )
        assert classify(offer, alemania).status == ACCEPTED

    def test_texas_acepta_por_region(self, texas):
        from recolector.geo import ACCEPTED, classify

        offer = make_offer(
            market_key="texas_us", location_region="Texas", location_city="Houston",
            location_raw="Houston, Texas",
        )
        assert classify(offer, texas).status == ACCEPTED

    def test_texas_acepta_abreviatura_en_texto_libre(self, texas):
        """Jooble devuelve 'Houston, TX' sin campo de estado."""
        from recolector.geo import ACCEPTED, classify

        offer = make_offer(market_key="texas_us", location_raw="Houston, TX",
                           location_city="Houston", location_region="TX")
        assert classify(offer, texas).status == ACCEPTED

    def test_texas_rechaza_otro_estado(self, texas):
        from recolector.geo import REJECTED, classify

        offer = make_offer(
            market_key="texas_us", location_region="Illinois",
            location_city="Chicago", location_raw="Chicago, Illinois",
        )
        assert classify(offer, texas).status == REJECTED

    def test_ubicacion_generica_queda_indeterminada(self, texas):
        """No se descarta en silencio: se marca y se cuenta aparte."""
        from recolector.geo import UNRESOLVED, classify

        offer = make_offer(market_key="texas_us", location_raw="United States")
        assert classify(offer, texas).status == UNRESOLVED


# ---------------------------------------------------------------------------
#  Umbral salarial
# ---------------------------------------------------------------------------

class TestSalario:
    @pytest.fixture
    def floor(self):
        return SalaryFloor(
            mode="report",
            amount=170000,
            currency="USD",
            period="year",
            on_missing_salary="keep",
            use_range_top=True,
            fx_to_usd={"USD": 1.0, "MXN": 0.055},
            fx_updated="2026-08-26",
        )

    def test_cumple_en_pesos(self, floor):
        # 3.800.000 MXN * 0,055 = 209.000 USD
        offer = make_offer(salary_min=3200000.0, salary_max=3800000.0, salary_currency="MXN")
        assert evaluate(offer, floor).status == MEETS

    def test_no_cumple_en_pesos(self, floor):
        # 1.800.000 MXN * 0,055 = 99.000 USD
        offer = make_offer(salary_min=1600000.0, salary_max=1800000.0, salary_currency="MXN")
        assert evaluate(offer, floor).status == BELOW

    def test_sin_salario_es_desconocido_no_incumplimiento(self, floor):
        """La distinción que evita la conclusión falsa de toda la v0."""
        assert evaluate(make_offer(), floor).status == UNKNOWN

    def test_salario_en_texto_libre_no_se_parsea(self, floor):
        """Jooble manda '$250,000 - $300,000 por mes' (pesos al MES).

        Un parser ingenuo lo leería como 250.000 USD al año y diría que cumple
        de sobra. Son ~198.000 USD anuales, pero por pura casualidad: el mismo
        texto con otra divisa o periodicidad daría cualquier cosa. Por eso NO
        se parsea y se cuenta como desconocido.
        """
        offer = make_offer(salary_raw="$250,000 - $300,000 por mes")
        decision = evaluate(offer, floor)
        assert decision.status == UNKNOWN
        assert "texto libre" in decision.reason

    def test_divisa_desconocida_no_se_inventa(self, floor):
        offer = make_offer(salary_min=500000.0, salary_max=500000.0, salary_currency="JPY")
        assert evaluate(offer, floor).status == UNKNOWN

    def test_usa_el_techo_del_rango(self, floor):
        """150k-200k cuenta como que cumple: a este nivel se negocia."""
        offer = make_offer(salary_min=150000.0, salary_max=200000.0, salary_currency="USD")
        assert evaluate(offer, floor).status == MEETS


# ---------------------------------------------------------------------------
#  Fechas
# ---------------------------------------------------------------------------

class TestFechas:
    def test_formato_de_adzuna(self):
        from recolector.connectors.base import parse_datetime_loose

        parsed = parse_datetime_loose("2026-08-01T10:12:33Z")
        assert parsed == datetime(2026, 8, 1, 10, 12, 33, tzinfo=timezone.utc)

    def test_formato_de_jooble_con_siete_decimales(self):
        """`datetime.fromisoformat` no acepta 7 decimales de segundo."""
        from recolector.connectors.base import parse_datetime_loose

        parsed = parse_datetime_loose("2026-08-01T10:12:33.0000000")
        assert parsed is not None
        assert parsed.year == 2026 and parsed.month == 8 and parsed.day == 1
        assert parsed.tzinfo is not None  # siempre con zona horaria

    def test_fecha_ilegible_devuelve_none_y_no_inventa_hoy(self):
        """Inventar `now()` haría pasar por reciente una oferta de fecha
        desconocida, y el filtro de antigüedad dejaría de significar nada.
        """
        from recolector.connectors.base import parse_datetime_loose

        assert parse_datetime_loose("ayer por la tarde") is None
        assert parse_datetime_loose(None) is None
        assert parse_datetime_loose("") is None
