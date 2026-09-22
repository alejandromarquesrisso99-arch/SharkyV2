"""El paquete se importa y su versión es la declarada en pyproject.toml."""

import tomllib
from pathlib import Path

import sharky

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_el_paquete_se_importa_con_version():
    assert sharky.__version__
    assert sharky.__version__ != "0.0.0"


def test_la_version_coincide_con_pyproject():
    datos = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert sharky.__version__ == datos["project"]["version"]
