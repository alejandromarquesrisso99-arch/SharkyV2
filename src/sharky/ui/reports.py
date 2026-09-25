"""Informes (GUIA §5.10, punto 6, y §7, H9): la pantalla, la tarjeta «Control diario» del Panel
y el lanzador que comparten.

- **Pantalla Informes** (docs/maqueta/06-informes.png): filtros por tipo, la lista por fecha
  con el coste de cada informe o la marca «Sin IA», y el lector Markdown (QTextBrowser) con el
  modelo, el esfuerzo y el coste. «Exportar .md».
- **Tarjeta «Control diario»** del Panel: su estado, la fecha, la conclusión, «Leer» y
  «Ejecutar ahora» con el precio aproximado al lado. El diálogo de confirmación lo repite junto
  a lo que va a hacer y al gasto del mes; al terminar se enseña el coste real.
- **ReportRunner**: primero actualiza los precios con la descarga de siempre (gratis; el aviso
  de un stop sale en ese momento) y después, en un hilo de trabajo, el control diario con
  Claude (services/reports.py). No hay «Cancelar» mientras escribe Claude: es una llamada corta
  y ya pagada. Al cerrar la app se corta y no se guarda nada.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from sharky.core.formatting import format_usd, month_name
from sharky.core.models import Report, ReportKind
from sharky.core.reports import (
    ACTION_LABELS,
    NO_AI_LABEL,
    AIAction,
    BudgetCheck,
    usage_text,
)
from sharky.services import secrets
from sharky.services.ai import ClaudeClient
from sharky.services.db import Database
from sharky.services.market import load_valuation, local_now
from sharky.services.reports import (
    TRIGGER_MANUAL,
    DailyOutcome,
    budget_check,
    create_daily_report,
    daily_for,
    latest_report,
    price_for,
)
from sharky.services.repositories import ReportRepository
from sharky.services.settings import Settings
from sharky.ui.pages import muted, restyle, set_state, state_label
from sharky.ui.portfolio import PriceRefresher
from sharky.ui.theme import ThemeController
from sharky.ui.workers import Worker, start

log = logging.getLogger(__name__)

#: El título de cada tipo de informe.
KIND_TITLES: dict[ReportKind, str] = {
    ReportKind.DAILY: "Control diario",
    ReportKind.WEEKLY: "Noticias semanales",
    ReportKind.MONTHLY: "Estudio mensual",
    ReportKind.EXPLORATION: "Exploración de mercado",
}

#: Los filtros de la lista: texto y tipo (None = todos).
FILTERS: tuple[tuple[str, ReportKind | None], ...] = (
    ("Todos", None),
    ("Diario", ReportKind.DAILY),
    ("Semanal", ReportKind.WEEKLY),
    ("Mensual", ReportKind.MONTHLY),
    ("Exploración", ReportKind.EXPLORATION),
)

LIST_WIDTH = 250


def report_title(report: Report) -> str:
    """«Control diario», «Estudio mensual — agosto»…"""
    titulo = KIND_TITLES[report.kind]
    if report.kind is ReportKind.MONTHLY:
        try:
            anio, mes = (int(x) for x in report.period.split("-")[:2])
        except ValueError:
            return titulo
        return f"{titulo} — {month_name(mes)} {anio}"
    return titulo


def report_heading(report: Report) -> str:
    """«Control diario · 25/09/2026»."""
    return f"{report_title(report)} · {report.created_at:%d/%m/%Y}"


def report_meta(report: Report) -> str:
    """«claude-sonnet-5 · esfuerzo low · 0,03 $» o «Sin análisis de IA · …»."""
    partes = []
    if not report.used_ai:
        partes.append(NO_AI_LABEL)
    if report.model:
        partes.append(report.model)
    if report.effort:
        partes.append(f"esfuerzo {report.effort}")
    partes.append(format_usd(report.cost_usd) if report.cost_usd > 0 else "gratis")
    return " · ".join(partes)


def report_details(report: Report) -> str:
    """Para la ayuda del coste: cuándo se hizo y los tokens."""
    texto = f"Hecho el {report.created_at:%d/%m/%Y a las %H:%M}."
    if report.input_tokens or report.output_tokens:
        texto += " " + usage_text(report.input_tokens, report.output_tokens,
                                  report.web_searches) + "."
    if report.error:
        texto += f" {NO_AI_LABEL}: {report.error}"
    return texto


def export_text(report: Report) -> str:
    """El informe como fichero .md: su título, la línea del modelo y el coste, y el cuerpo."""
    return f"# {report_heading(report)}\n\n*{report_meta(report)}*\n\n{report.markdown}"


def export_name(report: Report) -> str:
    return f"sharky-{report.kind.value.lower()}-{report.period}.md"


def conclusion_lines(conclusion: str) -> str:
    """Las viñetas en Markdown de una conclusión, como texto para una etiqueta."""
    lineas = []
    for linea in conclusion.splitlines():
        limpia = linea.strip()
        if not limpia:
            continue
        for vineta in ("- ", "* ", "• "):
            if limpia.startswith(vineta):
                limpia = "• " + limpia[len(vineta):]
                break
        lineas.append(limpia.replace("**", ""))
    return "\n".join(lineas)


def _chip(texto: str, estilo: str) -> tuple[QFrame, QLabel]:
    marco = QFrame()
    marco.setObjectName(estilo)
    caja = QHBoxLayout(marco)
    caja.setContentsMargins(8, 1, 8, 1)
    etiqueta = QLabel(texto)
    caja.addWidget(etiqueta)
    marco.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return marco, etiqueta


# -- el lanzador ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RunPlan:
    """Lo que se enseña antes de lanzar una acción: con qué, cuánto costaría y si cabe."""

    action: AIAction
    model: str
    effort: str
    has_key: bool
    priced: bool
    budget: BudgetCheck
    existing: Report | None
    month: int

    @property
    def uses_ai(self) -> bool:
        return self.has_key and self.priced and self.budget.allowed

    @property
    def no_ai_reason(self) -> str | None:
        if not self.has_key:
            return "no hay clave de Claude"
        if not self.priced:
            return f"falta el precio de {self.model} en Ajustes → Claude"
        if not self.budget.allowed:
            return self.budget.reason
        return None

    @property
    def price_text(self) -> str:
        """Lo que va al lado del botón: «≈ 0,02–0,05 $» o «gratis»."""
        if self.uses_ai:
            return self.budget.estimate.text
        return "gratis, sin IA" if not self.has_key else "gratis: tope de gasto"

    @property
    def spent_text(self) -> str:
        return f"Gasto de {month_name(self.month)}: {self.budget.spent_text}."


def run_daily_job(
    db: Database,
    settings: Settings,
    now: Callable[[], datetime],
    market_at: datetime | None,
    key_loader: Callable[[], str | None],
    client_factory: Callable[[str], ClaudeClient],
    cancel: threading.Event,
) -> DailyOutcome:
    """El control diario en un hilo de trabajo, con los precios que acaban de guardarse."""
    valoracion = load_valuation(db.connection(), now(), market_at)
    return create_daily_report(
        db, valoracion, settings, now, key_loader(),
        trigger=TRIGGER_MANUAL, cancel=cancel, client_factory=client_factory,
    )


class ReportRunner(QObject):
    """«Ejecutar ahora» del control diario: precios primero (gratis) y luego Claude, en
    segundo plano. Una sola ejecución a la vez. Las señales llegan al hilo de la interfaz."""

    started = Signal()
    #: Lo que se está haciendo ahora, para enseñarlo junto a la barra.
    stageChanged = Signal(str)
    #: Ha terminado, con su DailyOutcome.
    finished = Signal(object)
    failed = Signal(str)
    #: Ha terminado, bien o mal (después de `finished` o `failed`).
    stopped = Signal()

    def __init__(
        self,
        db: Database,
        refresher: PriceRefresher,
        settings: Settings,
        *,
        now: Callable[[], datetime] = local_now,
        key_loader: Callable[[], str | None] = secrets.load_api_key,
        client_factory: Callable[[str], ClaudeClient] = ClaudeClient,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._refresher = refresher
        self.settings = settings
        self._now = now
        self._key_loader = key_loader
        self._client_factory = client_factory
        self._waiting_prices = False
        self._worker: Worker | None = None
        self._cancel: threading.Event | None = None
        refresher.stopped.connect(self._on_prices_done)

    @property
    def running(self) -> bool:
        return self._waiting_prices or self._worker is not None

    def plan_daily(self) -> RunPlan:
        """Con qué modelo iría, cuánto costaría y si cabe en el tope. Solo lee."""
        conn = self._db.connection()
        ahora = self._now()
        ai = self.settings.ai
        return RunPlan(
            AIAction.DAILY,
            ai.daily.model,
            ai.daily.effort,
            self._key_loader() is not None,
            price_for(ai, ai.daily.model) is not None,
            budget_check(conn, ai, AIAction.DAILY, ahora),
            daily_for(conn, ahora.date()),
            ahora.month,
        )

    def start_daily(self) -> bool:
        """Empieza: precios y después el control diario. False si ya estaba en marcha."""
        if self.running:
            return False
        self._cancel = threading.Event()
        self.started.emit()
        self.stageChanged.emit("Actualizando precios (gratis)…")
        self._waiting_prices = True
        if not self._refresher.running and not self._refresher.start():
            self._waiting_prices = False
            self._start_report()
        return True

    def _on_prices_done(self) -> None:
        if not self._waiting_prices:
            return
        self._waiting_prices = False
        if self._cancel is not None and self._cancel.is_set():
            self._finish()
            return
        # Sin red también se sigue: el informe dice que los precios no son fiables.
        self._start_report()

    def _start_report(self) -> None:
        self.stageChanged.emit("Claude está escribiendo el control diario…")
        trabajo = Worker(
            run_daily_job,
            self._db,
            self.settings,
            self._now,
            self._refresher.market_at,
            self._key_loader,
            self._client_factory,
            self._cancel or threading.Event(),
        )
        trabajo.signals.finished.connect(self._on_done)
        trabajo.signals.failed.connect(self._on_failed)
        self._worker = trabajo  # sin referencia, las señales se perderían
        start(trabajo)

    def _on_done(self, outcome: DailyOutcome) -> None:
        self._worker = None
        self.finished.emit(outcome)
        self._finish()

    def _on_failed(self, message: str) -> None:
        self._worker = None
        if not (self._cancel is not None and self._cancel.is_set()):
            self.failed.emit(message)
        self._finish()

    def _finish(self) -> None:
        self._cancel = None
        self.stopped.emit()

    def shutdown(self) -> None:
        """Al cerrar la app: se corta la respuesta de Claude y no se guarda nada."""
        if self._cancel is not None:
            self._cancel.set()


# -- la tarjeta del Panel ---------------------------------------------------------------------


def _day(moment: datetime, today: date) -> str:
    if moment.date() == today:
        return f"hoy, {moment:%H:%M}"
    return f"{moment:%d/%m/%Y, %H:%M}"


class DailyCard(QFrame):
    """La tarjeta «Control diario» del Panel."""

    #: «Leer»: el id del informe.
    readRequested = Signal(int)

    def __init__(
        self,
        db: Database,
        runner: ReportRunner,
        *,
        now: Callable[[], datetime] = local_now,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._db = db
        self._runner = runner
        self._now = now
        self.report: Report | None = None
        self.plan: RunPlan | None = None

        caja = QVBoxLayout(self)
        caja.setContentsMargins(20, 16, 20, 16)
        caja.setSpacing(8)
        fila = QHBoxLayout()
        fila.setSpacing(10)
        titulo = QLabel(ACTION_LABELS[AIAction.DAILY])
        titulo.setObjectName("cardTitle")
        fila.addWidget(titulo)
        self.chip, self.chip_label = _chip("Pendiente", "chipMuted")
        fila.addWidget(self.chip)
        fila.addStretch(1)
        caja.addLayout(fila)

        self.when_label = muted("")
        caja.addWidget(self.when_label)
        self.conclusion_label = QLabel()
        self.conclusion_label.setWordWrap(True)
        self.conclusion_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        caja.addWidget(self.conclusion_label)

        self.progress = QWidget()
        progreso = QHBoxLayout(self.progress)
        progreso.setContentsMargins(0, 0, 0, 0)
        self.stage_label = muted("")
        progreso.addWidget(self.stage_label, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        self.bar.setMaximumWidth(120)
        progreso.addWidget(self.bar)
        self.progress.setVisible(False)
        caja.addWidget(self.progress)

        self.message_label = state_label()
        self.message_label.setVisible(False)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        caja.addWidget(self.message_label)

        botones = QHBoxLayout()
        botones.setSpacing(8)
        self.read_button = QPushButton("Leer")
        self.read_button.clicked.connect(self._on_read)
        botones.addWidget(self.read_button)
        botones.addStretch(1)
        self.run_button = QPushButton("Ejecutar ahora")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self.run_now)
        botones.addWidget(self.run_button)
        caja.addLayout(botones)

        runner.started.connect(self._on_started)
        runner.stageChanged.connect(self.stage_label.setText)
        runner.finished.connect(self._on_finished)
        runner.failed.connect(self._on_failed)
        runner.stopped.connect(self.refresh)
        self.refresh()

    # -- estado ------------------------------------------------------------------------

    def refresh(self) -> None:
        """Pone al día la tarjeta con lo guardado y el precio del botón."""
        conn = self._db.connection()
        ahora = self._now()
        hoy = ahora.date()
        de_hoy = daily_for(conn, hoy)
        ultimo = de_hoy or latest_report(conn, ReportKind.DAILY)
        self.report = ultimo
        self.plan = self._runner.plan_daily()

        en_marcha = self._runner.running
        if en_marcha:
            texto, estilo = "En marcha", "chipMuted"
        elif de_hoy is None:
            texto, estilo = "Pendiente", "chipMuted"
        elif de_hoy.used_ai:
            texto, estilo = "Hecho hoy", "chipOk"
        else:
            texto, estilo = "Sin IA", "chipWarn"
        self.chip_label.setText(texto)
        restyle(self.chip, estilo)

        if ultimo is None:
            self.when_label.setText("Todavía no hay ningún control diario.")
            self.when_label.setToolTip("")
            self.conclusion_label.setText(
                "El control del día con Claude: estado, niveles e incumplimientos, y una "
                "conclusión corta que releerán el semanal y el mensual."
            )
            restyle(self.conclusion_label, "muted")
        else:
            prefijo = "" if de_hoy is not None else "Último: "
            self.when_label.setText(f"{prefijo}{_day(ultimo.created_at, hoy)} · "
                                    f"{report_meta(ultimo)}")
            self.when_label.setToolTip(report_details(ultimo))
            self.conclusion_label.setText(conclusion_lines(ultimo.conclusion))
            restyle(self.conclusion_label, "conclusion")
        self.read_button.setEnabled(ultimo is not None and ultimo.id is not None)
        self.run_button.setText(f"Ejecutar ahora · {self.plan.price_text}")
        self.run_button.setToolTip(
            "Actualiza los precios (gratis) y pide a Claude el control del día. "
            f"{self.plan.spent_text}"
        )
        self.run_button.setEnabled(not en_marcha)
        self.progress.setVisible(en_marcha)

    # -- acciones ----------------------------------------------------------------------

    def _on_read(self) -> None:
        if self.report is not None and self.report.id is not None:
            self.readRequested.emit(self.report.id)

    def run_now(self) -> None:
        plan = self._runner.plan_daily()
        if not self.confirm_run(plan):
            return
        set_state(self.message_label, "", "muted")
        self._runner.start_daily()

    def _on_started(self) -> None:
        self.run_button.setEnabled(False)
        self.progress.setVisible(True)
        self.chip_label.setText("En marcha")
        restyle(self.chip, "chipMuted")
        set_state(self.message_label, "", "muted")

    def _on_finished(self, outcome: DailyOutcome) -> None:
        informe = outcome.report
        if informe.used_ai:
            texto = (
                f"Control diario guardado. Coste real: {format_usd(informe.cost_usd)} "
                f"({usage_text(informe.input_tokens, informe.output_tokens)})."
            )
            set_state(self.message_label, texto, "okText")
        else:
            texto = f"Guardado sin análisis de IA: {informe.error}"
            if informe.cost_usd > 0:
                texto += f" Coste real de los intentos: {format_usd(informe.cost_usd)}."
            set_state(self.message_label, texto, "warnText")

    def _on_failed(self, message: str) -> None:
        set_state(self.message_label, f"No se ha podido hacer el control diario: {message}",
                  "dangerText")

    # -- diálogo (los tests lo sustituyen) -----------------------------------------------

    def confirm_run(self, plan: RunPlan) -> bool:
        caja = QMessageBox(self)
        caja.setIcon(QMessageBox.Icon.Question)
        caja.setWindowTitle(ACTION_LABELS[AIAction.DAILY])
        lineas = []
        if plan.uses_ai:
            caja.setText("¿Hacer ahora el control diario con Claude?")
            lineas.append(
                "Primero se actualizan los precios (gratis). Después Claude "
                f"({plan.model}, esfuerzo {plan.effort}) escribe el análisis del día con las "
                "cifras que calcula Sharky."
            )
            lineas.append(f"Coste aproximado: {plan.budget.estimate.text}.")
            boton = f"Ejecutar ({plan.budget.estimate.text})"
        else:
            caja.setText("El control diario saldrá sin análisis de IA.")
            lineas.append(
                f"Motivo: {plan.no_ai_reason}. Se actualizan los precios y se guarda la parte "
                f"que calcula Sharky, con la etiqueta «{NO_AI_LABEL}». Gratis."
            )
            boton = "Generar sin IA (gratis)"
        lineas.append(plan.spent_text)
        if plan.existing is not None:
            lineas.append(
                f"Ya hay un control de hoy ({plan.existing.created_at:%H:%M}): se sustituirá."
            )
        caja.setInformativeText("\n\n".join(lineas))
        ejecutar = caja.addButton(boton, QMessageBox.ButtonRole.AcceptRole)
        cancelar = caja.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        caja.setDefaultButton(cancelar)
        caja.exec()
        return caja.clickedButton() is ejecutar


# -- la pantalla ----------------------------------------------------------------------------


class ReportItem(QPushButton):
    """Un informe en la lista: título, fecha y coste (o «Sin IA»)."""

    def __init__(self, report: Report, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.report = report
        self.setObjectName("reportItem")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        rejilla = QGridLayout(self)
        rejilla.setContentsMargins(14, 10, 14, 10)
        rejilla.setHorizontalSpacing(8)
        rejilla.setVerticalSpacing(4)
        titulo = QLabel(report_title(report))
        titulo.setObjectName("itemTitle")
        titulo.setWordWrap(True)
        rejilla.addWidget(titulo, 0, 0)
        if report.used_ai:
            self.cost_label: QWidget = muted(format_usd(report.cost_usd))
            self.cost_label.setWordWrap(False)
        else:
            self.cost_label, _texto = _chip("Sin IA", "chipWarn")
        rejilla.addWidget(self.cost_label, 0, 1, Qt.AlignmentFlag.AlignTop)
        fecha = muted(f"{report.created_at:%d/%m}")
        fecha.setWordWrap(False)
        rejilla.addWidget(fecha, 1, 0, 1, 2)
        rejilla.setColumnStretch(0, 1)
        self.setMinimumHeight(rejilla.sizeHint().height() + 6)
        self.setToolTip(report_details(report))
        self.setAccessibleName(f"{report_heading(report)}: {report_meta(report)}")


class ReportsPage(QWidget):
    """La sección Informes."""

    def __init__(
        self,
        db: Database,
        theme: ThemeController,
        runner: ReportRunner | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._theme = theme
        self.kind: ReportKind | None = None
        self.items: list[ReportItem] = []
        self.current: Report | None = None

        caja = QHBoxLayout(self)
        caja.setContentsMargins(0, 0, 0, 0)
        caja.setSpacing(18)

        izquierda = QVBoxLayout()
        izquierda.setSpacing(10)
        self.filter_buttons: dict[ReportKind | None, QPushButton] = {}
        grupo = QButtonGroup(self)
        grupo.setExclusive(True)
        filtros = QGridLayout()
        filtros.setHorizontalSpacing(6)
        filtros.setVerticalSpacing(6)
        for n, (texto, tipo) in enumerate(FILTERS):
            boton = QPushButton(texto)
            boton.setObjectName("filterChip")
            boton.setCheckable(True)
            boton.setCursor(Qt.CursorShape.PointingHandCursor)
            boton.clicked.connect(lambda _c=False, t=tipo: self.set_filter(t))
            grupo.addButton(boton)
            filtros.addWidget(boton, n // 2, n % 2)
            self.filter_buttons[tipo] = boton
        self.filter_buttons[None].setChecked(True)
        izquierda.addLayout(filtros)

        desplazable = QScrollArea()
        desplazable.setWidgetResizable(True)
        desplazable.setFrameShape(QFrame.Shape.NoFrame)
        lista = QWidget()
        self._list_box = QVBoxLayout(lista)
        self._list_box.setContentsMargins(0, 0, 0, 0)
        self._list_box.setSpacing(8)
        self._list_box.addStretch(1)
        desplazable.setWidget(lista)
        izquierda.addWidget(desplazable, 1)
        self.empty_list_label = muted("")
        izquierda.addWidget(self.empty_list_label)
        panel_izquierdo = QWidget()
        panel_izquierdo.setLayout(izquierda)
        panel_izquierdo.setFixedWidth(LIST_WIDTH)
        caja.addWidget(panel_izquierdo)

        lector = QFrame()
        lector.setObjectName("card")
        contenido = QVBoxLayout(lector)
        contenido.setContentsMargins(20, 18, 20, 18)
        contenido.setSpacing(10)
        cabecera = QHBoxLayout()
        cabecera.setSpacing(12)
        titulos = QVBoxLayout()
        titulos.setSpacing(2)
        self.heading_label = QLabel()
        self.heading_label.setObjectName("cardTitle")
        self.heading_label.setWordWrap(True)
        titulos.addWidget(self.heading_label)
        self.meta_label = muted("")
        titulos.addWidget(self.meta_label)
        cabecera.addLayout(titulos, 1)
        self.export_button = QPushButton("Exportar .md")
        self.export_button.clicked.connect(self.export_current)
        cabecera.addWidget(self.export_button, 0, Qt.AlignmentFlag.AlignTop)
        contenido.addLayout(cabecera)
        linea = QFrame()
        linea.setObjectName("separator")
        linea.setFrameShape(QFrame.Shape.HLine)
        contenido.addWidget(linea)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.setFrameShape(QFrame.Shape.NoFrame)
        contenido.addWidget(self.browser, 1)
        self.status_label = muted("")
        self.status_label.setVisible(False)
        contenido.addWidget(self.status_label)
        caja.addWidget(lector, 1)

        if runner is not None:
            runner.finished.connect(self._on_report_saved)
        self.reload()

    # -- datos -------------------------------------------------------------------------

    def reports(self) -> list[Report]:
        todos = ReportRepository(self._db.connection()).list_all()
        return [r for r in todos if self.kind is None or r.kind is self.kind]

    def reload(self, select_id: int | None = None) -> None:
        """Vuelve a leer la lista. Deja elegido `select_id`, el que estaba o el primero."""
        elegido = select_id if select_id is not None else (
            self.current.id if self.current is not None else None
        )
        while self._list_box.count() > 1:
            elemento = self._list_box.takeAt(0)
            if elemento.widget() is not None:
                elemento.widget().deleteLater()
        self.items = []
        informes = self.reports()
        for informe in informes:
            item = ReportItem(informe)
            item.clicked.connect(lambda _c=False, i=item: self._show(i.report))
            self._list_box.insertWidget(self._list_box.count() - 1, item)
            self.items.append(item)
        if not informes:
            self.empty_list_label.setText(
                "Todavía no hay informes." if self.kind is None
                else "No hay informes de este tipo."
            )
        self.empty_list_label.setVisible(not informes)
        destino = next((r for r in informes if r.id == elegido), None) or (
            informes[0] if informes else None
        )
        self._show(destino)

    def set_filter(self, kind: ReportKind | None) -> None:
        self.kind = kind
        boton = self.filter_buttons[kind]
        if not boton.isChecked():
            boton.setChecked(True)
        self.reload()

    def select_report(self, report_id: int) -> bool:
        """Enseña ese informe (quita el filtro si hace falta)."""
        if self.kind is not None:
            self.kind = None
            self.filter_buttons[None].setChecked(True)
        self.reload(select_id=report_id)
        return self.current is not None and self.current.id == report_id

    def _show(self, report: Report | None) -> None:
        self.current = report
        for item in self.items:
            item.setChecked(report is not None and item.report.id == report.id)
        self.status_label.setVisible(False)
        if report is None:
            self.heading_label.setText("Informes")
            self.meta_label.setText("")
            self.meta_label.setToolTip("")
            self.browser.setMarkdown(
                "Todavía no hay informes. El primero sale del Panel: tarjeta "
                "**Control diario** → **Ejecutar ahora**."
            )
            self.export_button.setEnabled(False)
            return
        self.heading_label.setText(report_heading(report))
        self.meta_label.setText(report_meta(report))
        self.meta_label.setToolTip(report_details(report))
        self.browser.setMarkdown(report.markdown)
        self.export_button.setEnabled(True)

    def _on_report_saved(self, outcome: DailyOutcome) -> None:
        self.reload(select_id=outcome.report.id)

    # -- exportar ----------------------------------------------------------------------

    def export_current(self) -> None:
        if self.current is None:
            return
        ruta = self.choose_export_path(export_name(self.current))
        if ruta is None:
            return
        try:
            ruta.write_text(export_text(self.current), encoding="utf-8")
        except OSError as error:
            log.warning("No se ha podido exportar el informe (%s)", type(error).__name__)
            self.show_error(f"No se ha podido guardar {ruta.name}: {error.strerror or error}")
            return
        log.info("Informe exportado a %s", ruta)
        self.status_label.setText(f"Exportado: {ruta}")
        self.status_label.setVisible(True)

    # -- diálogos (los tests los sustituyen) ---------------------------------------------

    def choose_export_path(self, default_name: str) -> Path | None:
        carpeta = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        )
        inicial = str(Path(carpeta) / default_name) if carpeta else default_name
        ruta, _filtro = QFileDialog.getSaveFileName(
            self, "Exportar informe", inicial, "Markdown (*.md)"
        )
        return Path(ruta) if ruta else None

    def show_error(self, message: str) -> None:
        QMessageBox.warning(self, "Sharky", message)

    def showEvent(self, event: object) -> None:
        super().showEvent(event)
        self.reload()
