"""Repositorios: leer y escribir los registros de `core.models` en sus tablas (GUIA §5.1).

Cada repositorio recibe la conexión del hilo, así varias escrituras comparten una misma
transacción:

    with db.transaction() as conn:
        id_op = TradeRepository(conn).add(operacion)
        CashMovementRepository(conn).add(movimiento_de_esa_operacion)

Escribir fuera de una transacción es un error de programación y se rechaza.

Aquí está lo común (dar de alta, consultar y listar) y lo propio de cada hito: la foto del NAV
y la auditoría (H6), y las tesis y la vigilancia de sus niveles (H7).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import types
import typing
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from functools import cache
from typing import Any

from sharky.core.csv_import import Opening
from sharky.core.ledger import cash_balance
from sharky.core.levels import (
    LevelCheck,
    ProposalState,
    changes,
    evaluate_levels,
    from_json,
    levels_of,
    proposal_blocker,
    proposal_levels,
    proposal_states,
    target_proposal_event,
    texts_of,
    to_decimal,
    to_json,
    validate_levels,
)
from sharky.core.mandate import Finding, MandateRules, audit, snapshot_for
from sharky.core.models import (
    AlertStatus,
    Asset,
    Author,
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
    ThesisEventKind,
    ThesisStatus,
    Trade,
    WatchlistItem,
)
from sharky.core.valuation import Valuation, normalize_currency

log = logging.getLogger(__name__)


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

    def list_active(self) -> list[Thesis]:
        return self._select("status = ?", (ThesisStatus.ACTIVE.value,), order="ticker")

    def update(self, thesis: Thesis) -> bool:
        """Sustituye los datos de la tesis con ese id. Devuelve si existía.

        Solo la usan las operaciones de más abajo, que dejan cada cambio en el historial.
        """
        _require_transaction(self.conn)
        if thesis.id is None:
            raise ValueError("La tesis no tiene id: no está guardada")
        columnas = _columns(Thesis, with_id=False)
        asignaciones = ", ".join(f'"{c}" = ?' for c in columnas)
        valores = [to_db(getattr(thesis, c)) for c in columnas]
        cursor = self.conn.execute(
            f"UPDATE theses SET {asignaciones} WHERE id = ?", [*valores, thesis.id]
        )
        return cursor.rowcount > 0


class ThesisEventRepository(_Repository):
    """El historial solo crece: no hay forma de cambiar ni borrar un evento."""

    table, record = "thesis_events", ThesisEvent

    def add(self, event: ThesisEvent) -> int:
        return _insert(self.conn, self.table, event)

    def get(self, event_id: int) -> ThesisEvent | None:
        return self._one("id = ?", (event_id,))

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

    def pending(self, day: date) -> list[LevelAlert]:
        """Los avisos de ese día que todavía no se han enseñado."""
        return self._select("alert_date = ? AND notified = 0", (to_db(day),), order="id")

    def mark_notified(self, ids: Iterable[int]) -> None:
        _require_transaction(self.conn)
        self.conn.executemany(
            "UPDATE level_alerts SET notified = 1 WHERE id = ?", [(i,) for i in ids]
        )


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


# -- tesis (GUIA §5.6, H7) --------------------------------------------------------------------


class ThesisError(ValueError):
    """Los datos de una tesis no valen o la operación no se puede hacer. El mensaje (una línea
    por error) se puede enseñar."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


QUICK_ADD_NOTE = "Tesis creada con el alta rápida"


def _levels_currency(text: str) -> str:
    divisa = normalize_currency(text or "")
    if divisa is None:
        raise ThesisError([f"«{text}» no es una divisa: tres letras, como EUR o USD."])
    return divisa


def create_theses(
    conn: sqlite3.Connection,
    theses: Sequence[Thesis],
    now: datetime,
    note: str = QUICK_ADD_NOTE,
) -> list[int]:
    """Da de alta tesis nuevas, cada una con su evento de creación (autor: el usuario). Va
    dentro de la transacción de quien llama: o entran todas o ninguna.

    Si alguna no vale, lanza ThesisError con todos los errores a la vez, con su ticker.
    """
    _require_transaction(conn)
    activos = AssetRepository(conn)
    repositorio = ThesisRepository(conn)
    errores: list[str] = []
    limpias: list[Thesis] = []
    vistos: set[str] = set()
    for tesis in theses:
        propios = validate_levels(tesis.entry_price, tesis.stop, tesis.target, tesis.conviction)
        divisa = normalize_currency(tesis.levels_currency or "")
        if divisa is None:
            propios.append(f"«{tesis.levels_currency}» no es una divisa.")
        if activos.get(tesis.ticker) is None:
            propios.append("no existe ese activo.")
        elif repositorio.active_for(tesis.ticker) is not None or tesis.ticker in vistos:
            propios.append("ya tiene una tesis activa.")
        vistos.add(tesis.ticker)
        errores.extend(f"{tesis.ticker}: {e}" for e in propios)
        limpias.append(
            replace(tesis, levels_currency=divisa or tesis.levels_currency,
                    status=ThesisStatus.ACTIVE, closed_on=None, id=None)
        )
    if errores:
        raise ThesisError(errores)
    eventos = ThesisEventRepository(conn)
    ids: list[int] = []
    for tesis in limpias:
        id_tesis = repositorio.add(tesis)
        eventos.add(
            ThesisEvent(id_tesis, now, ThesisEventKind.CREATED, Author.USER, note,
                        new_value=to_json(levels_of(tesis)))
        )
        ids.append(id_tesis)
    log.info("Tesis creadas: %s", ", ".join(t.ticker for t in limpias))
    return ids


@dataclass(frozen=True)
class ThesisEdit:
    """Lo que el usuario deja en el editor de una tesis."""

    entry_price: Decimal | None
    stop: Decimal | None
    target: Decimal | None
    conviction: int | None
    levels_currency: str
    why: str = ""
    catalysts: str = ""
    risks: str = ""
    invalidation: str = ""


def _active_thesis(conn: sqlite3.Connection, thesis_id: int) -> Thesis:
    tesis = ThesisRepository(conn).get(thesis_id)
    if tesis is None:
        raise ThesisError(["No existe esa tesis."])
    if tesis.status is not ThesisStatus.ACTIVE:
        raise ThesisError(["La tesis está cerrada: ya no se edita."])
    return tesis


def update_thesis(
    conn: sqlite3.Connection,
    thesis_id: int,
    edit: ThesisEdit,
    now: datetime,
    reason: str = "",
) -> tuple[int, ...]:
    """Guarda lo editado. Cada cambio queda en el historial con el valor anterior y el nuevo:
    los números en un CAMBIO_NIVELES (con el motivo) y los textos en una REVISION, las dos del
    usuario. Sin cambios no escribe nada. Devuelve los eventos añadidos.
    """
    _require_transaction(conn)
    actual = _active_thesis(conn, thesis_id)
    divisa = _levels_currency(edit.levels_currency)
    nueva = replace(
        actual,
        entry_price=edit.entry_price,
        stop=edit.stop,
        target=edit.target,
        conviction=edit.conviction,
        levels_currency=divisa,
        why=edit.why.strip(),
        catalysts=edit.catalysts.strip(),
        risks=edit.risks.strip(),
        invalidation=edit.invalidation.strip(),
    )
    antes_n, despues_n = changes(levels_of(actual), levels_of(nueva))
    antes_t, despues_t = changes(texts_of(actual), texts_of(nueva))
    if not despues_n and not despues_t:
        return ()
    orden = "stop" in despues_n or "objetivo" in despues_n
    errores = validate_levels(nueva.entry_price, nueva.stop, nueva.target, nueva.conviction,
                              check_order=orden)
    if errores:
        raise ThesisError(errores)
    ThesisRepository(conn).update(nueva)
    eventos = ThesisEventRepository(conn)
    ids: list[int] = []
    if despues_n:
        ids.append(eventos.add(ThesisEvent(
            thesis_id, now, ThesisEventKind.LEVELS_CHANGED, Author.USER, reason.strip(),
            old_value=to_json(antes_n), new_value=to_json(despues_n),
        )))
    if despues_t:
        ids.append(eventos.add(ThesisEvent(
            thesis_id, now, ThesisEventKind.REVIEW, Author.USER,
            old_value=to_json(antes_t), new_value=to_json(despues_t),
        )))
    log.info("Tesis de %s editada (%s)", actual.ticker,
             ", ".join([*despues_n, *despues_t]))
    return tuple(ids)


_STATE_TEXT = {
    ProposalState.APPLIED: "Esa propuesta ya se aplicó.",
    ProposalState.DISCARDED: "Esa propuesta ya se descartó.",
    ProposalState.SUPERSEDED: "Hay una propuesta más reciente que sustituye a esta.",
}


def _pending_proposal(conn: sqlite3.Connection, event_id: int) -> tuple[ThesisEvent, Thesis]:
    evento = ThesisEventRepository(conn).get(event_id)
    if evento is None or evento.kind is not ThesisEventKind.PROPOSAL:
        raise ThesisError(["No existe esa propuesta."])
    tesis = _active_thesis(conn, evento.thesis_id)
    estado = proposal_states(ThesisEventRepository(conn).list_for(tesis.id)).get(event_id)
    if estado is not ProposalState.PENDING:
        raise ThesisError([_STATE_TEXT.get(estado, "Esa propuesta ya no está pendiente.")])
    return evento, tesis


def apply_proposal(conn: sqlite3.Connection, event_id: int, now: datetime) -> Thesis:
    """«Aplicar»: el usuario acepta una propuesta. Los números cambian con un CAMBIO_NIVELES
    suyo que apunta a la propuesta; la propuesta no se toca."""
    _require_transaction(conn)
    evento, tesis = _pending_proposal(conn, event_id)
    bloqueo = proposal_blocker(evento, tesis)
    if bloqueo:
        raise ThesisError([bloqueo])
    propuestos = proposal_levels(evento)
    conviccion = propuestos.get("conviccion", tesis.conviction)
    nueva = replace(
        tesis,
        stop=to_decimal(propuestos["stop"]) if propuestos.get("stop") is not None else tesis.stop,
        target=(
            to_decimal(propuestos["objetivo"])
            if propuestos.get("objetivo") is not None
            else tesis.target
        ),
        conviction=int(conviccion) if conviccion is not None else None,
    )
    errores = validate_levels(nueva.entry_price, nueva.stop, nueva.target, nueva.conviction,
                              check_order=False)
    if errores:
        raise ThesisError(errores)
    antes, despues = changes(levels_of(tesis), levels_of(nueva))
    ThesisRepository(conn).update(nueva)
    ThesisEventRepository(conn).add(ThesisEvent(
        tesis.id, now, ThesisEventKind.LEVELS_CHANGED, Author.USER, "",
        old_value=to_json(antes), new_value=to_json(despues), ref_event_id=event_id,
    ))
    log.info("Propuesta %d aplicada a la tesis de %s", event_id, tesis.ticker)
    return nueva


def discard_proposal(conn: sqlite3.Connection, event_id: int, now: datetime) -> int:
    """«Descartar»: el usuario no la quiere. Queda un evento suyo que apunta a la propuesta."""
    _require_transaction(conn)
    _evento, tesis = _pending_proposal(conn, event_id)
    id_evento = ThesisEventRepository(conn).add(ThesisEvent(
        tesis.id, now, ThesisEventKind.REVIEW, Author.USER, "", ref_event_id=event_id,
    ))
    log.info("Propuesta %d descartada en la tesis de %s", event_id, tesis.ticker)
    return id_evento


# -- vigilancia de niveles ---------------------------------------------------------------------


def load_level_checks(conn: sqlite3.Connection, valuation: Valuation) -> tuple[LevelCheck, ...]:
    """Vigila las tesis activas con esta valoración (GUIA §5.6). Solo lee."""
    tesis = ThesisRepository(conn).list_active()
    eventos = ThesisEventRepository(conn)
    historiales = {t.id: eventos.list_for(t.id) for t in tesis if t.id is not None}
    return evaluate_levels(tesis, valuation, FxRateRepository(conn).latest_all(), historiales)


@dataclass(frozen=True)
class RecordedLevels:
    """Lo que ha dejado guardado la vigilancia de niveles."""

    checks: tuple[LevelCheck, ...]
    new_alerts: tuple[LevelAlert, ...]  # los avisos que no existían todavía hoy
    proposals: tuple[int, ...]  # las propuestas de Sharky añadidas al historial


def _repeats_last_proposal(events: Sequence[ThesisEvent], proposal: ThesisEvent) -> bool:
    """La última propuesta de Sharky de esa tesis ya proponía lo mismo (aplicada, descartada o
    pendiente): no se vuelve a proponer cada día."""
    anteriores = [
        e for e in events if e.kind is ThesisEventKind.PROPOSAL and e.author is Author.SYSTEM
    ]
    if not anteriores:
        return False
    antes, ahora = from_json(anteriores[-1].new_value), from_json(proposal.new_value)
    return (
        antes.get("divisa") == ahora.get("divisa")
        and to_decimal(antes.get("stop")) == to_decimal(ahora.get("stop"))
    )


def record_levels(
    conn: sqlite3.Connection, valuation: Valuation, now: datetime
) -> RecordedLevels:
    """Vigila los niveles y guarda los avisos del día en `level_alerts`: uno por ticker, tipo
    y día (el que ya existía no se repite ni se vuelve a notificar). Un OBJETIVO nuevo añade al
    historial de su tesis la propuesta de subir el stop, si lo sube. Va dentro de la
    transacción de quien llama.
    """
    _require_transaction(conn)
    comprobaciones = load_level_checks(conn, valuation)
    avisos = LevelAlertRepository(conn)
    eventos = ThesisEventRepository(conn)
    nuevos: list[LevelAlert] = []
    propuestas: list[int] = []
    for c in comprobaciones:
        aviso = c.alert(now.date())
        if aviso is None:
            continue
        id_aviso = avisos.add(aviso)
        if id_aviso is None:
            continue  # ya avisado hoy
        nuevos.append(replace(aviso, id=id_aviso))
        if c.thesis.id is None:
            continue
        propuesta = target_proposal_event(c, c.thesis.id, now)
        if propuesta is not None and not _repeats_last_proposal(
            eventos.list_for(c.thesis.id), propuesta
        ):
            propuestas.append(eventos.add(propuesta))
    if nuevos:
        log.info("Avisos de niveles nuevos: %s",
                 ", ".join(f"{a.ticker} {a.kind}" for a in nuevos))
    return RecordedLevels(comprobaciones, tuple(nuevos), tuple(propuestas))


class RunRepository(_Repository):
    table, record = "runs", Run

    def add(self, run: Run) -> int:
        return _insert(self.conn, self.table, run)

    def list_recent(self, limit: int = 200) -> list[Run]:
        filas = self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
        )
        return [row_to(Run, fila) for fila in filas]
