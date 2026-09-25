"""Los informes con Claude (GUIA §5.7). En el H9, el control diario.

El orden importa, y es el de la guía («lo crítico, calculado antes y aparte»):

1. **Primero lo que no necesita IA**: la foto del NAV, la auditoría del mandato y la vigilancia
   de niveles se guardan en su propia transacción, confirmada antes de llamar a Claude. Si
   Claude tarda, falla o no hay clave, los avisos de stop ya están guardados.
2. El contexto del día y la parte determinista (core/reports.py).
3. Claude, solo si hay clave, el precio del modelo está en Ajustes y el gasto del mes más lo
   que costaría la acción cabe en el tope. Si no, el informe sale igual con la etiqueta
   «Sin análisis de IA: <motivo>».
4. En una transacción: el informe (el de hoy sustituye al que hubiera) y su fila en el
   Registro de ejecuciones, con el coste real. El gasto del mes es la suma de esa columna.

Todo corre en un hilo de trabajo: lee la base de datos y habla con Claude.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from string import Template

from sharky import paths
from sharky.core.formatting import format_usd
from sharky.core.mandate import MandateRules, audit, day_change, held_since, snapshot_for
from sharky.core.models import Price, Report, ReportKind, Run, RunStatus
from sharky.core.reports import (
    ACTION_STEPS,
    AIAction,
    BudgetCheck,
    CostEstimate,
    DailyInputs,
    TokenPrice,
    call_cost,
    compose_daily,
    daily_prompt_fields,
    estimate_cost,
    find_price,
    is_forced_since,
    mandate_text,
    no_ai_text,
    usage_text,
    watch_positions,
)
from sharky.core.valuation import Valuation
from sharky.services.ai import AIError, AIErrorKind, AIRequest, AIResult, AIUsage, ClaudeClient
from sharky.services.db import Database
from sharky.services.repositories import (
    BreachRepository,
    CashMovementRepository,
    NavSnapshotRepository,
    PriceRepository,
    RecordedLevels,
    ReportRepository,
    RunRepository,
    ThesisRepository,
    TradeRepository,
    record_levels,
    record_valuation,
)
from sharky.services.settings import ActionAI, AISettings, Settings

log = logging.getLogger(__name__)

#: `max_tokens` del diario (GUIA §5.7): el razonamiento gasta del mismo margen que el texto.
DAILY_MAX_TOKENS = 16_000

#: Quién lanzó una acción (`runs.triggered_by`).
TRIGGER_MANUAL = "manual"

NO_KEY_REASON = "no hay clave de Claude (se añade en Ajustes → Claude)."


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


# -- el control diario ----------------------------------------------------------------------


def latest_report(conn: sqlite3.Connection, kind: ReportKind) -> Report | None:
    return ReportRepository(conn).latest(kind)


def daily_for(conn: sqlite3.Connection, day: date) -> Report | None:
    """El control diario de ese día, si lo hay."""
    fila = conn.execute(
        "SELECT id FROM reports WHERE kind = ? AND period = ?",
        (ReportKind.DAILY.value, day.isoformat()),
    ).fetchone()
    return ReportRepository(conn).get(fila["id"]) if fila is not None else None


def _last_daily_before(conn: sqlite3.Connection, day: date) -> date | None:
    fila = conn.execute(
        "SELECT MAX(period) AS periodo FROM reports WHERE kind = ? AND period < ?",
        (ReportKind.DAILY.value, day.isoformat()),
    ).fetchone()
    return date.fromisoformat(fila["periodo"]) if fila and fila["periodo"] else None


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
    """El contexto del control diario con lo guardado (después de guardar la foto del día)."""
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


@dataclass(frozen=True)
class DailyOutcome:
    """Lo que ha dejado el control diario."""

    report: Report
    run: Run
    levels: RecordedLevels
    estimate: CostEstimate
    ai_error: AIErrorKind | None = None

    @property
    def cost_usd(self) -> Decimal:
        return self.report.cost_usd


ClientFactory = Callable[[str], ClaudeClient]


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
) -> DailyOutcome:
    """El control diario de hoy (GUIA §5.7), con Claude si se puede. Siempre lo guarda, salvo
    si se cancela (al cerrar la app), que no guarda nada. Corre en un hilo de trabajo."""
    inicio = now()
    hoy = inicio.date()
    reglas = settings.mandate.rules()
    ai = settings.ai
    accion = action_settings(ai, AIAction.DAILY)

    # 1. Lo crítico, antes y aparte: confirmado antes de llamar a Claude.
    with db.transaction() as tx:
        record_valuation(tx, valuation, reglas, inicio)
        niveles = record_levels(tx, valuation, inicio)
    conn = db.connection()

    # 2. El contexto y la parte determinista.
    datos = gather_daily_inputs(conn, valuation, reglas, hoy, niveles)
    tope = budget_check(conn, ai, AIAction.DAILY, inicio)

    # 3. Claude, si se puede.
    motivo: str | None = None
    resultado: AIResult | None = None
    uso = AIUsage()
    fallo: AIErrorKind | None = None
    llamado = False
    if not api_key:
        motivo = NO_KEY_REASON
    elif price_for(ai, accion.model) is None:
        motivo = f"falta el precio de {accion.model} en Ajustes → Claude."
    elif not tope.allowed:
        motivo = tope.reason + "."
        log.info("Control diario sin IA: %s", motivo)
    else:
        llamado = True
        peticion = AIRequest(
            model=accion.model,
            effort=accion.effort,
            max_tokens=DAILY_MAX_TOKENS,
            system=system_prompt(reglas),
            prompt=render_prompt("diario", daily_prompt_fields(datos)),
        )
        cliente = client_factory(api_key)
        try:
            resultado = cliente.generate(peticion, cancel)
            uso = resultado.usage
        except AIError as error:
            if error.kind is AIErrorKind.CANCELLED:
                log.info("Control diario cancelado: no se guarda")
                raise
            motivo, uso, fallo = error.message, error.usage, error.kind
            log.warning("Control diario sin IA: Claude ha fallado (%s)", error.kind)
        finally:
            cliente.close()

    # 4. El informe y su fila en el Registro, juntos.
    coste = cost_of(ai, accion.model, uso) if llamado else Decimal("0")
    compuesto = compose_daily(
        datos,
        resultado.text if resultado else None,
        motivo,
        truncated=bool(resultado and resultado.truncated),
    )
    esfuerzo = resultado.effort_text if resultado else (accion.effort if llamado else None)
    informe = Report(
        kind=ReportKind.DAILY,
        period=hoy.isoformat(),
        created_at=inicio,
        markdown=compuesto.markdown,
        used_ai=resultado is not None,
        conclusion=compuesto.conclusion,
        watch_positions=watch_positions(datos),
        model=(resultado.model if resultado else accion.model) if llamado else None,
        effort=esfuerzo,
        input_tokens=uso.input_tokens,
        output_tokens=uso.output_tokens,
        web_searches=uso.web_searches,
        cost_usd=coste,
        error=motivo,
    )
    detalle = _run_detail(informe, motivo)
    ejecucion = Run(
        started_at=inicio,
        triggered_by=trigger,
        step=ACTION_STEPS[AIAction.DAILY],
        status=RunStatus.ERROR if fallo is not None else RunStatus.OK,
        detail=detalle,
        finished_at=now(),
        cost_usd=coste,
    )
    with db.transaction() as tx:
        id_informe = ReportRepository(tx).save(informe)
        id_ejecucion = RunRepository(tx).add(ejecucion)
    log.info("Control diario del %s guardado (%s)", hoy, detalle)
    return DailyOutcome(
        replace(informe, id=id_informe),
        replace(ejecucion, id=id_ejecucion),
        niveles,
        tope.estimate,
        fallo,
    )


def _run_detail(report: Report, reason: str | None) -> str:
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
    return " · ".join(partes) or "Control diario guardado."
