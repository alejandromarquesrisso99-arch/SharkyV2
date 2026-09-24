"""Cartera (GUIA §5.10, punto 2, y §7, H5).

- Tabla ordenable con cada posición: unidades, precio y su divisa, valor en euros, peso, PnL,
  de dónde sale el precio (procedencia) y los niveles de su tesis.
- Exposición por sector frente al tope del mandato, y el efectivo.
- «Actualizar precios (gratis)» descarga en un hilo de trabajo, con barra de progreso y
  «Cancelar»: la ventana no se congela.
- «Editar activo»: símbolo (con «Probar» y la sugerencia por ISIN), sector y clase.

Todo lo que se enseña sale de `core.valuation`; aquí no se calcula ni un euro.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import IntEnum
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QPointF,
    QRectF,
    QSize,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from sharky.core.formatting import (
    MINUS,
    format_amount,
    format_eur,
    format_pct,
    format_price,
    format_signed_amount,
    format_units,
)
from sharky.core.models import Asset, AssetClass, PriceSource, Thesis, ThesisStatus
from sharky.core.valuation import PositionValue, Valuation
from sharky.services.db import Database
from sharky.services.market import (
    AssetEditError,
    FxProvider,
    MarketRefresh,
    PriceProvider,
    Quote,
    SymbolSuggestion,
    edit_asset,
    load_valuation,
    local_now,
    probe_symbol,
    refresh_market,
)
from sharky.services.repositories import AssetRepository, ThesisRepository
from sharky.services.settings import Settings
from sharky.ui import theme as theme_module
from sharky.ui.pages import card, muted, set_state, state_label
from sharky.ui.theme import ThemeController
from sharky.ui.workers import Worker, start

log = logging.getLogger(__name__)


class Col(IntEnum):
    TICKER = 0
    NAME = 1
    UNITS = 2
    PRICE = 3
    VALUE = 4
    WEIGHT = 5
    PNL_EUR = 6
    PNL_PCT = 7
    SOURCE = 8
    STOP = 9
    TARGET = 10


HEADERS: dict[Col, str] = {
    Col.TICKER: "Ticker",
    Col.NAME: "Nombre",
    Col.UNITS: "Unidades",
    Col.PRICE: "Precio",
    Col.VALUE: "Valor (€)",
    Col.WEIGHT: "Peso",
    Col.PNL_EUR: "PnL (€)",
    Col.PNL_PCT: "PnL (%)",
    Col.SOURCE: "Procedencia",
    Col.STOP: "Stop",
    Col.TARGET: "Objetivo",
}
NUMERIC_COLUMNS = frozenset(
    {Col.UNITS, Col.PRICE, Col.VALUE, Col.WEIGHT, Col.PNL_EUR, Col.PNL_PCT, Col.STOP, Col.TARGET}
)

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
SOURCE_ROLE = Qt.ItemDataRole.UserRole + 2

SOURCE_LABELS: dict[PriceSource, str] = {
    PriceSource.MARKET: "Mercado",
    PriceSource.CACHE: "Caché",
    PriceSource.STALE: "Antiguo",
    PriceSource.COST: "Coste",
}
#: Color de estado de cada procedencia: fiable en verde; lo demás, en ámbar. Siempre con su
#: etiqueta, nunca solo el color.
SOURCE_TOKENS: dict[PriceSource, str] = {
    PriceSource.MARKET: "ok",
    PriceSource.CACHE: "ok",
    PriceSource.STALE: "warn",
    PriceSource.COST: "warn",
}
_SOURCE_ORDER = (PriceSource.MARKET, PriceSource.CACHE, PriceSource.STALE, PriceSource.COST)

CLASS_LABELS: dict[AssetClass, str] = {
    AssetClass.STOCK: "Acción",
    AssetClass.ETF: "ETF",
    AssetClass.ETC: "ETC",
    AssetClass.CRYPTO: "Cripto",
}

NO_THESIS = "sin tesis"
DASH = "—"
FIFO_NOTE = (
    "El PnL se mide contra el coste medio ponderado: no sirve para la declaración de la renta, "
    "que va por FIFO."
)
ROW_HEIGHT = 34


def pretty_sector(sector: str) -> str:
    """«Renta_Variable_Global» → «Renta Variable Global»."""
    return sector.replace("_", " ")


def when(moment: datetime, now: datetime) -> str:
    """«hoy a las 18:05» o «23/09/2026 a las 18:05»."""
    dia = "hoy" if moment.date() == now.date() else f"{moment:%d/%m/%Y}"
    return f"{dia} a las {moment:%H:%M}"


def _levels(value: Decimal | None, currency: str) -> str:
    return f"{format_price(value)} {currency}" if value is not None else DASH


def _number(value: Decimal | None) -> float:
    return float(value) if value is not None else float("-inf")


# -- la tabla ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioRow:
    """Una fila: la posición valorada y su tesis activa, si la hay."""

    position: PositionValue
    thesis: Thesis | None = None


class PortfolioModel(QAbstractTableModel):
    """Las posiciones valoradas. Los colores los pide al tema en cada momento."""

    def __init__(self, colors: Callable[[str], Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[PortfolioRow] = []
        self._colors = colors

    def set_rows(self, rows: list[PortfolioRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, row: int) -> PortfolioRow:
        return self._rows[row]

    def refresh_colors(self) -> None:
        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(self._rows) - 1, len(HEADERS) - 1)
            )

    # -- QAbstractTableModel ------------------------------------------------------------

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()  # noqa: B008
    ) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()  # noqa: B008
    ) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[Col(section)]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return self._alignment(Col(section))
        return None

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if not index.isValid():
            return None
        fila = self._rows[index.row()]
        columna = Col(index.column())
        if role == Qt.ItemDataRole.DisplayRole:
            return self.text(fila, columna)
        if role == SORT_ROLE:
            return self._sort_key(fila, columna)
        if role == SOURCE_ROLE and columna is Col.SOURCE:
            return fila.position.source
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return self._alignment(columna)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._foreground(fila, columna)
        if role == Qt.ItemDataRole.FontRole and columna is Col.TICKER:
            fuente = QFont()
            fuente.setBold(True)
            return fuente
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.tooltip(fila, columna) or None
        return None

    # -- contenido ----------------------------------------------------------------------

    @staticmethod
    def text(fila: PortfolioRow, columna: Col) -> str:
        p = fila.position
        tesis = fila.thesis
        if columna is Col.TICKER:
            return p.ticker
        if columna is Col.NAME:
            return p.name
        if columna is Col.UNITS:
            return format_units(p.units)
        if columna is Col.PRICE:
            if p.price is None:
                return DASH
            return f"{format_price(p.price.price)} {p.price.currency}"
        if columna is Col.VALUE:
            return format_amount(p.value_eur)
        if columna is Col.WEIGHT:
            return format_pct(p.weight)
        if columna is Col.PNL_EUR:
            return format_signed_amount(p.pnl_eur) if p.pnl_eur is not None else DASH
        if columna is Col.PNL_PCT:
            return format_pct(p.pnl_pct, signed=True) if p.pnl_pct is not None else DASH
        if columna is Col.SOURCE:
            return SOURCE_LABELS[p.source]
        if columna is Col.STOP:
            return NO_THESIS if tesis is None else _levels(tesis.stop, tesis.levels_currency)
        if columna is Col.TARGET:
            return DASH if tesis is None else _levels(tesis.target, tesis.levels_currency)
        return ""

    @staticmethod
    def _sort_key(fila: PortfolioRow, columna: Col) -> Any:
        p = fila.position
        tesis = fila.thesis
        claves: dict[Col, Callable[[], Any]] = {
            Col.TICKER: lambda: p.ticker.lower(),
            Col.NAME: lambda: p.name.lower(),
            Col.UNITS: lambda: float(p.units),
            Col.PRICE: lambda: _number(p.price.price if p.price else None),
            Col.VALUE: lambda: float(p.value_eur),
            Col.WEIGHT: lambda: float(p.weight),
            Col.PNL_EUR: lambda: _number(p.pnl_eur),
            Col.PNL_PCT: lambda: _number(p.pnl_pct),
            Col.SOURCE: lambda: _SOURCE_ORDER.index(p.source),
            Col.STOP: lambda: _number(tesis.stop if tesis else None),
            Col.TARGET: lambda: _number(tesis.target if tesis else None),
        }
        return claves[columna]()

    @staticmethod
    def _alignment(columna: Col) -> Qt.AlignmentFlag:
        if columna in NUMERIC_COLUMNS:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

    def _foreground(self, fila: PortfolioRow, columna: Col) -> Any:
        p = fila.position
        if columna in (Col.PNL_EUR, Col.PNL_PCT) and p.pnl_eur is not None:
            signo = format_signed_amount(p.pnl_eur)[0]  # lo que se ve: un PnL de 0,00 va sin color
            if signo == "+":
                return self._colors("ok")
            if signo == MINUS:
                return self._colors("danger")
        if columna is Col.STOP and fila.thesis is None:
            return self._colors("text_muted")
        return None

    @staticmethod
    def tooltip(fila: PortfolioRow, columna: Col) -> str:
        p = fila.position
        if columna in (Col.SOURCE, Col.PRICE):
            return source_detail(p)
        if columna in (Col.PNL_EUR, Col.PNL_PCT, Col.VALUE):
            return (
                f"Coste: {format_eur(p.cost_eur)} ({format_price(p.avg_cost_eur)} € por "
                "unidad, con comisiones)." + (f"\n{p.note}" if p.at_cost and p.note else "")
            )
        return ""


def source_detail(p: PositionValue) -> str:
    """De dónde sale el valor de una posición, con fechas: para la ayuda emergente."""
    lineas = [f"Procedencia: {SOURCE_LABELS[p.source]}."]
    if p.price is not None:
        descargado = f"{p.price.fetched_at:%d/%m/%Y a las %H:%M}"
        lineas.append(
            f"Precio: {format_price(p.price.price)} {p.price.currency}, cierre del "
            f"{p.price.price_date:%d/%m/%Y}, descargado el {descargado} "
            f"({SOURCE_LABELS[p.price_source].lower()})."
        )
    if p.fx is not None and p.fx_to_eur is not None and p.fx_source is not None:
        descargado = (
            f"descargado el {p.fx.fetched_at:%d/%m/%Y a las %H:%M}"
            if p.fx.fetched_at is not None
            else "sin hora de descarga"
        )
        lineas.append(
            f"Cambio {p.fx.currency}→EUR: {format_price(p.fx.rate_to_eur)}, del "
            f"{p.fx.rate_date:%d/%m/%Y}, {descargado} ({SOURCE_LABELS[p.fx_source].lower()})."
        )
    if p.note:
        lineas.append(p.note)
    return "\n".join(lineas)


class SourceChipDelegate(QStyledItemDelegate):
    """La procedencia como etiqueta con su color de estado («Mercado», «Coste»…)."""

    PAD_X = 10
    PAD_Y = 3

    def __init__(self, theme: ThemeController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        opcion = QStyleOptionViewItem(option)
        self.initStyleOption(opcion, index)
        texto = opcion.text
        opcion.text = ""
        estilo = opcion.widget.style() if opcion.widget is not None else QApplication.style()
        estilo.drawControl(QStyle.ControlElement.CE_ItemViewItem, opcion, painter, opcion.widget)
        origen = index.data(SOURCE_ROLE)
        if origen is None or not texto:
            return
        color_texto, fondo, borde = theme_module.chip_colors(
            self._theme.effective, SOURCE_TOKENS[origen]
        )
        fuente = QFont(option.font)
        fuente.setBold(True)
        medidas = QFontMetrics(fuente)
        ancho = medidas.horizontalAdvance(texto) + 2 * self.PAD_X
        alto = medidas.height() + 2 * self.PAD_Y
        rect = QRectF(
            option.rect.left() + 6, option.rect.center().y() - alto / 2 + 1, ancho, alto
        )
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(borde, 1))
        painter.setBrush(fondo)
        painter.drawRoundedRect(rect, alto / 2, alto / 2)
        painter.setFont(fuente)
        painter.setPen(color_texto)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, texto)
        painter.restore()

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        base = super().sizeHint(option, index)
        fuente = QFont(option.font)
        fuente.setBold(True)
        ancho = QFontMetrics(fuente).horizontalAdvance(str(index.data() or "")) + 2 * self.PAD_X
        return QSize(max(base.width(), ancho + 16), base.height())


# -- exposición por sector --------------------------------------------------------------


class ExposureBar(QWidget):
    """Una barra horizontal: fondo, relleno con el peso y una marca en el tope."""

    def __init__(self, theme: ThemeController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._fraction = 0.0
        self._limit: float | None = None
        self._token = "accent"
        self.setMinimumSize(120, 16)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, fraction: float, limit: float | None, token: str) -> None:
        """`fraction` y `limit` van de 0 a 1 sobre el ancho de la barra."""
        self._fraction = max(0.0, min(1.0, fraction))
        self._limit = None if limit is None else max(0.0, min(1.0, limit))
        self._token = token
        self.update()

    def paintEvent(self, _event: object) -> None:
        tema = self._theme.effective
        pintor = QPainter(self)
        pintor.setRenderHint(QPainter.RenderHint.Antialiasing)
        alto = 8.0
        arriba = (self.height() - alto) / 2
        ancho = float(self.width())
        pintor.setPen(Qt.PenStyle.NoPen)
        pintor.setBrush(theme_module.color(tema, "surface_alt"))
        pintor.drawRoundedRect(QRectF(0, arriba, ancho, alto), alto / 2, alto / 2)
        if self._fraction > 0:
            pintor.setBrush(theme_module.color(tema, self._token))
            relleno = max(alto, ancho * self._fraction)
            pintor.drawRoundedRect(QRectF(0, arriba, relleno, alto), alto / 2, alto / 2)
        if self._limit is not None:
            pintor.setPen(QPen(theme_module.color(tema, "text"), 1.5))
            x = ancho * self._limit
            pintor.drawLine(QPointF(x, arriba - 4), QPointF(x, arriba + alto + 4))
        pintor.end()


class ExposureCard(QFrame):
    """«Exposición por sector»: cada sector frente al tope del mandato, y el efectivo."""

    def __init__(self, theme: ThemeController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._theme = theme
        caja = QVBoxLayout(self)
        caja.setContentsMargins(20, 18, 20, 18)
        caja.setSpacing(10)
        titulo = QLabel("Exposición por sector")
        titulo.setObjectName("cardTitle")
        caja.addWidget(titulo)
        self.subtitle = muted("")
        caja.addWidget(self.subtitle)
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(16)
        self._grid.setVerticalSpacing(8)
        self._grid.setColumnStretch(1, 1)
        caja.addLayout(self._grid)
        #: (sector, texto del porcentaje, supera el tope) de cada fila, para los tests.
        self.rows: list[tuple[str, str, bool]] = []
        self._bars: list[ExposureBar] = []

    def set_valuation(self, valuation: Valuation, limit: Decimal) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.rows = []
        self._bars = []
        tope = format_pct(limit, 0 if limit * 100 == int(limit * 100) else 1)
        self.subtitle.setText(f"La marca vertical es el tope del {tope} por sector del mandato.")

        pesos = [s.weight for s in valuation.sectors] + [valuation.cash_weight]
        escala = float(min(Decimal(1), max([limit * 2, *pesos, Decimal("0.01")])))
        filas = [
            (pretty_sector(s.sector), s.weight, s.exceeds(limit), limit)
            for s in valuation.sectors
        ]
        filas.append(("Efectivo", valuation.cash_weight, False, None))
        for numero, (nombre, peso, supera, marca) in enumerate(filas):
            etiqueta = QLabel(nombre)
            barra = ExposureBar(self._theme)
            token = "danger" if supera else ("text_muted" if marca is None else "accent")
            barra.set_values(
                float(peso) / escala, None if marca is None else float(marca) / escala, token
            )
            texto = format_pct(peso)
            if supera:
                texto += f" · supera el {tope}"
            cifra = state_label(texto, "dangerText" if supera else "exposureValue")
            cifra.setWordWrap(False)
            cifra.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._grid.addWidget(etiqueta, numero, 0)
            self._grid.addWidget(barra, numero, 1)
            self._grid.addWidget(cifra, numero, 2)
            self.rows.append((nombre, texto, supera))
            self._bars.append(barra)

    def repaint_bars(self) -> None:
        for barra in self._bars:
            barra.update()


# -- el pie -----------------------------------------------------------------------------


def footer_text(valuation: Valuation, now: datetime) -> str:
    """El pie de la Cartera: patrimonio, cobertura, qué no es fiable y por qué, y el FIFO."""
    partes = [
        f"Patrimonio (NAV): {format_eur(valuation.nav_eur)} · efectivo "
        f"{format_eur(valuation.cash_eur)} ({format_pct(valuation.cash_weight)})."
    ]
    total = len(valuation.positions)
    if total:
        fiables = total - len(valuation.unreliable)
        partes.append(
            f"Cobertura {format_pct(valuation.coverage)}: {fiables} de {total} posiciones con "
            "precio fiable (de mercado o guardado hace menos de 24 horas)."
        )
    else:
        partes.append(f"Cobertura {format_pct(valuation.coverage)}.")
    if not valuation.reliable:
        partes.append("Por debajo del 90 %: esta valoración no es fiable.")
    for p in valuation.unreliable:
        partes.append(f"{p.ticker}: {p.note}" if p.note else f"{p.ticker}: precio no fiable.")
    if total:
        partes.append(FIFO_NOTE)
    if valuation.market_at is not None:
        partes.append(f"Última actualización de precios: {when(valuation.market_at, now)}.")
    elif total:
        partes.append("Pulsa «Actualizar precios» para descargar los de ahora.")
    return " ".join(partes)


# -- el trabajo de fondo -----------------------------------------------------------------


@dataclass(frozen=True)
class RefreshOutcome:
    refresh: MarketRefresh
    valuation: Valuation


def refresh_and_value(
    db: Database,
    prices: PriceProvider,
    fx: FxProvider,
    now: Callable[[], datetime],
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> RefreshOutcome:
    """Descarga, guarda y valora. Corre en un hilo de trabajo."""
    refresco = refresh_market(db, prices, fx, now(), progress, cancel)
    valoracion = load_valuation(db.connection(), now(), refresco.fetched_at)
    return RefreshOutcome(refresco, valoracion)


# -- la página --------------------------------------------------------------------------


class PortfolioPage(QWidget):
    """La sección Cartera."""

    #: Ha terminado una actualización de precios (bien o mal).
    refreshFinished = Signal()

    def __init__(
        self,
        db: Database,
        theme: ThemeController,
        prices: PriceProvider,
        fx: FxProvider,
        *,
        settings: Settings | None = None,
        now: Callable[[], datetime] = local_now,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._theme = theme
        self._prices = prices
        self._fx = fx
        self._settings = settings or Settings()
        self._now = now
        self._market_at: datetime | None = None
        self._cancel: threading.Event | None = None
        self._worker: Worker | None = None
        self.valuation: Valuation | None = None
        self.last_refresh: MarketRefresh | None = None

        self.header_actions = self._build_actions()

        exterior = QVBoxLayout(self)
        exterior.setContentsMargins(0, 0, 0, 0)
        desplazable = QScrollArea()
        desplazable.setWidgetResizable(True)
        desplazable.setFrameShape(QFrame.Shape.NoFrame)
        exterior.addWidget(desplazable)
        interior = QWidget()
        desplazable.setWidget(interior)
        caja = QVBoxLayout(interior)
        caja.setContentsMargins(0, 0, 0, 0)
        caja.setSpacing(16)

        caja.addWidget(self._build_progress())
        self.messages_box = QFrame()
        self.messages_box.setObjectName("warnBox")
        mensajes = QVBoxLayout(self.messages_box)
        mensajes.setContentsMargins(14, 10, 14, 10)
        self.messages_label = QLabel()
        self.messages_label.setWordWrap(True)
        self.messages_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        mensajes.addWidget(self.messages_label)
        self.messages_box.setVisible(False)
        caja.addWidget(self.messages_box)

        caja.addWidget(self._build_table_card())
        self.exposure = ExposureCard(theme)
        caja.addWidget(self.exposure)
        self.footer = muted("")
        self.footer.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        caja.addWidget(self.footer)
        caja.addStretch(1)

        theme.themeChanged.connect(self._on_theme_changed)
        self.reload()

    # -- construcción ------------------------------------------------------------------

    def _build_actions(self) -> QWidget:
        acciones = QWidget()
        fila = QHBoxLayout(acciones)
        fila.setContentsMargins(0, 0, 0, 0)
        fila.setSpacing(10)
        self.edit_button = QPushButton("Editar activo")
        self.edit_button.setToolTip("Símbolo de cotización (con «Probar»), sector y clase.")
        self.edit_button.clicked.connect(self.edit_selected)
        fila.addWidget(self.edit_button)
        self.refresh_button = QPushButton("Actualizar precios (gratis)")
        self.refresh_button.setToolTip(
            "Descarga el último cierre de cada posición y los tipos de cambio. No llama a Claude."
        )
        self.refresh_button.clicked.connect(self.start_refresh)
        fila.addWidget(self.refresh_button)
        return acciones

    def _build_progress(self) -> QWidget:
        self.progress_box = QFrame()
        self.progress_box.setObjectName("card")
        fila = QHBoxLayout(self.progress_box)
        fila.setContentsMargins(16, 10, 16, 10)
        fila.setSpacing(12)
        self.progress_label = QLabel("Descargando precios…")
        fila.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 0)
        fila.addWidget(self.progress_bar, 1)
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.clicked.connect(self.cancel_refresh)
        fila.addWidget(self.cancel_button)
        self.progress_box.setVisible(False)
        return self.progress_box

    def _build_table_card(self) -> QFrame:
        marco, contenido = card("Posiciones")
        self.empty_label = muted("No hay posiciones: la cartera es solo efectivo.")
        contenido.addWidget(self.empty_label)

        self.model = PortfolioModel(self._color, self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(Col.VALUE, Qt.SortOrder.DescendingOrder)
        self.table.setItemDelegateForColumn(Col.SOURCE, SourceChipDelegate(self._theme, self))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        fuente = self.table.font()
        with suppress(AttributeError, TypeError):  # cifras de ancho fijo, si Qt lo sabe hacer
            fuente.setFeature(QFont.Tag("tnum"), 1)
            self.table.setFont(fuente)
        cabecera = self.table.horizontalHeader()
        cabecera.setHighlightSections(False)
        cabecera.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        cabecera.setSectionResizeMode(Col.NAME, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(lambda _indice: self.edit_selected())
        self.table.selectionModel().selectionChanged.connect(self._update_buttons)
        contenido.addWidget(self.table)
        return marco

    # -- datos -------------------------------------------------------------------------

    def _color(self, token: str) -> Any:
        return theme_module.color(self._theme.effective, token)

    @property
    def sector_limit(self) -> Decimal:
        return Decimal(str(self._settings.mandate.max_sector_weight_pct)) / 100

    def reload(self) -> None:
        """Vuelve a valorar con lo guardado (sin descargar nada)."""
        valoracion = load_valuation(self._db.connection(), self._now(), self._market_at)
        self._show(valoracion)

    def _show(self, valuation: Valuation) -> None:
        self.valuation = valuation
        tesis = {
            t.ticker: t
            for t in ThesisRepository(self._db.connection()).list_all()
            if t.status is ThesisStatus.ACTIVE
        }
        filas = [PortfolioRow(p, tesis.get(p.ticker)) for p in valuation.positions]
        self.model.set_rows(filas)
        vacia = not filas
        self.empty_label.setVisible(vacia)
        self.table.setVisible(not vacia)
        self._fit_table()
        self.exposure.set_valuation(valuation, self.sector_limit)
        self.footer.setText(footer_text(valuation, self._now()))
        self._update_buttons()

    def _fit_table(self) -> None:
        """La tabla, tan alta como sus filas: la página entera es la que se desplaza."""
        filas = self.proxy.rowCount()
        alto = self.table.horizontalHeader().sizeHint().height() + filas * ROW_HEIGHT + 4
        if self.table.horizontalScrollBar().isVisible():
            alto += self.table.horizontalScrollBar().sizeHint().height()
        self.table.setFixedHeight(alto)

    def rows(self) -> list[PortfolioRow]:
        """Las filas en el orden en que se ven."""
        return [
            self.model.row_at(self.proxy.mapToSource(self.proxy.index(r, 0)).row())
            for r in range(self.proxy.rowCount())
        ]

    def selected_row(self) -> PortfolioRow | None:
        seleccion = self.table.selectionModel().selectedRows()
        if not seleccion:
            return None
        return self.model.row_at(self.proxy.mapToSource(seleccion[0]).row())

    def select_ticker(self, ticker: str) -> bool:
        for r in range(self.proxy.rowCount()):
            fila = self.model.row_at(self.proxy.mapToSource(self.proxy.index(r, 0)).row())
            if fila.position.ticker == ticker:
                self.table.selectRow(r)
                return True
        return False

    @property
    def refreshing(self) -> bool:
        return self._worker is not None

    def _update_buttons(self, *_args: object) -> None:
        self.refresh_button.setEnabled(not self.refreshing)
        self.edit_button.setEnabled(not self.refreshing and self.selected_row() is not None)

    def _show_messages(self, lines: list[str], state: str = "warnBox") -> None:
        if self.messages_box.objectName() != state:
            self.messages_box.setObjectName(state)
            self.messages_box.style().unpolish(self.messages_box)
            self.messages_box.style().polish(self.messages_box)
        self.messages_label.setText("\n".join(lines))
        self.messages_box.setVisible(bool(lines))

    # -- actualizar precios ------------------------------------------------------------

    def start_refresh(self) -> None:
        """«Actualizar precios»: en un hilo de trabajo, con progreso y «Cancelar»."""
        if self.refreshing:
            return
        self._cancel = threading.Event()
        trabajo = Worker(
            refresh_and_value, self._db, self._prices, self._fx, self._now, cancel=self._cancel
        ).pass_progress()
        trabajo.signals.progress.connect(self._on_progress)
        trabajo.signals.finished.connect(self._on_refresh_done)
        trabajo.signals.failed.connect(self._on_refresh_failed)
        self._worker = trabajo
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("Descargando precios…")
        self.cancel_button.setEnabled(True)
        self.progress_box.setVisible(True)
        self._show_messages([])
        self._update_buttons()
        start(trabajo)

    def cancel_refresh(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
            self.cancel_button.setEnabled(False)
            self.progress_label.setText("Cancelando…")

    def shutdown(self) -> None:
        """Al cerrar la app: que una descarga a medias no la retenga."""
        if self._cancel is not None:
            self._cancel.set()

    def _on_progress(self, done: int, total: int) -> None:
        if self._cancel is not None and self._cancel.is_set():
            return
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(done)
        self.progress_label.setText(f"Descargando precios: {done} de {total}…")

    def _finish_refresh(self) -> None:
        self._worker = None
        self._cancel = None
        self.progress_box.setVisible(False)
        self._update_buttons()
        self.refreshFinished.emit()

    def _on_refresh_done(self, outcome: RefreshOutcome) -> None:
        self.last_refresh = outcome.refresh
        self._market_at = outcome.refresh.fetched_at
        self._show(outcome.valuation)
        self._show_messages(outcome.refresh.messages)
        self._finish_refresh()

    def _on_refresh_failed(self, message: str) -> None:
        self._show_messages([f"No se han podido actualizar los precios: {message}"], "dangerBox")
        self._finish_refresh()

    # -- editar un activo --------------------------------------------------------------

    def edit_selected(self) -> None:
        fila = self.selected_row()
        if fila is None or self.refreshing:
            return
        activo = AssetRepository(self._db.connection()).get(fila.position.ticker)
        if activo is None:
            return
        dialogo = EditAssetDialog(self._db, activo, self._prices, self)
        try:
            if self.run_dialog(dialogo):
                cambio = dialogo.saved is not None and (
                    dialogo.saved.yahoo_symbol != activo.yahoo_symbol
                )
                self.reload()
                self.select_ticker(activo.ticker)
                self._show_messages(
                    [
                        f"{activo.ticker}: símbolo cambiado. Pulsa «Actualizar precios» para "
                        "descargar su precio."
                    ]
                    if cambio
                    else []
                )
        finally:
            dialogo.deleteLater()

    def run_dialog(self, dialog: QDialog) -> bool:
        """Enseña el diálogo y dice si se ha guardado (los tests lo sustituyen)."""
        return dialog.exec() == QDialog.DialogCode.Accepted

    # -- tema y visibilidad ------------------------------------------------------------

    def _on_theme_changed(self, *_args: object) -> None:
        self.model.refresh_colors()
        self.table.viewport().update()
        self.exposure.repaint_bars()

    def showEvent(self, event: object) -> None:
        super().showEvent(event)
        if not self.refreshing:
            self.reload()


# -- «Editar activo» ----------------------------------------------------------------------


class EditAssetDialog(QDialog):
    """Símbolo de Yahoo (con «Probar» y la sugerencia por ISIN), sector y clase.

    La divisa no se edita: la manda el proveedor al actualizar precios (GUIA §5.3).
    """

    def __init__(
        self,
        db: Database,
        asset: Asset,
        prices: PriceProvider,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._prices = prices
        self.asset = asset
        self.saved: Asset | None = None
        self._workers: list[Worker] = []

        self.setWindowTitle(f"Editar activo · {asset.ticker}")
        self.setMinimumWidth(520)
        caja = QVBoxLayout(self)
        caja.setContentsMargins(20, 18, 20, 18)
        caja.setSpacing(8)

        titulo = QLabel(f"{asset.ticker} · {asset.name}")
        titulo.setObjectName("cardTitle")
        caja.addWidget(titulo)
        datos = f"Divisa de cotización: {asset.currency}"
        if asset.isin:
            datos += f" · ISIN {asset.isin}"
        caja.addWidget(muted(datos))
        caja.addSpacing(6)

        caja.addWidget(QLabel("Símbolo de Yahoo Finance"))
        fila = QHBoxLayout()
        self.symbol_edit = QLineEdit(asset.yahoo_symbol or "")
        self.symbol_edit.setPlaceholderText("Por ejemplo, SAN.MC")
        self.symbol_edit.textChanged.connect(self._on_symbol_changed)
        fila.addWidget(self.symbol_edit, 1)
        self.probe_button = QPushButton("Probar")
        self.probe_button.setToolTip("Pide a Yahoo el último cierre de este símbolo (gratis).")
        self.probe_button.clicked.connect(self.probe)
        fila.addWidget(self.probe_button)
        caja.addLayout(fila)
        self.probe_status = state_label()
        self.probe_status.setVisible(False)
        caja.addWidget(self.probe_status)

        self.suggest_button: QPushButton | None = None
        self.suggestions = QComboBox()
        self.suggestions.setVisible(False)
        self.suggestions.activated.connect(self._on_suggestion_chosen)
        self.suggest_status = state_label()
        self.suggest_status.setVisible(False)
        if asset.isin:
            fila = QHBoxLayout()
            self.suggest_button = QPushButton("Sugerir por ISIN")
            self.suggest_button.setToolTip(f"Busca en Yahoo los símbolos del ISIN {asset.isin}.")
            self.suggest_button.clicked.connect(self.suggest)
            fila.addWidget(self.suggest_button)
            fila.addWidget(self.suggestions, 1)
            caja.addLayout(fila)
            caja.addWidget(self.suggest_status)
        caja.addSpacing(6)

        caja.addWidget(QLabel("Sector"))
        self.sector_edit = QLineEdit(asset.sector or "")
        self.sector_edit.setPlaceholderText("Sin espacios: Tecnologia, Renta_Variable…")
        caja.addWidget(self.sector_edit)

        caja.addWidget(QLabel("Clase"))
        self.class_combo = QComboBox()
        for clase, etiqueta in CLASS_LABELS.items():
            self.class_combo.addItem(etiqueta, clase)
        self.class_combo.setCurrentIndex(self.class_combo.findData(asset.asset_class))
        caja.addWidget(self.class_combo)

        self.error_label = state_label(state="dangerText")
        self.error_label.setVisible(False)
        caja.addWidget(self.error_label)
        caja.addSpacing(6)

        botones = QHBoxLayout()
        botones.addStretch(1)
        cancelar = QPushButton("Cancelar")
        cancelar.clicked.connect(self.reject)
        botones.addWidget(cancelar)
        self.save_button = QPushButton("Guardar")
        self.save_button.setObjectName("primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        botones.addWidget(self.save_button)
        caja.addLayout(botones)
        self._on_symbol_changed()

    # -- guardar -----------------------------------------------------------------------

    def selected_class(self) -> AssetClass:
        return AssetClass(self.class_combo.currentData())

    def save(self) -> None:
        try:
            with self._db.transaction() as conn:
                self.saved = edit_asset(
                    conn,
                    self.asset.ticker,
                    yahoo_symbol=self.symbol_edit.text(),
                    sector=self.sector_edit.text(),
                    asset_class=self.selected_class(),
                )
        except AssetEditError as error:
            set_state(self.error_label, str(error), "dangerText")
            return
        log.info("Activo %s editado", self.asset.ticker)
        self.accept()

    # -- probar ------------------------------------------------------------------------

    def _on_symbol_changed(self, *_args: object) -> None:
        self.probe_button.setEnabled(bool(self.symbol_edit.text().strip()))
        set_state(self.probe_status, "", "muted")

    def _run(
        self, worker: Worker, done: Callable[[Any], None], failed: Callable[[str], None]
    ) -> None:
        worker.signals.finished.connect(done)
        worker.signals.failed.connect(failed)
        self._workers.append(worker)  # sin referencia, las señales se perderían
        start(worker)

    def probe(self) -> None:
        simbolo = self.symbol_edit.text().strip()
        if not simbolo:
            return
        self.probe_button.setEnabled(False)
        set_state(self.probe_status, f"Preguntando a Yahoo por {simbolo}…", "muted")
        self._run(Worker(probe_symbol, self._prices, simbolo), self._on_probe_done,
                  self._on_probe_failed)

    def _on_probe_done(self, quote: Quote) -> None:
        self.probe_button.setEnabled(bool(self.symbol_edit.text().strip()))
        if quote.symbol != self.symbol_edit.text().strip():
            return  # el usuario ya ha escrito otro símbolo
        texto = (
            f"Yahoo: {format_price(quote.price)} {quote.currency}, cierre del "
            f"{quote.close_date:%d/%m/%Y}"
        )
        if quote.name:
            texto += f" · {quote.name}"
        estado = "okText"
        if quote.currency != self.asset.currency:
            texto += (
                f". Ojo: cotiza en {quote.currency} y el activo dice {self.asset.currency}; al "
                f"actualizar precios se usará {quote.currency}."
            )
            estado = "warnText"
        set_state(self.probe_status, texto, estado)

    def _on_probe_failed(self, message: str) -> None:
        self.probe_button.setEnabled(bool(self.symbol_edit.text().strip()))
        set_state(self.probe_status, message, "dangerText")

    # -- sugerir por ISIN --------------------------------------------------------------

    def suggest(self) -> None:
        if not self.asset.isin or self.suggest_button is None:
            return
        self.suggest_button.setEnabled(False)
        set_state(self.suggest_status, f"Buscando el ISIN {self.asset.isin} en Yahoo…", "muted")
        self._run(Worker(self._prices.search_isin, self.asset.isin), self._on_suggestions,
                  self._on_suggest_failed)

    def _on_suggestions(self, suggestions: list[SymbolSuggestion]) -> None:
        if self.suggest_button is not None:
            self.suggest_button.setEnabled(True)
        self.suggestions.clear()
        if not suggestions:
            self.suggestions.setVisible(False)
            set_state(self.suggest_status, "Yahoo no conoce ningún símbolo con ese ISIN.",
                      "warnText")
            return
        for s in suggestions:
            detalle = " · ".join(x for x in (s.name, s.exchange, s.quote_type) if x)
            self.suggestions.addItem(f"{s.symbol} — {detalle}" if detalle else s.symbol,
                                     s.symbol)
        self.suggestions.setVisible(True)
        set_state(
            self.suggest_status,
            "Elige uno de la lista y pulsa «Probar» para ver su precio.",
            "muted",
        )
        if not self.symbol_edit.text().strip():
            self._on_suggestion_chosen(0)

    def _on_suggestion_chosen(self, index: int) -> None:
        simbolo = self.suggestions.itemData(index)
        if simbolo:
            self.symbol_edit.setText(str(simbolo))

    def _on_suggest_failed(self, message: str) -> None:
        if self.suggest_button is not None:
            self.suggest_button.setEnabled(True)
        set_state(self.suggest_status, message, "dangerText")
