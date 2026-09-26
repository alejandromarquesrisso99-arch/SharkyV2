"""Tipos del dominio (GUIA §5.1): lo que se guarda en la base de datos, sin saber cómo.

Reglas que valen para todo el programa:

- Importes, precios, unidades y cualquier cifra que salga de ellos son `Decimal`, nunca
  `float`: el efectivo es una suma y tiene que cuadrar al céntimo.
- Las proporciones (drawdown, cobertura, pesos, caída desde máximos) son fracciones:
  `Decimal("0.03")` es un 3 %. El porcentaje solo aparece al mostrarlas.
- Los nombres de código van en inglés; los valores guardados son los de la guía, en español.
- Nada de aquí lee el reloj: las fechas y horas llegan siempre desde fuera.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

# -- valores cerrados -------------------------------------------------------------------


class AssetClass(StrEnum):
    STOCK = "ACCION"
    ETF = "ETF"
    ETC = "ETC"
    CRYPTO = "CRIPTO"


class TradeKind(StrEnum):
    # Posición que ya existía al crear la cartera: a coste medio y en EUR (precio = coste
    # medio, cambio 1, sin comisión). La divisa de cotización está en su activo.
    OPENING = "APERTURA"
    BUY = "COMPRA"
    SELL = "VENTA"


class CashKind(StrEnum):
    INITIAL = "INICIAL"
    DEPOSIT = "INGRESO"
    WITHDRAWAL = "RETIRADA"
    DIVIDEND = "DIVIDENDO"
    INTEREST = "INTERES"
    FEE = "COMISION"
    TAX = "IMPUESTO"
    ADJUSTMENT = "AJUSTE"
    TRADE = "OPERACION"


class PriceSource(StrEnum):
    """Procedencia de un precio, por orden de preferencia (GUIA §5.3)."""

    MARKET = "MERCADO"
    CACHE = "CACHE"
    STALE = "ANTIGUO"
    COST = "COSTE"

    @property
    def reliable(self) -> bool:
        """Solo MERCADO y CACHE son fiables."""
        return self in (PriceSource.MARKET, PriceSource.CACHE)


class MandateState(StrEnum):
    OPTIMAL = "OPTIMO"
    ALERT = "ALERTA"
    INTENSIVE_CARE = "CUIDADOS_INTENSIVOS"
    LOCKDOWN = "BLOQUEO"


class ThesisStatus(StrEnum):
    ACTIVE = "ACTIVA"
    CLOSED = "CERRADA"


class ThesisEventKind(StrEnum):
    CREATED = "CREACION"
    LEVELS_CHANGED = "CAMBIO_NIVELES"
    REVIEW = "REVISION"
    PROPOSAL = "PROPUESTA"
    CLOSED = "CIERRE"


class Author(StrEnum):
    USER = "USUARIO"
    CLAUDE = "CLAUDE"
    SYSTEM = "SISTEMA"


class LevelAlertKind(StrEnum):
    STOP = "STOP"
    TARGET = "OBJETIVO"


class WatchlistSource(StrEnum):
    USER = "USUARIO"
    EXPLORER = "EXPLORADOR"


class AlertOrigin(StrEnum):
    EXPLORER = "EXPLORADOR"
    WATCHLIST = "VIGILANCIA"


class AlertStatus(StrEnum):
    ACTIVE = "ACTIVA"
    EXECUTED = "EJECUTADA"
    EXPIRED = "EXPIRADA"
    DISCARDED = "DESCARTADA"


class ReportKind(StrEnum):
    DAILY = "DIARIO"
    WEEKLY = "SEMANAL"
    MONTHLY = "MENSUAL"
    EXPLORATION = "EXPLORACION"


class RunStatus(StrEnum):
    OK = "OK"
    SKIPPED = "OMITIDO"
    ERROR = "ERROR"


# -- registros --------------------------------------------------------------------------
# Los campos se llaman igual que las columnas de su tabla. `id` es None hasta guardarlo.


@dataclass(frozen=True)
class Asset:
    """Tabla `assets`."""

    ticker: str
    name: str
    currency: str  # divisa de cotización: EUR, USD, GBp…
    asset_class: AssetClass = AssetClass.STOCK
    isin: str | None = None
    yahoo_symbol: str | None = None
    sector: str | None = None


@dataclass(frozen=True)
class Trade:
    """Tabla `trades`: una operación ya ejecutada en el bróker.

    `amount_eur` es el importe bruto en EUR (unidades × precio × cambio), sin la comisión.
    """

    trade_date: date
    ticker: str
    kind: TradeKind
    units: Decimal
    price: Decimal
    currency: str
    fx_to_eur: Decimal
    fee_eur: Decimal
    amount_eur: Decimal
    stop: Decimal | None = None
    target: Decimal | None = None
    levels_currency: str | None = None
    forced: bool = False
    reason: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class CashMovement:
    """Tabla `cash_movements`. El importe lleva signo: lo que entra suma, lo que sale resta."""

    movement_date: date
    kind: CashKind
    amount_eur: Decimal
    trade_id: int | None = None  # solo en los movimientos de tipo OPERACION
    note: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class Price:
    """Tabla `prices`: un cierre por ticker y día."""

    ticker: str
    price_date: date
    price: Decimal
    currency: str
    source: PriceSource
    fetched_at: datetime


@dataclass(frozen=True)
class FxRate:
    """Tabla `fx_rates`: cuántos EUR vale una unidad de la divisa ese día.

    `fetched_at` es cuándo se descargó (migración 2); en los cambios guardados antes de
    existir la columna vale None y cuentan como antiguos.
    """

    currency: str
    rate_date: date
    rate_to_eur: Decimal
    source: PriceSource
    fetched_at: datetime | None = None


@dataclass(frozen=True)
class NavSnapshot:
    """Tabla `nav_snapshots`: una foto por día. Drawdown y cobertura son fracciones."""

    snapshot_date: date
    nav_eur: Decimal
    cash_eur: Decimal
    fund_units: Decimal  # participaciones
    unit_value: Decimal  # valor por participación (empieza en 100)
    high_water_mark: Decimal  # máximo del valor por participación
    drawdown: Decimal
    state: MandateState
    coverage: Decimal
    reliable: bool


@dataclass(frozen=True)
class Thesis:
    """Tabla `theses`. Una sola ACTIVA por ticker; las cerradas se conservan."""

    ticker: str
    levels_currency: str
    opened_on: date
    entry_price: Decimal | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    conviction: int | None = None  # de 1 a 10
    status: ThesisStatus = ThesisStatus.ACTIVE
    why: str = ""
    catalysts: str = ""
    risks: str = ""
    invalidation: str = ""
    closed_on: date | None = None
    realized_pnl_eur: Decimal | None = None
    close_reason: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class ThesisEvent:
    """Tabla `thesis_events`: el historial de una tesis, en el que solo se añade.

    `old_value` y `new_value` guardan lo que cambió (por ejemplo, los niveles). `ref_event_id`
    enlaza un evento con otro anterior: así se sabe qué propuesta se aplicó o se descartó sin
    tocar la propuesta, que nunca se reescribe.
    """

    thesis_id: int
    created_at: datetime
    kind: ThesisEventKind
    author: Author
    text: str = ""
    old_value: str | None = None
    new_value: str | None = None
    ref_event_id: int | None = None
    id: int | None = None


@dataclass(frozen=True)
class LevelAlert:
    """Tabla `level_alerts`: un aviso de stop u objetivo por ticker, tipo y día."""

    alert_date: date
    ticker: str
    kind: LevelAlertKind
    price_eur: Decimal
    level_eur: Decimal
    proposed_stop: Decimal | None = None
    criterion: str | None = None
    notified: bool = False
    id: int | None = None


@dataclass(frozen=True)
class Breach:
    """Tabla `breaches`: un incumplimiento del mandato (regla + sujeto) y su antigüedad."""

    rule: str
    subject: str
    opened_at: datetime
    last_seen_at: datetime
    is_open: bool = True
    id: int | None = None


@dataclass(frozen=True)
class WatchlistItem:
    """Tabla `watchlist`: el universo del radar."""

    ticker: str
    name: str
    added_by: WatchlistSource
    added_on: date
    yahoo_symbol: str | None = None
    currency: str | None = None
    sector: str | None = None


@dataclass(frozen=True)
class RadarAlert:
    """Tabla `alerts`: lo que sale del filtro del radar, pase o no (GUIA §5.8).

    Un candidato descartado por el filtro se guarda igual, con su motivo en `reason`, sin los
    niveles que no se pudieron calcular y sin peso máximo (solo lo tiene lo que pasó el filtro).
    Nombre, símbolo, sector e invalidación (migración 4) guardan la idea del candidato para
    poder añadirlo a vigilancia o comprarlo después.
    """

    created_on: date
    ticker: str
    origin: AlertOrigin
    status: AlertStatus
    price: Decimal | None = None
    currency: str | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    ratio: Decimal | None = None
    drawdown_from_high: Decimal | None = None
    max_weight: Decimal | None = None
    summary: str = ""
    reason: str | None = None
    report_id: int | None = None
    name: str | None = None
    yahoo_symbol: str | None = None
    sector: str | None = None
    invalidation: str = ""
    id: int | None = None


@dataclass(frozen=True)
class Report:
    """Tabla `reports`. `watch_positions` solo se usa en el diario."""

    kind: ReportKind
    period: str
    created_at: datetime
    markdown: str
    used_ai: bool
    conclusion: str = ""
    watch_positions: tuple[str, ...] = ()
    model: str | None = None
    effort: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    cost_usd: Decimal = Decimal("0")
    error: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class Run:
    """Tabla `runs`: un paso de la rutina, o una acción lanzada a mano.

    `cost_usd` es lo que ha costado de verdad en Claude (migración 3), también si falló: los
    intentos fallidos se cobran igual. El gasto del mes es la suma de esta columna.
    """

    started_at: datetime
    triggered_by: str
    step: str
    status: RunStatus
    detail: str = ""
    finished_at: datetime | None = None
    cost_usd: Decimal = Decimal("0")
    id: int | None = None
