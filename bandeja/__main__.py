"""Permite `python -m bandeja`."""

import sys

from .server import main

raise SystemExit(main(sys.argv[1:]))
