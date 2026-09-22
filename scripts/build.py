"""Compila Sharky y comprueba el resultado (GUIA §7, H1).

    uv run python scripts/build.py

Limpia lo anterior, empaqueta con PyInstaller (modo carpeta, sin consola) y lanza la
autocomprobación del propio exe, primero sin red y después con red. Devuelve 0 solo si el
ejecutable existe y se autocomprueba sin fallos; los avisos (Yahoo lento, sin conexión) no
tumban la compilación.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
SPEC = RAIZ / "packaging" / "sharky.spec"
DIST = RAIZ / "dist"
BUILD = RAIZ / "build"
CARPETA_EXE = DIST / "Sharky"
EXE = CARPETA_EXE / "Sharky.exe"
INFORME = Path(tempfile.gettempdir()) / "sharky_selftest.txt"

log = logging.getLogger("build")


def limpiar() -> None:
    for carpeta in (BUILD, CARPETA_EXE):
        if carpeta.exists():
            log.info("Limpiando %s", carpeta)
            shutil.rmtree(carpeta)


def empaquetar() -> None:
    orden = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
    ]
    log.info("Empaquetando con PyInstaller...")
    subprocess.run(orden, cwd=RAIZ, check=True)


def tamano_total(carpeta: Path) -> int:
    return sum(f.stat().st_size for f in carpeta.rglob("*") if f.is_file())


def autocomprobar(online: bool) -> bool:
    """Lanza el exe con --selftest y enseña su informe. True si no hubo fallos."""
    if INFORME.exists():
        INFORME.unlink()
    orden = [str(EXE), "--selftest"] + (["--online"] if online else [])
    log.info("")
    log.info("Autocomprobación del ejecutable (%s)", "con red" if online else "sin red")
    resultado = subprocess.run(orden, cwd=RAIZ)
    if INFORME.exists():
        for linea in INFORME.read_text(encoding="utf-8").splitlines():
            log.info("  %s", linea)
    else:
        log.error("  El ejecutable no ha escrito %s", INFORME)
        return False
    log.info("  Código de salida: %d", resultado.returncode)
    return resultado.returncode == 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    limpiar()
    try:
        empaquetar()
    except subprocess.CalledProcessError as error:
        log.error("PyInstaller ha fallado (código %d)", error.returncode)
        return 1
    if not EXE.is_file():
        log.error("No se ha generado %s", EXE)
        return 1
    log.info("")
    log.info("Generado %s (%.0f MB en la carpeta)", EXE, tamano_total(CARPETA_EXE) / 1e6)

    sin_red = autocomprobar(online=False)
    con_red = autocomprobar(online=True)
    log.info("")
    if sin_red and con_red:
        log.info("Compilación correcta.")
        return 0
    log.error("La autocomprobación del ejecutable ha fallado: revisa packaging/sharky.spec")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
