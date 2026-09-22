"""Rutas del programa: carpeta de datos del usuario y recursos del paquete.

Todo son rutas absolutas (GUIA §3: al arrancar con Windows, el directorio de trabajo no es
el del programa). La carpeta de datos no se cachea nunca: `SHARKY_DATA_DIR` puede cambiar
entre llamadas y los tests la mueven.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Sharky"
DATA_DIR_ENV = "SHARKY_DATA_DIR"
ICON_FILE = "sharky.ico"


def data_dir() -> Path:
    r"""Carpeta de datos: `SHARKY_DATA_DIR` si está definida, si no `%LOCALAPPDATA%\Sharky`."""
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser().absolute()
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if base:
        return Path(base).absolute() / APP_NAME
    # Fuera de Windows (solo desarrollo): el equivalente razonable.
    return Path.home().absolute() / ".local" / "share" / APP_NAME


def logs_dir() -> Path:
    return data_dir() / "logs"


def backups_dir() -> Path:
    return data_dir() / "backups"


def cache_dir() -> Path:
    return data_dir() / "cache"


def db_path() -> Path:
    return data_dir() / "sharky.db"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def log_path() -> Path:
    return logs_dir() / "sharky.log"


def ensure_data_dirs() -> Path:
    """Crea la carpeta de datos y sus subcarpetas. Devuelve la carpeta de datos."""
    raiz = data_dir()
    for carpeta in (raiz, logs_dir(), backups_dir(), cache_dir()):
        carpeta.mkdir(parents=True, exist_ok=True)
    return raiz


def resources_dir() -> Path:
    """Carpeta de recursos del paquete, tanto en desarrollo como dentro del exe."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        raiz = Path(base) if base else Path(sys.executable).absolute().parent
        return raiz / "sharky" / "resources"
    return Path(__file__).absolute().parent / "resources"


def resource_path(*partes: str) -> Path:
    """Ruta de un recurso del paquete, por ejemplo `resource_path("sharky.ico")`."""
    return resources_dir().joinpath(*partes)


def icon_path() -> Path:
    return resource_path(ICON_FILE)
