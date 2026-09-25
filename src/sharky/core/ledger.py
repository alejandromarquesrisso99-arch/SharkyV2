"""El libro: posiciones y PnL realizado a partir de las operaciones (GUIA §5.1).

Las posiciones no se guardan en ningún sitio: se derivan siempre de `trades` con coste medio
ponderado.

- Una compra (o una APERTURA) suma sus unidades y su coste; la comisión de compra suma al
  coste.
- Una venta resta unidades al coste medio del momento; la comisión de venta resta del importe.
  PnL realizado = importe neto de la venta − unidades × coste medio.
- Vender todo deja la posición a cero, sin restos de redondeo: el coste que sale es justo el
  que quedaba.

El efectivo tampoco se guarda: es la suma de los movimientos de efectivo. Cada operación
tiene el suyo (OPERACION); los demás se registran a mano con su signo: lo que entra suma y lo
que sale resta.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sharky.core.formatting import format_units
from sharky.core.models import CashKind, CashMovement, Trade, TradeKind

ZERO = Decimal("0")

#: Aviso que la app enseña junto a cualquier PnL realizado.
TAX_WARNING = (
    "El PnL realizado se calcula con coste medio ponderado. No sirve para la declaración de "
    "la renta: en España se usa FIFO."
)


class LedgerError(ValueError):
    """Las operaciones no cuadran (por ejemplo, se vende más de lo que hay)."""


@dataclass(frozen=True)
class Position:
    """Una posición abierta."""

    ticker: str
    units: Decimal
    cost_eur: Decimal  # coste total de lo que queda, con las comisiones de compra

    @property
    def avg_cost_eur(self) -> Decimal:
        """Coste medio por unidad, en EUR."""
        return self.cost_eur / self.units


@dataclass(frozen=True)
class RealizedSale:
    """Lo que dejó una venta."""

    ticker: str
    trade_date: date
    units: Decimal
    net_proceeds_eur: Decimal  # importe − comisión
    cost_basis_eur: Decimal  # unidades × coste medio
    closes_position: bool
    trade_id: int | None = None

    @property
    def pnl_eur(self) -> Decimal:
        return self.net_proceeds_eur - self.cost_basis_eur


@dataclass(frozen=True)
class Ledger:
    """Posiciones abiertas y ventas realizadas."""

    positions: dict[str, Position]
    sales: tuple[RealizedSale, ...]

    def position(self, ticker: str) -> Position | None:
        return self.positions.get(ticker)

    def realized_pnl_eur(self, ticker: str | None = None) -> Decimal:
        """PnL realizado de todas las ventas, o solo de las de un ticker."""
        return sum(
            (v.pnl_eur for v in self.sales if ticker is None or v.ticker == ticker),
            ZERO,
        )


def build_ledger(trades: Iterable[Trade]) -> Ledger:
    """Recorre las operaciones por fecha y construye el libro.

    Dentro del mismo día se respeta el orden en que llegan (el repositorio las da por id).
    """
    unidades: dict[str, Decimal] = {}
    costes: dict[str, Decimal] = {}
    ventas: list[RealizedSale] = []

    for op in sorted(trades, key=lambda t: t.trade_date):
        if op.units <= 0:
            raise LedgerError(f"Operación de {op.ticker} con unidades no positivas: {op.units}")
        tenia = unidades.get(op.ticker, ZERO)
        coste = costes.get(op.ticker, ZERO)

        if op.kind is TradeKind.SELL:
            if op.units > tenia:
                raise LedgerError(
                    f"No se pueden vender {format_units(op.units)} unidades de {op.ticker}: "
                    f"solo hay {format_units(tenia)}."
                )
            cierra = op.units == tenia
            base = coste if cierra else op.units * coste / tenia
            ventas.append(
                RealizedSale(
                    ticker=op.ticker,
                    trade_date=op.trade_date,
                    units=op.units,
                    net_proceeds_eur=op.amount_eur - op.fee_eur,
                    cost_basis_eur=base,
                    closes_position=cierra,
                    trade_id=op.id,
                )
            )
            unidades[op.ticker] = tenia - op.units
            costes[op.ticker] = ZERO if cierra else coste - base
        else:
            unidades[op.ticker] = tenia + op.units
            costes[op.ticker] = coste + op.amount_eur + op.fee_eur

    abiertas = {
        ticker: Position(ticker=ticker, units=u, cost_eur=costes[ticker])
        for ticker, u in sorted(unidades.items())
        if u > 0
    }
    return Ledger(positions=abiertas, sales=tuple(ventas))


def cycle_realized_pnl(ledger: Ledger, ticker: str) -> Decimal | None:
    """El PnL realizado de la posición de `ticker` desde que se abrió por última vez: todas las
    ventas desde la anterior que la dejó a cero (sin ella) hasta la última. Al venderla entera,
    es el PnL de toda la posición, con las ventas parciales de antes. None si no hay ventas."""
    ventas = [v for v in ledger.sales if v.ticker == ticker]
    if not ventas:
        return None
    inicio = 0
    for numero, venta in enumerate(ventas[:-1]):
        if venta.closes_position:
            inicio = numero + 1
    return sum((v.pnl_eur for v in ventas[inicio:]), ZERO)


def trade_amount_eur(units: Decimal, price: Decimal, fx_to_eur: Decimal) -> Decimal:
    """Importe bruto de una operación en EUR (sin la comisión): unidades × precio × cambio."""
    return units * price * fx_to_eur


def trade_date_error(
    day: date,
    today: date,
    ticker: str,
    portfolio_start: date | None,
    last_trade: date | None,
) -> str | None:
    """Por qué una operación no puede llevar esa fecha (None si puede).

    Ni futura, ni anterior a la creación de la cartera (lo de antes ya está en sus posiciones
    iniciales), ni anterior a la última operación de ese ticker: se cambiaría el coste medio de
    ventas ya hechas o se reabriría una posición cerrada.
    """
    if day > today:
        return "La fecha no puede ser futura."
    if portfolio_start is not None and day < portfolio_start:
        return (
            f"La cartera empieza el {portfolio_start:%d/%m/%Y}: lo anterior ya está en sus "
            "posiciones iniciales."
        )
    if last_trade is not None and day < last_trade:
        return (
            f"La última operación de {ticker} es del {last_trade:%d/%m/%Y}: esta tiene que ser "
            "de ese día o posterior."
        )
    return None


def cash_balance(movements: Iterable[CashMovement]) -> Decimal:
    """El efectivo es la suma de los movimientos de efectivo, con su signo."""
    return sum((m.amount_eur for m in movements), ZERO)


def trade_cash_amount(trade: Trade) -> Decimal | None:
    """Lo que una operación mueve en el efectivo (su movimiento de tipo OPERACION).

    Una compra saca importe + comisión; una venta mete importe − comisión. Una APERTURA no
    mueve efectivo: la posición ya existía y el efectivo inicial es el que queda.
    """
    if trade.kind is TradeKind.OPENING:
        return None
    if trade.kind is TradeKind.BUY:
        return -(trade.amount_eur + trade.fee_eur)
    return trade.amount_eur - trade.fee_eur


# -- movimientos de efectivo sueltos -----------------------------------------------------

#: Cómo se llama cada tipo de movimiento al enseñarlo.
CASH_LABELS: dict[CashKind, str] = {
    CashKind.INITIAL: "Efectivo inicial",
    CashKind.DEPOSIT: "Ingreso",
    CashKind.WITHDRAWAL: "Retirada",
    CashKind.DIVIDEND: "Dividendo",
    CashKind.INTEREST: "Interés",
    CashKind.FEE: "Comisión",
    CashKind.TAX: "Impuesto",
    CashKind.ADJUSTMENT: "Ajuste",
    CashKind.TRADE: "Operación",
}

#: Los movimientos que se registran a mano con un importe, y su signo. El AJUSTE sale de «mi
#: saldo real es X» (`adjustment_amount`); INICIAL y OPERACION los pone Sharky.
MANUAL_CASH_SIGNS: dict[CashKind, int] = {
    CashKind.DEPOSIT: 1,
    CashKind.WITHDRAWAL: -1,
    CashKind.DIVIDEND: 1,
    CashKind.INTEREST: 1,
    CashKind.FEE: -1,
    CashKind.TAX: -1,
}


def signed_cash_amount(kind: CashKind, amount_eur: Decimal) -> Decimal:
    """El importe con su signo: un ingreso de 100 € suma 100; una retirada de 100 €, −100."""
    if kind not in MANUAL_CASH_SIGNS:
        raise ValueError(f"El movimiento {kind} no se registra con un importe")
    if amount_eur <= 0:
        raise ValueError("El importe tiene que ser mayor que 0")
    return amount_eur * MANUAL_CASH_SIGNS[kind]


def adjustment_amount(cash_eur: Decimal, real_balance_eur: Decimal) -> Decimal:
    """«Mi saldo real es X»: el ajuste que deja el efectivo en X."""
    return real_balance_eur - cash_eur
