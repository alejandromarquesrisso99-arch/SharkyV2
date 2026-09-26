"""Informes (GUIA §5.10, punto 6, y §7, H9 y H10): la pantalla, las tarjetas de los tres informes
del Panel y el lanzador que comparten.

- **Pantalla Informes** (docs/maqueta/06-informes.png): filtros por tipo, la lista por fecha
  con el coste de cada informe o la marca «Sin IA», y el lector Markdown (QTextBrowser) con el
  modelo, el esfuerzo y el coste. «Exportar .md». Los enlaces de «Fuentes» se abren fuera.
- **Tarjetas «Control diario», «Noticias semanales» y «Estudio mensual»** del Panel: su estado
  (qué toca, según core/schedule.py), la fecha, la conclusión, «Leer» y «Ejecutar ahora» con el
  precio aproximado al lado. El diálogo de confirmación lo repite junto a lo que va a hacer y al
  gasto del mes; el del mensual deja elegir el mes anterior o el mes en curso (parcial). Al
  terminar se enseña el coste real.
- **ReportRunner**: primero actualiza los precios con la descarga de siempre (gratis; el aviso
  de un stop sale en ese momento) y después, en un hilo de trabajo, el informe con Claude
  (services/reports.py). Uno a la vez. No hay «Cancelar» mientras escribe Claude: al cerrar la
  app se corta y no se guarda nada.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from sharky.core.formatting import format_usd, month_name, weekday_name
from sharky.core.models import Report, ReportKind
from sharky.core.reports import (
    ACTION_LABELS,
    NO_AI_LABEL,
    AIAction,
    BudgetCheck,
    usage_text,
)
from sharky.core.schedule import Month, parse_day, weekly_rule_text, weekly_window
from sharky.services import secrets
from sharky.services.ai import ClaudeClient
from sharky.services.db import Database
from sharky.services.market import load_valuation, local_now
from sharky.services.reports import (
    TRIGGER_MANUAL,
    ReportOutcome,
    ReportSchedule,
    budget_check,
    create_daily_report,
    create_monthly_report,
    create_weekly_report,
    daily_for,
    is_partial_study,
    latest_report,
    monthly_for,
    price_for,
    report_schedule,
    weekly_for,
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

#: El tipo de informe de cada acción.
ACTION_KINDS: dict[AIAction, ReportKind] = {
    AIAction.DAILY: ReportKind.DAILY,
    AIAction.WEEKLY: ReportKind.WEEKLY,
    AIAction.MONTHLY: ReportKind.MONTHLY,
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
    """«Control diario», «Noticias semanales — del 21/09 al 27/09», «Estudio mensual — agosto
    2026» (con «(parcial)» si se hizo antes de que acabara el mes)…"""
    titulo = KIND_TITLES[report.kind]
    if report.kind is ReportKind.MONTHLY:
        mes = Month.parse(report.period)
        if mes is None:
            return titulo
        texto = f"{titulo} — {month_name(mes.month)} {mes.year}"
        return f"{texto} (parcial)" if is_partial_study(report) else texto
    if report.kind is ReportKind.WEEKLY:
        hasta = parse_day(report.period)
        if hasta is None:
            return titulo
        desde, _ = weekly_window(hasta)
        return f"{titulo} — del {desde:%d/%m} al {hasta:%d/%m}"
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
class MonthOption:
    """Un mes que se puede estudiar con «Ejecutar ahora»."""

    month: Month
    partial: bool  # el mes en curso: hasta hoy
    existing: Report | None  # el estudio de ese mes que se sustituiría

    @property
    def label(self) -> str:
        if self.partial:
            return f"Mes en curso (parcial): {self.month.label}, hasta hoy"
        return f"Mes anterior: {self.month.label}"


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
    month: int  # el mes del gasto
    #: Mensual: los meses que se pueden estudiar y el que se propone.
    month_options: tuple[MonthOption, ...] = ()
    study: Month | None = None

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

    def option(self, month: Month | None) -> MonthOption | None:
        return next((o for o in self.month_options if o.month == month), None)


def run_report_job(
    action: AIAction,
    month: Month | None,
    db: Database,
    settings: Settings,
    now: Callable[[], datetime],
    market_at: datetime | None,
    key_loader: Callable[[], str | None],
    client_factory: Callable[[str], ClaudeClient],
    cancel: threading.Event,
    progress: Callable[[int, int], None] | None = None,
) -> ReportOutcome:
    """Un informe en un hilo de trabajo, con los precios que acaban de guardarse."""
    valoracion = load_valuation(db.connection(), now(), market_at)
    comun = {"trigger": TRIGGER_MANUAL, "cancel": cancel, "client_factory": client_factory}
    if action is AIAction.WEEKLY:
        return create_weekly_report(db, valoracion, settings, now, key_loader(), **comun)
    if action is AIAction.MONTHLY:
        if month is None:
            raise ValueError("Falta el mes del estudio")
        return create_monthly_report(db, valoracion, settings, now, key_loader(), month,
                                     on_stage=progress, **comun)
    return create_daily_report(db, valoracion, settings, now, key_loader(), **comun)


def run_daily_job(
    db: Database,
    settings: Settings,
    now: Callable[[], datetime],
    market_at: datetime | None,
    key_loader: Callable[[], str | None],
    client_factory: Callable[[str], ClaudeClient],
    cancel: threading.Event,
) -> ReportOutcome:
    """El control diario en un hilo de trabajo (el nombre del H9)."""
    return run_report_job(AIAction.DAILY, None, db, settings, now, market_at, key_loader,
                          client_factory, cancel)


#: Lo que se enseña mientras Claude trabaja.
STAGE_TEXTS: dict[AIAction, str] = {
    AIAction.DAILY: "Claude está escribiendo el control diario…",
    AIAction.WEEKLY: "Claude está buscando noticias en la web…",
    AIAction.MONTHLY: "Claude está escribiendo el estudio (paso 1 de 2)…",
}
EXTRACTION_STAGE = "Claude está extrayendo los veredictos (paso 2 de 2)…"


class ReportRunner(QObject):
    """«Ejecutar ahora» de los informes: precios primero (gratis) y luego Claude, en segundo
    plano. Una sola ejecución a la vez. Las señales llegan al hilo de la interfaz."""

    started = Signal()
    #: Lo que se está haciendo ahora, para enseñarlo junto a la barra.
    stageChanged = Signal(str)
    #: Ha terminado, con su ReportOutcome.
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
        #: La acción en marcha (None si no hay ninguna) y, en el mensual, su mes.
        self.action: AIAction | None = None
        self.month: Month | None = None
        refresher.stopped.connect(self._on_prices_done)

    @property
    def running(self) -> bool:
        return self._waiting_prices or self._worker is not None

    def schedule(self) -> ReportSchedule:
        """Qué toca hoy. Solo lee."""
        return report_schedule(self._db.connection(), self.settings, self._now().date())

    def plan(self, action: AIAction) -> RunPlan:
        """Con qué modelo iría, cuánto costaría, si cabe en el tope y qué sustituiría. Solo
        lee."""
        conn = self._db.connection()
        ahora = self._now()
        ai = self.settings.ai
        ajuste = getattr(ai, action.value)
        opciones: tuple[MonthOption, ...] = ()
        estudio: Month | None = None
        if action is AIAction.MONTHLY:
            agenda = report_schedule(conn, self.settings, ahora.date())
            meses = [(agenda.previous_month, False), (agenda.current_month, True)]
            opciones = tuple(MonthOption(m, parcial, monthly_for(conn, m))
                             for m, parcial in meses if m is not None)
            # Se propone el que toca; si no toca ninguno, el anterior si aún no tiene estudio
            # (el primer mes incompleto, a mano) y, si no, el mes en curso.
            anterior = opciones[0] if opciones and not opciones[0].partial else None
            if agenda.monthly_due is not None:
                estudio = agenda.monthly_due
            elif anterior is not None and anterior.existing is None:
                estudio = anterior.month
            else:
                estudio = agenda.current_month
            existente = monthly_for(conn, estudio)
        elif action is AIAction.WEEKLY:
            existente = weekly_for(conn, ahora.date())
        else:
            existente = daily_for(conn, ahora.date())
        return RunPlan(
            action,
            ajuste.model,
            ajuste.effort,
            self._key_loader() is not None,
            price_for(ai, ajuste.model) is not None,
            budget_check(conn, ai, action, ahora),
            existente,
            ahora.month,
            opciones,
            estudio,
        )

    def plan_daily(self) -> RunPlan:
        return self.plan(AIAction.DAILY)

    def start(self, action: AIAction, month: Month | None = None) -> bool:
        """Empieza: precios y después el informe. False si ya había uno en marcha."""
        if self.running:
            return False
        if action is AIAction.MONTHLY and month is None:
            raise ValueError("El estudio mensual necesita su mes")
        self.action = action
        self.month = month
        self._cancel = threading.Event()
        self.started.emit()
        self.stageChanged.emit("Actualizando precios (gratis)…")
        self._waiting_prices = True
        if not self._refresher.running and not self._refresher.start():
            self._waiting_prices = False
            self._start_report()
        return True

    def start_daily(self) -> bool:
        return self.start(AIAction.DAILY)

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
        accion = self.action or AIAction.DAILY
        self.stageChanged.emit(STAGE_TEXTS[accion])
        trabajo = Worker(
            run_report_job,
            accion,
            self.month,
            self._db,
            self.settings,
            self._now,
            self._refresher.market_at,
            self._key_loader,
            self._client_factory,
            self._cancel or threading.Event(),
        )
        if accion is AIAction.MONTHLY:
            trabajo.pass_progress()
            trabajo.signals.progress.connect(self._on_progress)
        trabajo.signals.finished.connect(self._on_done)
        trabajo.signals.failed.connect(self._on_failed)
        self._worker = trabajo  # sin referencia, las señales se perderían
        start(trabajo)

    def _on_progress(self, step: int, _steps: int) -> None:
        if step >= 2:
            self.stageChanged.emit(EXTRACTION_STAGE)

    def _on_done(self, outcome: ReportOutcome) -> None:
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
        self.action = None
        self.month = None
        self.stopped.emit()

    def shutdown(self) -> None:
        """Al cerrar la app: se corta la respuesta de Claude y no se guarda nada."""
        if self._cancel is not None:
            self._cancel.set()


# -- las tarjetas del Panel -------------------------------------------------------------------


def _day(moment: datetime, today: date) -> str:
    if moment.date() == today:
        return f"hoy, {moment:%H:%M}"
    return f"{moment:%d/%m/%Y, %H:%M}"


def _short_day(day: date, today: date) -> str:
    """«hoy», «mañana» o «domingo 04/10»."""
    if day == today:
        return "hoy"
    if day == today + timedelta(days=1):
        return "mañana"
    return f"{weekday_name(day.weekday())} {day:%d/%m}"


@dataclass(frozen=True)
class CardState:
    """Lo que enseña una tarjeta: el informe que se lee y la etiqueta de estado."""

    report: Report | None  # el que se lee con «Leer»
    current: bool  # es el del periodo de ahora (si no, «Último: …»)
    chip: str
    chip_style: str


class ReportCard(QFrame):
    """Una tarjeta de informe del Panel. Cada tipo dice qué toca y qué enseñar."""

    action: AIAction = AIAction.DAILY
    #: Lo que dice la tarjeta si todavía no hay ningún informe de su tipo.
    empty_when = ""
    empty_text = ""
    #: La ayuda de «Ejecutar ahora».
    run_help = ""
    saved_text = ""
    failed_text = ""

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
        titulo = QLabel(ACTION_LABELS[self.action])
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
        runner.stageChanged.connect(self._on_stage)
        runner.finished.connect(self._on_finished)
        runner.failed.connect(self._on_failed)
        runner.stopped.connect(self.refresh)
        self.refresh()

    # -- estado ------------------------------------------------------------------------

    @property
    def mine(self) -> bool:
        """La ejecución en marcha es de esta tarjeta."""
        return self._runner.action is self.action

    def state(self, schedule: ReportSchedule) -> CardState:  # pragma: no cover
        raise NotImplementedError

    def refresh(self) -> None:
        """Pone al día la tarjeta con lo guardado, lo que toca y el precio del botón."""
        hoy = self._now().date()
        agenda = self._runner.schedule()
        estado = self.state(agenda)
        self.report = estado.report
        self.plan = self._runner.plan(self.action)

        en_marcha = self._runner.running and self.mine
        texto, estilo = ("En marcha", "chipMuted") if en_marcha else (estado.chip,
                                                                     estado.chip_style)
        self.chip_label.setText(texto)
        restyle(self.chip, estilo)

        informe = estado.report
        if informe is None:
            self.when_label.setText(self.empty_when)
            self.when_label.setToolTip("")
            self.conclusion_label.setText(self.empty_text)
            restyle(self.conclusion_label, "muted")
        else:
            prefijo = "" if estado.current else "Último: "
            self.when_label.setText(f"{prefijo}{self._when(informe, hoy)} · "
                                    f"{report_meta(informe)}")
            self.when_label.setToolTip(report_details(informe))
            self.conclusion_label.setText(conclusion_lines(informe.conclusion))
            restyle(self.conclusion_label, "conclusion")
        self.read_button.setEnabled(informe is not None and informe.id is not None)
        self.run_button.setText(f"Ejecutar ahora · {self.plan.price_text}")
        self.run_button.setToolTip(f"{self.run_help} {self.plan.spent_text}")
        self.run_button.setEnabled(not self._runner.running)
        self.progress.setVisible(en_marcha)

    def _when(self, report: Report, today: date) -> str:
        return _day(report.created_at, today)

    # -- acciones ----------------------------------------------------------------------

    def _on_read(self) -> None:
        if self.report is not None and self.report.id is not None:
            self.readRequested.emit(self.report.id)

    def run_now(self) -> None:
        plan = self._runner.plan(self.action)
        respuesta = self.confirm_run(plan)
        if not respuesta:
            return
        mes = respuesta if isinstance(respuesta, Month) else plan.study
        set_state(self.message_label, "", "muted")
        self._runner.start(self.action, mes)

    def _on_started(self) -> None:
        self.run_button.setEnabled(False)
        if not self.mine:
            return
        self.progress.setVisible(True)
        self.chip_label.setText("En marcha")
        restyle(self.chip, "chipMuted")
        set_state(self.message_label, "", "muted")

    def _on_stage(self, text: str) -> None:
        if self.mine:
            self.stage_label.setText(text)

    def _on_finished(self, outcome: ReportOutcome) -> None:
        informe = outcome.report
        if informe.kind is not ACTION_KINDS[self.action]:
            return
        if informe.used_ai:
            uso = usage_text(informe.input_tokens, informe.output_tokens, informe.web_searches)
            texto = f"{self.saved_text} Coste real: {format_usd(informe.cost_usd)} ({uso})."
            texto += self.extra_message(outcome)
            set_state(self.message_label, texto, "okText")
        else:
            texto = f"Guardado sin análisis de IA: {informe.error}"
            if informe.cost_usd > 0:
                texto += f" Coste real de los intentos: {format_usd(informe.cost_usd)}."
            set_state(self.message_label, texto, "warnText")

    def extra_message(self, outcome: ReportOutcome) -> str:
        return ""

    def _on_failed(self, message: str) -> None:
        if self.mine:
            set_state(self.message_label, f"No se ha podido hacer {self.failed_text}: {message}",
                      "dangerText")

    # -- diálogo (los tests lo sustituyen) -----------------------------------------------

    def confirm_run(self, plan: RunPlan) -> bool | Month:
        """True para lanzar (en el mensual, o el mes elegido); False para no hacer nada."""
        caja = QMessageBox(self)
        caja.setIcon(QMessageBox.Icon.Question)
        caja.setWindowTitle(ACTION_LABELS[self.action])
        caja.setText(plan_question(plan))
        caja.setInformativeText("\n\n".join(plan_lines(plan, plan.existing)))
        ejecutar = caja.addButton(plan_button(plan), QMessageBox.ButtonRole.AcceptRole)
        cancelar = caja.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        caja.setDefaultButton(cancelar)
        caja.exec()
        return caja.clickedButton() is ejecutar


#: Qué hace cada acción, para el diálogo de confirmación.
_WHAT: dict[AIAction, str] = {
    AIAction.DAILY: "escribe el análisis del día con las cifras que calcula Sharky.",
    AIAction.WEEKLY: "busca en la web las noticias de la semana de cada posición y cita sus "
                     "fuentes.",
    AIAction.MONTHLY: "escribe el estudio del mes y después se extrae un veredicto por "
                      "posición. Las revisiones y propuestas van a Tesis: no cambia ningún "
                      "número.",
}
_QUESTIONS: dict[AIAction, str] = {
    AIAction.DAILY: "¿Hacer ahora el control diario con Claude?",
    AIAction.WEEKLY: "¿Hacer ahora el semanal de noticias con Claude?",
    AIAction.MONTHLY: "¿Hacer ahora el estudio mensual con Claude?",
}
_NO_AI_QUESTIONS: dict[AIAction, str] = {
    AIAction.DAILY: "El control diario saldrá sin análisis de IA.",
    AIAction.WEEKLY: "El semanal saldrá sin análisis de IA.",
    AIAction.MONTHLY: "El estudio mensual saldrá sin análisis de IA.",
}
_EXISTING: dict[AIAction, str] = {
    AIAction.DAILY: "Ya hay un control de hoy",
    AIAction.WEEKLY: "Ya hay un semanal de hoy",
    AIAction.MONTHLY: "Ya hay un estudio de ese mes",
}


def plan_question(plan: RunPlan) -> str:
    return _QUESTIONS[plan.action] if plan.uses_ai else _NO_AI_QUESTIONS[plan.action]


def plan_button(plan: RunPlan) -> str:
    return f"Ejecutar ({plan.budget.estimate.text})" if plan.uses_ai else "Generar sin IA (gratis)"


def plan_lines(plan: RunPlan, existing: Report | None) -> list[str]:
    """El texto del diálogo de confirmación: qué hará, cuánto cuesta, el gasto del mes y si
    sustituye un informe que ya existe."""
    lineas = []
    if plan.uses_ai:
        lineas.append(
            f"Primero se actualizan los precios (gratis). Después Claude ({plan.model}, esfuerzo "
            f"{plan.effort}) {_WHAT[plan.action]}"
        )
        lineas.append(f"Coste aproximado: {plan.budget.estimate.text}.")
    else:
        lineas.append(
            f"Motivo: {plan.no_ai_reason}. Se actualizan los precios y se guarda la parte que "
            f"calcula Sharky, con la etiqueta «{NO_AI_LABEL}». Gratis."
        )
    lineas.append(plan.spent_text)
    if existing is not None:
        if plan.action is AIAction.MONTHLY:
            lineas.append(f"{_EXISTING[plan.action]} (hecho el {existing.created_at:%d/%m/%Y}): "
                          "se sustituirá.")
        else:
            lineas.append(f"{_EXISTING[plan.action]} ({existing.created_at:%H:%M}): se "
                          "sustituirá.")
    return lineas


class DailyCard(ReportCard):
    """La tarjeta «Control diario» del Panel."""

    action = AIAction.DAILY
    empty_when = "Todavía no hay ningún control diario."
    empty_text = (
        "El control del día con Claude: estado, niveles e incumplimientos, y una conclusión "
        "corta que releerán el semanal y el mensual."
    )
    run_help = "Actualiza los precios (gratis) y pide a Claude el control del día."
    saved_text = "Control diario guardado."
    failed_text = "el control diario"

    def state(self, schedule: ReportSchedule) -> CardState:
        de_hoy = daily_for(self._db.connection(), schedule.today)
        ultimo = de_hoy or latest_report(self._db.connection(), ReportKind.DAILY)
        if de_hoy is None:
            return CardState(ultimo, False, "Pendiente", "chipMuted")
        if de_hoy.used_ai:
            return CardState(de_hoy, True, "Hecho hoy", "chipOk")
        return CardState(de_hoy, True, "Sin IA", "chipWarn")


class WeeklyCard(ReportCard):
    """La tarjeta «Noticias semanales» del Panel."""

    action = AIAction.WEEKLY
    empty_when = "Todavía no hay ningún semanal."
    empty_text = (
        "Las noticias de la semana de cada posición, buscadas en la web con sus fuentes, y una "
        "conclusión que releerá el estudio mensual."
    )
    run_help = ("Actualiza los precios (gratis) y pide a Claude las noticias de la semana, con "
                "búsqueda web.")
    saved_text = "Semanal guardado."
    failed_text = "el semanal"

    def state(self, schedule: ReportSchedule) -> CardState:
        de_hoy = weekly_for(self._db.connection(), schedule.today)
        ultimo = de_hoy or latest_report(self._db.connection(), ReportKind.WEEKLY)
        self.setToolTip(f"Toca {weekly_rule_text(schedule.weekday)}.")
        if de_hoy is not None:
            if de_hoy.used_ai:
                return CardState(de_hoy, True, "Hecho hoy", "chipOk")
            return CardState(de_hoy, True, "Sin IA", "chipWarn")
        if schedule.weekly_due:
            return CardState(ultimo, False, "Toca hoy", "chipWarn")
        if schedule.next_weekly is not None:
            cuando = _short_day(schedule.next_weekly, schedule.today)
            return CardState(ultimo, False, f"Próximo: {cuando}", "chipMuted")
        return CardState(ultimo, False, "Pendiente", "chipMuted")

class MonthlyCard(ReportCard):
    """La tarjeta «Estudio mensual» del Panel."""

    action = AIAction.MONTHLY
    empty_when = "Todavía no hay ningún estudio mensual."
    empty_text = (
        "El estudio del mes con un veredicto por posición; sus propuestas llegan a Tesis para "
        "que decidas tú."
    )
    run_help = ("Actualiza los precios (gratis) y pide a Claude el estudio del mes y un "
                "veredicto por posición.")
    saved_text = "Estudio mensual guardado."
    failed_text = "el estudio mensual"

    def state(self, schedule: ReportSchedule) -> CardState:
        db = self._db.connection()
        ultimo = latest_report(db, ReportKind.MONTHLY)
        anterior = schedule.previous_month
        if schedule.monthly_due is not None:
            return CardState(ultimo, False, f"Toca: {month_name(schedule.monthly_due.month)}",
                             "chipWarn")
        del_anterior = monthly_for(db, anterior) if anterior is not None else None
        if del_anterior is not None and not is_partial_study(del_anterior):
            nombre = month_name(anterior.month)
            if del_anterior.used_ai:
                return CardState(del_anterior, True, f"Hecho: {nombre}", "chipOk")
            return CardState(del_anterior, True, "Sin IA", "chipWarn")
        if schedule.next_monthly is not None:
            return CardState(ultimo, False,
                             f"Próximo: {_short_day(schedule.next_monthly, schedule.today)}",
                             "chipMuted")
        return CardState(ultimo, False, "Pendiente", "chipMuted")

    def _when(self, report: Report, today: date) -> str:
        mes = report_title(report).removeprefix(f"{KIND_TITLES[ReportKind.MONTHLY]} — ")
        return f"{mes}, {_day(report.created_at, today)}"

    def extra_message(self, outcome: ReportOutcome) -> str:
        if outcome.verdicts_unavailable:
            return f" Veredictos no disponibles: {outcome.verdicts_unavailable}"
        if outcome.thesis_events:
            return f" Añadidos a Tesis: {len(outcome.thesis_events)} revisiones y propuestas."
        return ""

    def confirm_run(self, plan: RunPlan) -> bool | Month:
        dialogo = MonthlyRunDialog(plan, self)
        if dialogo.exec() != QDialog.DialogCode.Accepted:
            return False
        return dialogo.chosen


class MonthlyRunDialog(QDialog):
    """La confirmación del estudio mensual: el mes (el anterior o el en curso, parcial), lo que
    hará, lo que cuesta y si sustituye un estudio que ya existe."""

    def __init__(self, plan: RunPlan, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle(ACTION_LABELS[AIAction.MONTHLY])
        self.setMinimumWidth(520)
        caja = QVBoxLayout(self)
        caja.setContentsMargins(20, 18, 20, 18)
        caja.setSpacing(10)
        titulo = QLabel(plan_question(plan))
        titulo.setObjectName("cardTitle")
        titulo.setWordWrap(True)
        caja.addWidget(titulo)
        caja.addWidget(QLabel("¿Qué mes?"))
        self.month_buttons: dict[Month, QRadioButton] = {}
        grupo = QButtonGroup(self)
        for opcion in plan.month_options:
            boton = QRadioButton(opcion.label)
            boton.toggled.connect(self._sync)
            grupo.addButton(boton)
            caja.addWidget(boton)
            self.month_buttons[opcion.month] = boton
        if not any(not o.partial for o in plan.month_options):
            caja.addWidget(muted("El mes anterior no se ofrece: la cartera todavía no existía."))
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        caja.addWidget(self.info_label)
        botones = QHBoxLayout()
        botones.addStretch(1)
        cancelar = QPushButton("Cancelar")
        cancelar.setDefault(True)
        cancelar.clicked.connect(self.reject)
        botones.addWidget(cancelar)
        self.accept_button = QPushButton(plan_button(plan))
        self.accept_button.setObjectName("primary")
        self.accept_button.clicked.connect(self.accept)
        botones.addWidget(self.accept_button)
        caja.addLayout(botones)
        elegido = self.month_buttons.get(plan.study) if plan.study is not None else None
        (elegido or next(iter(self.month_buttons.values()))).setChecked(True)
        self._sync()

    @property
    def chosen(self) -> Month:
        return next(m for m, b in self.month_buttons.items() if b.isChecked())

    def choose(self, month: Month) -> None:
        self.month_buttons[month].setChecked(True)

    def _sync(self, *_args: object) -> None:
        if not any(b.isChecked() for b in self.month_buttons.values()):
            return
        opcion = self.plan.option(self.chosen)
        existente = opcion.existing if opcion is not None else None
        self.info_label.setText("\n\n".join(plan_lines(self.plan, existente)))


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

    def _on_report_saved(self, outcome: ReportOutcome) -> None:
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
