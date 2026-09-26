"""Los informes con Claude (GUIA §5.7): el control diario (H9), las noticias semanales y el
estudio mensual (H10).

El orden importa, y es el de la guía («lo crítico, calculado antes y aparte»):

1. **Primero lo que no necesita IA**: la foto del NAV, la auditoría del mandato y la vigilancia
   de niveles se guardan en su propia transacción, confirmada antes de llamar a Claude. Si
   Claude tarda, falla o no hay clave, los avisos de stop ya están guardados.
2. El contexto y la parte determinista (core/reports.py).
3. Claude, solo si hay clave, el precio del modelo está en Ajustes y el gasto del mes más lo
   que costaría la acción cabe en el tope. Si no, el informe sale igual con la etiqueta
   «Sin análisis de IA: <motivo>».
4. En una transacción: el informe (el de su mismo periodo se sustituye) y su fila en el
   Registro de ejecuciones, con el coste real; en el mensual, también las revisiones y
   propuestas de Claude en las tesis. El gasto del mes es la suma de la columna del Registro.

- **Semanal:** búsqueda web con `max_uses = min(30, 3 × posiciones)`, `pause_turn` y las
  fuentes citadas (services/ai.py). Periodo: el día en que se hace; cubre los 7 días que acaban
  ese día. Sin posiciones, no hay nada que buscar: sale sin llamar a Claude.
- **Mensual:** en dos pasos. A, el estudio (streaming, esfuerzo del ajuste). B, la extracción de
  un veredicto por posición con `messages.parse`; si B falla, el estudio se guarda igual y los
  veredictos quedan «no disponibles». Cada veredicto válido se añade a su tesis como revisión y,
  si cambia el stop o el objetivo, como propuesta sin aplicar: ningún número de la tesis cambia.
  Periodo: el mes («2026-08»); el estudio parcial del mes en curso lo sustituye el completo.

Todo corre en un hilo de trabajo: lee la base de datos y habla con Claude.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from string import Template

from pydantic import BaseModel, Field

from sharky import paths
from sharky.core.formatting import format_usd
from sharky.core.levels import ProposalState, proposal_states
from sharky.core.mandate import MandateRules, audit, day_change, held_since, snapshot_for
from sharky.core.models import (
    Author,
    Price,
    Report,
    ReportKind,
    Run,
    RunStatus,
    Thesis,
    ThesisEvent,
    ThesisEventKind,
    ThesisStatus,
    TradeKind,
)
from sharky.core.reports import (
    ACTION_STEPS,
    AIAction,
    BudgetCheck,
    ComposedReport,
    CostEstimate,
    DailyInputs,
    MonthlyInputs,
    RawVerdict,
    ThesisContext,
    TokenPrice,
    VerdictsOutcome,
    WeeklyInputs,
    call_cost,
    check_verdicts,
    compose_daily,
    compose_monthly,
    compose_weekly,
    daily_prompt_fields,
    estimate_cost,
    extraction_tickers,
    find_price,
    is_forced_since,
    mandate_text,
    month_change,
    monthly_prompt_fields,
    no_ai_text,
    usage_text,
    verdict_events,
    watch_positions,
    weekly_prompt_fields,
)
from sharky.core.schedule import (
    Month,
    daily_due,
    is_complete_study,
    manual_months,
    monthly_due,
    next_monthly,
    next_weekly,
    parse_day,
    weekly_due,
    weekly_window,
)
from sharky.core.valuation import Valuation
from sharky.services.ai import (
    AIError,
    AIErrorKind,
    AIRequest,
    AIResult,
    AIUsage,
    ClaudeClient,
    ExtractRequest,
    web_search_tool,
)
from sharky.services.db import Database
from sharky.services.repositories import (
    AssetRepository,
    BreachRepository,
    CashMovementRepository,
    NavSnapshotRepository,
    PriceRepository,
    RecordedLevels,
    ReportRepository,
    RunRepository,
    ThesisEventRepository,
    ThesisRepository,
    TradeRepository,
    portfolio_start,
    record_levels,
    record_valuation,
)
from sharky.services.settings import ActionAI, AISettings, Settings

log = logging.getLogger(__name__)

#: `max_tokens` de cada informe (GUIA §5.7): el razonamiento gasta del mismo margen que el texto.
DAILY_MAX_TOKENS = 16_000
WEEKLY_MAX_TOKENS = 32_000
MONTHLY_MAX_TOKENS = 48_000
#: El paso B del mensual: una llamada corta, sin razonamiento.
EXTRACTION_MAX_TOKENS = 8_000

#: Quién lanzó una acción (`runs.triggered_by`).
TRIGGER_MANUAL = "manual"

NO_KEY_REASON = "no hay clave de Claude (se añade en Ajustes → Claude)."
NO_POSITIONS_WEEKLY = "no hay posiciones: no hay noticias que buscar."
NO_POSITIONS_MONTHLY = "no hay posiciones que estudiar."


# -- prompts --------------------------------------------------------------------------------


def prompt_template(name: str) -> Template:
    """Un prompt de `resources/prompts/` con `string.Template` ($variable), para no chocar con
    las llaves del Markdown."""
    ruta = paths.resource_path("prompts", f"{name}.md")
    return Template(ruta.read_text(encoding="utf-8"))


def render_prompt(name: str, fields: dict[str, str]) -> str:
    """El prompt con sus campos. Falta un campo → KeyError: es un error de programación."""
    return prompt_template(name).substitute(fields).strip()


def system_prompt(rules: MandateRules) -> str:
    """El prompt de sistema con el mandato vigente, generado desde los ajustes."""
    return render_prompt("sistema", {"mandato": mandate_text(rules)})


# -- coste y tope ---------------------------------------------------------------------------


def _decimal(value: float) -> Decimal:
    return Decimal(repr(value))


def token_prices(ai: AISettings) -> dict[str, TokenPrice]:
    return {
        modelo: TokenPrice(_decimal(p.input_per_mtok), _decimal(p.output_per_mtok))
        for modelo, p in ai.prices.items()
    }


def price_for(ai: AISettings, model: str) -> TokenPrice | None:
    return find_price(token_prices(ai), model)


def cost_of(ai: AISettings, model: str, usage: AIUsage) -> Decimal:
    """Lo que ha costado `usage` con los precios de Ajustes (0 si el modelo no tiene precio)."""
    precio = price_for(ai, model)
    if precio is None:
        return Decimal("0")
    return call_cost(usage.input_tokens, usage.output_tokens, usage.web_searches, precio,
                     _decimal(ai.web_search_per_1000_usd))


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month_start(now: datetime) -> datetime:
    inicio = month_start(now)
    return inicio.replace(year=inicio.year + 1, month=1) if inicio.month == 12 else (
        inicio.replace(month=inicio.month + 1)
    )


def spent_this_month(conn: sqlite3.Connection, now: datetime) -> Decimal:
    """El gasto en Claude del mes en curso, del Registro de ejecuciones."""
    return RunRepository(conn).spent_between(month_start(now), next_month_start(now))


def action_estimate(conn: sqlite3.Connection, action: AIAction) -> CostEstimate:
    """La media de las tres últimas ejecuciones con Claude de la acción, o la tabla."""
    recientes = [
        r.cost_usd
        for r in RunRepository(conn).list_for_step(ACTION_STEPS[action])
        if r.status is RunStatus.OK and r.cost_usd > 0
    ]
    return estimate_cost(action, recientes)


def budget_check(
    conn: sqlite3.Connection, ai: AISettings, action: AIAction, now: datetime
) -> BudgetCheck:
    return BudgetCheck(
        spent_this_month(conn, now), _decimal(ai.monthly_budget_usd), action_estimate(conn, action)
    )


def action_settings(ai: AISettings, action: AIAction) -> ActionAI:
    return getattr(ai, action.value)


# -- lo guardado ----------------------------------------------------------------------------


def latest_report(conn: sqlite3.Connection, kind: ReportKind) -> Report | None:
    return ReportRepository(conn).latest(kind)


def daily_for(conn: sqlite3.Connection, day: date) -> Report | None:
    """El control diario de ese día, si lo hay."""
    return ReportRepository(conn).for_period(ReportKind.DAILY, day.isoformat())


def weekly_for(conn: sqlite3.Connection, day: date) -> Report | None:
    """El semanal hecho ese día, si lo hay."""
    return ReportRepository(conn).for_period(ReportKind.WEEKLY, day.isoformat())


def monthly_for(conn: sqlite3.Connection, month: Month) -> Report | None:
    """El estudio de ese mes (completo o parcial), si lo hay."""
    return ReportRepository(conn).for_period(ReportKind.MONTHLY, month.key)


def _period_day(report: Report) -> date | None:
    return parse_day(report.period)


def last_report_day(conn: sqlite3.Connection, kind: ReportKind) -> date | None:
    """El periodo del último diario o semanal, como fecha."""
    dias = [d for d in map(_period_day, ReportRepository(conn).list_kind(kind)) if d]
    return max(dias, default=None)


def completed_months(conn: sqlite3.Connection) -> set[Month]:
    """Los meses con un estudio completo (hecho cuando el mes ya había terminado)."""
    hechos = set()
    for informe in ReportRepository(conn).list_kind(ReportKind.MONTHLY):
        mes = Month.parse(informe.period)
        if mes is not None and is_complete_study(mes, informe.created_at.date()):
            hechos.add(mes)
    return hechos


def is_partial_study(report: Report) -> bool:
    """Un estudio mensual hecho antes de que terminara su mes."""
    mes = Month.parse(report.period)
    return mes is not None and not is_complete_study(mes, report.created_at.date())


@dataclass(frozen=True)
class ReportSchedule:
    """Qué toca hoy (core/schedule.py con lo guardado): para las tarjetas del Panel y la rutina
    (H12)."""

    today: date
    weekday: int  # el día elegido para el semanal (0 = lunes … 6 = domingo)
    daily_due: bool
    weekly_due: bool
    next_weekly: date | None
    monthly_due: Month | None
    next_monthly: date | None
    previous_month: Month | None  # lo que ofrece «Ejecutar ahora» del mensual
    current_month: Month


def report_schedule(conn: sqlite3.Connection, settings: Settings, today: date) -> ReportSchedule:
    inicio = portfolio_start(conn)
    dia = settings.automation.weekly_report_weekday
    ultimo_semanal = last_report_day(conn, ReportKind.WEEKLY)
    anterior, actual = manual_months(today, inicio)
    return ReportSchedule(
        today=today,
        weekday=dia,
        daily_due=daily_due(today, last_report_day(conn, ReportKind.DAILY)),
        weekly_due=weekly_due(today, dia, ultimo_semanal, inicio),
        next_weekly=next_weekly(today, dia, ultimo_semanal, inicio),
        monthly_due=monthly_due(today, inicio, completed_months(conn)),
        next_monthly=next_monthly(today, inicio),
        previous_month=anterior,
        current_month=actual,
    )


# -- el contexto -----------------------------------------------------------------------------


def _last_daily_before(conn: sqlite3.Connection, day: date) -> date | None:
    dias = [d for d in map(_period_day, ReportRepository(conn).list_kind(ReportKind.DAILY))
            if d and d < day]
    return max(dias, default=None)


def _reference_close(
    history: list[Price], current: Price, last_daily: date | None
) -> Price | None:
    """El cierre con el que se compara «desde el último control»: el último guardado antes del
    de ahora y no posterior al día del último control (sin control anterior, el cierre de
    antes)."""
    candidatos = [
        p for p in history
        if p.price_date < current.price_date and (last_daily is None or p.price_date <= last_daily)
    ]
    return max(candidatos, key=lambda p: p.price_date) if candidatos else None


def gather_daily_inputs(
    conn: sqlite3.Connection,
    valuation: Valuation,
    rules: MandateRules,
    today: date,
    levels: RecordedLevels,
) -> DailyInputs:
    """El contexto del día con lo guardado (después de guardar la foto del día)."""
    fotos = NavSnapshotRepository(conn).list_all()
    movimientos = CashMovementRepository(conn).list_all()
    foto = snapshot_for(today, valuation, fotos, movimientos)
    ultimo = _last_daily_before(conn, today)
    precios = PriceRepository(conn)
    referencias: dict[str, Price] = {}
    for p in valuation.positions:
        if p.price is None:
            continue
        referencia = _reference_close(precios.list_for(p.ticker), p.price, ultimo)
        if referencia is not None:
            referencias[p.ticker] = referencia
    return DailyInputs(
        today=today,
        valuation=valuation,
        snapshot=foto,
        held_since=held_since(foto, fotos),
        change=day_change(foto, fotos, movimientos),
        rules=rules,
        findings=audit(valuation, foto.state, rules),
        open_breaches={(b.rule, b.subject): b for b in BreachRepository(conn).list_open()},
        levels=levels.checks,
        with_thesis=frozenset(t.ticker for t in ThesisRepository(conn).list_active()),
        references=referencias,
        last_daily=ultimo,
        forced=tuple(
            t for t in TradeRepository(conn).list_all() if is_forced_since(t, ultimo)
        ),
    )


def gather_weekly_inputs(conn: sqlite3.Connection, day: DailyInputs) -> WeeklyInputs:
    """El contexto del semanal: el del día, más la semana (los 7 días que acaban hoy)."""
    desde, hasta = weekly_window(day.today)
    precios = PriceRepository(conn)
    referencias: dict[str, Price] = {}
    for p in day.valuation.positions:
        if p.price is None:
            continue
        # El cierre de hace una semana: el último anterior al primer día de la semana.
        anteriores = [c for c in precios.list_for(p.ticker)
                      if c.price_date < desde and c.price_date < p.price.price_date]
        if anteriores:
            referencias[p.ticker] = max(anteriores, key=lambda c: c.price_date)
    return WeeklyInputs(
        day=day,
        since=desde,
        until=hasta,
        assets={a.ticker: a for a in AssetRepository(conn).list_all()},
        references=referencias,
        dailies=tuple(ReportRepository(conn).list_between(
            ReportKind.DAILY, desde.isoformat(), hasta.isoformat()
        )),
    )


def _thesis_contexts(conn: sqlite3.Connection) -> tuple[ThesisContext, ...]:
    eventos = ThesisEventRepository(conn)
    contextos = []
    for tesis in ThesisRepository(conn).list_active():
        if tesis.id is None:
            continue
        historial = eventos.list_for(tesis.id)
        revisiones = [e for e in historial if e.kind is ThesisEventKind.REVIEW
                      and e.author is Author.CLAUDE and e.ref_event_id is None]
        estados = proposal_states(historial)
        pendientes = tuple(e for e in historial
                           if e.id is not None and estados.get(e.id) is ProposalState.PENDING)
        contextos.append(ThesisContext(tesis, revisiones[-1] if revisiones else None, pendientes))
    return tuple(contextos)


def gather_monthly_inputs(
    conn: sqlite3.Connection, day: DailyInputs, month: Month
) -> MonthlyInputs:
    """El contexto del estudio de `month`: el del día y lo del mes, hasta hoy si es el mes en
    curso (parcial)."""
    parcial = not is_complete_study(month, day.today)
    hasta = min(month.last_day, day.today)
    primero, ultimo = month.first_day.isoformat(), hasta.isoformat()
    informes = ReportRepository(conn)
    return MonthlyInputs(
        day=day,
        month=month,
        partial=parcial,
        theses=_thesis_contexts(conn),
        trades=tuple(
            t for t in TradeRepository(conn).list_all()
            if t.kind is not TradeKind.OPENING and month.first_day <= t.trade_date <= hasta
        ),
        dailies=tuple(informes.list_between(ReportKind.DAILY, primero, ultimo)),
        weeklies=tuple(informes.list_between(ReportKind.WEEKLY, primero, ultimo)),
        previous=monthly_for(conn, month.previous),
        change=month_change(NavSnapshotRepository(conn).list_all(), month, day.today),
    )


# -- el resultado ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportOutcome:
    """Lo que ha dejado un informe."""

    report: Report
    run: Run
    levels: RecordedLevels
    estimate: CostEstimate
    ai_error: AIErrorKind | None = None
    #: Mensual: los eventos añadidos a las tesis (revisiones y propuestas de Claude).
    thesis_events: tuple[int, ...] = ()
    #: Mensual: por qué no hay veredictos, si no los hay.
    verdicts_unavailable: str | None = None

    @property
    def cost_usd(self) -> Decimal:
        return self.report.cost_usd


ClientFactory = Callable[[str], ClaudeClient]
StageCallback = Callable[[int, int], None]


@dataclass
class _Claude:
    """Cómo ha ido Claude en un informe."""

    result: AIResult | None = None
    reason: str | None = None  # por qué no hay análisis
    usage: AIUsage = field(default_factory=AIUsage)
    error: AIErrorKind | None = None
    called: bool = False


def _blocker(
    ai: AISettings, action: ActionAI, api_key: str | None, budget: BudgetCheck,
    label: str,
) -> str | None:
    """Por qué no se llama a Claude (None si se puede)."""
    if not api_key:
        return NO_KEY_REASON
    if price_for(ai, action.model) is None:
        return f"falta el precio de {action.model} en Ajustes → Claude."
    if not budget.allowed:
        log.info("%s sin IA: %s", label, budget.reason)
        return budget.reason + "."
    return None


def _generate(client: ClaudeClient, request: AIRequest, cancel: threading.Event | None,
              label: str) -> _Claude:
    """Una llamada de texto a Claude. Si se cancela (al cerrar la app), no se guarda nada."""
    estado = _Claude(called=True)
    try:
        estado.result = client.generate(request, cancel)
        estado.usage = estado.result.usage
    except AIError as error:
        if error.kind is AIErrorKind.CANCELLED:
            log.info("%s cancelado: no se guarda", label)
            raise
        estado.reason, estado.usage, estado.error = error.message, error.usage, error.kind
        log.warning("%s sin IA: Claude ha fallado (%s)", label, error.kind)
    return estado


def _incomplete(result: AIResult | None) -> str | None:
    """Por qué el texto de Claude puede estar a medias."""
    if result is None:
        return None
    if result.truncated:
        return "El análisis de Claude se cortó por longitud."
    if result.paused:
        return ("La búsqueda de Claude seguía en pausa tras los reenvíos: el texto puede estar "
                "a medias.")
    return None


def _record_critical(db: Database, valuation: Valuation, rules: MandateRules,
                     now: datetime) -> RecordedLevels:
    """Lo crítico, antes y aparte: confirmado antes de llamar a Claude."""
    with db.transaction() as tx:
        record_valuation(tx, valuation, rules, now)
        return record_levels(tx, valuation, now)


def _report(kind: ReportKind, period: str, created: datetime, composed: ComposedReport,
            claude: _Claude, action: ActionAI, cost: Decimal, **extra: object) -> Report:
    resultado = claude.result
    return Report(
        kind=kind,
        period=period,
        created_at=created,
        markdown=composed.markdown,
        used_ai=resultado is not None,
        conclusion=composed.conclusion,
        model=(resultado.model if resultado else action.model) if claude.called else None,
        effort=(resultado.effort_text if resultado else action.effort) if claude.called else None,
        input_tokens=claude.usage.input_tokens,
        output_tokens=claude.usage.output_tokens,
        web_searches=claude.usage.web_searches,
        cost_usd=cost,
        error=claude.reason,
        **extra,
    )


def _run(action: AIAction, started: datetime, trigger: str, claude: _Claude, report: Report,
         finished: datetime, notes: Sequence[str] = ()) -> Run:
    return Run(
        started_at=started,
        triggered_by=trigger,
        step=ACTION_STEPS[action],
        status=RunStatus.ERROR if claude.error is not None else RunStatus.OK,
        detail=_run_detail(report, claude.reason, action, notes),
        finished_at=finished,
        cost_usd=report.cost_usd,
    )


# -- el control diario ----------------------------------------------------------------------


def create_daily_report(
    db: Database,
    valuation: Valuation,
    settings: Settings,
    now: Callable[[], datetime],
    api_key: str | None,
    *,
    trigger: str = TRIGGER_MANUAL,
    cancel: threading.Event | None = None,
    client_factory: ClientFactory = ClaudeClient,
) -> ReportOutcome:
    """El control diario de hoy (GUIA §5.7), con Claude si se puede. Siempre lo guarda, salvo
    si se cancela (al cerrar la app), que no guarda nada. Corre en un hilo de trabajo."""
    inicio = now()
    hoy = inicio.date()
    reglas = settings.mandate.rules()
    ai = settings.ai
    accion = action_settings(ai, AIAction.DAILY)

    # 1. Lo crítico, antes y aparte.
    niveles = _record_critical(db, valuation, reglas, inicio)
    conn = db.connection()

    # 2. El contexto y la parte determinista.
    datos = gather_daily_inputs(conn, valuation, reglas, hoy, niveles)
    tope = budget_check(conn, ai, AIAction.DAILY, inicio)

    # 3. Claude, si se puede.
    claude = _Claude(reason=_blocker(ai, accion, api_key, tope, "Control diario"))
    if claude.reason is None and api_key:
        peticion = AIRequest(
            model=accion.model,
            effort=accion.effort,
            max_tokens=DAILY_MAX_TOKENS,
            system=system_prompt(reglas),
            prompt=render_prompt("diario", daily_prompt_fields(datos)),
        )
        cliente = client_factory(api_key)
        try:
            claude = _generate(cliente, peticion, cancel, "Control diario")
        finally:
            cliente.close()

    # 4. El informe y su fila en el Registro, juntos.
    coste = cost_of(ai, accion.model, claude.usage) if claude.called else Decimal("0")
    compuesto = compose_daily(
        datos,
        claude.result.text if claude.result else None,
        claude.reason,
        truncated=bool(claude.result and claude.result.truncated),
    )
    informe = _report(ReportKind.DAILY, hoy.isoformat(), inicio, compuesto, claude, accion,
                      coste, watch_positions=watch_positions(datos))
    ejecucion = _run(AIAction.DAILY, inicio, trigger, claude, informe, now())
    with db.transaction() as tx:
        id_informe = ReportRepository(tx).save(informe)
        id_ejecucion = RunRepository(tx).add(ejecucion)
    log.info("Control diario del %s guardado (%s)", hoy, ejecucion.detail)
    return ReportOutcome(
        replace(informe, id=id_informe),
        replace(ejecucion, id=id_ejecucion),
        niveles,
        tope.estimate,
        claude.error,
    )


# -- el semanal de noticias -------------------------------------------------------------------


def create_weekly_report(
    db: Database,
    valuation: Valuation,
    settings: Settings,
    now: Callable[[], datetime],
    api_key: str | None,
    *,
    trigger: str = TRIGGER_MANUAL,
    cancel: threading.Event | None = None,
    client_factory: ClientFactory = ClaudeClient,
) -> ReportOutcome:
    """Las noticias semanales (GUIA §5.7) de los 7 días que acaban hoy, con la búsqueda web de
    Claude si se puede. Siempre lo guarda, salvo si se cancela. Corre en un hilo de trabajo."""
    inicio = now()
    hoy = inicio.date()
    reglas = settings.mandate.rules()
    ai = settings.ai
    accion = action_settings(ai, AIAction.WEEKLY)

    niveles = _record_critical(db, valuation, reglas, inicio)
    conn = db.connection()
    datos = gather_weekly_inputs(conn, gather_daily_inputs(conn, valuation, reglas, hoy, niveles))
    tope = budget_check(conn, ai, AIAction.WEEKLY, inicio)

    posiciones = len(valuation.positions)
    motivo = NO_POSITIONS_WEEKLY if posiciones == 0 else None
    claude = _Claude(reason=motivo or _blocker(ai, accion, api_key, tope, "Semanal"))
    if claude.reason is None and api_key:
        peticion = AIRequest(
            model=accion.model,
            effort=accion.effort,
            max_tokens=WEEKLY_MAX_TOKENS,
            system=system_prompt(reglas),
            prompt=render_prompt("semanal", weekly_prompt_fields(datos)),
            tools=(web_search_tool(posiciones),),
        )
        cliente = client_factory(api_key)
        try:
            claude = _generate(cliente, peticion, cancel, "Semanal")
        finally:
            cliente.close()

    coste = cost_of(ai, accion.model, claude.usage) if claude.called else Decimal("0")
    resultado = claude.result
    compuesto = compose_weekly(
        datos,
        resultado.text if resultado else None,
        [(s.url, s.title) for s in resultado.sources] if resultado else (),
        claude.reason,
        _incomplete(resultado),
    )
    informe = _report(ReportKind.WEEKLY, hoy.isoformat(), inicio, compuesto, claude, accion,
                      coste)
    notas = []
    if resultado is not None:
        notas.append(f"{len(resultado.sources)} fuentes")
        if resultado.continuations:
            notas.append(f"{resultado.continuations} reenvíos por pausa")
    ejecucion = _run(AIAction.WEEKLY, inicio, trigger, claude, informe, now(), notas)
    with db.transaction() as tx:
        id_informe = ReportRepository(tx).save(informe)
        id_ejecucion = RunRepository(tx).add(ejecucion)
    log.info("Semanal del %s guardado (%s)", hoy, ejecucion.detail)
    return ReportOutcome(
        replace(informe, id=id_informe),
        replace(ejecucion, id=id_ejecucion),
        niveles,
        tope.estimate,
        claude.error,
    )


# -- el estudio mensual -----------------------------------------------------------------------


class ExtractedVerdict(BaseModel):
    """Un veredicto del estudio (el esquema del paso B)."""

    ticker: str = Field(description="El ticker de la posición, tal cual aparece en la lista.")
    verdict: str = Field(description="El veredicto: MANTENER, REDUCIR, AMPLIAR o CERRAR.")
    reason: str = Field(description="El motivo, en 1 o 2 frases.")
    invalidation: str = Field(description="Qué invalidaría el veredicto.")
    proposed_stop: float | None = Field(
        description="El stop que propone el estudio, en la divisa de los niveles de la tesis; "
                    "null si el estudio no propone una cifra."
    )
    proposed_target: float | None = Field(
        description="El objetivo que propone el estudio, en la divisa de los niveles de la "
                    "tesis; null si el estudio no propone una cifra."
    )


class MonthlyVerdicts(BaseModel):
    """Los veredictos del estudio mensual, uno por ticker de la lista."""

    verdicts: list[ExtractedVerdict] = Field(description="Un veredicto por ticker de la lista.")


def _number(value: float | None) -> Decimal | None:
    if value is None or not math.isfinite(value):
        return None
    return Decimal(repr(value))


def raw_verdicts(extracted: MonthlyVerdicts) -> list[RawVerdict]:
    return [
        RawVerdict(v.ticker, v.verdict, v.reason, v.invalidation, _number(v.proposed_stop),
                   _number(v.proposed_target))
        for v in extracted.verdicts
    ]


def create_monthly_report(
    db: Database,
    valuation: Valuation,
    settings: Settings,
    now: Callable[[], datetime],
    api_key: str | None,
    month: Month,
    *,
    trigger: str = TRIGGER_MANUAL,
    cancel: threading.Event | None = None,
    client_factory: ClientFactory = ClaudeClient,
    on_stage: StageCallback | None = None,
) -> ReportOutcome:
    """El estudio mensual de `month` (GUIA §5.7), en dos pasos: A, el estudio; B, un veredicto
    por posición. Los veredictos válidos se añaden a sus tesis como revisiones (y propuestas sin
    aplicar), en la misma transacción que el informe. Si B falla, el estudio se guarda igual.
    Corre en un hilo de trabajo; `on_stage(paso, pasos)` avisa de cada paso."""
    inicio = now()
    hoy = inicio.date()
    reglas = settings.mandate.rules()
    ai = settings.ai
    accion = action_settings(ai, AIAction.MONTHLY)

    niveles = _record_critical(db, valuation, reglas, inicio)
    conn = db.connection()
    datos = gather_monthly_inputs(conn, gather_daily_inputs(conn, valuation, reglas, hoy,
                                                            niveles), month)
    tope = budget_check(conn, ai, AIAction.MONTHLY, inicio)
    tesis = {c.thesis.ticker: c.thesis for c in datos.theses}
    tickers = [p.ticker for p in valuation.positions]

    motivo = NO_POSITIONS_MONTHLY if not tickers else None
    claude = _Claude(reason=motivo or _blocker(ai, accion, api_key, tope, "Estudio mensual"))
    veredictos = VerdictsOutcome(unavailable="no hay análisis de Claude.")
    uso_b = AIUsage()
    sin_medir = False
    if claude.reason is None and api_key:
        cliente = client_factory(api_key)
        try:
            if on_stage is not None:
                on_stage(1, 2)
            claude = _generate(cliente, AIRequest(
                model=accion.model,
                effort=accion.effort,
                max_tokens=MONTHLY_MAX_TOKENS,
                system=system_prompt(reglas),
                prompt=render_prompt("mensual", monthly_prompt_fields(datos)),
            ), cancel, "Estudio mensual")
            if claude.result is not None:
                if on_stage is not None:
                    on_stage(2, 2)
                veredictos, uso_b, sin_medir = _extract_verdicts(
                    cliente, accion, claude.result.text, datos, tickers, tesis, inicio, cancel
                )
        finally:
            cliente.close()

    uso = claude.usage + uso_b
    coste = cost_of(ai, accion.model, uso) if claude.called else Decimal("0")
    claude.usage = uso
    compuesto = compose_monthly(
        datos,
        claude.result.text if claude.result else None,
        veredictos,
        claude.reason,
        _incomplete(claude.result),
    )
    informe = _report(ReportKind.MONTHLY, month.key, inicio, compuesto, claude, accion, coste)
    notas = _verdict_notes(veredictos, len(tickers), sin_medir) if claude.result else []
    ejecucion = _run(AIAction.MONTHLY, inicio, trigger, claude, informe, now(), notas)
    ids_eventos: list[int] = []
    with db.transaction() as tx:
        id_informe = ReportRepository(tx).save(informe)
        id_ejecucion = RunRepository(tx).add(ejecucion)
        ids_eventos = _add_thesis_events(tx, veredictos.events)
    log.info("Estudio mensual de %s guardado (%s)", month.key, ejecucion.detail)
    return ReportOutcome(
        replace(informe, id=id_informe),
        replace(ejecucion, id=id_ejecucion),
        niveles,
        tope.estimate,
        claude.error,
        tuple(ids_eventos),
        veredictos.unavailable,
    )


def _extract_verdicts(
    client: ClaudeClient,
    action: ActionAI,
    study: str,
    inputs: MonthlyInputs,
    tickers: Sequence[str],
    theses: Mapping[str, Thesis],
    now: datetime,
    cancel: threading.Event | None,
) -> tuple[VerdictsOutcome, AIUsage, bool]:
    """El paso B: los veredictos del estudio, validados, y lo que añadirán a cada tesis. Si
    falla, el motivo (los veredictos quedan «no disponibles»)."""
    peticion = ExtractRequest(
        model=action.model,
        max_tokens=EXTRACTION_MAX_TOKENS,
        prompt=render_prompt("mensual_extraccion", {
            "tickers": extraction_tickers(inputs),
            "estudio": study.strip(),
        }),
    )
    try:
        extraido = client.extract(peticion, MonthlyVerdicts, cancel)
    except AIError as error:
        if error.kind is AIErrorKind.CANCELLED:
            raise
        log.warning("Estudio mensual: veredictos no disponibles (%s)", error.kind)
        return VerdictsOutcome(unavailable=error.message), error.usage, error.unmeasured
    comprobados = check_verdicts(raw_verdicts(extraido.value), tickers, theses)
    eventos: dict[str, tuple[ThesisEvent, ...]] = {}
    for v in comprobados.verdicts:
        t = inputs.thesis_for(v.ticker)
        if t is not None:
            eventos[v.ticker] = verdict_events(v, t, now, inputs.title, inputs.month.key)
    if comprobados.rejected:
        log.info("Estudio mensual: %d veredictos descartados", len(comprobados.rejected))
    return VerdictsOutcome(comprobados, None, eventos), extraido.usage, False


def _add_thesis_events(
    conn: sqlite3.Connection, events: Mapping[str, tuple[ThesisEvent, ...]]
) -> list[int]:
    """Añade al historial las revisiones y propuestas de Claude de las tesis que siguen
    activas. Nunca cambia la tesis: solo añade eventos."""
    repositorio = ThesisEventRepository(conn)
    tesis = ThesisRepository(conn)
    ids = []
    for eventos in events.values():
        for evento in eventos:
            actual = tesis.get(evento.thesis_id)
            if actual is None or actual.status is not ThesisStatus.ACTIVE:
                continue
            ids.append(repositorio.add(evento))
    return ids


def _verdict_notes(outcome: VerdictsOutcome, positions: int, unmeasured: bool) -> list[str]:
    notas = []
    if outcome.check is not None:
        notas.append(f"veredictos: {len(outcome.check.verdicts)} de {positions}")
        propuestas = sum(1 for es in outcome.events.values() for e in es
                         if e.kind is ThesisEventKind.PROPOSAL)
        if propuestas:
            notas.append(f"{propuestas} propuestas en Tesis")
    elif outcome.unavailable:
        notas.append(f"veredictos no disponibles: {outcome.unavailable}")
    if unmeasured:
        notas.append("coste del paso B sin medir")
    return notas


# -- el Registro -----------------------------------------------------------------------------


_SAVED_TEXT = {
    AIAction.DAILY: "Control diario guardado.",
    AIAction.WEEKLY: "Semanal guardado.",
    AIAction.MONTHLY: "Estudio mensual guardado.",
    AIAction.EXPLORER: "Exploración guardada.",
}


def _run_detail(report: Report, reason: str | None, action: AIAction,
                notes: Sequence[str] = ()) -> str:
    """La línea del Registro: modelo, esfuerzo, tokens y coste, o por qué no hubo IA."""
    partes = []
    if reason:
        partes.append(no_ai_text(reason))
    if report.model:
        partes.append(f"{report.model} · esfuerzo {report.effort}")
    if report.input_tokens or report.output_tokens:
        partes.append(usage_text(report.input_tokens, report.output_tokens, report.web_searches))
    if report.cost_usd > 0:
        partes.append(format_usd(report.cost_usd))
    partes.extend(notes)
    return " · ".join(partes) or _SAVED_TEXT.get(action, "Informe guardado.")
