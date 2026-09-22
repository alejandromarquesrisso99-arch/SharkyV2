"""Configuración común de los tests: Qt sin ventanas y carpeta de datos de usar y tirar."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def carpeta_de_datos(tmp_path, monkeypatch):
    """Ningún test toca la carpeta de datos real.

    Lleva espacios y tildes a propósito: es el caso del tercer criterio del hito H1.
    """
    carpeta = tmp_path / "Datos de José"
    monkeypatch.setenv("SHARKY_DATA_DIR", str(carpeta))
    return carpeta
