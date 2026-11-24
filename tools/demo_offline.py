"""
Demo offline: imprime un reporte completo sin red, sin API keys y sin gastar
cuota, usando las respuestas de ejemplo de tests/fixtures.py.

    python tools/demo_offline.py

Sirve para ver la forma del entregable antes de tener credenciales, y para
comprobar que el pipeline entero funciona despues de tocar codigo.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# El script vive en tools/, asi que hay que anadir la raiz del proyecto al
# path para poder importar `recolector` y `tests`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from recolector.report import build_report  # noqa: E402
from tests.offline import run_offline  # noqa: E402


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    print("DEMO OFFLINE - datos de ejemplo, no son ofertas reales")
    print("=" * 88)
    print()

    # TemporaryDirectory se borra solo al salir del `with`.
    with tempfile.TemporaryDirectory(prefix="recolector-demo-") as tmp:
        stats, config, profile, storage = run_offline(Path(tmp))
        try:
            report = build_report(
                stats=stats,
                config=config,
                profile=profile,
                storage=storage,
                quota_lines=["Demo offline: no se gasto ninguna peticion."],
            )
        finally:
            storage.close()

    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
