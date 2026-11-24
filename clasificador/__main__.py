"""Permite ejecutar el paquete directamente:

    python -m clasificador

Python busca este archivo cuando se invoca un paquete con `-m`.
"""

from .cli import main

raise SystemExit(main())
