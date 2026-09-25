"""Compila Sharky y comprueba el resultado (GUIA §7, H1 y H2).

    uv run python scripts/build.py

Limpia lo anterior, empaqueta con PyInstaller (modo carpeta, sin consola), lanza la
autocomprobación del propio exe —primero sin red y después con red— y, si todo ha ido bien,
compila el instalador con Inno Setup. Devuelve 0 solo si salen el ejecutable y el instalador
y la autocomprobación no da ningún fallo; los avisos (Yahoo lento, sin conexión) no tumban la
compilación.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
SPEC = RAIZ / "packaging" / "sharky.spec"
ISS = RAIZ / "packaging" / "sharky.iss"
PYPROJECT = RAIZ / "pyproject.toml"
DIST = RAIZ / "dist"
BUILD = RAIZ / "build"
CARPETA_EXE = DIST / "Sharky"
EXE = CARPETA_EXE / "Sharky.exe"
INFORME = Path(tempfile.gettempdir()) / "sharky_selftest.txt"

#: Donde vive ISCC.exe cuando no está en el PATH: instalación por usuario (winget) y las dos
#: de sistema (instalador normal y choco en la CI).
CARPETAS_INNO = (
    ("LOCALAPPDATA", "Programs/Inno Setup 6"),
    ("ProgramFiles(x86)", "Inno Setup 6"),
    ("ProgramFiles", "Inno Setup 6"),
)
ORDEN_WINGET = "winget install --id JRSoftware.InnoSetup -e"

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


def version_del_proyecto() -> str:
    """La versión vive solo en pyproject.toml (GUIA §6)."""
    datos = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return datos["project"]["version"]


def buscar_iscc() -> Path | None:
    """El compilador de Inno Setup: primero en el PATH, luego en las rutas habituales."""
    en_el_path = shutil.which("ISCC.exe") or shutil.which("iscc")
    if en_el_path:
        return Path(en_el_path)
    for variable, resto in CARPETAS_INNO:
        base = os.environ.get(variable)
        if not base:
            continue
        candidato = Path(base).joinpath(*resto.split("/")) / "ISCC.exe"
        if candidato.is_file():
            return candidato
    return None


def compilar_instalador(iscc: Path, version: str) -> Path:
    """Compila el instalador y devuelve su ruta. Lanza si Inno Setup falla."""
    destino = DIST / f"Sharky-Setup-{version}.exe"
    if destino.exists():
        destino.unlink()
    orden = [str(iscc), f"/DMiVersion={version}", str(ISS)]
    log.info("")
    log.info("Compilando el instalador con Inno Setup (versión %s)", version)
    resultado = subprocess.run(
        orden, cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if resultado.returncode != 0:
        for linea in (resultado.stdout or "").splitlines()[-20:]:
            log.error("  %s", linea)
        for linea in (resultado.stderr or "").splitlines()[-20:]:
            log.error("  %s", linea)
        raise subprocess.CalledProcessError(resultado.returncode, orden)
    if not destino.is_file():
        raise FileNotFoundError(f"Inno Setup no ha dejado {destino}")
    return destino


def salida_en_utf8() -> None:
    """Con la salida redirigida a un fichero o una tubería (la CI), Windows la abre en cp1252 y
    cualquier carácter de fuera («→» en el informe del selftest) rompe el registro."""
    for flujo in (sys.stdout, sys.stderr):
        if isinstance(flujo, io.TextIOWrapper):
            flujo.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    salida_en_utf8()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    version = version_del_proyecto()
    iscc = buscar_iscc()  # se busca antes de compilar: mejor enterarse ya que al final
    if iscc is None:
        log.error("No se encuentra ISCC.exe (el compilador de Inno Setup). Instálalo con:")
        log.error("    %s", ORDEN_WINGET)
        return 1
    log.info("Inno Setup: %s", iscc)

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
    if not (sin_red and con_red):
        log.info("")
        log.error("La autocomprobación del ejecutable ha fallado: revisa packaging/sharky.spec")
        log.error("No se compila el instalador con un ejecutable que no se sostiene.")
        return 1

    try:
        instalador = compilar_instalador(iscc, version)
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        log.error("El instalador no se ha podido compilar: %s", error)
        return 1
    log.info("Generado %s (%.0f MB)", instalador, instalador.stat().st_size / 1e6)

    log.info("")
    log.info("Compilación correcta.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
