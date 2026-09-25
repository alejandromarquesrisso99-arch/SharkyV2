"""La base de datos: conexión por hilo, WAL, espera, transacciones y migraciones."""

import sqlite3
import threading
import time

import pytest

from sharky.services.db import (
    MIGRATIONS,
    TABLES,
    Database,
    DatabaseError,
    Migration,
    NewerDatabaseError,
    latest_version,
)


def esquema(db: Database) -> list[tuple]:
    """Todo el esquema (tablas, índices y disparadores), para comparar antes y después."""
    return db.connection().execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()


# -- migraciones ------------------------------------------------------------------------


def test_una_base_de_datos_nueva_queda_en_la_ultima_version(tmp_path):
    db = Database(tmp_path / "sharky.db")
    try:
        assert db.user_version() == 0
        assert db.migrate() == len(MIGRATIONS)
        assert db.user_version() == latest_version()
        assert set(TABLES) <= db.tables()
    finally:
        db.close_all()


def test_las_migraciones_son_idempotentes(db):
    antes = [tuple(fila) for fila in esquema(db)]
    assert db.migrate() == 0
    assert db.migrate() == 0
    assert [tuple(fila) for fila in esquema(db)] == antes
    assert db.user_version() == latest_version()


def test_migrar_otra_vez_tras_reabrir_no_cambia_nada(db):
    antes = [tuple(fila) for fila in esquema(db)]
    db.close_all()
    otra = Database(db.path)
    try:
        assert otra.migrate() == 0
        assert [tuple(fila) for fila in esquema(otra)] == antes
    finally:
        otra.close_all()


def test_estan_todas_las_tablas_de_la_guia(db):
    assert db.tables() == set(TABLES)
    assert len(TABLES) == 14


def test_solo_se_aplican_las_migraciones_pendientes(tmp_path):
    uno = Migration(1, "tabla a", "CREATE TABLE a (x INTEGER) STRICT;")
    dos = Migration(2, "tabla b", "CREATE TABLE b (y INTEGER) STRICT;")
    db = Database(tmp_path / "prueba.db", migrations=[uno])
    try:
        assert db.migrate() == 1
    finally:
        db.close_all()
    db = Database(tmp_path / "prueba.db", migrations=[uno, dos])
    try:
        assert db.migrate() == 1  # solo la 2
        assert db.user_version() == 2
        assert {"a", "b"} <= db.tables()
    finally:
        db.close_all()


def test_una_migracion_que_falla_no_deja_nada_a_medias(tmp_path):
    uno = Migration(1, "tabla a", "CREATE TABLE a (x INTEGER) STRICT;")
    rota = Migration(
        2,
        "rota a mitad",
        "CREATE TABLE b (y INTEGER) STRICT; INSERT INTO no_existe VALUES (1);",
    )
    db = Database(tmp_path / "prueba.db", migrations=[uno, rota])
    try:
        with pytest.raises(DatabaseError, match="versión 2"):
            db.migrate()
        assert db.user_version() == 1  # la 1 entró; la 2, nada
        assert "a" in db.tables()
        assert "b" not in db.tables()
    finally:
        db.close_all()


def test_una_base_de_datos_de_una_version_mas_nueva_no_se_toca(tmp_path):
    ruta = tmp_path / "futura.db"
    with sqlite3.connect(ruta) as conexion:
        conexion.execute(f"PRAGMA user_version = {latest_version() + 1}")
    conexion.close()
    db = Database(ruta)
    try:
        with pytest.raises(NewerDatabaseError, match="más nueva"):
            db.migrate()
        assert db.user_version() == latest_version() + 1
    finally:
        db.close_all()


def test_las_migraciones_tienen_que_ir_numeradas(tmp_path):
    with pytest.raises(ValueError):
        Database(tmp_path / "x.db", migrations=[Migration(2, "sin la 1", "")])


# -- conexión ---------------------------------------------------------------------------


def test_cada_conexion_lleva_wal_espera_y_claves_foraneas(db):
    conexion = db.connection()
    assert conexion.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conexion.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conexion.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_una_conexion_por_hilo(db):
    principal = db.connection()
    assert db.connection() is principal  # el mismo hilo reutiliza la suya

    del_otro_hilo = []
    hilo = threading.Thread(target=lambda: del_otro_hilo.append(db.connection()))
    hilo.start()
    hilo.join()
    assert del_otro_hilo[0] is not principal
    fk = del_otro_hilo[0].execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1  # la del otro hilo también está configurada


def test_cerrar_todas_y_seguir_trabajando(db):
    antes = db.connection()
    db.close_all()
    despues = db.connection()
    assert despues is not antes
    assert despues.execute("SELECT count(*) FROM assets").fetchone()[0] == 0


def test_un_escritor_espera_al_otro_en_lugar_de_fallar(db):
    """busy_timeout: con otro hilo escribiendo, se espera; no salta «database is locked»."""
    dentro = threading.Event()
    errores: list[BaseException] = []

    def escritor_lento():
        try:
            with db.transaction() as conn:
                conn.execute("INSERT INTO runs (started_at, triggered_by, step, status) "
                             "VALUES ('2026-01-01T08:00:00', 'prueba', 'lento', 'OK')")
                dentro.set()
                time.sleep(0.3)
        except BaseException as error:  # pragma: no cover - solo si falla
            errores.append(error)

    hilo = threading.Thread(target=escritor_lento)
    hilo.start()
    assert dentro.wait(5)
    with db.transaction() as conn:  # espera a que el otro termine
        conn.execute("INSERT INTO runs (started_at, triggered_by, step, status) "
                     "VALUES ('2026-01-01T08:00:01', 'prueba', 'rapido', 'OK')")
    hilo.join()
    assert not errores
    pasos = [f[0] for f in db.connection().execute("SELECT step FROM runs ORDER BY id")]
    assert pasos == ["lento", "rapido"]


# -- transacciones ----------------------------------------------------------------------


def _insertar_activo(conn, ticker):
    conn.execute(
        "INSERT INTO assets (ticker, name, currency) VALUES (?, 'Inventada', 'EUR')", (ticker,)
    )


def _activos(db):
    return [f[0] for f in db.connection().execute("SELECT ticker FROM assets ORDER BY ticker")]


def test_una_transaccion_que_termina_bien_se_guarda(db):
    with db.transaction() as conn:
        _insertar_activo(conn, "ACME")
        _insertar_activo(conn, "BETA")
    assert _activos(db) == ["ACME", "BETA"]


def test_una_transaccion_que_falla_no_deja_nada(db):
    with pytest.raises(RuntimeError), db.transaction() as conn:
        _insertar_activo(conn, "ACME")
        raise RuntimeError("algo ha ido mal a mitad")
    assert _activos(db) == []
    assert not db.connection().in_transaction


def test_una_transaccion_dentro_de_otra_es_la_misma(db):
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        _insertar_activo(conn, "ACME")
        with db.transaction() as interior:
            assert interior is conn
            _insertar_activo(interior, "ACME")  # repetido: falla y se deshace todo
    assert _activos(db) == []


def test_las_claves_foraneas_se_cumplen(db):
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(
            "INSERT INTO trades (trade_date, ticker, kind, units, price, currency, fx_to_eur, "
            "fee_eur, amount_eur) VALUES ('2026-01-02', 'NOEXISTE', 'COMPRA', '1', '1', 'EUR', "
            "'1', '0', '1')"
        )


def test_las_tablas_son_estrictas(db):
    """Un texto en una columna de enteros se rechaza en lugar de guardarse tal cual."""
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(
            "INSERT INTO reports (kind, period, created_at, markdown, used_ai, input_tokens) "
            "VALUES ('DIARIO', '2026-01-02', '2026-01-02T08:00:00', '', 0, 'muchos')"
        )


def test_los_valores_cerrados_se_comprueban(db):
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(
            "INSERT INTO runs (started_at, triggered_by, step, status) "
            "VALUES ('2026-01-02T08:00:00', 'prueba', 'paso', 'QUIZAS')"
        )


def test_la_migracion_3_da_coste_al_registro(tmp_path):
    """H9: el coste de Claude va también en `runs`; lo de antes queda a 0 $."""
    ruta = tmp_path / "vieja.db"
    vieja = Database(ruta, MIGRATIONS[:2])
    try:
        vieja.migrate()
        with vieja.transaction() as conn:
            conn.execute(
                "INSERT INTO runs (started_at, triggered_by, step, status) "
                "VALUES ('2026-09-20T08:00:00+02:00', 'arranque', 'Valorar', 'OK')"
            )
    finally:
        vieja.close_all()
    nueva = Database(ruta)
    try:
        assert nueva.migrate() == 1
        fila = nueva.connection().execute("SELECT cost_usd FROM runs").fetchone()
        assert fila["cost_usd"] == "0"
        with pytest.raises(sqlite3.IntegrityError), nueva.transaction() as conn:
            conn.execute(
                "INSERT INTO runs (started_at, triggered_by, step, status, cost_usd) "
                "VALUES ('2026-09-21T08:00:00+02:00', 'manual', 'Informe diario', 'OK', NULL)"
            )
    finally:
        nueva.close_all()
