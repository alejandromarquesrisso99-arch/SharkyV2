"""Repositorios: leer y escribir los registros de `core.models` en sus tablas (GUIA §5.1).

Cada repositorio recibe la conexión del hilo, así varias escrituras comparten una misma
transacción:

    with db.transaction() as conn:
        id_op = TradeRepository(conn).add(operacion)
        CashMovementRepository(conn).add(movimiento_de_esa_operacion)

Escribir fuera de una transacción es un error de programación y se rechaza.

Aquí solo hay lo común (dar de alta, consultar y listar). Lo propio de cada pantalla (cerrar
una tesis, resolver un incumplimiento…) llega con su hito.
"""

from __future__ import annotations

import json
import sqlite3
import types
import typing
from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from functools import cache
from typing import Any

from sharky.core.csv_import import Opening
from sharky.core.ledger import cash_balance
from sharky.core.mandate import Finding, MandateRules, audit, snapshot_for
from sharky.core.models import (
    AlertStatus,
    Asset,
    Breach,
    CashMovement,
    FxRate,
    LevelAlert,
    NavSnapshot,
    Price,
    RadarAlert,
    Report,
    ReportKind,
    Run,
    Thesis,
    ThesisEvent,
    ThesisStatus,
    Trade,
    WatchlistItem,
)
from sharky.core.valuation import Valuation


class NotInTransactionError(RuntimeError):
    """Se ha intentado escribir sin abrir antes una transacción."""


# -- conversión entre registros y filas -------------------------------------------------


def to_db(value: Any) -> Any:
    """Un valor de Python tal como se guarda en SQLite."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"No se puede guardar una cifra no finita: {value}")
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return json.dumps(list(value), ensure_ascii=False)
    return value


def _base_type(anotacion: Any) -> Any:
    """`Decimal | None` → `Decimal`."""
    if isinstance(anotacion, types.UnionType) or typing.get_origin(anotacion) is typing.Union:
        resto = [a for a in typing.get_args(anotacion) if a is not type(None)]
        return resto[0] if len(resto) == 1 else anotacion
    return anotacion


def from_db(value: Any, anotacion: Any) -> Any:
    """Un valor leído de SQLite, convertido al tipo del campo."""
    if value is None:
        return None
    tipo = _base_type(anotacion)
    if tipo is Decimal:
        return Decimal(value)
    if tipo is datetime:
        return datetime.fromisoformat(value)
    if tipo is date:
        return date.fromisoformat(value)
    if tipo is bool:
        return bool(value)
    if typing.get_origin(tipo) is tuple:
        return tuple(json.loads(value))
    if isinstance(tipo, type) and issubclass(tipo, StrEnum):
        return tipo(value)
    return value


@cache
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _columns(cls: type, *, with_id: bool) -> list[str]:
    return [f.name for f in fields(cls) if with_id or f.name != "id"]


def row_to[T](cls: type[T], row: sqlite3.Row) -> T:
    """Una fila de la tabla convertida en su registro."""
    tipos = _hints(cls)
    claves = set(row.keys())
    datos = {f.name: from_db(row[f.name], tipos[f.name]) for f in fields(cls) if f.name in claves}
    return cls(**datos)


def _require_transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise NotInTransactionError(
            "Toda escritura va dentro de una transacción: usa `with db.transaction()`"
        )


def _insert(conn: sqlite3.Connection, table: str, record: Any, *, verb: str = "INSERT") -> int:
    _require_transaction(conn)
    tiene_id = any(f.name == "id" for f in fields(record))
    columnas = _columns(type(record), with_id=False)
    valores = [to_db(getattr(record, c)) for c in columnas]
    lista = ", ".join(f'"{c}"' for c in columnas)
    huecos = ", ".join("?" for _ in columnas)
    cursor = conn.execute(f'{verb} INTO "{table}" ({lista}) VALUES ({huecos})', valores)
    return int(cursor.lastrowid) if tiene_id else 0


class _Repository:
    table: str
    record: type

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def _select(self, where: str = "", params: tuple = (), order: str = "") -> list[Any]:
        sql = f'SELECT * FROM "{self.table}"'
        if where:
            sql += f" WHERE {where}"
        if order:
            sql += f" ORDER BY {order}"
        return [row_to(self.record, fila) for fila in self.conn.execute(sql, params)]

    def _one(self, where: str, params: tuple) -> Any | None:
        filas = self._select(where, params)
        return filas[0] if filas else None


# -- repositorios -----------------------------------------------------------------------


class AssetRepository(_Repository):
    table, record = "assets", Asset

    def add(self, asset: Asset) -> None:
        _insert(self.conn, self.table, asset)

    def get(self, ticker: str) -> Asset | None:
        return self._one("ticker = ?", (ticker,))

    def list_all(self) -> list[Asset]:
        return self._select(order="ticker")

    def update(self, asset: Asset) -> bool:
        """Sustituye los datos del activo con ese ticker. Devuelve si existía."""
        _require_transaction(self.conn)
        columnas = [c for c in _columns(Asset, with_id=False) if c != "ticker"]
        asignaciones = ", ".join(f'"{c}" = ?' for c in columnas)
        valores = [to_db(getattr(asset, c)) for c in columnas]
        cursor = self.conn.execute(
            f"UPDATE assets SET {asignaciones} WHERE ticker = ?", [*valores, asset.ticker]
        )
        return cursor.rowcount > 0


class TradeRepository(_Repository):
    table, record = "trades", Trade

    def add(self, trade: Trade) -> int:
        return _insert(self.conn, self.table, trade)

    def get(self, trade_id: int) -> Trade | None:
        return self._one("id = ?", (trade_id,))

    def list_all(self) -> list[Trade]:
        """Todas, en el orden en que se ejecutaron (fecha y, dentro del día, alta)."""
        return self._select(order="trade_date, id")

    def list_for(self, ticker: str) -> list[Trade]:
        return self._select("ticker = ?", (ticker,), order="trade_date, id")


class CashMovementRepository(_Repository):
    table, record = "cash_movements", CashMovement

    def add(self, movement: CashMovement) -> int:
        return _insert(self.conn, self.table, movement)

    def list_all(self) -> list[CashMovement]:
        return self._select(order="movement_date, id")

    def balance(self) -> Decimal:
        """El efectivo: la suma exacta de todos los movimientos."""
        return cash_balance(self.list_all())


class PriceRepository(_Repository):
    table, record = "prices", Price

    def save(self, price: Price) -> None:
        """Guarda el cierre del día; si ya había uno de ese ticker y día, lo sustituye."""
        _insert(self.conn, self.table, price, verb="INSERT OR REPLACE")

    def latest(self, ticker: str) -> Price | None:
        filas = self._select("ticker = ?", (ticker,), order="price_date DESC, fetched_at DESC")
        return filas[0] if filas else None

    def latest_all(self) -> dict[str, Price]:
        """El último precio de cada ticker."""
        ultimos: dict[str, Price] = {}
        for precio in self._select(order="ticker, price_date DESC, fetched_at DESC"):
            ultimos.setdefault(precio.ticker, precio)
        return ultimos

    def list_for(self, ticker: str) -> list[Price]:
        return self._select("ticker = ?", (ticker,), order="price_date")

    def delete_for(self, ticker: str) -> int:
        """Borra los precios guardados de un ticker (por ejemplo, al cambiarle el símbolo:
        eran de otro valor). Devuelve cuántos había."""
        _require_transaction(self.conn)
        return self.conn.execute("DELETE FROM prices WHERE ticker = ?", (ticker,)).rowcount


class FxRateRepository(_Repository):
    table, record = "fx_rates", FxRate

    def save(self, rate: FxRate) -> None:
        _insert(self.conn, self.table, rate, verb="INSERT OR REPLACE")

    def latest(self, currency: str) -> FxRate | None:
        filas = self._select(
            "currency = ?", (currency,), order="rate_date DESC, fetched_at DESC"
        )
        return filas[0] if filas else None

    def latest_all(self) -> dict[str, FxRate]:
        """El último cambio de cada divisa."""
        ultimos: dict[str, FxRate] = {}
        for cambio in self._select(order="currency, rate_date DESC, fetched_at DESC"):
            ultimos.setdefault(cambio.currency, cambio)
        return ultimos


class NavSnapshotRepository(_Repository):
    table, record = "nav_snapshots", NavSnapshot

    def save(self, snapshot: NavSnapshot) -> bool:
        """Una foto por día: la nueva sustituye a la del mismo día salvo que aquella fuera
        fiable y esta no (GUIA §5.1). Devuelve si se ha guardado."""
        anterior = self.get(snapshot.snapshot_date)
        if anterior is not None and anterior.reliable and not snapshot.reliable:
            return False
        _insert(self.conn, self.table, snapshot, verb="INSERT OR REPLACE")
        return True

    def get(self, day: date) -> NavSnapshot | None:
        return self._one("snapshot_date = ?", (to_db(day),))

    def list_all(self) -> list[NavSnapshot]:
        return self._select(order="snapshot_date")

    def latest(self, *, reliable_only: bool = False) -> NavSnapshot | None:
        filas = self._select("reliable = 1" if reliable_only else "", order="snapshot_date DESC")
        return filas[0] if filas else None


class ThesisRepository(_Repository):
    table, record = "theses", Thesis

    def add(self, thesis: Thesis) -> int:
        return _insert(self.conn, self.table, thesis)

    def get(self, thesis_id: int) -> Thesis | None:
        return self._one("id = ?", (thesis_id,))

    def active_for(self, ticker: str) -> Thesis | None:
        return self._one("ticker = ? AND status = ?", (ticker, ThesisStatus.ACTIVE.value))

    def list_all(self) -> list[Thesis]:
        return self._select(order="opened_on, id")


class ThesisEventRepository(_Repository):
    """El historial solo crece: no hay forma de cambiar ni borrar un evento."""

    table, record = "thesis_events", ThesisEvent

    def add(self, event: ThesisEvent) -> int:
        return _insert(self.conn, self.table, event)

    def list_for(self, thesis_id: int) -> list[ThesisEvent]:
        return self._select("thesis_id = ?", (thesis_id,), order="id")


class LevelAlertRepository(_Repository):
    table, record = "level_alerts", LevelAlert

    def add(self, alert: LevelAlert) -> int | None:
        """Guarda el aviso. Si ese día ya había uno igual (ticker y tipo), devuelve None."""
        _require_transaction(self.conn)
        try:
            return _insert(self.conn, self.table, alert)
        except sqlite3.IntegrityError:
            existente = self._one(
                "alert_date = ? AND ticker = ? AND kind = ?",
                (to_db(alert.alert_date), alert.ticker, alert.kind.value),
            )
            if existente is None:
                raise
            return None

    def list_for_date(self, day: date) -> list[LevelAlert]:
        return self._select("alert_date = ?", (to_db(day),), order="id")


class BreachRepository(_Repository):
    table, record = "breaches", Breach

    def add(self, breach: Breach) -> int:
        return _insert(self.conn, self.table, breach)

    def list_open(self) -> list[Breach]:
        return self._select("is_open = 1", order="opened_at, id")

    def list_all(self) -> list[Breach]:
        return self._select(order="opened_at, id")

    def sync(self, keys: Iterable[tuple[str, str]], now: datetime) -> list[Breach]:
        """Deja abiertos justo los incumplimientos `(regla, sujeto)` de la última auditoría.

        Los que ya estaban abiertos conservan su fecha de apertura (así se sabe cuántos días
        llevan) y se anotan como vistos ahora; los nuevos se abren ahora; los que ya no están se
        cierran. Un incumplimiento que vuelve después de cerrarse es uno nuevo. Devuelve los
        abiertos.
        """
        _require_transaction(self.conn)
        abiertos = {(b.rule, b.subject): b for b in self.list_open()}
        vistos = dict.fromkeys(keys)
        for clave in vistos:
            existente = abiertos.get(clave)
            if existente is not None:
                self.conn.execute(
                    "UPDATE breaches SET last_seen_at = ? WHERE id = ?",
                    (to_db(now), existente.id),
                )
            else:
                regla, sujeto = clave
                self.add(Breach(regla, sujeto, opened_at=now, last_seen_at=now))
        for clave, existente in abiertos.items():
            if clave not in vistos:
                self.conn.execute("UPDATE breaches SET is_open = 0 WHERE id = ?", (existente.id,))
        return self.list_open()


class WatchlistRepository(_Repository):
    table, record = "watchlist", WatchlistItem

    def add(self, item: WatchlistItem) -> None:
        _insert(self.conn, self.table, item)

    def remove(self, ticker: str) -> bool:
        _require_transaction(self.conn)
        return self.conn.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker,)).rowcount > 0

    def list_all(self) -> list[WatchlistItem]:
        return self._select(order="ticker")


class RadarAlertRepository(_Repository):
    table, record = "alerts", RadarAlert

    def add(self, alert: RadarAlert) -> int:
        return _insert(self.conn, self.table, alert)

    def list_by_status(self, status: AlertStatus) -> list[RadarAlert]:
        return self._select("status = ?", (status.value,), order="created_on, id")


class ReportRepository(_Repository):
    table, record = "reports", Report

    def save(self, report: Report) -> int:
        """Guarda el informe. Un diario, semanal o mensual sustituye al de su mismo periodo."""
        _require_transaction(self.conn)
        if report.kind is not ReportKind.EXPLORATION:
            self.conn.execute(
                "DELETE FROM reports WHERE kind = ? AND period = ?",
                (report.kind.value, report.period),
            )
        return _insert(self.conn, self.table, report)

    def get(self, report_id: int) -> Report | None:
        return self._one("id = ?", (report_id,))

    def latest(self, kind: ReportKind) -> Report | None:
        filas = self._select("kind = ?", (kind.value,), order="created_at DESC, id DESC")
        return filas[0] if filas else None

    def list_all(self) -> list[Report]:
        return self._select(order="created_at DESC, id DESC")


class PortfolioExistsError(RuntimeError):
    """Ya hay una cartera: el asistente de primer arranque no escribe encima."""


def has_portfolio(conn: sqlite3.Connection) -> bool:
    """Hay cartera si hay alguna operación o algún movimiento de efectivo.

    El asistente guarda siempre el movimiento INICIAL (aunque sea de 0 €), así que una cartera
    recién creada sin posiciones también cuenta. Y nunca se ofrece el asistente sobre una base
    de datos que ya tenga movimientos.
    """
    fila = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM trades) OR EXISTS (SELECT 1 FROM cash_movements)"
    ).fetchone()
    return bool(fila[0])


def create_portfolio(conn: sqlite3.Connection, opening: Opening) -> None:
    """Guarda la cartera inicial (GUIA §5.2): activos, operaciones APERTURA, efectivo INICIAL y
    primera foto del NAV. Va dentro de la transacción de quien llama: o entra todo o nada.

    Comprueba otra vez, ya dentro de la transacción, que no haya cartera.
    """
    _require_transaction(conn)
    if has_portfolio(conn):
        raise PortfolioExistsError("Ya hay una cartera en la base de datos: no se toca.")
    activos = AssetRepository(conn)
    for activo in opening.assets:
        activos.add(activo)
    operaciones = TradeRepository(conn)
    for operacion in opening.trades:
        operaciones.add(operacion)
    CashMovementRepository(conn).add(opening.initial_cash)
    NavSnapshotRepository(conn).save(opening.snapshot)


@dataclass(frozen=True)
class RecordedValuation:
    """Lo que ha dejado guardado una valoración: su foto del NAV y la auditoría."""

    snapshot: NavSnapshot
    saved: bool  # False si ya había una foto fiable de ese día y esta no lo es
    findings: tuple[Finding, ...]
    breaches: tuple[Breach, ...]  # los abiertos tras auditar


def record_valuation(
    conn: sqlite3.Connection, valuation: Valuation, rules: MandateRules, now: datetime
) -> RecordedValuation:
    """Guarda la foto del NAV del día y la auditoría del mandato (GUIA §5.4). Va dentro de la
    transacción de quien llama.

    Una foto por día: la última valoración fiable sustituye a la anterior; una no fiable no
    sustituye a una fiable. La auditoría se hace con el estado de la foto (el vigente, o el
    que se mantiene si la valoración no es fiable).
    """
    _require_transaction(conn)
    fotos = NavSnapshotRepository(conn)
    foto = snapshot_for(
        now.date(), valuation, fotos.list_all(), CashMovementRepository(conn).list_all()
    )
    guardada = fotos.save(foto)
    hallazgos = audit(valuation, foto.state, rules)
    abiertos = BreachRepository(conn).sync((h.key for h in hallazgos), now)
    return RecordedValuation(foto, guardada, hallazgos, tuple(abiertos))


class RunRepository(_Repository):
    table, record = "runs", Run

    def add(self, run: Run) -> int:
        return _insert(self.conn, self.table, run)

    def list_recent(self, limit: int = 200) -> list[Run]:
        filas = self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
        )
        return [row_to(Run, fila) for fila in filas]
