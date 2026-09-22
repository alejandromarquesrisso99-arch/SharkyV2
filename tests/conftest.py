"""Configuración común de los tests: Qt siempre sin ventanas."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
