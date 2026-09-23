"""Configuración común de los tests: Qt sin ventanas, carpeta de datos de usar y tirar y ni
rastro del Administrador de credenciales de verdad."""

import os

import pytest

from fakes import FakeKeyring

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def carpeta_de_datos(tmp_path, monkeypatch):
    """Ningún test toca la carpeta de datos real.

    Lleva espacios y tildes a propósito: es el caso del tercer criterio del hito H1.
    """
    carpeta = tmp_path / "Datos de José"
    monkeypatch.setenv("SHARKY_DATA_DIR", str(carpeta))
    return carpeta


@pytest.fixture(autouse=True)
def keyring_falso(monkeypatch):
    """Todos los tests usan un Administrador de credenciales en memoria (CLAUDE.md)."""
    from sharky.services import secrets, selftest

    falso = FakeKeyring()
    monkeypatch.setattr(secrets, "keyring", falso)
    monkeypatch.setattr(secrets, "configure_backend", lambda: "FakeKeyring")
    monkeypatch.setattr(selftest, "keyring", falso)
    monkeypatch.setattr(selftest, "configure_keyring", lambda: "FakeKeyring")
    return falso


@pytest.fixture
def db(carpeta_de_datos):
    """La base de datos de la carpeta de datos del test, ya migrada."""
    from sharky import paths
    from sharky.services.db import Database

    base = Database(paths.db_path())
    base.migrate()
    yield base
    base.close_all()
