"""Copias de seguridad de `sharky.db` con la API de backup de SQLite (GUIA §5.1 y §7, H3).

- Una copia es un único fichero `backups\\sharky-AAAAMMDD-HHMMSS.db`, coherente aunque la app
  esté escribiendo (la API de backup copia una foto consistente, WAL incluido). Se escribe
  con otro nombre y se renombra al final: una copia a medias nunca parece buena.
- Se guardan las 14 últimas.
- Restaurar comprueba antes que el fichero es una copia de Sharky, que está íntegra y que no
  es de una versión más nueva; guarda una copia de lo que hay ahora; vuelca la copia sobre la
  base de datos con la misma API y aplica las migraciones que falten. Quien llama reinicia la
  app después.
- Borrar la cartera guarda una copia de lo que hay (`…-antes-de-borrar.db`, que entra en la
  rotación como cualquier otra) y vuelca encima una base de datos vacía con el esquema al día.
  Quien llama reinicia la app, que abre el asistente de primer arranque.

La hora llega siempre desde fuera: este módulo no lee el reloj.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import tempfile
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sharky import paths
from sharky.services.db import TABLES, Database, DatabaseError, latest_version

log = logging.getLogger(__name__)

KEEP_BACKUPS = 14
PREFIX = "sharky-"
SUFFIX = ".db"
PARTIAL_SUFFIX = ".parcial"
BEFORE_RESTORE_LABEL = "antes-de-restaurar"
BEFORE_WIPE_LABEL = "antes-de-borrar"
_NAME = re.compile(r"^sharky-(\d{8}-\d{6})(?:-\d+)?(?:-[a-z-]+)?\.db$")
#: Sin estas tablas, un fichero no es una copia de Sharky.
REQUIRED_TABLES = frozenset({"assets", "trades", "cash_movements"})


class BackupError(Exception):
    """La copia no se ha podido hacer o restaurar. El mensaje se puede enseñar al usuario."""


@dataclass(frozen=True)
class RestoreResult:
    restored_from: Path
    safety_copy: Path


def backup_time(path: Path) -> datetime | None:
    """La fecha y hora de una copia, sacada de su nombre."""
    encontrado = _NAME.match(path.name)
    if not encontrado:
        return None
    return datetime.strptime(encontrado.group(1), "%Y%m%d-%H%M%S")


def list_backups(folder: Path | None = None) -> list[Path]:
    """Las copias de la carpeta, de la más antigua a la más reciente."""
    carpeta = folder if folder is not None else paths.backups_dir()
    if not carpeta.is_dir():
        return []
    copias = [p for p in carpeta.glob(f"{PREFIX}*{SUFFIX}") if backup_time(p) is not None]
    return sorted(copias, key=lambda p: (backup_time(p), p.name))


def prune_backups(folder: Path | None = None, keep: int = KEEP_BACKUPS) -> list[Path]:
    """Borra las copias que sobran (se quedan las `keep` más recientes). Devuelve las borradas."""
    copias = list_backups(folder)
    sobran = copias[: max(0, len(copias) - keep)]
    borradas = []
    for copia in sobran:
        try:
            copia.unlink()
            borradas.append(copia)
        except OSError:
            log.warning("No se ha podido borrar la copia antigua %s", copia, exc_info=True)
    if borradas:
        log.info("Borradas %d copias antiguas (se guardan las %d últimas)", len(borradas), keep)
    return borradas


def _free_name(carpeta: Path, now: datetime, label: str) -> Path:
    base = f"{PREFIX}{now.strftime('%Y%m%d-%H%M%S')}"
    extra = f"-{label}" if label else ""
    candidato = carpeta / f"{base}{extra}{SUFFIX}"
    n = 2
    while candidato.exists():
        candidato = carpeta / f"{base}-{n}{extra}{SUFFIX}"
        n += 1
    return candidato


def create_backup(
    db: Database,
    now: datetime,
    folder: Path | None = None,
    *,
    label: str = "",
    keep: int = KEEP_BACKUPS,
) -> Path:
    """Hace una copia de la base de datos y devuelve su ruta."""
    carpeta = folder if folder is not None else paths.backups_dir()
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = _free_name(carpeta, now, label)
    parcial = destino.with_name(destino.name + PARTIAL_SUFFIX)
    try:
        with closing(sqlite3.connect(parcial)) as copia:
            db.connection().backup(copia)
            # La copia es un solo fichero, sin -wal al lado.
            copia.execute("PRAGMA journal_mode = DELETE")
        os.replace(parcial, destino)
    except (sqlite3.Error, OSError) as error:
        with suppress(OSError):
            parcial.unlink()
        log.exception("La copia de seguridad ha fallado")
        raise BackupError(f"No se ha podido hacer la copia de seguridad: {error}") from error
    log.info("Copia de seguridad guardada en %s", destino)
    prune_backups(carpeta, keep)
    return destino


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)


def validate_backup(path: Path, latest: int | None = None) -> int:
    """Comprueba que `path` es una copia de Sharky que se puede restaurar. Devuelve su versión.

    `latest` es el último esquema que conoce este Sharky: una copia más nueva no se restaura.
    """
    ultima = latest_version() if latest is None else latest
    if not path.is_file():
        raise BackupError(f"No existe el fichero {path}")
    try:
        with closing(_open_read_only(path)) as conexion:
            integridad = conexion.execute("PRAGMA integrity_check").fetchone()[0]
            version = int(conexion.execute("PRAGMA user_version").fetchone()[0])
            tablas = {
                fila[0]
                for fila in conexion.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
    except sqlite3.DatabaseError as error:
        raise BackupError(f"{path.name} no es una copia de Sharky ({error})") from error
    if integridad != "ok":
        raise BackupError(f"{path.name} está dañada: {integridad}")
    if version < 1 or not REQUIRED_TABLES <= tablas:
        raise BackupError(f"{path.name} no es una copia de Sharky")
    if version > ultima:
        raise BackupError(
            f"{path.name} es de una versión de Sharky más nueva (esquema {version}). "
            "Actualiza Sharky antes de restaurarla."
        )
    return version


def restore_backup(
    db: Database,
    source: Path,
    now: datetime,
    folder: Path | None = None,
) -> RestoreResult:
    """Sustituye todos los datos por los de la copia `source`."""
    origen = Path(source).absolute()
    if origen == db.path:
        raise BackupError("Ese fichero es la base de datos en uso, no una copia")
    version = validate_backup(origen, latest_version(db.migrations))
    seguridad = create_backup(db, now, folder, label=BEFORE_RESTORE_LABEL)
    try:
        with closing(_open_read_only(origen)) as copia:
            copia.backup(db.connection())
        aplicadas = db.migrate()
    except (sqlite3.Error, DatabaseError) as error:
        log.exception("La restauración ha fallado")
        raise BackupError(
            f"No se ha podido restaurar {origen.name}: {error}. Lo que había antes está en "
            f"{seguridad.name}."
        ) from error
    faltan = set(TABLES) - db.tables()
    if faltan:
        raise BackupError(f"Tras restaurar faltan tablas: {', '.join(sorted(faltan))}")
    log.info(
        "Restaurada la copia %s (esquema %d, %d migraciones aplicadas). Lo anterior quedó en %s",
        origen.name, version, aplicadas, seguridad.name,
    )
    return RestoreResult(restored_from=origen, safety_copy=seguridad)


def wipe_portfolio(db: Database, now: datetime, folder: Path | None = None) -> Path:
    """«Borrar cartera» (Ajustes → Datos): se borra todo lo que hay en la base de datos.

    Primero guarda una copia de lo que hay (`…-antes-de-borrar.db`) y devuelve su ruta: con
    «Restaurar copia…» se recupera mientras siga entre las 14 últimas. Después vuelca sobre la
    base de datos una vacía con el esquema al día, con la misma API que restaurar: así no hace
    falta borrar tabla a tabla (el historial de las tesis no admite borrados). Los ajustes, la
    clave de Claude y las copias no se tocan. Quien llama reinicia la app.
    """
    seguridad = create_backup(db, now, folder, label=BEFORE_WIPE_LABEL)
    try:
        with tempfile.TemporaryDirectory(
            prefix="sharky_vacia_", ignore_cleanup_errors=True
        ) as carpeta:
            vacia = Database(Path(carpeta) / "sharky.db", db.migrations)
            try:
                vacia.migrate()
                vacia.connection().backup(db.connection())
            finally:
                vacia.close_all()
    except (sqlite3.Error, DatabaseError, OSError) as error:
        log.exception("Borrar la cartera ha fallado")
        raise BackupError(
            f"No se ha podido borrar la cartera: {error}. Lo que había sigue en "
            f"{seguridad.name}."
        ) from error
    faltan = set(TABLES) - db.tables()
    if faltan:
        raise BackupError(f"Tras borrar la cartera faltan tablas: {', '.join(sorted(faltan))}")
    log.info("Cartera borrada. Lo que había quedó en %s", seguridad.name)
    return seguridad
