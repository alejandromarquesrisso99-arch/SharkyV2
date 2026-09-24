"""Las capas (CLAUDE.md y GUIA §6): core <- services <- ui, nunca al revés.

core es lógica pura: sin Qt, sin red y sin disco. services no importa nada de ui.
"""

import ast
from pathlib import Path

import pytest

import sharky

PAQUETE = Path(sharky.__file__).parent

#: Lo que core no puede importar: ni las otras capas ni nada de interfaz, red o disco.
PROHIBIDO_EN_CORE = (
    "sharky.services", "sharky.ui", "PySide6", "anthropic", "yfinance", "keyring", "sqlite3",
    "httpx", "httpx2", "socket", "urllib", "pathlib", "os", "shutil", "tempfile",
)
PROHIBIDO_EN_SERVICES = ("sharky.ui",)


def importaciones(fichero: Path) -> set[str]:
    arbol = ast.parse(fichero.read_text(encoding="utf-8"))
    nombres: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
            nombres.add(nodo.module)
    return nombres


def prohibidas(fichero: Path, prohibido: tuple[str, ...]) -> list[str]:
    return sorted(
        nombre
        for nombre in importaciones(fichero)
        if any(nombre == p or nombre.startswith(p + ".") for p in prohibido)
    )


@pytest.mark.parametrize(
    "fichero", sorted((PAQUETE / "core").glob("*.py")), ids=lambda f: f.name
)
def test_core_es_logica_pura(fichero):
    assert prohibidas(fichero, PROHIBIDO_EN_CORE) == []


@pytest.mark.parametrize(
    "fichero", sorted((PAQUETE / "services").glob("*.py")), ids=lambda f: f.name
)
def test_services_no_importa_la_interfaz(fichero):
    assert prohibidas(fichero, PROHIBIDO_EN_SERVICES) == []


def test_core_no_lee_el_reloj():
    for fichero in (PAQUETE / "core").glob("*.py"):
        texto = fichero.read_text(encoding="utf-8")
        for llamada in ("datetime.now(", "date.today(", "time.time("):
            assert llamada not in texto, f"{fichero.name} llama a {llamada}…)"
