"""La base de datos: `sharky.db`, la fuente de verdad (GUIA §5.1 y §3).

- **Una conexión por hilo.** Cada hilo que la pide recibe la suya y la reutiliza. La clase
  las apunta todas para poder cerrarlas de golpe al salir o antes de restaurar una copia.
- **WAL, `busy_timeout` de 5 s y claves foráneas** en cada conexión.
- **Una transacción por operación.** Toda escritura va dentro de `with db.transaction():`,
  que empieza con `BEGIN IMMEDIATE` (el bloqueo de escritura se toma al principio y no a
  mitad) y acaba en COMMIT, o en ROLLBACK si algo falla.
- **Migraciones numeradas** con `PRAGMA user_version`. Cada migración va en su propia
  transacción junto con el cambio de versión: o entra entera o no entra. Migrar una base de
  datos al día no hace nada.

Los importes y las unidades se guardan como texto (`Decimal` exacto); las fechas, en ISO 8601.
Las tablas son STRICT: SQLite rechaza un valor del tipo equivocado en lugar de convertirlo.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 5000


class DatabaseError(Exception):
    """Algo impide usar la base de datos. El mensaje se puede enseñar al usuario."""


class NewerDatabaseError(DatabaseError):
    """La base de datos es de una versión de Sharky más nueva que esta."""


@dataclass(frozen=True)
class Migration:
    """Un paso del esquema. `version` es el `user_version` que deja al terminar."""

    version: int
    description: str
    sql: str


# -- esquema ----------------------------------------------------------------------------
# Importes, precios, unidades y proporciones: TEXT con un Decimal dentro.
# Fechas: TEXT 'AAAA-MM-DD'. Fechas con hora: TEXT ISO 8601 con su desfase horario.
# Proporciones (drawdown, cobertura, pesos, caídas): fracciones, 0.03 = 3 %.

_V1_SCHEMA = """
CREATE TABLE assets (
    ticker       TEXT NOT NULL PRIMARY KEY,
    name         TEXT NOT NULL,
    isin         TEXT,
    yahoo_symbol TEXT,
    currency     TEXT NOT NULL,
    asset_class  TEXT NOT NULL DEFAULT 'ACCION'
                 CHECK (asset_class IN ('ACCION', 'ETF', 'ETC', 'CRIPTO')),
    sector       TEXT
) STRICT;

CREATE TABLE trades (
    id              INTEGER PRIMARY KEY,
    trade_date      TEXT NOT NULL,
    ticker          TEXT NOT NULL REFERENCES assets (ticker),
    kind            TEXT NOT NULL CHECK (kind IN ('APERTURA', 'COMPRA', 'VENTA')),
    units           TEXT NOT NULL,
    price           TEXT NOT NULL,
    currency        TEXT NOT NULL,
    fx_to_eur       TEXT NOT NULL,
    fee_eur         TEXT NOT NULL,
    amount_eur      TEXT NOT NULL,
    stop            TEXT,
    target          TEXT,
    levels_currency TEXT,
    forced          INTEGER NOT NULL DEFAULT 0 CHECK (forced IN (0, 1)),
    reason          TEXT
) STRICT;
CREATE INDEX trades_by_ticker ON trades (ticker, trade_date, id);

CREATE TABLE cash_movements (
    id            INTEGER PRIMARY KEY,
    movement_date TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('INICIAL', 'INGRESO', 'RETIRADA', 'DIVIDENDO',
                                                'INTERES', 'COMISION', 'IMPUESTO', 'AJUSTE',
                                                'OPERACION')),
    amount_eur    TEXT NOT NULL,
    trade_id      INTEGER REFERENCES trades (id),
    note          TEXT,
    -- El movimiento de una operación, y solo ese, apunta a su operación.
    CHECK ((kind = 'OPERACION') = (trade_id IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX cash_movements_one_per_trade
    ON cash_movements (trade_id) WHERE trade_id IS NOT NULL;

CREATE TABLE prices (
    ticker     TEXT NOT NULL,
    price_date TEXT NOT NULL,
    price      TEXT NOT NULL,
    currency   TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('MERCADO', 'CACHE', 'ANTIGUO', 'COSTE')),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (ticker, price_date)
) STRICT;

CREATE TABLE fx_rates (
    currency    TEXT NOT NULL,
    rate_date   TEXT NOT NULL,
    rate_to_eur TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('MERCADO', 'CACHE', 'ANTIGUO', 'COSTE')),
    PRIMARY KEY (currency, rate_date)
) STRICT;

CREATE TABLE nav_snapshots (
    snapshot_date   TEXT NOT NULL PRIMARY KEY,
    nav_eur         TEXT NOT NULL,
    cash_eur        TEXT NOT NULL,
    fund_units      TEXT NOT NULL,
    unit_value      TEXT NOT NULL,
    high_water_mark TEXT NOT NULL,
    drawdown        TEXT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('OPTIMO', 'ALERTA', 'CUIDADOS_INTENSIVOS',
                                                   'BLOQUEO')),
    coverage        TEXT NOT NULL,
    reliable        INTEGER NOT NULL CHECK (reliable IN (0, 1))
) STRICT;

CREATE TABLE theses (
    id               INTEGER PRIMARY KEY,
    ticker           TEXT NOT NULL REFERENCES assets (ticker),
    levels_currency  TEXT NOT NULL,
    opened_on        TEXT NOT NULL,
    entry_price      TEXT,
    stop             TEXT,
    target           TEXT,
    conviction       INTEGER CHECK (conviction BETWEEN 1 AND 10),
    status           TEXT NOT NULL DEFAULT 'ACTIVA' CHECK (status IN ('ACTIVA', 'CERRADA')),
    why              TEXT NOT NULL DEFAULT '',
    catalysts        TEXT NOT NULL DEFAULT '',
    risks            TEXT NOT NULL DEFAULT '',
    invalidation     TEXT NOT NULL DEFAULT '',
    closed_on        TEXT,
    realized_pnl_eur TEXT,
    close_reason     TEXT,
    CHECK ((status = 'CERRADA') = (closed_on IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX theses_one_active ON theses (ticker) WHERE status = 'ACTIVA';

CREATE TABLE thesis_events (
    id           INTEGER PRIMARY KEY,
    thesis_id    INTEGER NOT NULL REFERENCES theses (id),
    created_at   TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('CREACION', 'CAMBIO_NIVELES', 'REVISION',
                                               'PROPUESTA', 'CIERRE')),
    author       TEXT NOT NULL CHECK (author IN ('USUARIO', 'CLAUDE', 'SISTEMA')),
    text         TEXT NOT NULL DEFAULT '',
    old_value    TEXT,
    new_value    TEXT,
    ref_event_id INTEGER REFERENCES thesis_events (id)
) STRICT;
CREATE INDEX thesis_events_by_thesis ON thesis_events (thesis_id, id);
-- El historial solo crece: nada se reescribe ni se borra.
CREATE TRIGGER thesis_events_no_update BEFORE UPDATE ON thesis_events
BEGIN
    SELECT RAISE(ABORT, 'El historial de una tesis solo admite añadir');
END;
CREATE TRIGGER thesis_events_no_delete BEFORE DELETE ON thesis_events
BEGIN
    SELECT RAISE(ABORT, 'El historial de una tesis solo admite añadir');
END;

CREATE TABLE level_alerts (
    id            INTEGER PRIMARY KEY,
    alert_date    TEXT NOT NULL,
    ticker        TEXT NOT NULL REFERENCES assets (ticker),
    kind          TEXT NOT NULL CHECK (kind IN ('STOP', 'OBJETIVO')),
    price_eur     TEXT NOT NULL,
    level_eur     TEXT NOT NULL,
    proposed_stop TEXT,
    criterion     TEXT,
    notified      INTEGER NOT NULL DEFAULT 0 CHECK (notified IN (0, 1)),
    UNIQUE (alert_date, ticker, kind)
) STRICT;

CREATE TABLE breaches (
    id           INTEGER PRIMARY KEY,
    rule         TEXT NOT NULL,
    subject      TEXT NOT NULL DEFAULT '',
    opened_at    TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    is_open      INTEGER NOT NULL DEFAULT 1 CHECK (is_open IN (0, 1))
) STRICT;
CREATE UNIQUE INDEX breaches_one_open ON breaches (rule, subject) WHERE is_open = 1;

CREATE TABLE watchlist (
    ticker       TEXT NOT NULL PRIMARY KEY,
    name         TEXT NOT NULL,
    added_by     TEXT NOT NULL CHECK (added_by IN ('USUARIO', 'EXPLORADOR')),
    added_on     TEXT NOT NULL,
    yahoo_symbol TEXT,
    currency     TEXT,
    sector       TEXT
) STRICT;

CREATE TABLE reports (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('DIARIO', 'SEMANAL', 'MENSUAL',
                                                  'EXPLORACION')),
    period          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    markdown        TEXT NOT NULL,
    used_ai         INTEGER NOT NULL CHECK (used_ai IN (0, 1)),
    conclusion      TEXT NOT NULL DEFAULT '',
    watch_positions TEXT NOT NULL DEFAULT '[]',
    model           TEXT,
    effort          TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    web_searches    INTEGER NOT NULL DEFAULT 0,
    cost_usd        TEXT NOT NULL DEFAULT '0',
    error           TEXT
) STRICT;
-- Un informe por tipo y periodo («Ejecutar ahora» lo sustituye); exploraciones, las que haya.
CREATE UNIQUE INDEX reports_one_per_period
    ON reports (kind, period) WHERE kind <> 'EXPLORACION';

CREATE TABLE alerts (
    id                 INTEGER PRIMARY KEY,
    created_on         TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    origin             TEXT NOT NULL CHECK (origin IN ('EXPLORADOR', 'VIGILANCIA')),
    status             TEXT NOT NULL CHECK (status IN ('ACTIVA', 'EJECUTADA', 'EXPIRADA',
                                                       'DESCARTADA')),
    price              TEXT,
    currency           TEXT,
    stop               TEXT,
    target             TEXT,
    ratio              TEXT,
    drawdown_from_high TEXT,
    max_weight         TEXT,
    summary            TEXT NOT NULL DEFAULT '',
    reason             TEXT,
    report_id          INTEGER REFERENCES reports (id)
) STRICT;
CREATE UNIQUE INDEX alerts_one_active ON alerts (ticker) WHERE status = 'ACTIVA';

CREATE TABLE runs (
    id           INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    triggered_by TEXT NOT NULL,
    step         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('OK', 'OMITIDO', 'ERROR')),
    detail       TEXT NOT NULL DEFAULT ''
) STRICT;
CREATE INDEX runs_by_start ON runs (started_at);
"""

# H5: la regla CACHE («guardado hace menos de 24 h», GUIA §5.3) también vale para el tipo de
# cambio, así que hace falta saber cuándo se descargó, igual que en `prices`. Los cambios de
# antes quedan sin hora y cuentan como antiguos.
_V2_FX_FETCHED_AT = """
ALTER TABLE fx_rates ADD COLUMN fetched_at TEXT;
"""

# H9: el coste real de cada llamada a Claude se guarda también en el Registro (GUIA §5.7). El
# gasto del mes y la estimación de cada botón salen de aquí y no de `reports`: «Ejecutar ahora»
# sustituye el informe, y su coste no puede desaparecer de la cuenta del mes.
_V3_RUNS_COST = """
ALTER TABLE runs ADD COLUMN cost_usd TEXT NOT NULL DEFAULT '0';
CREATE INDEX runs_by_step ON runs (step, started_at);
"""

# H11: el filtro del radar necesita el histórico diario de 12 meses (máximo, mínimo y cierre:
# el ATR no sale solo de los cierres de `prices`), guardado por símbolo de Yahoo como caché. Y
# cada fila del radar guarda la idea del candidato (nombre, símbolo, sector e invalidación) para
# poder añadirlo a vigilancia o comprarlo días después.
_V4_RADAR = """
CREATE TABLE price_history (
    symbol     TEXT NOT NULL,
    bar_date   TEXT NOT NULL,
    high       TEXT NOT NULL,
    low        TEXT NOT NULL,
    close      TEXT NOT NULL,
    currency   TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, bar_date)
) STRICT;
ALTER TABLE alerts ADD COLUMN name TEXT;
ALTER TABLE alerts ADD COLUMN yahoo_symbol TEXT;
ALTER TABLE alerts ADD COLUMN sector TEXT;
ALTER TABLE alerts ADD COLUMN invalidation TEXT NOT NULL DEFAULT '';
CREATE INDEX alerts_by_ticker ON alerts (ticker, created_on);
"""

#: Todas las migraciones, en orden. Las nuevas se añaden al final y nunca se editan.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "Esquema inicial: las tablas de GUIA §5.1", _V1_SCHEMA),
    Migration(2, "Hora de descarga de los tipos de cambio", _V2_FX_FETCHED_AT),
    Migration(3, "Coste de Claude en el Registro de ejecuciones", _V3_RUNS_COST),
    Migration(4, "Histórico diario del radar y la idea de cada candidato", _V4_RADAR),
)

#: Las tablas que tiene que tener cualquier base de datos de Sharky.
TABLES: tuple[str, ...] = (
    "assets",
    "trades",
    "cash_movements",
    "prices",
    "fx_rates",
    "nav_snapshots",
    "theses",
    "thesis_events",
    "level_alerts",
    "breaches",
    "watchlist",
    "reports",
    "alerts",
    "runs",
    "price_history",
)


def latest_version(migrations: Sequence[Migration] = MIGRATIONS) -> int:
    return migrations[-1].version if migrations else 0


def configure_connection(conn: sqlite3.Connection) -> None:
    """Lo que necesita cualquier conexión de Sharky: WAL, espera y claves foráneas."""
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    modo = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(modo).lower() != "wal":
        log.warning("SQLite no ha aceptado el modo WAL (sigue en %s)", modo)
    conn.execute("PRAGMA foreign_keys = ON")


class Database:
    """`sharky.db` con una conexión por hilo."""

    def __init__(self, path: Path, migrations: Sequence[Migration] = MIGRATIONS) -> None:
        self.path = Path(path).absolute()
        self.migrations = tuple(migrations)
        versiones = [m.version for m in self.migrations]
        if versiones != list(range(1, len(versiones) + 1)):
            raise ValueError(f"Las migraciones tienen que ir numeradas desde 1: {versiones}")
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []
        self._generation = 0

    # -- conexiones --------------------------------------------------------------------

    def connection(self) -> sqlite3.Connection:
        """La conexión de este hilo. La primera vez se abre y se configura."""
        guardada = getattr(self._local, "entry", None)
        if guardada is not None and guardada[0] == self._generation:
            return guardada[1]
        conexion = self._open()
        with self._lock:
            self._connections.append(conexion)
            self._local.entry = (self._generation, conexion)
        return conexion

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # autocommit=True: las transacciones las abre y cierra transaction(), a mano.
            # check_same_thread=False solo para que close_all() pueda cerrarlas desde el
            # hilo principal; cada conexión la usa únicamente el hilo que la abrió.
            conexion = sqlite3.connect(
                self.path,
                timeout=BUSY_TIMEOUT_MS / 1000,
                autocommit=True,
                check_same_thread=False,
            )
            configure_connection(conexion)
        except sqlite3.Error as error:
            raise DatabaseError(
                f"No se puede abrir la base de datos {self.path}: {error}"
            ) from error
        log.debug("Conexión abierta con %s en el hilo %s", self.path, threading.get_ident())
        return conexion

    def close_all(self) -> None:
        """Cierra las conexiones de todos los hilos. Las próximas peticiones abren otras."""
        with self._lock:
            conexiones, self._connections = self._connections, []
            self._generation += 1
        for conexion in conexiones:
            with suppress(sqlite3.Error):
                conexion.close()
        if conexiones:
            log.debug("Cerradas %d conexiones con %s", len(conexiones), self.path)

    # -- transacciones -----------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Una transacción de escritura: entra entera o no entra nada.

        Si este hilo ya está dentro de una, la nueva forma parte de ella (y un error dentro
        deshace la de fuera entera).
        """
        conexion = self.connection()
        if conexion.in_transaction:
            yield conexion
            return
        conexion.execute("BEGIN IMMEDIATE")
        try:
            yield conexion
        except BaseException:
            if conexion.in_transaction:
                conexion.execute("ROLLBACK")
            raise
        else:
            conexion.execute("COMMIT")

    # -- esquema -----------------------------------------------------------------------

    def user_version(self) -> int:
        return int(self.connection().execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> int:
        """Lleva el esquema a la última versión. Devuelve cuántas migraciones ha aplicado."""
        actual = self.user_version()
        ultima = latest_version(self.migrations)
        if actual > ultima:
            raise NewerDatabaseError(
                f"La base de datos es de una versión de Sharky más nueva (esquema {actual}; "
                f"esta versión conoce hasta el {ultima}). Actualiza Sharky."
            )
        aplicadas = 0
        for migracion in self.migrations:
            if migracion.version <= actual:
                continue
            log.info("Migrando la base de datos a la versión %d: %s",
                     migracion.version, migracion.description)
            try:
                with self.transaction() as conexion:
                    # Con autocommit=True, executescript no hace COMMIT por su cuenta: el
                    # script entero y el cambio de versión van en esta misma transacción.
                    conexion.executescript(migracion.sql)
                    conexion.execute(f"PRAGMA user_version = {int(migracion.version)}")
            except sqlite3.Error as error:
                raise DatabaseError(
                    f"No se ha podido migrar la base de datos a la versión "
                    f"{migracion.version}: {error}"
                ) from error
            aplicadas += 1
        return aplicadas

    def tables(self) -> set[str]:
        filas = self.connection().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        return {fila[0] for fila in filas}
