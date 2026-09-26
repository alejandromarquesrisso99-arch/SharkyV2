"""Radar (GUIA §5.8 y §5.10, punto 5; §7, H11; docs/maqueta/05-radar.png).

- **Alertas activas**, con sus niveles, el ratio, la caída desde el máximo y el peso máximo
  sugerido, y los botones «Comprar» (abre Operar con los niveles de la alerta) y «Descartar».
- **Candidatos que no pasaron el filtro**, con su motivo: el precio puede acompañar más adelante.
  Los del explorador se pueden «Vigilar» aunque no hayan dado alerta.
- **Lista de vigilancia** editable: «Añadir valor…» (con «Probar» el símbolo) y quitar un valor
  pulsándolo. «Pasar el filtro ahora (gratis)» hace el paso 3 de la rutina (caducar y filtrar),
  que en el H12 correrá solo.
- **Historial** de alertas caducadas, ejecutadas y descartadas.
- **«Buscar oportunidades nuevas»** en la cabecera, con su precio aproximado: lo lanza el
  `ReportRunner` que comparten los informes (primero los precios, gratis; después Claude y el
  filtro). El informe queda en Informes con su coste real.

La tarjeta «Oportunidades en radar» del Panel también vive aquí. Aquí no se calcula ningún
número: salen de `core/radar.py` y los guardan los servicios.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sharky.core.formatting import format_pct, format_usd, pretty_sector
from sharky.core.levels import format_level
from sharky.core.mandate import STATE_LABELS, buying_allowed
from sharky.core.models import (
    AlertOrigin,
    AlertStatus,
    RadarAlert,
    WatchlistItem,
    WatchlistSource,
)
from sharky.core.radar import (
    active_alerts,
    alert_history,
    drop_text,
    expires_on,
    ratio_text,
    rejected_candidates,
)
from sharky.core.reports import AIAction, usage_text
from sharky.services.db import Database
from sharky.services.explorer import (
    ExplorationOutcome,
    RadarError,
    RadarOutcome,
    add_to_watchlist,
    discard_alert,
    last_exploration,
    refresh_radar,
    remove_from_watchlist,
    watch_candidate,
)
from sharky.services.market import (
    HistoryProvider,
    PriceProvider,
    Quote,
    load_valuation,
    local_now,
    probe_symbol,
)
from sharky.services.reports import TRIGGER_MANUAL
from sharky.services.repositories import (
    RadarAlertRepository,
    WatchlistRepository,
    current_state,
)
from sharky.services.settings import Settings
from sharky.ui.pages import card, muted, restyle, set_state, state_label
from sharky.ui.reports import ReportRunner, RunPlan, plan_button, plan_lines, plan_question
from sharky.ui.theme import ThemeController
from sharky.ui.workers import Worker, start

log = logging.getLogger(__name__)

#: Cuántas filas enseña el historial y la tarjeta del Panel.
HISTORY_ROWS = 20
PANEL_ALERTS = 3
WATCH_COLUMNS = 5

STATUS_LABELS = {
    AlertStatus.EXECUTED: "Ejecutada",
    AlertStatus.EXPIRED: "Caducada",
    AlertStatus.DISCARDED: "Descartada",
    AlertStatus.ACTIVE: "Activa",
}
ORIGIN_LABELS = {AlertOrigin.EXPLORER: "Explorador", AlertOrigin.WATCHLIST: "Vigilancia"}


def origin_text(alert: RadarAlert) -> str:
    """«Exploración del 18/12» o «Vigilancia · 25/09»."""
    if alert.origin is AlertOrigin.EXPLORER:
        return f"Exploración del {alert.created_on:%d/%m}"
    return f"Vigilancia · {alert.created_on:%d/%m}"


def _level(value, currency: str | None) -> str:
    if value is None or not currency:
        return "—"
    return format_level(value, currency)


def _plain_label(text: str, *, right: bool = False, bold: bool = False) -> QLabel:
    etiqueta = QLabel(text)
    if right:
        etiqueta.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if bold:
        etiqueta.setObjectName("itemTitle")
    return etiqueta


def _clear(layout: QGridLayout | QVBoxLayout) -> None:
    while layout.count():
        elemento = layout.takeAt(0)
        widget = elemento.widget()
        if widget is not None:
            widget.deleteLater()
        elif elemento.layout() is not None:
            _clear(elemento.layout())


# -- el filtro gratis (paso 3) ------------------------------------------------------------------


class RadarRunner(QObject):
    """«Pasar el filtro ahora (gratis)»: caducar y filtrar la lista de vigilancia en un hilo de
    trabajo. Una pasada a la vez; las señales llegan al hilo de la interfaz."""

    started = Signal()
    #: Ha terminado, con su RadarOutcome.
    finished = Signal(object)
    failed = Signal(str)
    stopped = Signal()

    def __init__(
        self,
        db: Database,
        history: HistoryProvider,
        settings: Settings,
        *,
        now: Callable[[], datetime] = local_now,
        market_at: Callable[[], datetime | None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._history = history
        self._settings = settings
        self._now = now
        self._market_at = market_at or (lambda: None)
        self._worker: Worker | None = None
        self._cancel: threading.Event | None = None

    @property
    def running(self) -> bool:
        return self._worker is not None

    def start(self) -> bool:
        if self.running:
            return False
        self._cancel = threading.Event()
        trabajo = Worker(
            refresh_radar, self._db, self._history, self._settings, self._now,
            trigger=TRIGGER_MANUAL, market_at=self._market_at(), cancel=self._cancel,
        )
        trabajo.signals.finished.connect(self._on_done)
        trabajo.signals.failed.connect(self._on_failed)
        self._worker = trabajo  # sin referencia, las señales se perderían
        self.started.emit()
        start(trabajo)
        return True

    def shutdown(self) -> None:
        """Al cerrar la app: se corta la descarga y no se guarda nada."""
        if self._cancel is not None:
            self._cancel.set()

    def _on_done(self, outcome: RadarOutcome) -> None:
        self._worker = None
        self._cancel = None
        self.finished.emit(outcome)
        self.stopped.emit()

    def _on_failed(self, message: str) -> None:
        cancelado = self._cancel is not None and self._cancel.is_set()
        self._worker = None
        self._cancel = None
        if not cancelado:
            self.failed.emit(message)
        self.stopped.emit()


# -- añadir un valor a la lista de vigilancia -----------------------------------------------------


class AddWatchDialog(QDialog):
    """«Añadir valor…»: ticker, símbolo de Yahoo (con «Probar»), nombre y sector. Al añadirlo se
    guarda; `item` es lo guardado."""

    def __init__(
        self,
        db: Database,
        prices: PriceProvider | None,
        now: Callable[[], datetime],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Añadir a la lista de vigilancia")
        self.setMinimumWidth(460)
        self._db = db
        self._prices = prices
        self._now = now
        self._worker: Worker | None = None
        self.currency: str | None = None
        self.item: WatchlistItem | None = None

        caja = QVBoxLayout(self)
        caja.setContentsMargins(20, 18, 20, 18)
        caja.setSpacing(10)
        titulo = QLabel("Añadir un valor a la lista de vigilancia")
        titulo.setObjectName("cardTitle")
        caja.addWidget(titulo)
        caja.addWidget(muted("El filtro lo repasará cada día con precios reales, gratis. Hace "
                             "falta su símbolo de Yahoo Finance (por ejemplo, CCJ o LDO.MI)."))
        rejilla = QGridLayout()
        rejilla.setHorizontalSpacing(10)
        rejilla.setVerticalSpacing(8)
        self.ticker_edit = QLineEdit()
        self.ticker_edit.setPlaceholderText("CCJ")
        self.symbol_edit = QLineEdit()
        self.symbol_edit.setPlaceholderText("CCJ")
        self.probe_button = QPushButton("Probar")
        self.probe_button.clicked.connect(self.probe)
        self.probe_button.setEnabled(prices is not None)
        self.name_edit = QLineEdit()
        self.sector_edit = QLineEdit()
        self.sector_edit.setPlaceholderText("Sin espacios: Energia_nuclear")
        for fila, (texto, campo) in enumerate((
            ("Ticker", self.ticker_edit),
            ("Símbolo de Yahoo", self.symbol_edit),
            ("Nombre", self.name_edit),
            ("Sector (opcional)", self.sector_edit),
        )):
            etiqueta = QLabel(texto)
            etiqueta.setObjectName("fieldLabel")
            rejilla.addWidget(etiqueta, fila, 0)
            rejilla.addWidget(campo, fila, 1)
        rejilla.addWidget(self.probe_button, 1, 2)
        rejilla.setColumnStretch(1, 1)
        caja.addLayout(rejilla)
        self.probe_label = muted("")
        caja.addWidget(self.probe_label)
        self.error_label = state_label(state="dangerText")
        self.error_label.setVisible(False)
        caja.addWidget(self.error_label)

        botones = QHBoxLayout()
        botones.addStretch(1)
        cancelar = QPushButton("Cancelar")
        cancelar.clicked.connect(self.reject)
        botones.addWidget(cancelar)
        self.add_button = QPushButton("Añadir")
        self.add_button.setObjectName("primary")
        self.add_button.setDefault(True)
        self.add_button.clicked.connect(self.save)
        botones.addWidget(self.add_button)
        caja.addLayout(botones)
        self.symbol_edit.textChanged.connect(self._on_symbol)

    def _on_symbol(self, *_args: object) -> None:
        self.currency = None
        self.probe_label.setText("")

    def probe(self) -> None:
        """«Probar»: el último cierre del símbolo (en segundo plano); rellena el nombre."""
        simbolo = self.symbol_edit.text().strip()
        if self._prices is None or not simbolo:
            return
        self.probe_button.setEnabled(False)
        self.probe_label.setText(f"Preguntando a Yahoo por {simbolo}…")
        trabajo = Worker(probe_symbol, self._prices, simbolo)
        trabajo.signals.finished.connect(self._on_probe_done)
        trabajo.signals.failed.connect(self._on_probe_failed)
        self._worker = trabajo
        start(trabajo)

    def _on_probe_done(self, quote: Quote) -> None:
        self._worker = None
        self.probe_button.setEnabled(True)
        self.currency = quote.currency
        if quote.name and not self.name_edit.text().strip():
            self.name_edit.setText(quote.name)
        self.probe_label.setText(
            f"{quote.symbol}: {format_level(quote.price, quote.currency)}, cierre del "
            f"{quote.close_date:%d/%m/%Y}."
        )

    def _on_probe_failed(self, message: str) -> None:
        self._worker = None
        self.probe_button.setEnabled(True)
        self.probe_label.setText(message)

    def save(self) -> bool:
        """«Añadir»: lo guarda si vale; si no, enseña los errores."""
        try:
            with self._db.transaction() as conn:
                self.item = add_to_watchlist(
                    conn, self.ticker_edit.text(), self.name_edit.text(),
                    self.symbol_edit.text(), self.sector_edit.text(), self._now(),
                    currency=self.currency,
                )
        except RadarError as error:
            set_state(self.error_label, "\n".join(error.errors), "dangerText")
            return False
        self.accept()
        return True


# -- la pantalla ----------------------------------------------------------------------------------


@dataclass
class AlertRow:
    """Una alerta activa en la tabla, con sus botones."""

    alert: RadarAlert
    buy_button: QPushButton
    discard_button: QPushButton
    watch_button: QPushButton | None = None


class RadarPage(QWidget):
    """La sección Radar."""

    #: «Comprar»: la alerta, para abrir Operar con sus niveles.
    buyRequested = Signal(object)
    #: Han cambiado las alertas o la lista de vigilancia (para el Panel y el lateral).
    alertsChanged = Signal()

    def __init__(
        self,
        db: Database,
        theme: ThemeController,
        *,
        settings: Settings | None = None,
        runner: ReportRunner | None = None,
        radar_runner: RadarRunner | None = None,
        prices: PriceProvider | None = None,
        now: Callable[[], datetime] = local_now,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._theme = theme
        self._settings = settings or Settings()
        self._runner = runner
        self._radar_runner = radar_runner
        self._prices = prices
        self._now = now
        self.alert_rows: list[AlertRow] = []
        self.rejected_rows: list[tuple[RadarAlert, QPushButton | None]] = []
        self.watch_chips: dict[str, QPushButton] = {}
        self.history_rows: list[RadarAlert] = []
        self.plan: RunPlan | None = None

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
        self.message_label = state_label()
        self.message_label.setVisible(False)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        caja.addWidget(self.message_label)
        caja.addWidget(self._build_alerts_card())
        abajo = QHBoxLayout()
        abajo.setSpacing(16)
        abajo.addWidget(self._build_rejected_card(), 1, Qt.AlignmentFlag.AlignTop)
        abajo.addWidget(self._build_watch_card(), 1, Qt.AlignmentFlag.AlignTop)
        caja.addLayout(abajo)
        caja.addWidget(self._build_history_card())
        caja.addStretch(1)

        if runner is not None:
            runner.started.connect(self._on_runner_started)
            runner.stageChanged.connect(self._on_stage)
            runner.finished.connect(self._on_runner_finished)
            runner.failed.connect(self._on_runner_failed)
            runner.stopped.connect(self._on_runner_stopped)
        if radar_runner is not None:
            radar_runner.started.connect(self._on_filter_started)
            radar_runner.finished.connect(self._on_filter_finished)
            radar_runner.failed.connect(self._on_filter_failed)
            radar_runner.stopped.connect(self._on_filter_stopped)
        self.reload()

    # -- construcción ------------------------------------------------------------------

    def _build_actions(self) -> QWidget:
        acciones = QWidget()
        fila = QHBoxLayout(acciones)
        fila.setContentsMargins(0, 0, 0, 0)
        fila.setSpacing(10)
        self.last_label = muted("")
        self.last_label.setWordWrap(False)
        fila.addWidget(self.last_label)
        self.explore_button = QPushButton("Buscar oportunidades nuevas")
        self.explore_button.setObjectName("primary")
        self.explore_button.clicked.connect(self.explore)
        self.explore_button.setVisible(self._runner is not None)
        fila.addWidget(self.explore_button)
        return acciones

    def _build_progress(self) -> QWidget:
        self.progress = QFrame()
        self.progress.setObjectName("card")
        fila = QHBoxLayout(self.progress)
        fila.setContentsMargins(16, 10, 16, 10)
        fila.setSpacing(12)
        self.stage_label = QLabel()
        fila.addWidget(self.stage_label, 1)
        barra = QProgressBar()
        barra.setRange(0, 0)
        barra.setTextVisible(False)
        barra.setMaximumWidth(160)
        fila.addWidget(barra)
        self.progress.setVisible(False)
        return self.progress

    def _build_alerts_card(self) -> QFrame:
        marco, caja = card("Alertas activas")
        caja.setSpacing(8)
        self.alerts_subtitle = muted("")
        caja.addWidget(self.alerts_subtitle)
        self.buying_label = state_label(state="dangerText")
        self.buying_label.setVisible(False)
        caja.addWidget(self.buying_label)
        self._alerts_grid = QGridLayout()
        self._alerts_grid.setHorizontalSpacing(14)
        self._alerts_grid.setVerticalSpacing(10)
        caja.addLayout(self._alerts_grid)
        self.alerts_empty = muted("")
        caja.addWidget(self.alerts_empty)
        return marco

    def _build_rejected_card(self) -> QFrame:
        marco, caja = card("Candidatos que no pasaron el filtro")
        caja.setSpacing(6)
        caja.addWidget(muted("Se guardan con su motivo: el precio puede acompañar más adelante. "
                             "Los del explorador se pueden vigilar aunque no hayan dado alerta."))
        self._rejected_box = QVBoxLayout()
        self._rejected_box.setSpacing(0)
        caja.addLayout(self._rejected_box)
        return marco

    def _build_watch_card(self) -> QFrame:
        marco, caja = card("Lista de vigilancia")
        self.watch_subtitle = muted("")
        caja.addWidget(self.watch_subtitle)
        self._watch_grid = QGridLayout()
        self._watch_grid.setHorizontalSpacing(8)
        self._watch_grid.setVerticalSpacing(8)
        caja.addLayout(self._watch_grid)
        botones = QHBoxLayout()
        botones.setSpacing(10)
        self.add_button = QPushButton("Añadir valor…")
        self.add_button.clicked.connect(self.add_value)
        botones.addWidget(self.add_button)
        self.filter_button = QPushButton("Pasar el filtro ahora (gratis)")
        self.filter_button.setToolTip(
            "Caduca las alertas que toca y pasa el filtro a la lista de vigilancia con precios "
            "reales. No llama a Claude. Desde el hito H12 se hace solo cada día."
        )
        self.filter_button.clicked.connect(self.run_filter)
        self.filter_button.setVisible(self._radar_runner is not None)
        botones.addWidget(self.filter_button)
        botones.addStretch(1)
        caja.addLayout(botones)
        return marco

    def _build_history_card(self) -> QFrame:
        marco, caja = card("Historial de alertas")
        caja.setSpacing(6)
        caja.addWidget(muted("Alertas caducadas, ejecutadas y descartadas por ti."))
        self._history_grid = QGridLayout()
        self._history_grid.setHorizontalSpacing(14)
        self._history_grid.setVerticalSpacing(6)
        caja.addLayout(self._history_grid)
        self.history_empty = muted("")
        caja.addWidget(self.history_empty)
        return marco

    # -- datos -------------------------------------------------------------------------

    def reload(self) -> None:
        """Vuelve a leer las alertas, los descartes y la lista (sin descargar nada)."""
        conn = self._db.connection()
        ahora = self._now()
        filas = RadarAlertRepository(conn).list_all()
        lista = WatchlistRepository(conn).list_all()
        valoracion = load_valuation(conn, ahora)
        estado = current_state(conn, valoracion, ahora.date())
        en_cartera = {p.ticker.upper() for p in valoracion.positions}
        vigilados = {w.ticker.upper() for w in lista}
        validez = self._settings.radar.alert_validity_days

        activas = active_alerts(filas)
        self._show_alerts(activas, vigilados, validez)
        self.alerts_subtitle.setText(
            f"Niveles calculados con precios reales. Caducan a los {validez} días, si rompen su "
            "stop o si alcanzan el objetivo sin ti."
        )
        set_state(
            self.buying_label,
            "" if buying_allowed(estado) or not activas else
            f"Compras prohibidas en {STATE_LABELS[estado]}: las alertas se guardan, pero Operar "
            "rechazará la compra.",
            "dangerText",
        )
        self._show_rejected(rejected_candidates(filas), vigilados)
        self._show_watchlist(lista, en_cartera)
        self.history_rows = alert_history(filas)[:HISTORY_ROWS]
        self._show_history(self.history_rows)
        self._show_header()

    def _show_header(self) -> None:
        informe = last_exploration(self._db.connection())
        if informe is None:
            self.last_label.setText("Todavía no hay ninguna exploración")
        else:
            coste = format_usd(informe.cost_usd) if informe.cost_usd > 0 else "sin coste"
            self.last_label.setText(f"Última exploración: {informe.created_at:%d/%m} · {coste}")
        if self._runner is None:
            return
        self.plan = self._runner.plan(AIAction.EXPLORER)
        self.explore_button.setText(
            f"Buscar oportunidades nuevas ({self.plan.budget.estimate.text})"
        )
        self.explore_button.setToolTip(
            "Claude busca ideas nuevas en la web y el filtro calcula los niveles con precios "
            f"reales. {self.plan.spent_text}"
            + (f" Ahora no se puede: {self.plan.no_ai_reason}." if not self.plan.uses_ai else "")
        )
        self.explore_button.setEnabled(not self._runner.running)

    def _show_alerts(self, alerts: list[RadarAlert], watched: set[str], validity: int) -> None:
        _clear(self._alerts_grid)
        self.alert_rows = []
        self.alerts_empty.setText(
            "No hay alertas activas. El filtro repasa la lista de vigilancia con precios reales; "
            "«Buscar oportunidades nuevas» le da ideas nuevas."
        )
        self.alerts_empty.setVisible(not alerts)
        if not alerts:
            return
        cabeceras = ("Ticker", "Idea", "Precio", "Stop", "Objetivo", "R:R", "Desde máx.",
                     "Peso máx.", "")
        for columna, texto in enumerate(cabeceras):
            etiqueta = QLabel(texto)
            etiqueta.setObjectName("fieldLabel")
            if 2 <= columna <= 7:
                etiqueta.setAlignment(Qt.AlignmentFlag.AlignRight)
            self._alerts_grid.addWidget(etiqueta, 0, columna)
        for fila, a in enumerate(alerts, start=1):
            quien = QWidget()
            columna_ticker = QVBoxLayout(quien)
            columna_ticker.setContentsMargins(0, 0, 0, 0)
            columna_ticker.setSpacing(2)
            columna_ticker.addWidget(_plain_label(a.ticker, bold=True))
            if a.name and a.name != a.ticker:
                nombre = muted(a.name)
                columna_ticker.addWidget(nombre)
            self._alerts_grid.addWidget(quien, fila, 0, Qt.AlignmentFlag.AlignTop)

            idea = QWidget()
            columna_idea = QVBoxLayout(idea)
            columna_idea.setContentsMargins(0, 0, 0, 0)
            columna_idea.setSpacing(2)
            texto = QLabel(a.summary or "Sin idea escrita (valor de tu lista de vigilancia).")
            texto.setWordWrap(True)
            if a.invalidation:
                texto.setToolTip(f"Qué la invalidaría: {a.invalidation}")
            columna_idea.addWidget(texto)
            columna_idea.addWidget(muted(
                f"{origin_text(a)} · caduca el {expires_on(a, validity):%d/%m}"
            ))
            self._alerts_grid.addWidget(idea, fila, 1, Qt.AlignmentFlag.AlignTop)

            valores = (
                _level(a.price, a.currency),
                _level(a.stop, a.currency),
                _level(a.target, a.currency),
                ratio_text(a.ratio) if a.ratio is not None else "—",
                drop_text(a.drawdown_from_high) if a.drawdown_from_high is not None else "—",
                format_pct(a.max_weight) if a.max_weight is not None else "—",
            )
            for columna, valor in enumerate(valores, start=2):
                self._alerts_grid.addWidget(_plain_label(valor, right=True), fila, columna,
                                            Qt.AlignmentFlag.AlignTop)

            botones = QWidget()
            columna_botones = QVBoxLayout(botones)
            columna_botones.setContentsMargins(0, 0, 0, 0)
            columna_botones.setSpacing(6)
            comprar = QPushButton("Comprar")
            comprar.setObjectName("primary")
            comprar.setToolTip("Abre Operar con el stop y el objetivo de esta alerta.")
            comprar.clicked.connect(lambda _c=False, x=a: self.buy(x))
            columna_botones.addWidget(comprar)
            descartar = QPushButton("Descartar")
            descartar.clicked.connect(lambda _c=False, x=a: self.discard(x))
            columna_botones.addWidget(descartar)
            vigilar = None
            if a.origin is AlertOrigin.EXPLORER and a.ticker.upper() not in watched:
                vigilar = self._watch_button(a)
                columna_botones.addWidget(vigilar)
            self._alerts_grid.addWidget(botones, fila, 8, Qt.AlignmentFlag.AlignTop)
            self.alert_rows.append(AlertRow(a, comprar, descartar, vigilar))
        self._alerts_grid.setColumnStretch(1, 1)

    def _watch_button(self, alert: RadarAlert) -> QPushButton:
        boton = QPushButton("Vigilar")
        boton.setObjectName("link")
        boton.setCursor(Qt.CursorShape.PointingHandCursor)
        boton.setToolTip("Añade este candidato a tu lista de vigilancia: el filtro lo repasará "
                         "cada día.")
        boton.clicked.connect(lambda _c=False, x=alert: self.watch(x))
        return boton

    def _show_rejected(self, rejected: list[RadarAlert], watched: set[str]) -> None:
        _clear(self._rejected_box)
        self.rejected_rows = []
        if not rejected:
            self._rejected_box.addWidget(muted("Ningún descarte todavía."))
            return
        for numero, a in enumerate(rejected):
            if numero:
                linea = QFrame()
                linea.setObjectName("separator")
                linea.setFrameShape(QFrame.Shape.HLine)
                self._rejected_box.addWidget(linea)
            fila = QWidget()
            caja = QHBoxLayout(fila)
            caja.setContentsMargins(0, 8, 0, 8)
            caja.setSpacing(12)
            ticker = _plain_label(a.ticker, bold=True)
            ticker.setMinimumWidth(70)
            ticker.setToolTip(a.name or a.ticker)
            caja.addWidget(ticker, 0, Qt.AlignmentFlag.AlignTop)
            textos = QVBoxLayout()
            textos.setSpacing(2)
            motivo = QLabel(a.reason or "")
            motivo.setWordWrap(True)
            if a.summary:
                motivo.setToolTip(f"Idea: {a.summary}")
            textos.addWidget(motivo)
            textos.addWidget(muted(origin_text(a)))
            caja.addLayout(textos, 1)
            vigilar = None
            if a.origin is AlertOrigin.EXPLORER and a.ticker.upper() not in watched:
                vigilar = self._watch_button(a)
                caja.addWidget(vigilar, 0, Qt.AlignmentFlag.AlignTop)
            self._rejected_box.addWidget(fila)
            self.rejected_rows.append((a, vigilar))

    def _show_watchlist(self, items: list[WatchlistItem], held: set[str]) -> None:
        _clear(self._watch_grid)
        self.watch_chips = {}
        n = len(items)
        self.watch_subtitle.setText(
            f"{n} valor{'es' if n != 1 else ''}. El filtro los repasa con precios reales, "
            "gratis. Lo que ya tienes en cartera queda fuera. Pulsa uno para quitarlo."
            if n else "Vacía. Añade los valores que quieres que el filtro repase cada día."
        )
        for i, item in enumerate(items):
            en_cartera = item.ticker.upper() in held
            boton = QPushButton(f"{item.ticker} (en cartera)" if en_cartera else item.ticker)
            boton.setObjectName("filterChip")
            boton.setCursor(Qt.CursorShape.PointingHandCursor)
            boton.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            detalles = [item.name]
            if item.yahoo_symbol:
                detalles.append(f"Yahoo {item.yahoo_symbol}")
            if item.sector:
                detalles.append(pretty_sector(item.sector))
            quien = "el explorador" if item.added_by is WatchlistSource.EXPLORER else "ti"
            detalles.append(f"añadido por {quien} el {item.added_on:%d/%m/%Y}")
            if en_cartera:
                detalles.append("en cartera: el filtro no lo repasa")
            boton.setToolTip(" · ".join(detalles) + ". Pulsa para quitarlo de la lista.")
            boton.clicked.connect(lambda _c=False, x=item: self.remove(x))
            self._watch_grid.addWidget(boton, i // WATCH_COLUMNS, i % WATCH_COLUMNS)
            self.watch_chips[item.ticker] = boton
        self._watch_grid.setColumnStretch(WATCH_COLUMNS, 1)

    def _show_history(self, rows: list[RadarAlert]) -> None:
        _clear(self._history_grid)
        self.history_empty.setText("Todavía no hay alertas cerradas.")
        self.history_empty.setVisible(not rows)
        for fila, a in enumerate(rows):
            self._history_grid.addWidget(muted(f"{a.created_on:%d/%m/%Y}"), fila, 0,
                                         Qt.AlignmentFlag.AlignTop)
            self._history_grid.addWidget(_plain_label(a.ticker, bold=True), fila, 1,
                                         Qt.AlignmentFlag.AlignTop)
            self._history_grid.addWidget(_plain_label(STATUS_LABELS[a.status]), fila, 2,
                                         Qt.AlignmentFlag.AlignTop)
            motivo = muted(a.reason or "")
            self._history_grid.addWidget(motivo, fila, 3)
        self._history_grid.setColumnStretch(3, 1)

    # -- acciones ----------------------------------------------------------------------

    def _message(self, text: str, state: str = "okText") -> None:
        set_state(self.message_label, text, state)

    def buy(self, alert: RadarAlert) -> None:
        """«Comprar»: Operar con los niveles de la alerta (lo abre la ventana principal)."""
        self.buyRequested.emit(alert)

    def discard(self, alert: RadarAlert) -> None:
        if alert.id is None:
            return
        motivo = self.ask_discard_reason(alert)
        if motivo is None:
            return
        try:
            with self._db.transaction() as conn:
                discard_alert(conn, alert.id, motivo, self._now())
        except RadarError as error:
            self.show_error("\n".join(error.errors))
            self._changed()
            return
        self._message(f"Alerta de {alert.ticker} descartada: el filtro no la volverá a emitir "
                      "hasta que habría caducado.", "muted")
        self._changed()

    def watch(self, alert: RadarAlert) -> None:
        if alert.id is None:
            return
        try:
            with self._db.transaction() as conn:
                item = watch_candidate(conn, alert.id, self._now())
        except RadarError as error:
            self.show_error("\n".join(error.errors))
            return
        self._message(f"{item.ticker} añadido a la lista de vigilancia.")
        self._changed()

    def add_value(self) -> WatchlistItem | None:
        dialogo = AddWatchDialog(self._db, self._prices, self._now, self)
        try:
            if not self.run_dialog(dialogo) or dialogo.item is None:
                return None
            item = dialogo.item
        finally:
            dialogo.deleteLater()
        self._message(f"{item.ticker} añadido a la lista de vigilancia. El filtro lo repasará "
                      "la próxima vez (o pulsa «Pasar el filtro ahora»).")
        self._changed()
        return item

    def remove(self, item: WatchlistItem) -> None:
        if not self.confirm_remove(item):
            return
        with self._db.transaction() as conn:
            remove_from_watchlist(conn, item.ticker)
        self._message(f"{item.ticker} quitado de la lista de vigilancia.", "muted")
        self._changed()

    def run_filter(self) -> None:
        if self._radar_runner is not None:
            self._radar_runner.start()

    def explore(self) -> None:
        """«Buscar oportunidades nuevas»: enseña antes lo que costará y, si se confirma, lo
        lanza en segundo plano."""
        if self._runner is None or self._runner.running:
            return
        plan = self._runner.plan(AIAction.EXPLORER)
        if not plan.uses_ai:
            self.show_error(f"La búsqueda de oportunidades necesita a Claude y ahora no se puede "
                            f"lanzar: {plan.no_ai_reason}.")
            return
        if not self.confirm_explore(plan):
            return
        self._runner.start(AIAction.EXPLORER)

    def _changed(self) -> None:
        self.reload()
        self.alertsChanged.emit()

    # -- lo que llega de los lanzadores --------------------------------------------------

    def _mine(self) -> bool:
        return self._runner is not None and self._runner.action is AIAction.EXPLORER

    def _busy(self, text: str) -> None:
        self.stage_label.setText(text)
        self.progress.setVisible(True)

    def _on_runner_started(self) -> None:
        self.explore_button.setEnabled(False)
        if self._mine():
            self._message("", "muted")
            self._busy("Actualizando precios (gratis)…")

    def _on_stage(self, text: str) -> None:
        if self._mine():
            self._busy(text)

    def _on_runner_finished(self, outcome: object) -> None:
        if not isinstance(outcome, ExplorationOutcome):
            return
        informe = outcome.report
        if informe.used_ai:
            uso = usage_text(informe.input_tokens, informe.output_tokens, informe.web_searches)
            nuevas = outcome.new_alerts
            texto = (f"Exploración guardada en Informes. Coste real: "
                     f"{format_usd(informe.cost_usd)} ({uso}). ")
            if outcome.candidates_unavailable is not None:
                texto += f"Candidatos no disponibles: {outcome.candidates_unavailable}"
            elif nuevas:
                texto += (f"{len(nuevas)} alerta{'s' if len(nuevas) != 1 else ''} nueva"
                          f"{'s' if len(nuevas) != 1 else ''}: "
                          f"{', '.join(a.ticker for a in nuevas)}.")
            else:
                texto += ("Ninguna alerta nueva: el filtro no ve asimetría hoy con precios "
                          "reales.")
            self._message(texto, "okText")
        else:
            texto = f"Exploración guardada sin análisis de IA: {informe.error}"
            if informe.cost_usd > 0:
                texto += f" Coste real de los intentos: {format_usd(informe.cost_usd)}."
            self._message(texto, "warnText")
        self._changed()

    def _on_runner_failed(self, message: str) -> None:
        if self._mine():
            self._message(f"No se ha podido buscar oportunidades: {message}", "dangerText")

    def _on_runner_stopped(self) -> None:
        if not (self._radar_runner is not None and self._radar_runner.running):
            self.progress.setVisible(False)
        self._show_header()

    def _on_filter_started(self) -> None:
        self.filter_button.setEnabled(False)
        self._message("", "muted")
        self._busy("Pasando el filtro con precios reales (gratis)…")

    def _on_filter_finished(self, outcome: RadarOutcome) -> None:
        estado = "warnText" if outcome.offline else "okText"
        self._message(f"Filtro pasado: {outcome.message}", estado)
        self._changed()

    def _on_filter_failed(self, message: str) -> None:
        self._message(f"No se ha podido pasar el filtro: {message}", "dangerText")

    def _on_filter_stopped(self) -> None:
        self.filter_button.setEnabled(True)
        if not (self._runner is not None and self._mine()):
            self.progress.setVisible(False)

    # -- diálogos (los tests los sustituyen) ---------------------------------------------

    def ask_discard_reason(self, alert: RadarAlert) -> str | None:
        """El motivo (opcional) de descartar una alerta. None si se cancela."""
        texto, aceptado = QInputDialog.getText(
            self, "Descartar alerta",
            f"¿Descartar la alerta de {alert.ticker}? El filtro no la volverá a emitir hasta "
            "que habría caducado.\n\nMotivo (opcional):",
        )
        return texto if aceptado else None

    def confirm_remove(self, item: WatchlistItem) -> bool:
        caja = QMessageBox(self)
        caja.setIcon(QMessageBox.Icon.Question)
        caja.setWindowTitle("Lista de vigilancia")
        caja.setText(f"¿Quitar {item.ticker} ({item.name}) de la lista de vigilancia?")
        caja.setInformativeText("Sus alertas y descartes se conservan.")
        quitar = caja.addButton("Quitar", QMessageBox.ButtonRole.AcceptRole)
        cancelar = caja.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        caja.setDefaultButton(cancelar)
        caja.exec()
        return caja.clickedButton() is quitar

    def confirm_explore(self, plan: RunPlan) -> bool:
        caja = QMessageBox(self)
        caja.setIcon(QMessageBox.Icon.Question)
        caja.setWindowTitle("Buscar oportunidades")
        caja.setText(plan_question(plan))
        caja.setInformativeText("\n\n".join(plan_lines(plan, None)))
        lanzar = caja.addButton(plan_button(plan), QMessageBox.ButtonRole.AcceptRole)
        cancelar = caja.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        caja.setDefaultButton(cancelar)
        caja.exec()
        return caja.clickedButton() is lanzar

    def run_dialog(self, dialog: QDialog) -> bool:
        return dialog.exec() == QDialog.DialogCode.Accepted

    def show_error(self, message: str) -> None:
        QMessageBox.warning(self, "Sharky", message)

    def shutdown(self) -> None:
        if self._radar_runner is not None:
            self._radar_runner.shutdown()

    def showEvent(self, event: object) -> None:
        super().showEvent(event)
        self.reload()


# -- la tarjeta del Panel -------------------------------------------------------------------------


class RadarCard(QFrame):
    """«Oportunidades en radar» (GUIA §5.10, punto 1): las alertas activas y el acceso al
    Radar."""

    openRequested = Signal()

    def __init__(
        self,
        db: Database,
        *,
        now: Callable[[], datetime] = local_now,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._db = db
        self._now = now
        self.alerts: list[RadarAlert] = []
        caja = QVBoxLayout(self)
        caja.setContentsMargins(20, 16, 20, 16)
        caja.setSpacing(8)
        fila = QHBoxLayout()
        fila.setSpacing(10)
        titulo = QLabel("Oportunidades en radar")
        titulo.setObjectName("cardTitle")
        fila.addWidget(titulo)
        self.count_label = QLabel()
        self.count_label.setObjectName("chipNeutral")
        fila.addWidget(self.count_label)
        fila.addStretch(1)
        caja.addLayout(fila)
        self.lines_label = QLabel()
        self.lines_label.setWordWrap(True)
        caja.addWidget(self.lines_label)
        self.last_label = muted("")
        caja.addWidget(self.last_label)
        botones = QHBoxLayout()
        botones.addStretch(1)
        self.open_button = QPushButton("Abrir Radar")
        self.open_button.clicked.connect(self.openRequested)
        botones.addWidget(self.open_button)
        caja.addLayout(botones)
        self.refresh()

    def refresh(self) -> None:
        conn = self._db.connection()
        self.alerts = active_alerts(RadarAlertRepository(conn).list_all())
        n = len(self.alerts)
        self.count_label.setText("1 activa" if n == 1 else f"{n} activas")
        if not self.alerts:
            self.lines_label.setText(
                "Ninguna alerta activa. El filtro repasa tu lista de vigilancia con precios "
                "reales, gratis."
            )
            restyle(self.lines_label, "muted")
        else:
            lineas = []
            for a in self.alerts[:PANEL_ALERTS]:
                partes = [a.ticker]
                if a.ratio is not None:
                    partes.append(f"R:R {ratio_text(a.ratio)}")
                if a.drawdown_from_high is not None:
                    partes.append(f"{drop_text(a.drawdown_from_high)} desde máx.")
                lineas.append("• " + " · ".join(partes))
            if n > PANEL_ALERTS:
                lineas.append(f"… y {n - PANEL_ALERTS} más")
            self.lines_label.setText("\n".join(lineas))
            restyle(self.lines_label, "conclusion")
        informe = last_exploration(conn)
        self.last_label.setText(
            f"Última exploración: {informe.created_at:%d/%m/%Y}" if informe is not None
            else "Todavía no hay ninguna exploración."
        )


def active_count(db: Database) -> int:
    """Cuántas alertas activas hay (para el lateral)."""
    return len(RadarAlertRepository(db.connection()).active())
