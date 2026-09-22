"""Punto de entrada del ejecutable (PyInstaller).

Imports absolutos a propósito (GUIA §3): un `__main__.py` con imports relativos no funciona
dentro del exe. Y antes de importar nada, los flujos de salida: en una aplicación sin consola
`sys.stdout` y `sys.stderr` valen None y la primera escritura revienta.
"""

import os
import sys

_nulo = None
for _nombre in ("stdout", "stderr", "__stdout__", "__stderr__"):
    if getattr(sys, _nombre, None) is None:
        if _nulo is None:
            _nulo = open(os.devnull, "w", encoding="utf-8")
        setattr(sys, _nombre, _nulo)

from sharky.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
