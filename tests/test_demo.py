"""La demo pública (`tools/demo.py`) monta su base sin red ni API keys, y cada
oferta inventada sobrevive al pipeline real y llega clasificada a la bandeja."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import demo  # noqa: E402
from recolector.storage import Storage  # noqa: E402
from tests import fixtures  # noqa: E402


def test_la_demo_monta_su_base_y_clasifica_todo(tmp_path):
    antes = dict(fixtures.PAYLOADS)
    base = demo.preparar(tmp_path)

    with Storage(base) as almacen:
        empresas = {
            fila["company"]
            for fila in almacen.connection.execute("SELECT company FROM offers")
        }
        sin_veredicto = almacen.connection.execute(
            """SELECT COUNT(*) FROM offers o
               LEFT JOIN clasificaciones c USING (dedupe_key)
               WHERE c.dedupe_key IS NULL"""
        ).fetchone()[0]
        assert almacen.ultimo_ranking() is not None

    assert empresas == set(demo.VEREDICTOS)
    assert sin_veredicto == 0
    # No deja tocadas las respuestas de ejemplo del resto de pruebas.
    assert fixtures.PAYLOADS == antes
