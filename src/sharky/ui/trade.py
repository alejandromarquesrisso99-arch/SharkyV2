"""Operar (GUIA §5.5 y §5.10, punto 3; §7, H8).

- A la izquierda, el registro de una compra o una venta ya ejecutada en el bróker: ticker (con
  autocompletado; si es nuevo, sus datos), unidades, precio, divisa y su cambio a EUR, comisión,
  fecha y, en las compras, stop, objetivo y divisa de los niveles. «Revisar» enseña la
  validación del mandato antes de guardar nada: si cumple, «Registrar»; si no, el primer paso
  que falla con su motivo (y los demás debajo) y, si solo falla el mandato, «Registrar
  igualmente», que pide confirmación y un motivo.
- A la derecha, la posición en ese ticker, los límites del mandato en el estado vigente y los
  movimientos de efectivo sueltos (ingreso, retirada, dividendo, interés, comisión, impuesto y
  «Ajustar saldo»).

Aquí no se calcula nada: la validación sale de `core.mandate` y el registro, en una sola
transacción, de los repositorios.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from PySide6.QtCore import QDate, QStringListModel, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sharky.core.csv_import import asset_from_fields
from sharky.core.formatting import (
    format_eur,
    format_limit_pct,
    format_number,
    format_pct,
    format_price,
    format_signed_amount,
    format_units,
    parse_decimal,
)
from sharky.core.ledger import CASH_LABELS, MANUAL_CASH_SIGNS, TAX_WARNING, adjustment_amount
from sharky.core.levels import LevelChoice, LevelUpdate, format_level
from sharky.core.mandate import (
    FLOW_KINDS,
    STATE_LABELS,
    STATE_TOKENS,
    CheckResult,
    MandateRules,
    buying_allowed,
)
from sharky.core.models import (
    Asset,
    AssetClass,
    CashKind,
    CashMovement,
    MandateState,
    Thesis,
    TradeKind,
)
from sharky.core.valuation import BASE_CURRENCY, Valuation, normalize_currency
from sharky.services.db import Database
from sharky.services.market import local_now
from sharky.services.repositories import (
    AssetRepository,
    RecordedTrade,
    ThesisRepository,
    TradeError,
    TradeReview,
    TradeTicket,
    current_state,
    load_valuation,
    rate_to_eur,
    record_balance_adjustment,
    record_cash_movement,
    record_trade,
    review_trade,
)
from sharky.services.settings import Settings
from sharky.ui.pages import card, muted, restyle, set_state, state_label
from sharky.ui.portfolio import CLASS_LABELS, PriceRefresher, RefreshOutcome
from sharky.ui.theme import ThemeController

log = logging.getLogger(__name__)

RIGHT_WIDTH = 330

FORCE_LABEL = "Registrar igualmente (queda marcada como forzada y el diario lo menciona)"
FLOW_NOTE = (
    "Ingresos y retiradas cambian el número de participaciones, no su valor: no cuentan como "
    "ganancia ni como pérdida."
)
#: Estilo de la línea de estado de cada color (siempre con su texto, nunca solo el color).
TOKEN_TEXT = {"ok": "okText", "warn": "warnText", "danger": "dangerText"}


# -- utilidades --------------------------------------------------------------------------------


def _parse(text: str, name: str, errors: list[str], *, required: bool = False) -> Decimal | None:
    """Un número de un campo (coma o punto). Vacío es None, o un error si hace falta."""
    limpio = text.strip()
    if not limpio:
        if required:
            errors.append(f"Falta {name}.")
        return None
    try:
        return parse_decimal(limpio)
    except ValueError:
        errors.append(f"{name[0].upper()}{name[1:]}: «{limpio}» no es un número.")
        return None


def _qdate(day: date) -> QDate:
    return QDate(day.year, day.month, day.day)


def _plain(value: Decimal) -> str:
    """Un número para escribirlo en un campo, sin separador de miles: 1234,5."""
    texto = f"{value.normalize():f}"
    return texto.replace(".", ",")


def _lower_first(text: str) -> str:
    """«La liquidez caería…» → «la liquidez caería…», pero «ASML quedaría…» se queda igual."""
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def also_text(others: tuple[CheckResult, ...]) -> str:
    """«Además, la liquidez caería…» (uno) o una lista (varios)."""
    if not others:
        return ""
    if len(others) == 1:
        return f"Además, {_lower_first(others[0].message)}"
    return "Además:\n" + "\n".join(f"· {r.message}" for r in others)


def steps_text(review: TradeReview) -> str:
    """Los pasos de la validación, uno por línea, con ✓ o ✗ (no solo el color)."""
    return "\n".join(
        f"{'✓' if r.passed else '✗'} {r.message}" for r in review.validation.results
    )


def outcome_text(review: TradeReview) -> str:
    """Lo que hará la operación al registrarla: importe, efectivo, tesis y PnL."""
    op = review.ticket
    partes: list[str] = []
    if op.is_buy:
        total = op.amount_eur + op.fee_eur
        partes.append(
            f"Importe {format_eur(op.amount_eur)} + comisión {format_eur(op.fee_eur)} = "
            f"{format_eur(total)}. Efectivo después: "
            f"{format_eur(review.valuation.cash_eur - total)}."
        )
        if review.new_asset:
            partes.append(f"Se dará de alta el activo {op.ticker} ({review.asset.name}).")
        if review.opens_thesis and op.levels_currency is not None:
            niveles = " y ".join(
                f"{nombre} {format_level(valor, op.levels_currency)}"
                for nombre, valor in (("stop", op.stop), ("objetivo", op.target))
                if valor is not None
            )
            partes.append(
                f"{op.ticker} no tiene tesis: se abrirá con la entrada al precio de compra y "
                f"{niveles}. Luego la completas en Tesis."
            )
        elif review.level_update is not None:
            partes.append(
                "El stop o el objetivo no coinciden con los de su tesis: al registrar te "
                "pregunto si la actualizas."
            )
        return "\n".join(partes)
    neto = op.amount_eur - op.fee_eur
    partes.append(f"Importe neto {format_eur(neto)} (importe − comisión).")
    if review.sale_pnl_eur is not None:
        partes.append(
            f"PnL realizado de esta venta: {format_signed_amount(review.sale_pnl_eur)} € "
            "(coste medio ponderado)."
        )
    if review.closes_position:
        texto = "Vendes toda la posición."
        if review.closes_thesis and review.position_pnl_eur is not None:
            texto += (
                " Se cerrará su tesis con el PnL realizado de toda la posición: "
                f"{format_signed_amount(review.position_pnl_eur)} €."
            )
        partes.append(texto)
    elif review.thesis is not None:
        partes.append("Venta parcial: su tesis sigue abierta.")
    partes.append(TAX_WARNING)
    return "\n".join(partes)


def done_text(recorded: RecordedTrade) -> str:
    """El aviso tras registrar: «Compra registrada: 1 ASML a 700,00 EUR.»."""
    t = recorded.trade
    que = "Compra registrada" if t.kind is TradeKind.BUY else "Venta registrada"
    texto = f"{que}: {format_units(t.units)} {t.ticker} a {format_price(t.price)} {t.currency}."
    if recorded.forced:
        texto += " Queda marcada como forzada."
    if recorded.opened_thesis is not None:
        texto += " Su tesis se ha abierto: complétala en Tesis."
    elif recorded.updated_thesis is not None:
        texto += " Tu decisión sobre su tesis queda en el historial."
    elif recorded.closed_thesis is not None:
        texto += " Su tesis se ha cerrado."
    return texto


def _field(label: str, widget: QWidget) -> QWidget:
    """Un campo con su etiqueta encima; esconder el campo esconde también la etiqueta."""
    caja = QWidget()
    columna = QVBoxLayout(caja)
    columna.setContentsMargins(0, 0, 0, 0)
    columna.setSpacing(4)
    texto = QLabel(label)
    texto.setObjectName("fieldLabel")
    columna.addWidget(texto)
    columna.addWidget(widget)
    return caja


class WrappedCheck(QWidget):
    """Una casilla con su texto en varias líneas: un QCheckBox no parte el texto y obliga a un
    ancho mínimo enorme. Pulsar el texto también la marca."""

    toggled = Signal(bool)

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        fila = QHBoxLayout(self)
        fila.setContentsMargins(0, 0, 0, 0)
        fila.setSpacing(8)
        self.box = QCheckBox()
        self.box.setAccessibleName(text)
        fila.addWidget(self.box, 0, Qt.AlignmentFlag.AlignTop)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        fila.addWidget(self.label, 1)
        self.box.toggled.connect(self.toggled)

    def text(self) -> str:
        return self.label.text()

    def isChecked(self) -> bool:
        return self.box.isChecked()

    def setChecked(self, checked: bool) -> None:
        self.box.setChecked(checked)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.box.toggle()
        super().mousePressEvent(event)


def _currency_combo(*currencies: str) -> QComboBox:
    combo = QComboBox()
    combo.setEditable(True)
    for divisa in currencies:
        combo.addItem(divisa)
    return combo


# -- la página ---------------------------------------------------------------------------------


class TradePage(QWidget):
    """La sección Operar."""

    #: Se ha registrado una operación o un movimiento de efectivo.
    recorded = Signal()
    #: Una compra ha abierto la tesis de este ticker: toca completarla en Tesis.
    thesisOpened = Signal(str)

    def __init__(
        self,
        db: Database,
        theme: ThemeController,
        refresher: PriceRefresher,
        *,
        settings: Settings | None = None,
        now: Callable[[], datetime] = local_now,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._theme = theme
        self._refresher = refresher
        self._settings = settings or Settings()
        self._now = now
        self.valuation: Valuation | None = None
        self.state = MandateState.OPTIMAL
        self.assets: dict[str, Asset] = {}
        self.theses: dict[str, Thesis] = {}
        self.review: TradeReview | None = None
        self._reviewed: tuple[str, ...] | None = None
        #: Lo que Sharky ha puesto en cada campo; lo que escribe el usuario no se toca.
        self._prefilled: dict[int, str] = {}
        self._filling = False

        caja = QHBoxLayout(self)
        caja.setContentsMargins(0, 0, 0, 0)
        caja.setSpacing(16)
        caja.addWidget(self._build_left(), 1)
        caja.addWidget(self._build_right())

        refresher.succeeded.connect(self._on_refreshed)
        self._default_currencies()
        self._on_mode()
        self.reload()

    # -- construcción ------------------------------------------------------------------

    def _build_left(self) -> QWidget:
        desplazable = QScrollArea()
        desplazable.setWidgetResizable(True)
        desplazable.setFrameShape(QFrame.Shape.NoFrame)
        desplazable.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        interior = QWidget()
        desplazable.setWidget(interior)
        caja = QVBoxLayout(interior)
        caja.setContentsMargins(0, 0, 0, 0)
        caja.setSpacing(16)
        caja.addWidget(self._build_form())
        caja.addWidget(self._build_result())
        caja.addStretch(1)
        return desplazable

    def _build_form(self) -> QFrame:
        marco, caja = card("Registrar una operación ya ejecutada en tu bróker")
        caja.setSpacing(12)

        selector = QHBoxLayout()
        selector.setSpacing(0)
        self.buy_button = QPushButton("Compra")
        self.sell_button = QPushButton("Venta")
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        for boton in (self.buy_button, self.sell_button):
            boton.setCheckable(True)
            boton.setCursor(Qt.CursorShape.PointingHandCursor)
            self._mode_group.addButton(boton)
            selector.addWidget(boton)
        selector.addStretch(1)
        self.buy_button.setChecked(True)
        self._mode_group.buttonToggled.connect(lambda _b, marcado: marcado and self._on_mode())
        caja.addLayout(selector)

        self.ticker_edit = QLineEdit()
        self.ticker_edit.setPlaceholderText("ASML")
        self._tickers = QStringListModel(self)
        completar = QCompleter(self._tickers, self)
        completar.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.ticker_edit.setCompleter(completar)
        self.units_edit = QLineEdit()
        self.all_button = QPushButton("Todas")
        self.all_button.setObjectName("link")
        self.all_button.setToolTip("Vender todas las unidades que tienes.")
        self.all_button.clicked.connect(self.fill_all_units)
        unidades = QWidget()
        fila_unidades = QHBoxLayout(unidades)
        fila_unidades.setContentsMargins(0, 0, 0, 0)
        fila_unidades.setSpacing(6)
        fila_unidades.addWidget(self.units_edit, 1)
        fila_unidades.addWidget(self.all_button)
        self.price_edit = QLineEdit()
        self.currency_combo = _currency_combo(BASE_CURRENCY)
        self.fee_edit = QLineEdit()
        self.fee_edit.setPlaceholderText("0,00")
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd/MM/yyyy")
        self.date_edit.setMaximumDate(_qdate(self.today))
        self.date_edit.setDate(_qdate(self.today))
        self.fx_edit = QLineEdit()
        self.fx_hint = muted("")
        self.stop_edit = QLineEdit()
        self.target_edit = QLineEdit()
        self.levels_combo = _currency_combo(BASE_CURRENCY)
        for campo in (self.units_edit, self.price_edit, self.fee_edit, self.fx_edit,
                      self.stop_edit, self.target_edit):
            campo.setAlignment(Qt.AlignmentFlag.AlignRight)

        rejilla = QGridLayout()
        rejilla.setHorizontalSpacing(14)
        rejilla.setVerticalSpacing(10)
        rejilla.addWidget(_field("Ticker", self.ticker_edit), 0, 0)
        rejilla.addWidget(_field("Unidades", unidades), 0, 1)
        rejilla.addWidget(_field("Precio de ejecución", self.price_edit), 0, 2)
        rejilla.addWidget(_field("Divisa", self.currency_combo), 1, 0)
        rejilla.addWidget(_field("Comisión (€)", self.fee_edit), 1, 1)
        rejilla.addWidget(_field("Fecha", self.date_edit), 1, 2)
        self.fx_row = QWidget()
        fila_cambio = QHBoxLayout(self.fx_row)
        fila_cambio.setContentsMargins(0, 0, 0, 0)
        fila_cambio.setSpacing(14)
        self.fx_label = QLabel("Cambio a EUR")
        self.fx_label.setObjectName("fieldLabel")
        cambio = QWidget()
        columna_cambio = QVBoxLayout(cambio)
        columna_cambio.setContentsMargins(0, 0, 0, 0)
        columna_cambio.setSpacing(4)
        columna_cambio.addWidget(self.fx_label)
        columna_cambio.addWidget(self.fx_edit)
        fila_cambio.addWidget(cambio, 1)
        fila_cambio.addWidget(self.fx_hint, 2, Qt.AlignmentFlag.AlignBottom)
        rejilla.addWidget(self.fx_row, 2, 0, 1, 3)
        self.levels_row = QWidget()
        fila_niveles = QHBoxLayout(self.levels_row)
        fila_niveles.setContentsMargins(0, 0, 0, 0)
        fila_niveles.setSpacing(14)
        fila_niveles.addWidget(_field("Stop-loss", self.stop_edit), 1)
        fila_niveles.addWidget(_field("Objetivo", self.target_edit), 1)
        fila_niveles.addWidget(_field("Divisa de los niveles", self.levels_combo), 1)
        rejilla.addWidget(self.levels_row, 3, 0, 1, 3)
        for columna in range(3):
            rejilla.setColumnStretch(columna, 1)
        caja.addLayout(rejilla)

        caja.addWidget(self._build_new_asset())

        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("Por qué la haces (obligatorio si la registras "
                                            "igualmente)")
        caja.addWidget(_field("Motivo (opcional)", self.reason_edit))

        self.error_label = state_label(state="dangerText")
        self.error_label.setVisible(False)
        caja.addWidget(self.error_label)
        self.done_label = state_label(state="okText")
        self.done_label.setVisible(False)
        caja.addWidget(self.done_label)

        botones = QHBoxLayout()
        botones.setSpacing(10)
        self.review_button = QPushButton("Revisar")
        self.review_button.setObjectName("primary")
        self.review_button.setToolTip("Valida la operación contra el mandato. No guarda nada.")
        self.review_button.clicked.connect(self.review_now)
        botones.addWidget(self.review_button)
        self.clear_button = QPushButton("Limpiar")
        self.clear_button.clicked.connect(self.clear)
        botones.addWidget(self.clear_button)
        botones.addStretch(1)
        caja.addLayout(botones)

        for campo in (self.units_edit, self.price_edit, self.fee_edit, self.fx_edit,
                      self.stop_edit, self.target_edit):
            campo.textChanged.connect(self._on_edited)
        self.ticker_edit.textChanged.connect(self._on_ticker)
        self.currency_combo.currentTextChanged.connect(self._on_currency)
        self.levels_combo.currentTextChanged.connect(self._on_edited)
        self.date_edit.dateChanged.connect(self._on_edited)
        return marco

    def _build_new_asset(self) -> QFrame:
        self.new_asset_box = QFrame()
        self.new_asset_box.setObjectName("chip")
        caja = QVBoxLayout(self.new_asset_box)
        caja.setContentsMargins(14, 10, 14, 12)
        caja.setSpacing(8)
        self.new_asset_title = QLabel()
        self.new_asset_title.setObjectName("itemTitle")
        caja.addWidget(self.new_asset_title)
        caja.addWidget(muted("Se da de alta al registrar la compra. El símbolo de Yahoo sirve "
                             "para valorarlo a mercado; sin él, se valora a coste."))
        self.name_edit = QLineEdit()
        self.isin_edit = QLineEdit()
        self.symbol_edit = QLineEdit()
        self.quote_currency_edit = QLineEdit()
        self.sector_edit = QLineEdit()
        self.sector_edit.setPlaceholderText("Sin espacios: Renta_Variable")
        self.class_combo = QComboBox()
        for clase, etiqueta in CLASS_LABELS.items():
            self.class_combo.addItem(etiqueta, clase.value)
        rejilla = QGridLayout()
        rejilla.setHorizontalSpacing(14)
        rejilla.setVerticalSpacing(10)
        rejilla.addWidget(_field("Nombre", self.name_edit), 0, 0)
        rejilla.addWidget(_field("ISIN (opcional)", self.isin_edit), 0, 1)
        rejilla.addWidget(_field("Símbolo de Yahoo", self.symbol_edit), 0, 2)
        rejilla.addWidget(_field("Divisa de cotización", self.quote_currency_edit), 1, 0)
        rejilla.addWidget(_field("Sector", self.sector_edit), 1, 1)
        rejilla.addWidget(_field("Clase", self.class_combo), 1, 2)
        for columna in range(3):
            rejilla.setColumnStretch(columna, 1)
        caja.addLayout(rejilla)
        for campo in (self.name_edit, self.isin_edit, self.symbol_edit, self.quote_currency_edit,
                      self.sector_edit):
            campo.textChanged.connect(self._on_edited)
        self.class_combo.currentIndexChanged.connect(self._on_edited)
        return self.new_asset_box

    def _build_result(self) -> QFrame:
        self.result_box = QFrame()
        self.result_box.setObjectName("okBox")
        caja = QVBoxLayout(self.result_box)
        caja.setContentsMargins(18, 14, 18, 14)
        caja.setSpacing(8)
        cabecera = QHBoxLayout()
        cabecera.setSpacing(10)
        self.result_dot = QLabel()
        self.result_dot.setObjectName("dotOk")
        cabecera.addWidget(self.result_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.result_title = QLabel()
        self.result_title.setObjectName("okText")
        cabecera.addWidget(self.result_title, 1)
        caja.addLayout(cabecera)
        self.result_message = QLabel()
        self.result_message.setWordWrap(True)
        caja.addWidget(self.result_message)
        self.result_also = muted("")
        caja.addWidget(self.result_also)
        self.result_outcome = QLabel()
        self.result_outcome.setWordWrap(True)
        caja.addWidget(self.result_outcome)
        self.result_steps = muted("")
        caja.addWidget(self.result_steps)
        self.force_check = WrappedCheck(FORCE_LABEL)
        self.force_check.toggled.connect(self._update_register)
        caja.addWidget(self.force_check)
        botones = QHBoxLayout()
        self.register_button = QPushButton("Registrar")
        self.register_button.setObjectName("primary")
        self.register_button.clicked.connect(self.register)
        botones.addWidget(self.register_button)
        botones.addStretch(1)
        caja.addLayout(botones)
        self.result_box.setVisible(False)
        return self.result_box

    def _build_right(self) -> QWidget:
        derecha = QWidget()
        derecha.setFixedWidth(RIGHT_WIDTH)
        caja = QVBoxLayout(derecha)
        caja.setContentsMargins(0, 0, 0, 0)
        caja.setSpacing(16)

        self.position_card, contenido = card("Tu posición")
        self.position_title = next(
            e for e in self.position_card.findChildren(QLabel) if e.objectName() == "cardTitle"
        )
        self._position_grid, self.position_values = self._rows(
            contenido, ("Unidades", "Valor", "Peso", "PnL", "Stop de su tesis")
        )
        self.position_note = muted("")
        contenido.addWidget(self.position_note)
        caja.addWidget(self.position_card, 0, Qt.AlignmentFlag.AlignTop)

        marco, contenido = card("Límites del mandato")
        self.state_label = state_label()
        contenido.addWidget(self.state_label)
        self._limits_grid, self.limit_values = self._rows(
            contenido,
            ("Máximo por activo", "Máximo por sector", "Efectivo mínimo", "Efectivo máximo",
             "Riesgo por operación", "Ratio mínimo"),
        )
        self.buying_label = state_label(state="dangerText")
        contenido.addWidget(self.buying_label)
        self.coverage_label = state_label(state="warnText")
        contenido.addWidget(self.coverage_label)
        caja.addWidget(marco, 0, Qt.AlignmentFlag.AlignTop)

        marco, contenido = card("Movimientos de efectivo")
        self.cash_label = QLabel()
        self.cash_label.setObjectName("itemTitle")
        contenido.addWidget(self.cash_label)
        rejilla = QGridLayout()
        rejilla.setHorizontalSpacing(8)
        rejilla.setVerticalSpacing(8)
        self.cash_buttons: dict[str, QPushButton] = {}
        for numero, (clave, texto, tipo) in enumerate((
            ("ingreso", "Ingreso", CashKind.DEPOSIT),
            ("retirada", "Retirada", CashKind.WITHDRAWAL),
            ("dividendo", "Dividendo", CashKind.DIVIDEND),
            ("otro", "Otro…", CashKind.INTEREST),
            ("ajuste", "Ajustar saldo", None),
        )):
            boton = QPushButton(texto)
            boton.clicked.connect(lambda _c=False, t=tipo: self.open_cash_dialog(t))
            rejilla.addWidget(boton, numero // 3, numero % 3)
            self.cash_buttons[clave] = boton
        self.cash_buttons["otro"].setToolTip("Interés, comisión o impuesto.")
        self.cash_buttons["ajuste"].setToolTip(
            "Escribe tu saldo real en el bróker y Sharky crea el ajuste que cuadra."
        )
        contenido.addLayout(rejilla)
        contenido.addWidget(muted(FLOW_NOTE))
        caja.addWidget(marco, 0, Qt.AlignmentFlag.AlignTop)
        caja.addStretch(1)
        return derecha

    @staticmethod
    def _rows(
        layout: QVBoxLayout, names: tuple[str, ...]
    ) -> tuple[QGridLayout, dict[str, QLabel]]:
        rejilla = QGridLayout()
        rejilla.setHorizontalSpacing(12)
        rejilla.setVerticalSpacing(6)
        valores: dict[str, QLabel] = {}
        for fila, nombre in enumerate(names):
            nombre_fila = muted(nombre)
            nombre_fila.setWordWrap(False)
            rejilla.addWidget(nombre_fila, fila, 0)
            valor = QLabel()
            valor.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            rejilla.addWidget(valor, fila, 1)
            valores[nombre] = valor
        rejilla.setColumnStretch(0, 1)
        layout.addLayout(rejilla)
        return rejilla, valores

    @staticmethod
    def _show_row(grid: QGridLayout, label: QLabel, visible: bool) -> None:
        indice = grid.indexOf(label)
        fila = grid.getItemPosition(indice)[0]
        for columna in range(grid.columnCount()):
            elemento = grid.itemAtPosition(fila, columna)
            if elemento is not None and elemento.widget() is not None:
                elemento.widget().setVisible(visible)

    # -- datos -------------------------------------------------------------------------

    @property
    def is_buy(self) -> bool:
        return self.buy_button.isChecked()

    @property
    def today(self) -> date:
        return self._now().date()

    def reload(self) -> None:
        """Vuelve a leer la cartera con lo guardado (sin descargar nada). Lo escrito en el
        formulario se respeta."""
        conn = self._db.connection()
        ahora = self._now()
        self.valuation = load_valuation(conn, ahora, self._refresher.market_at)
        self.state = current_state(conn, self.valuation, ahora.date())
        self.assets = {a.ticker: a for a in AssetRepository(conn).list_all()}
        self.theses = {t.ticker: t for t in ThesisRepository(conn).list_active()}
        self.date_edit.setMaximumDate(_qdate(ahora.date()))
        self._update_completer()
        self._show_limits()
        self._show_position()
        self._show_cash()

    def asset(self, text: str | None = None) -> Asset | None:
        """El activo del ticker escrito, sin distinguir mayúsculas."""
        ticker = (self.ticker_edit.text() if text is None else text).strip()
        if not ticker:
            return None
        return self.assets.get(ticker) or next(
            (a for t, a in self.assets.items() if t.upper() == ticker.upper()), None
        )

    def held_units(self, ticker: str) -> Decimal:
        if self.valuation is None:
            return Decimal(0)
        posicion = self.valuation.position(ticker)
        return posicion.units if posicion is not None else Decimal(0)

    def _update_completer(self) -> None:
        if self.is_buy:
            tickers = sorted(self.assets)
        else:
            tickers = [p.ticker for p in self.valuation.positions] if self.valuation else []
        self._tickers.setStringList(tickers)

    # -- la columna de la derecha --------------------------------------------------------

    def _show_position(self) -> None:
        texto = self.ticker_edit.text().strip()
        activo = self.asset()
        titulo = f"Tu posición en {activo.ticker if activo else texto}" if texto else "Tu posición"
        self.position_title.setText(titulo)
        posicion = (
            self.valuation.position(activo.ticker)
            if activo is not None and self.valuation is not None else None
        )
        for etiqueta in self.position_values.values():
            self._show_row(self._position_grid, etiqueta, posicion is not None)
        if posicion is None:
            if not texto:
                nota = "Escribe un ticker para ver tu posición."
            elif activo is None:
                nota = f"{texto} es nuevo: se da de alta con la compra."
            else:
                nota = f"No tienes {activo.ticker} en cartera ({activo.name})."
            self.position_note.setText(nota)
            self.position_note.setVisible(True)
            return
        self.position_note.setVisible(False)
        v = self.position_values
        v["Unidades"].setText(format_units(posicion.units))
        v["Valor"].setText(format_eur(posicion.value_eur))
        tope = self._rules().max_asset_weight(self.state)
        v["Peso"].setText(format_pct(posicion.weight))
        restyle(v["Peso"], "dangerText" if posicion.weight > tope else "")
        v["Peso"].setToolTip(
            f"Por encima del tope del {format_limit_pct(tope)}" if posicion.weight > tope else ""
        )
        pnl = posicion.pnl_eur
        if pnl is None or posicion.pnl_pct is None:
            v["PnL"].setText("— (a coste)")
            restyle(v["PnL"], "")
        else:
            v["PnL"].setText(
                f"{format_signed_amount(pnl)} € ({format_pct(posicion.pnl_pct, signed=True)})"
            )
            restyle(v["PnL"], "okText" if pnl > 0 else ("dangerText" if pnl < 0 else ""))
        tesis = self.theses.get(posicion.ticker)
        if tesis is None:
            v["Stop de su tesis"].setText("Sin tesis")
            restyle(v["Stop de su tesis"], "warnText")
        else:
            v["Stop de su tesis"].setText(
                format_level(tesis.stop, tesis.levels_currency) if tesis.stop is not None
                else "Sin stop"
            )
            restyle(v["Stop de su tesis"], "" if tesis.stop is not None else "warnText")

    def _rules(self) -> MandateRules:
        return self._refresher.rules()

    def _show_limits(self) -> None:
        reglas = self._rules()
        estado = self.state
        set_state(self.state_label, f"Estado {STATE_LABELS[estado]}",
                  TOKEN_TEXT[STATE_TOKENS[estado]])
        v = self.limit_values
        v["Máximo por activo"].setText(format_limit_pct(reglas.max_asset_weight(estado)))
        v["Máximo por sector"].setText(format_limit_pct(reglas.max_sector_weight))
        v["Efectivo mínimo"].setText(format_limit_pct(reglas.min_cash(estado)))
        maximo = reglas.max_cash(estado)
        v["Efectivo máximo"].setText(format_limit_pct(maximo) if maximo is not None else "")
        self._show_row(self._limits_grid, v["Efectivo máximo"], maximo is not None)
        v["Riesgo por operación"].setText(f"{format_limit_pct(reglas.max_risk_per_trade)} del NAV")
        v["Ratio mínimo"].setText(format_number(reglas.min_reward_risk, 1))
        set_state(
            self.buying_label,
            "" if buying_allowed(estado) else "Compras prohibidas: vender siempre se puede.",
            "dangerText",
        )
        cobertura = ""
        if self.valuation is not None and self.valuation.unreliable:
            cobertura = (
                "Hay posiciones sin precio fiable: los pesos se calculan con lo guardado. "
                "Actualiza los precios para validar con los de ahora."
            )
        set_state(self.coverage_label, cobertura, "warnText")

    def _show_cash(self) -> None:
        if self.valuation is None:
            return
        v = self.valuation
        self.cash_label.setText(
            f"Efectivo: {format_eur(v.cash_eur)} ({format_pct(v.cash_weight)} del patrimonio)"
        )

    # -- el formulario -------------------------------------------------------------------

    def _prefill(self, widget: QLineEdit | QComboBox, value: str) -> None:
        """Pone un valor propuesto si el campo está vacío o conserva el último que puso
        Sharky: lo que escribe el usuario manda."""
        actual = widget.currentText() if isinstance(widget, QComboBox) else widget.text()
        anterior = self._prefilled.get(id(widget))
        if actual.strip() and actual != anterior:
            return
        self._filling = True
        try:
            if isinstance(widget, QComboBox):
                if widget.findText(value) < 0:
                    widget.addItem(value)
                widget.setCurrentText(value)
            else:
                widget.setText(value)
        finally:
            self._filling = False
        self._prefilled[id(widget)] = value

    def _default_currencies(self) -> None:
        """EUR de entrada en la divisa y en la de los niveles, como propuesta de Sharky: el
        ticker que se escriba la puede cambiar."""
        for combo in (self.currency_combo, self.levels_combo):
            self._prefilled[id(combo)] = combo.currentText()

    def _on_mode(self) -> None:
        compra = self.is_buy
        restyle(self.buy_button, "primary" if compra else "")
        restyle(self.sell_button, "" if compra else "primary")
        self.levels_row.setVisible(compra)
        self.all_button.setVisible(not compra)
        self._update_completer()
        self._on_ticker()

    def _on_ticker(self, *_args: object) -> None:
        activo = self.asset()
        texto = self.ticker_edit.text().strip()
        self.new_asset_box.setVisible(self.is_buy and bool(texto) and activo is None)
        self.new_asset_title.setText(f"Activo nuevo: {texto}")
        if activo is not None:
            self._prefill(self.currency_combo, activo.currency)
            tesis = self.theses.get(activo.ticker)
            if tesis is not None and self.is_buy:
                self._prefill(self.levels_combo, tesis.levels_currency)
                if tesis.stop is not None:
                    self._prefill(self.stop_edit, format_price(tesis.stop))
                if tesis.target is not None:
                    self._prefill(self.target_edit, format_price(tesis.target))
        self._on_currency()
        self._show_position()

    def _on_currency(self, *_args: object) -> None:
        divisa = normalize_currency(self.currency_combo.currentText())
        otra = divisa is not None and divisa != BASE_CURRENCY
        self.fx_row.setVisible(otra)
        if otra:
            self.fx_label.setText(f"Cambio a EUR (euros por 1 {divisa})")
            cambio = rate_to_eur(self._db.connection(), divisa)
            if cambio is not None:
                self._prefill(self.fx_edit, _plain(cambio))
                self.fx_hint.setText(
                    "Propuesto: el último cambio guardado. Pon el que aplicó tu bróker."
                )
            else:
                self.fx_hint.setText("No hay ningún cambio guardado: escribe el de tu bróker.")
        if divisa is not None:
            activo = self.asset()
            if activo is None:
                self._prefill(self.quote_currency_edit, divisa)
            if activo is None or activo.ticker not in self.theses:
                self._prefill(self.levels_combo, divisa)
        self._on_edited()

    def _field_texts(self) -> tuple[str, ...]:
        """Todo lo que cuenta para la validación (el motivo no: se puede cambiar después)."""
        return (
            str(self.is_buy),
            self.ticker_edit.text().strip(),
            self.units_edit.text().strip(),
            self.price_edit.text().strip(),
            self.currency_combo.currentText().strip(),
            self.fx_edit.text().strip() if self.fx_row.isVisibleTo(self) else "",
            self.fee_edit.text().strip(),
            self.date_edit.date().toString("yyyy-MM-dd"),
            self.stop_edit.text().strip(),
            self.target_edit.text().strip(),
            self.levels_combo.currentText().strip(),
            self.name_edit.text().strip(),
            self.isin_edit.text().strip(),
            self.symbol_edit.text().strip(),
            self.quote_currency_edit.text().strip(),
            self.sector_edit.text().strip(),
            str(self.class_combo.currentData()),
        )

    def _on_edited(self, *_args: object) -> None:
        """Cualquier cambio después de «Revisar» obliga a revisar otra vez."""
        if self._filling:
            return
        if self._reviewed is not None and self._field_texts() != self._reviewed:
            self._hide_review()
        set_state(self.done_label, "", "okText")

    def _hide_review(self) -> None:
        self.review = None
        self._reviewed = None
        self.result_box.setVisible(False)

    def fill_all_units(self) -> None:
        """«Todas»: las unidades que hay de ese ticker."""
        activo = self.asset()
        if activo is not None:
            self.units_edit.setText(_plain(self.held_units(activo.ticker)))

    def ticket(self, errors: list[str]) -> TradeTicket | None:
        """La operación escrita en el formulario; los errores de formato, en `errors`."""
        ticker = self.ticker_edit.text().strip()
        if not ticker:
            errors.append("Falta el ticker.")
        unidades = _parse(self.units_edit.text(), "las unidades", errors, required=True)
        precio = _parse(self.price_edit.text(), "el precio de ejecución", errors, required=True)
        comision = _parse(self.fee_edit.text(), "la comisión", errors) or Decimal(0)
        divisa = self.currency_combo.currentText().strip()
        cambio: Decimal | None = Decimal(1)
        if normalize_currency(divisa) not in (None, BASE_CURRENCY):
            cambio = _parse(self.fx_edit.text(), "el cambio a EUR", errors, required=True)
        compra = self.is_buy
        stop = objetivo = None
        niveles = None
        nuevo: Asset | None = None
        if compra:
            stop = _parse(self.stop_edit.text(), "el stop", errors)
            objetivo = _parse(self.target_edit.text(), "el objetivo", errors)
            niveles = self.levels_combo.currentText().strip()
            if ticker and self.asset(ticker) is None:
                nuevo, propios = asset_from_fields(
                    ticker,
                    self.name_edit.text(),
                    self.quote_currency_edit.text(),
                    isin=self.isin_edit.text(),
                    yahoo_symbol=self.symbol_edit.text(),
                    sector=self.sector_edit.text(),
                    asset_class=AssetClass(self.class_combo.currentData()),
                )
                errors.extend(propios)
        if errors or unidades is None or precio is None or cambio is None:
            return None
        return TradeTicket(
            kind=TradeKind.BUY if compra else TradeKind.SELL,
            ticker=ticker,
            trade_date=self.date_edit.date().toPython(),
            units=unidades,
            price=precio,
            currency=divisa,
            fx_to_eur=cambio,
            fee_eur=comision,
            stop=stop,
            target=objetivo,
            levels_currency=niveles,
            reason=self.reason_edit.text().strip(),
            new_asset=nuevo,
        )

    # -- «Revisar» y «Registrar» ---------------------------------------------------------

    def review_now(self) -> TradeReview | None:
        """«Revisar»: valida sin guardar nada y enseña el resultado."""
        set_state(self.done_label, "", "okText")
        errores: list[str] = []
        operacion = self.ticket(errores)
        if operacion is None:
            self._show_errors(errores)
            return None
        try:
            revision = review_trade(self._db.connection(), operacion, self._rules(),
                                    self._now(), self._refresher.market_at)
        except TradeError as error:
            self._show_errors(error.errors)
            return None
        set_state(self.error_label, "", "dangerText")
        self.review = revision
        self._reviewed = self._field_texts()
        self._show_review(revision)
        return revision

    def _show_errors(self, errors: list[str]) -> None:
        self._hide_review()
        set_state(self.error_label, "\n".join(errors), "dangerText")

    def _show_review(self, review: TradeReview) -> None:
        v = review.validation
        compra = review.ticket.is_buy
        if v.ok:
            caja, punto, titulo_estilo = "okBox", "dotOk", "okText"
            titulo = "Cumple el mandato" if compra else "Lista para registrar"
            mensaje = ""
        else:
            caja, punto, titulo_estilo = "rejectBox", "dotDanger", "dangerText"
            fallo = v.failure
            assert fallo is not None
            titulo = "No se puede registrar" if v.blocking is not None else (
                "Rechazada por el mandato"
            )
            mensaje = fallo.message
        restyle(self.result_box, caja)
        restyle(self.result_dot, punto)
        self.result_dot.setAccessibleName(titulo)
        set_state(self.result_title, titulo, titulo_estilo)
        self.result_message.setText(mensaje)
        self.result_message.setVisible(bool(mensaje))
        otros = also_text(v.also_failing)
        bloqueo = v.blocking
        if bloqueo is not None and bloqueo is not v.failure:
            otros = "\n".join(filter(None, (
                otros, f"No se puede registrar igualmente: {_lower_first(bloqueo.message)}"
            )))
        self.result_also.setText(otros)
        self.result_also.setVisible(bool(otros))
        salida = outcome_text(review) if bloqueo is None else ""
        self.result_outcome.setText(salida)
        self.result_outcome.setVisible(bool(salida))
        self.result_steps.setText(steps_text(review))
        self.force_check.setVisible(v.forceable)
        self.force_check.setChecked(False)
        self.register_button.setVisible(bloqueo is None)
        self.result_box.setVisible(True)
        self._update_register()

    def _update_register(self, *_args: object) -> None:
        revision = self.review
        if revision is None:
            return
        if revision.validation.ok:
            texto = "Registrar compra" if revision.ticket.is_buy else "Registrar venta"
            self.register_button.setEnabled(True)
        else:
            texto = "Registrar igualmente…"
            self.register_button.setEnabled(self.force_check.isChecked())
        self.register_button.setText(texto)
        restyle(self.register_button, "primary" if revision.validation.ok else "danger")

    def register(self) -> RecordedTrade | None:
        """«Registrar»: guarda la operación revisada, con su efectivo y su tesis, en una
        transacción. Si no cumple el mandato, pide confirmación y un motivo."""
        revision = self.review
        if revision is None or self._reviewed != self._field_texts():
            return None
        operacion = replace(revision.ticket, reason=self.reason_edit.text().strip())
        v = revision.validation
        forzada = False
        if not v.ok:
            if not v.forceable or not self.force_check.isChecked():
                return None
            motivo = self.ask_force_reason(revision, operacion.reason)
            if motivo is None:
                return None
            operacion = replace(operacion, reason=motivo.strip())
            self.reason_edit.setText(motivo.strip())
            forzada = True
        eleccion: LevelChoice | None = None
        if revision.level_update is not None:
            eleccion = self.ask_level_choice(revision.level_update)
            if eleccion is None:
                return None
        try:
            with self._db.transaction() as conn:
                hecho = record_trade(conn, operacion, self._rules(), self._now(),
                                     self._refresher.market_at, forced=forzada, choice=eleccion)
        except TradeError as error:
            self._show_errors(error.errors)
            return None
        self.clear()
        set_state(self.done_label, done_text(hecho), "okText")
        self.reload()
        self.recorded.emit()
        if hecho.opened_thesis is not None:
            self.thesisOpened.emit(hecho.trade.ticker)
        return hecho

    def clear(self) -> None:
        """«Limpiar»: vacía el formulario (se queda en compra o en venta)."""
        self._filling = True
        try:
            for campo in (self.ticker_edit, self.units_edit, self.price_edit, self.fee_edit,
                          self.fx_edit, self.stop_edit, self.target_edit, self.reason_edit,
                          self.name_edit, self.isin_edit, self.symbol_edit,
                          self.quote_currency_edit, self.sector_edit):
                campo.clear()
            self.currency_combo.setCurrentText(BASE_CURRENCY)
            self.levels_combo.setCurrentText(BASE_CURRENCY)
            self.class_combo.setCurrentIndex(0)
            self.date_edit.setDate(_qdate(self.today))
        finally:
            self._filling = False
        self._prefilled.clear()
        self._default_currencies()
        self._hide_review()
        set_state(self.error_label, "", "dangerText")
        set_state(self.done_label, "", "okText")
        self._on_ticker()

    # -- diálogos (los tests los sustituyen) ---------------------------------------------

    def ask_force_reason(self, review: TradeReview, reason: str) -> str | None:
        """Confirmación de «Registrar igualmente» con su motivo. None si se cancela."""
        dialogo = ForceDialog(review, reason, self)
        try:
            return dialogo.reason if self.run_dialog(dialogo) else None
        finally:
            dialogo.deleteLater()

    def ask_level_choice(self, update: LevelUpdate) -> LevelChoice | None:
        """Ampliar con otros niveles: qué se actualiza de la tesis. None si se cancela."""
        dialogo = LevelUpdateDialog(update, self)
        try:
            return dialogo.choice if self.run_dialog(dialogo) else None
        finally:
            dialogo.deleteLater()

    def open_cash_dialog(self, kind: CashKind | None) -> CashMovement | None:
        """Un movimiento de efectivo suelto; `kind` None es «Ajustar saldo»."""
        efectivo = self.valuation.cash_eur if self.valuation is not None else Decimal(0)
        dialogo = CashDialog(self._db, self._now, kind, efectivo, self)
        try:
            if not self.run_dialog(dialogo) or dialogo.movement is None:
                return None
            movimiento = dialogo.movement
        finally:
            dialogo.deleteLater()
        set_state(self.done_label, cash_done_text(movimiento), "okText")
        self.reload()
        self.recorded.emit()
        return movimiento

    def run_dialog(self, dialog: QDialog) -> bool:
        """Enseña el diálogo y dice si se ha aceptado (los tests lo sustituyen)."""
        return dialog.exec() == QDialog.DialogCode.Accepted

    # -- visibilidad ---------------------------------------------------------------------

    def _on_refreshed(self, _outcome: RefreshOutcome) -> None:
        # Precios nuevos: lo revisado ya no vale.
        self._hide_review()
        self.reload()

    def showEvent(self, event: object) -> None:
        super().showEvent(event)
        if not self._refresher.running:
            self.reload()


def cash_done_text(movement: CashMovement) -> str:
    """«Movimiento registrado: ingreso de 1.000,00 €.» Un ajuste lleva su signo."""
    nombre = CASH_LABELS[movement.kind].lower()
    if movement.kind is CashKind.ADJUSTMENT:
        importe = f"{format_signed_amount(movement.amount_eur)} €"
    else:
        importe = format_eur(abs(movement.amount_eur))
    return f"Movimiento registrado: {nombre} de {importe}."


# -- «Registrar igualmente» ----------------------------------------------------------------------


class ForceDialog(QDialog):
    """Confirmación de una operación que no cumple el mandato, con su motivo (obligatorio)."""

    def __init__(self, review: TradeReview, reason: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Registrar igualmente")
        self.setMinimumWidth(500)
        caja = QVBoxLayout(self)
        caja.setContentsMargins(20, 18, 20, 18)
        caja.setSpacing(10)
        titulo = QLabel("¿Registrarla aunque no cumpla el mandato?")
        titulo.setObjectName("cardTitle")
        caja.addWidget(titulo)
        fallo = review.validation.failure
        explicacion = QLabel(
            f"{fallo.message if fallo else ''}\n\nQueda marcada como forzada, con tu motivo, y "
            "el próximo informe diario la menciona."
        )
        explicacion.setWordWrap(True)
        caja.addWidget(explicacion)
        self.reason_edit = QLineEdit(reason)
        caja.addWidget(_field("Motivo (obligatorio)", self.reason_edit))
        botones = QHBoxLayout()
        botones.addStretch(1)
        cancelar = QPushButton("Cancelar")
        cancelar.setDefault(True)
        cancelar.clicked.connect(self.reject)
        botones.addWidget(cancelar)
        self.accept_button = QPushButton("Registrar igualmente")
        self.accept_button.setObjectName("danger")
        self.accept_button.clicked.connect(self.accept)
        botones.addWidget(self.accept_button)
        caja.addLayout(botones)
        self.reason_edit.textChanged.connect(self._sync)
        self._sync()

    @property
    def reason(self) -> str:
        return self.reason_edit.text().strip()

    def _sync(self, *_args: object) -> None:
        self.accept_button.setEnabled(bool(self.reason))


# -- ampliar una posición con tesis ------------------------------------------------------------


class LevelUpdateDialog(QDialog):
    """Los niveles de la compra no coinciden con los de la tesis: qué se actualiza (GUIA §5.5).
    La decisión queda en el historial de la tesis."""

    def __init__(self, update: LevelUpdate, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.update_ = update
        self.choice = LevelChoice()
        t = update.thesis
        self.setWindowTitle(f"Tesis de {t.ticker}")
        self.setMinimumWidth(520)
        caja = QVBoxLayout(self)
        caja.setContentsMargins(20, 18, 20, 18)
        caja.setSpacing(10)
        titulo = QLabel(f"¿Actualizas la tesis de {t.ticker}?")
        titulo.setObjectName("cardTitle")
        caja.addWidget(titulo)
        caja.addWidget(muted(
            "El stop o el objetivo de esta compra no coinciden con los de su tesis. Marca lo que "
            "quieres actualizar; tu decisión queda en el historial."
        ))
        self.checks: dict[str, WrappedCheck] = {}

        def viejo(valor: Decimal | None) -> str:
            return format_level(valor, t.levels_currency) if valor is not None else "—"

        if update.currency_changes:
            texto = (
                f"Pasar los niveles a {update.currency}: entrada "
                f"{format_level(update.entry, update.currency)} (nuevo coste medio), stop "
                f"{format_level(update.stop, update.currency)} y objetivo "
                f"{format_level(update.target, update.currency)}. Ahora: entrada "
                f"{viejo(t.entry_price)}, stop {viejo(t.stop)}, objetivo {viejo(t.target)}."
            )
            self.checks["todo"] = WrappedCheck(texto)
        else:
            divisa = update.currency
            self.checks["entrada"] = WrappedCheck(
                f"Entrada: {viejo(t.entry_price)} → {format_level(update.entry, divisa)} "
                "(nuevo coste medio)"
            )
            if update.stop_changes:
                self.checks["stop"] = WrappedCheck(
                    f"Stop: {viejo(t.stop)} → {format_level(update.stop, divisa)}"
                )
            if update.target_changes:
                self.checks["objetivo"] = WrappedCheck(
                    f"Objetivo: {viejo(t.target)} → {format_level(update.target, divisa)}"
                )
        for casilla in self.checks.values():
            casilla.setChecked(True)
            caja.addWidget(casilla)
        botones = QHBoxLayout()
        botones.addStretch(1)
        cancelar = QPushButton("Cancelar")
        cancelar.setToolTip("No se registra la compra.")
        cancelar.clicked.connect(self.reject)
        botones.addWidget(cancelar)
        self.keep_button = QPushButton("Mantener la tesis")
        self.keep_button.clicked.connect(self.keep)
        botones.addWidget(self.keep_button)
        self.update_button = QPushButton("Actualizar lo marcado")
        self.update_button.setObjectName("primary")
        self.update_button.setDefault(True)
        self.update_button.clicked.connect(self.apply)
        botones.addWidget(self.update_button)
        caja.addLayout(botones)

    def apply(self) -> None:
        marcado = {clave: c.isChecked() for clave, c in self.checks.items()}
        if "todo" in marcado:
            todo = marcado["todo"]
            self.choice = LevelChoice(entry=todo, stop=todo, target=todo)
        else:
            self.choice = LevelChoice(
                entry=marcado.get("entrada", False),
                stop=marcado.get("stop", False),
                target=marcado.get("objetivo", False),
            )
        self.accept()

    def keep(self) -> None:
        self.choice = LevelChoice()
        self.accept()


# -- movimientos de efectivo --------------------------------------------------------------------


class CashDialog(QDialog):
    """Un movimiento de efectivo suelto (GUIA §5.5): ingreso, retirada, dividendo, interés,
    comisión o impuesto, con su importe en positivo (el signo sale del tipo), o «Ajustar saldo»
    (`kind` None): «mi saldo real es X» crea el ajuste que cuadra."""

    def __init__(
        self,
        db: Database,
        now: Callable[[], datetime],
        kind: CashKind | None,
        cash_eur: Decimal,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._now = now
        self._cash = cash_eur
        self.adjust = kind is None
        self.movement: CashMovement | None = None
        self.setWindowTitle("Ajustar saldo" if self.adjust else "Movimiento de efectivo")
        self.setMinimumWidth(460)
        caja = QVBoxLayout(self)
        caja.setContentsMargins(20, 18, 20, 18)
        caja.setSpacing(10)
        titulo = QLabel("Ajustar saldo" if self.adjust else "Movimiento de efectivo")
        titulo.setObjectName("cardTitle")
        caja.addWidget(titulo)
        caja.addWidget(muted(f"Efectivo en Sharky: {format_eur(cash_eur)}."))

        self.kind_combo = QComboBox()
        for tipo in MANUAL_CASH_SIGNS:
            self.kind_combo.addItem(CASH_LABELS[tipo], tipo.value)
        if kind is not None:
            self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(kind.value)))
        self.amount_edit = QLineEdit()
        self.amount_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd/MM/yyyy")
        hoy = _qdate(now().date())
        self.date_edit.setMaximumDate(hoy)
        self.date_edit.setDate(hoy)
        self.note_edit = QLineEdit()

        fila = QHBoxLayout()
        fila.setSpacing(14)
        if not self.adjust:
            fila.addWidget(_field("Tipo", self.kind_combo), 1)
        importe = "Tu saldo real en el bróker (€)" if self.adjust else "Importe (€)"
        fila.addWidget(_field(importe, self.amount_edit), 1)
        fila.addWidget(_field("Fecha", self.date_edit), 1)
        caja.addLayout(fila)
        caja.addWidget(_field("Nota (opcional)", self.note_edit))
        self.hint_label = muted("")
        caja.addWidget(self.hint_label)
        self.error_label = state_label(state="dangerText")
        self.error_label.setVisible(False)
        caja.addWidget(self.error_label)
        botones = QHBoxLayout()
        botones.addStretch(1)
        cancelar = QPushButton("Cancelar")
        cancelar.clicked.connect(self.reject)
        botones.addWidget(cancelar)
        self.save_button = QPushButton("Registrar")
        self.save_button.setObjectName("primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        botones.addWidget(self.save_button)
        caja.addLayout(botones)
        self.kind_combo.currentIndexChanged.connect(self._sync)
        self.amount_edit.textChanged.connect(self._sync)
        self._sync()

    @property
    def kind(self) -> CashKind | None:
        return None if self.adjust else CashKind(self.kind_combo.currentData())

    def _sync(self, *_args: object) -> None:
        """La nota de abajo: qué hace el movimiento."""
        if self.adjust:
            try:
                real = parse_decimal(self.amount_edit.text())
            except ValueError:
                texto = "Sharky crea el ajuste que deja el efectivo en tu saldo real."
            else:
                ajuste = adjustment_amount(self._cash, real)
                texto = f"Se registrará un ajuste de {format_signed_amount(ajuste)} €."
        elif self.kind in FLOW_KINDS:
            texto = FLOW_NOTE
        else:
            texto = "Cuenta como ganancia o pérdida de la cartera."
        self.hint_label.setText(texto)

    def save(self) -> bool:
        errores: list[str] = []
        nombre = "tu saldo real" if self.adjust else "el importe"
        importe = _parse(self.amount_edit.text(), nombre, errores, required=True)
        if importe is None:
            set_state(self.error_label, "\n".join(errores), "dangerText")
            return False
        dia = self.date_edit.date().toPython()
        nota = self.note_edit.text()
        try:
            with self._db.transaction() as conn:
                if self.kind is None:
                    self.movement = record_balance_adjustment(conn, dia, importe, self._now(), nota)
                else:
                    self.movement = record_cash_movement(conn, self.kind, dia, importe,
                                                         self._now(), nota)
        except TradeError as error:
            set_state(self.error_label, str(error), "dangerText")
            return False
        self.accept()
        return True
