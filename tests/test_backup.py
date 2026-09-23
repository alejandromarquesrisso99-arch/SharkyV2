"""Copias de seguridad: ida y vuelta, rotación de 14 y copias que no se deben restaurar."""

import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal as D

import pytest

from sharky import paths
from sharky.core.models import Asset, CashKind, CashMovement, Trade, TradeKind
from sharky.services.backup import (
    KEEP_BACKUPS,
    BackupError,
    backup_time,
    create_backup,
    list_backups,
    restore_backup,
    validate_backup,
)
from sharky.services.db import Database, latest_version
from sharky.services.repositories import (
    AssetRepository,
    CashMovementRepository,
    TradeRepository,
)

MOMENTO = datetime(2026, 9, 23, 20, 30, 15)


def llenar(db):
    with db.transaction() as conn:
        AssetRepository(conn).add(Asset("ACME", "Acme Inventada", "EUR"))
        TradeRepository(conn).add(
            Trade(date(2026, 9, 1), "ACME", TradeKind.OPENING, D("10"), D("25.5"), "EUR",
                  D("1"), D("0"), D("255"))
        )
        CashMovementRepository(conn).add(
            CashMovement(date(2026, 9, 1), CashKind.INITIAL, D("1000.00"))
        )


def contenido(db):
    """Todas las filas de las tablas con datos, para comparar."""
    conn = db.connection()
    return {
        tabla: [tuple(f) for f in conn.execute(f"SELECT * FROM {tabla} ORDER BY 1")]
        for tabla in ("assets", "trades", "cash_movements")
    }


def test_la_copia_va_a_backups_con_la_fecha_en_el_nombre(db):
    llenar(db)
    copia = create_backup(db, MOMENTO)
    assert copia.parent == paths.backups_dir()
    assert copia.name == "sharky-20260923-203015.db"
    assert backup_time(copia) == MOMENTO
    assert list_backups() == [copia]


def test_la_copia_es_un_solo_fichero_completo(db):
    llenar(db)
    copia = create_backup(db, MOMENTO)
    assert sorted(p.name for p in copia.parent.iterdir()) == [copia.name]
    validate_backup(copia)
    with sqlite3.connect(copia) as conexion:
        assert conexion.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conexion.execute("SELECT count(*) FROM trades").fetchone()[0] == 1
    conexion.close()


def test_copia_y_restauracion_de_ida_y_vuelta(db):
    llenar(db)
    antes = contenido(db)
    copia = create_backup(db, MOMENTO)

    # Después de la copia, los datos cambian…
    with db.transaction() as conn:
        CashMovementRepository(conn).add(
            CashMovement(date(2026, 9, 20), CashKind.WITHDRAWAL, D("-400"))
        )
        conn.execute("DELETE FROM trades")
    assert contenido(db) != antes

    # …y al restaurar vuelven a ser los de la copia.
    resultado = restore_backup(db, copia, MOMENTO + timedelta(hours=1))
    assert contenido(db) == antes
    assert db.user_version() == latest_version()
    assert CashMovementRepository(db.connection()).balance() == D("1000.00")

    # Lo que había justo antes de restaurar queda guardado aparte.
    assert resultado.restored_from == copia
    assert "antes-de-restaurar" in resultado.safety_copy.name
    with sqlite3.connect(resultado.safety_copy) as conexion:
        assert conexion.execute("SELECT count(*) FROM trades").fetchone()[0] == 0
    conexion.close()


def test_restaurar_se_ve_desde_otra_conexion_y_otro_hilo(db):
    import threading

    llenar(db)
    copia = create_backup(db, MOMENTO)
    with db.transaction() as conn:
        conn.execute("DELETE FROM cash_movements")

    resultado = []

    def restaurar_en_otro_hilo():
        restore_backup(db, copia, MOMENTO)
        resultado.append(CashMovementRepository(db.connection()).balance())

    hilo = threading.Thread(target=restaurar_en_otro_hilo)
    hilo.start()
    hilo.join()
    assert resultado == [D("1000.00")]
    assert CashMovementRepository(db.connection()).balance() == D("1000.00")


def test_se_guardan_las_14_ultimas(db):
    llenar(db)
    for dia in range(20):
        create_backup(db, MOMENTO + timedelta(days=dia))
    copias = list_backups()
    assert len(copias) == KEEP_BACKUPS == 14
    assert backup_time(copias[0]) == MOMENTO + timedelta(days=6)
    assert backup_time(copias[-1]) == MOMENTO + timedelta(days=19)


def test_dos_copias_en_el_mismo_segundo_no_se_pisan(db):
    primera = create_backup(db, MOMENTO)
    segunda = create_backup(db, MOMENTO)
    assert primera != segunda
    assert primera.exists() and segunda.exists()
    assert backup_time(segunda) == MOMENTO


def test_la_rotacion_no_toca_lo_que_no_es_una_copia(db):
    ajeno = paths.backups_dir() / "mis-notas.txt"
    ajeno.parent.mkdir(parents=True, exist_ok=True)
    ajeno.write_text("no borrar", encoding="utf-8")
    for dia in range(16):
        create_backup(db, MOMENTO + timedelta(days=dia))
    assert ajeno.exists()


def test_la_copia_puede_ir_a_otra_carpeta(db, tmp_path):
    destino = tmp_path / "Otra carpeta con tildes (ñ)"
    copia = create_backup(db, MOMENTO, destino)
    assert copia.parent == destino
    assert list_backups() == []  # la carpeta de siempre, intacta


# -- lo que no se debe restaurar --------------------------------------------------------


def test_un_fichero_que_no_es_una_base_de_datos(db, tmp_path):
    falso = tmp_path / "sharky-20260101-000000.db"
    falso.write_bytes(b"esto no es SQLite" * 100)
    with pytest.raises(BackupError, match="no es una copia de Sharky"):
        restore_backup(db, falso, MOMENTO)


def test_una_base_de_datos_que_no_es_de_sharky(db, tmp_path):
    ajena = tmp_path / "ajena.db"
    with sqlite3.connect(ajena) as conexion:
        conexion.execute("CREATE TABLE recetas (nombre TEXT)")
        conexion.execute("PRAGMA user_version = 1")
    conexion.close()
    with pytest.raises(BackupError, match="no es una copia de Sharky"):
        validate_backup(ajena)


def test_una_copia_de_una_version_mas_nueva(db, tmp_path):
    copia = create_backup(db, MOMENTO, tmp_path)
    with sqlite3.connect(copia) as conexion:
        conexion.execute(f"PRAGMA user_version = {latest_version() + 1}")
    conexion.close()
    with pytest.raises(BackupError, match="más nueva"):
        restore_backup(db, copia, MOMENTO)


def test_un_fichero_que_no_existe(db, tmp_path):
    with pytest.raises(BackupError, match="No existe"):
        restore_backup(db, tmp_path / "no-esta.db", MOMENTO)


def test_la_base_de_datos_en_uso_no_es_una_copia(db):
    with pytest.raises(BackupError, match="en uso"):
        restore_backup(db, db.path, MOMENTO)


def test_si_la_copia_no_vale_no_se_toca_nada(db, tmp_path):
    llenar(db)
    antes = contenido(db)
    falso = tmp_path / "rota.db"
    falso.write_bytes(b"\x00" * 4096)
    with pytest.raises(BackupError):
        restore_backup(db, falso, MOMENTO)
    assert contenido(db) == antes
    assert list_backups() == []  # ni siquiera se ha hecho la copia de seguridad previa


def test_una_copia_de_un_esquema_anterior_se_migra_al_restaurar(tmp_path, db):
    """Si la copia es de un Sharky más viejo, al restaurarla se aplican las migraciones."""
    from sharky.services.db import MIGRATIONS, Migration

    llenar(db)
    copia = create_backup(db, MOMENTO, tmp_path)
    siguiente = Migration(latest_version() + 1, "prueba: tabla nueva",
                          "CREATE TABLE prueba_futura (x INTEGER) STRICT;")
    nueva = Database(tmp_path / "Sharky nuevo" / "sharky.db", [*MIGRATIONS, siguiente])
    try:
        nueva.migrate()
        restore_backup(nueva, copia, MOMENTO, tmp_path / "copias nuevas")
        assert nueva.user_version() == siguiente.version
        assert "prueba_futura" in nueva.tables()
        assert contenido(nueva) == contenido(db)
    finally:
        nueva.close_all()
