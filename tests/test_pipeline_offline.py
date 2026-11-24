"""
Prueba de extremo a extremo del pipeline, sin red y sin API keys.

Corre las dos primeras combinaciones contra las respuestas de ejemplo y
comprueba los números que sostienen el reporte. Si alguno de estos cambia sin
querer, el entregable de la v0 estaría mintiendo.

Las cifras esperadas salen de contar a mano los ejemplos de fixtures.py.
"""

from __future__ import annotations

import pytest

from recolector.filters import DROP_AGE, DROP_GEO, DROP_GEO_UNRESOLVED, DROP_TITLE
from recolector.report import build_report

from .offline import run_offline


@pytest.fixture(scope="module")
def corrida(tmp_path_factory):
    """Una sola corrida compartida por todas las pruebas del módulo.

    scope="module" hace que el fixture se cree una vez y se reutilice, en vez
    de repetir la corrida entera en cada prueba.
    """
    tmp_dir = tmp_path_factory.mktemp("recolector")
    stats, config, profile, storage = run_offline(tmp_dir)
    yield stats, config, profile, storage
    storage.close()


class TestRecuentos:
    def test_no_hubo_errores_ni_fuentes_mudas(self, corrida):
        stats, _, _, _ = corrida
        assert stats.errors == []
        assert stats.silent_sources == []

    def test_totales_por_etapa(self, corrida):
        stats, _, _, _ = corrida
        # 9 de México (5 adzuna + 3 jooble + 1 adzuna de la 2ª combinación)
        # 7 de Texas  (4 adzuna + 3 jooble)
        assert stats.raw_total() == 16
        # Caen 6: 1 becario, 1 antigua, 2 fuera de Texas, 1 remoto sin estado
        # y —desde las zonas del 24 septiembre 2026— 1 de Dallas, que es Texas
        # pero no la parte de Texas que ella quiere.
        assert stats.kept_total() == 10
        # De las 10 apariciones, 2 pares son la misma oferta por dos fuentes.
        assert stats.unique_total() == 8
        assert stats.duplicates_collapsed() == 2

    def test_ninguna_peticion_de_red(self, corrida):
        """Todo vino de caché: la cuota de Jooble sigue intacta."""
        stats, _, _, _ = corrida
        assert sum(stats.requests_by_source.values()) == 0
        assert stats.cache_hits > 0


class TestDeduplicacionEntreFuentes:
    def test_la_misma_oferta_por_dos_fuentes_se_fusiona(self, corrida):
        stats, _, _, _ = corrida
        # "Confidencial / CDMX" en México y "Lone Star / Houston" en Texas.
        assert len(stats.shared_keys()) == 2

    def test_aporte_exclusivo_de_cada_fuente(self, corrida):
        stats, _, _, _ = corrida
        # 6 únicas de adzuna - 2 compartidas = 4; 4 de jooble - 2 = 2 (la de
        # Dallas ya no entra: ver `test_totales_por_etapa`).
        assert len(stats.exclusive_keys("adzuna")) == 4
        assert len(stats.exclusive_keys("jooble")) == 2

    def test_se_detecta_la_sub_fusion(self, corrida):
        """El par 'Grupo Energético del Norte' llega con la ubicación a
        distinta profundidad: la clave estricta no lo fusiona, y el reporte
        tiene que avisarlo en vez de contarlo como dos ofertas y callarse.
        """
        stats, _, _, _ = corrida
        assert stats.under_merge_gap() == 1


class TestFiltros:
    def test_motivos_de_descarte(self, corrida):
        stats, _, _, _ = corrida
        assert stats.drop_reasons[DROP_TITLE] == 1            # becario
        assert stats.drop_reasons[DROP_AGE] == 1              # 75 días
        assert stats.drop_reasons[DROP_GEO] == 3              # Chicago, Oklahoma, Dallas
        assert stats.drop_reasons[DROP_GEO_UNRESOLVED] == 1   # "United States"

    def test_cada_descarte_lleva_ejemplo(self, corrida):
        """Nada se descarta en silencio: todo motivo tiene ejemplos legibles."""
        stats, _, _, _ = corrida
        for reason in stats.drop_reasons:
            assert stats.drop_examples[reason], f"sin ejemplo para «{reason}»"


class TestSalario:
    def test_cubos_salariales(self, corrida):
        stats, _, _, _ = corrida
        # Se evalúan TODAS las recibidas, también las descartadas.
        assert sum(stats.salary_buckets.values()) == 16
        assert stats.salary_buckets["cumple"] == 3
        assert stats.salary_buckets["no_cumple"] == 1
        assert stats.salary_buckets["desconocido"] == 12

    def test_se_marcan_los_salarios_estimados(self, corrida):
        """Adzuna a veces predice el salario. No es un dato del anuncio."""
        stats, _, _, _ = corrida
        assert stats.salary_predicted == 1

    def test_el_umbral_en_modo_report_no_descarta_nada(self, corrida):
        stats, _, profile, _ = corrida
        assert profile.hard_filters.salary_floor is not None
        assert profile.hard_filters.salary_floor.mode == "report"
        assert "salario" not in " ".join(stats.drop_reasons)


class TestReporte:
    def test_se_genera_completo(self, corrida):
        stats, config, profile, storage = corrida
        report = build_report(stats, config, profile, storage)

        for expected in (
            "VOLUMEN POR MERCADO",
            "VOLUMEN POR COMBINACIÓN",
            "VOLUMEN POR FUENTE Y APORTE EXCLUSIVO",
            "DEDUPLICACIÓN",
            "QUÉ SE DESCARTÓ Y POR QUÉ",
            "UMBRAL SALARIAL",
            "MUESTRA DE",
            "LECTURA AUTOMÁTICA",
        ):
            assert expected in report, f"falta la sección «{expected}»"

    def test_avisa_de_que_la_corrida_es_parcial(self, corrida):
        """Se corrieron 2 de 13 combinaciones: el reporte no puede dejar que
        eso se lea como volumen real del mercado.
        """
        stats, config, profile, storage = corrida
        report = build_report(stats, config, profile, storage)
        assert "CORRIDA PARCIAL" in report

    def test_la_muestra_reparte_entre_mercados(self, corrida):
        stats, config, profile, storage = corrida
        report = build_report(stats, config, profile, storage)
        assert "Alemania ·" in report
        assert "Texas, EE.UU. ·" in report


class TestSoloUnaFuente:
    def test_corrida_solo_adzuna(self, tmp_path):
        """Es el modo recomendado al principio, para no quemar cuota de Jooble."""
        stats, _, _, storage = run_offline(tmp_path, sources=("adzuna",))
        try:
            assert stats.errors == []
            assert stats.raw_total() == 10
            assert len(stats.exclusive_keys("adzuna")) == stats.unique_total()
        finally:
            storage.close()
