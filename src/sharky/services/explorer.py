"""El radar de oportunidades (GUIA §5.8): el filtro de cada día y el explorador con Claude.

- **Paso 3 de la rutina** (`refresh_radar`, §5.9): gratis y sin IA. Caduca las alertas que toca y
  pasa el filtro a la lista de vigilancia, con el histórico real de cada valor. Hasta el H12 lo
  lanza «Pasar el filtro ahora (gratis)» en la pantalla Radar; el H12 lo mete en la rutina.
- **Explorador** (`create_exploration`), solo cuando lo pides y con su coste a la vista. Dos
  pasos, como el estudio mensual: A, Claude busca ideas nuevas en la web (búsqueda web con
  `pause_turn` y fuentes, como el semanal); B, se extraen como mucho 8 candidatos con un modelo
  que no tiene dónde guardar un precio. Después, los candidatos pasan por el mismo filtro. El
  informe EXPLORACION, las alertas y los descartes y su fila del Registro (con el coste real) se
  guardan juntos. Si B falla, el informe se guarda igual y no hay alertas.
- **Lista de vigilancia y alertas**: añadir y quitar valores, descartar una alerta y vigilar un
  candidato del explorador aunque no haya dado alerta.

La frontera no se mueve: Claude aporta la idea; los números salen de `core/radar.py` con precios
reales. Todo lo que habla con la red corre en un hilo de trabajo.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from sharky.core.models import (
    AlertOrigin,
    AlertStatus,
    MandateState,
    RadarAlert,
    Report,
    ReportKind,
    Run,
    RunStatus,
    WatchlistItem,
    WatchlistSource,
)
from sharky.core.radar import (
    Candidate,
    DroppedCandidate,
    ExplorationInputs,
    Holdings,
    RadarPass,
    RadarRules,
    RawCandidate,
    check_candidates,
    clean_sector,
    clean_symbol,
    clean_ticker,
    compose_exploration,
    exploration_prompt_fields,
    pass_summary,
    radar_pass,
    watchlist_candidate,
)
from sharky.core.reports import AIAction, CostEstimate, mandate_text
from sharky.core.valuation import Valuation, normalize_currency
from sharky.services.ai import (
    MAX_WEB_SEARCHES,
    WEB_SEARCH_TOOL,
    AIError,
    AIErrorKind,
    AIRequest,
    AIResult,
    AIUsage,
    ClaudeClient,
    ExtractRequest,
)
from sharky.services.db import Database
from sharky.services.market import HistoryLoad, HistoryProvider, Progress, load_histories
from sharky.services.reports import (
    TRIGGER_MANUAL,
    ClientFactory,
    StageCallback,
    _blocker,
    _Claude,
    _generate,
    _incomplete,
    _report,
    _run,
    action_settings,
    budget_check,
    cost_of,
    render_prompt,
    system_prompt,
)
from sharky.services.repositories import (
    AssetRepository,
    RadarAlertRepository,
    ReportRepository,
    RunRepository,
    WatchlistRepository,
    current_state,
    load_valuation,
)
from sharky.services.settings import Settings

log = logging.getLogger(__name__)

#: El paso del radar en el Registro de ejecuciones (`runs.step`).
RADAR_STEP = "Radar"
#: `max_tokens` del paso A (GUIA §5.8) y del paso B (como el del mensual).
EXPLORATION_MAX_TOKENS = 32_000
EXTRACTION_MAX_TOKENS = 8_000
#: Búsquedas web del explorador (decidido en el H11: el mismo tope que el semanal).
EXPLORER_WEB_SEARCHES = MAX_WEB_SEARCHES


def explorer_search_tool() -> dict[str, object]:
    return {"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": EXPLORER_WEB_SEARCHES}


# -- el contexto del radar -------------------------------------------------------------------


@dataclass(frozen=True)
class RadarContext:
    """Lo que necesita una pasada del radar, leído de la base de datos."""

    today: date
    valuation: Valuation
    state: MandateState
    rules: RadarRules
    holdings: Holdings
    alerts: tuple[RadarAlert, ...]
    watchlist: tuple[WatchlistItem, ...]
    symbols: dict[str, str]  # ticker de cartera → símbolo de Yahoo

    @property
    def active(self) -> tuple[RadarAlert, ...]:
        return tuple(a for a in self.alerts if a.status is AlertStatus.ACTIVE)


def radar_context(
    conn: sqlite3.Connection,
    settings: Settings,
    now: datetime,
    market_at: datetime | None = None,
    valuation: Valuation | None = None,
) -> RadarContext:
    """La cartera (para dejar fuera lo que ya tienes), el estado del mandato (que fija el peso
    máximo sugerido), las alertas y la lista de vigilancia."""
    valoracion = valuation or load_valuation(conn, now, market_at)
    estado = current_state(conn, valoracion, now.date())
    simbolos = {a.ticker: a.yahoo_symbol for a in AssetRepository(conn).list_all()
                if a.yahoo_symbol}
    return RadarContext(
        today=now.date(),
        valuation=valoracion,
        state=estado,
        rules=settings.radar.rules(settings.mandate.rules(), estado),
        holdings=Holdings.of((p.ticker, simbolos.get(p.ticker)) for p in valoracion.positions),
        alerts=tuple(RadarAlertRepository(conn).list_all()),
        watchlist=tuple(WatchlistRepository(conn).list_all()),
        symbols={p.ticker: simbolos[p.ticker] for p in valoracion.positions
                 if p.ticker in simbolos},
    )


def compute_pass(
    db: Database,
    provider: HistoryProvider,
    context: RadarContext,
    candidates: Sequence[Candidate],
    now: datetime,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
) -> tuple[RadarPass, HistoryLoad]:
    """Descarga el histórico que hace falta (el de los candidatos y el de las alertas activas,
    para su caducidad) y calcula la pasada: primero caduca, después filtra. No guarda alertas."""
    simbolos = {c.yahoo_symbol for c in candidates
                if c.yahoo_symbol and not context.holdings.holds(c)}
    simbolos |= {a.yahoo_symbol for a in context.active if a.yahoo_symbol}
    carga = load_histories(db, provider, sorted(s for s in simbolos if s), now, progress,
                           cancel)
    if carga.offline:
        # Sin red: solo se filtra lo que tiene una caché fiable; el resto no se toca (si no, la
        # lista de descartes se llenaría de «no verificable» por un corte de conexión).
        candidates = [c for c in candidates if c.yahoo_symbol
                      and c.yahoo_symbol in carga.histories
                      and carga.histories[c.yahoo_symbol].reliable]
    pasada = radar_pass(candidates, context.alerts, context.holdings, carga.histories,
                        carga.failures, context.today, context.rules)
    return pasada, carga


def save_pass(
    conn: sqlite3.Connection, result: RadarPass, report_id: int | None = None
) -> list[RadarAlert]:
    """Guarda una pasada, dentro de la transacción de quien llama: primero las caducidades
    (así el mismo ticker puede volver a alertar hoy) y después lo que ha dicho el filtro. De cada
    ticker queda un solo descarte por día. Devuelve las filas guardadas."""
    alertas = RadarAlertRepository(conn)
    for caducada in result.expired:
        if caducada.alert.id is not None:
            alertas.close(caducada.alert.id, AlertStatus.EXPIRED, caducada.reason)
    guardadas = []
    for veredicto in result.verdicts:
        fila = veredicto.alert
        if fila is None:
            continue
        if fila.status is AlertStatus.ACTIVE and alertas.active_for(fila.ticker) is not None:
            continue  # otra pasada la ha emitido mientras tanto
        alertas.delete_rejections(fila.ticker, fila.created_on)
        fila = replace(fila, report_id=report_id)
        guardadas.append(replace(fila, id=alertas.add(fila)))
    return guardadas


# -- el paso 3: caducar y filtrar la lista de vigilancia ----------------------------------------


@dataclass(frozen=True)
class RadarOutcome:
    """Lo que ha dejado una pasada del filtro sobre la lista de vigilancia."""

    result: RadarPass
    saved: tuple[RadarAlert, ...]
    run: Run
    offline: bool = False

    @property
    def new_alerts(self) -> list[RadarAlert]:
        return [a for a in self.saved if a.status is AlertStatus.ACTIVE]

    @property
    def message(self) -> str:
        return self.run.detail


def refresh_radar(
    db: Database,
    provider: HistoryProvider,
    settings: Settings,
    now: Callable[[], datetime],
    *,
    trigger: str = TRIGGER_MANUAL,
    market_at: datetime | None = None,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
) -> RadarOutcome:
    """El paso 3 de la rutina (GUIA §5.9): caducar las alertas y pasar el filtro a la lista de
    vigilancia. Gratis: no llama a Claude. Queda en el Registro. Corre en un hilo de trabajo."""
    inicio = now()
    contexto = radar_context(db.connection(), settings, inicio, market_at)
    candidatos = [watchlist_candidate(w) for w in contexto.watchlist]
    pasada, carga = compute_pass(db, provider, contexto, candidatos, inicio, progress, cancel)
    if carga.cancelled:
        raise RadarCancelled("Pasada del radar cancelada.")
    if carga.offline:
        detalle = "Sin conexión con Yahoo: solo se ha filtrado lo que tenía histórico reciente"
        detalle += f" · {pass_summary(pasada)}"
        estado = RunStatus.SKIPPED
    elif not contexto.watchlist:
        detalle = "La lista de vigilancia está vacía"
        if pasada.expired:
            detalle += f" · {pass_summary(pasada)}"
        estado = RunStatus.OK
    else:
        detalle = pass_summary(pasada)
        estado = RunStatus.OK
    with db.transaction() as tx:
        guardadas = save_pass(tx, pasada)
        ejecucion = Run(inicio, trigger, RADAR_STEP, estado, detalle + ".", now())
        ejecucion = replace(ejecucion, id=RunRepository(tx).add(ejecucion))
    log.info("Radar: %s", detalle)
    return RadarOutcome(pasada, tuple(guardadas), ejecucion, carga.offline)


class RadarCancelled(Exception):
    """Se ha cancelado la descarga del histórico (al cerrar la app): no se guarda nada."""


# -- el explorador ---------------------------------------------------------------------------


class ExplorerCandidate(BaseModel):
    """Un candidato del explorador (el esquema del paso B). Solo la idea: a propósito, no hay
    ningún campo donde guardar un precio, un stop o un objetivo (GUIA §5.8)."""

    ticker: str = Field(description="El ticker corto del valor, sin espacios (por ejemplo, CCJ).")
    yahoo_symbol: str = Field(
        description="El símbolo en Yahoo Finance, con el sufijo de su bolsa si lo lleva (por "
                    "ejemplo, CCJ, LDO.MI o SAAB-B.ST)."
    )
    name: str = Field(description="El nombre de la empresa o del fondo.")
    sector: str = Field(description="El sector, en una o dos palabras.")
    thesis: str = Field(description="Por qué hay foso o catalizador, en 1 o 2 frases.")
    invalidation: str = Field(description="Qué invalidaría la idea.")


class ExplorerCandidates(BaseModel):
    """Los candidatos de la exploración, como mucho 8, en el orden del texto."""

    candidates: list[ExplorerCandidate] = Field(
        description="Las ideas nuevas que propone la exploración, como mucho 8."
    )


def raw_candidates(extracted: ExplorerCandidates) -> list[RawCandidate]:
    return [
        RawCandidate(c.ticker, c.yahoo_symbol, c.name, c.sector, c.thesis, c.invalidation)
        for c in extracted.candidates
    ]


class ExplorationBlocked(Exception):
    """El explorador no se lanza (sin clave, sin precio del modelo o por el tope de gasto). El
    mensaje se puede enseñar."""


@dataclass(frozen=True)
class ExplorationOutcome:
    """Lo que ha dejado una exploración."""

    report: Report
    run: Run
    estimate: CostEstimate
    radar: RadarPass | None = None
    alerts: tuple[RadarAlert, ...] = ()
    dropped: tuple[DroppedCandidate, ...] = ()
    ai_error: AIErrorKind | None = None
    #: Por qué no hay candidatos (el paso B falló o Claude no escribió la exploración).
    candidates_unavailable: str | None = None

    @property
    def cost_usd(self) -> Decimal:
        return self.report.cost_usd

    @property
    def new_alerts(self) -> list[RadarAlert]:
        return [a for a in self.alerts if a.status is AlertStatus.ACTIVE]


def exploration_inputs(context: RadarContext, settings: Settings) -> ExplorationInputs:
    return ExplorationInputs(
        today=context.today,
        valuation=context.valuation,
        state=context.state,
        rules=settings.mandate.rules(),
        radar=context.rules,
        watchlist=context.watchlist,
        active=context.active,
        symbols=context.symbols,
    )


def exploration_blocker(conn: sqlite3.Connection, settings: Settings, api_key: str | None,
                        now: datetime) -> str | None:
    """Por qué no se puede lanzar el explorador ahora (None si se puede)."""
    ai = settings.ai
    accion = action_settings(ai, AIAction.EXPLORER)
    return _blocker(ai, accion, api_key, budget_check(conn, ai, AIAction.EXPLORER, now),
                    "Exploración")


def create_exploration(
    db: Database,
    valuation: Valuation,
    settings: Settings,
    now: Callable[[], datetime],
    api_key: str | None,
    history: HistoryProvider,
    *,
    trigger: str = TRIGGER_MANUAL,
    cancel: threading.Event | None = None,
    client_factory: ClientFactory = ClaudeClient,
    on_stage: StageCallback | None = None,
    market_at: datetime | None = None,
) -> ExplorationOutcome:
    """«Buscar oportunidades nuevas» (GUIA §5.8): A, la exploración con búsqueda web; B, los
    candidatos (solo ideas); y el filtro con precios reales. Guarda el informe, las alertas y
    los descartes y su fila del Registro en una transacción. Si se cancela (al cerrar la app), no
    guarda nada. Lanza ExplorationBlocked si no se puede llamar a Claude. Corre en un hilo de
    trabajo; `on_stage(paso, 3)` avisa de cada paso."""
    inicio = now()
    ai = settings.ai
    accion = action_settings(ai, AIAction.EXPLORER)
    reglas = settings.mandate.rules()
    conn = db.connection()
    contexto = radar_context(conn, settings, inicio, market_at, valuation)
    tope = budget_check(conn, ai, AIAction.EXPLORER, inicio)
    motivo = _blocker(ai, accion, api_key, tope, "Exploración")
    if motivo is not None or not api_key:
        raise ExplorationBlocked(motivo or "falta la clave de Claude.")
    entradas = exploration_inputs(contexto, settings)

    def paso(numero: int) -> None:
        if on_stage is not None:
            on_stage(numero, 3)

    claude = _Claude()
    uso_b = AIUsage()
    sin_medir = False
    crudos: list[RawCandidate] | None = None
    no_disponibles: str | None = None
    cliente = client_factory(api_key)
    try:
        paso(1)
        claude = _generate(cliente, AIRequest(
            model=accion.model,
            effort=accion.effort,
            max_tokens=EXPLORATION_MAX_TOKENS,
            system=system_prompt(reglas),
            prompt=render_prompt("explorador",
                                 exploration_prompt_fields(entradas, mandate_text(reglas))),
            tools=(explorer_search_tool(),),
        ), cancel, "Exploración")
        if claude.result is not None:
            paso(2)
            crudos, uso_b, sin_medir, no_disponibles = _extract_candidates(
                cliente, accion.model, claude.result.text, cancel
            )
        else:
            no_disponibles = "no hay exploración de Claude."
    finally:
        cliente.close()

    candidatos, fuera = check_candidates(crudos or [])
    pasada: RadarPass | None = None
    if no_disponibles is None:
        paso(3)
        pasada, carga = compute_pass(db, history, contexto, candidatos, now(), cancel=cancel)
        if carga.cancelled:
            raise AIError(AIErrorKind.CANCELLED, "cancelado.")

    claude.usage = claude.usage + uso_b
    coste = cost_of(ai, accion.model, claude.usage) if claude.called else Decimal("0")
    resultado = claude.result
    compuesto = compose_exploration(
        entradas,
        resultado.text if resultado else None,
        [(s.url, s.title) for s in resultado.sources] if resultado else (),
        claude.reason,
        _incomplete(resultado),
        pasada,
        fuera,
        no_disponibles if resultado is not None else None,
    )
    informe = _report(ReportKind.EXPLORATION, inicio.date().isoformat(), inicio, compuesto,
                      claude, accion, coste)
    notas = _notes(resultado, pasada, fuera, no_disponibles, sin_medir)
    ejecucion = _run(AIAction.EXPLORER, inicio, trigger, claude, informe, now(), notas)
    with db.transaction() as tx:
        id_informe = ReportRepository(tx).save(informe)
        guardadas = save_pass(tx, pasada, id_informe) if pasada is not None else []
        id_ejecucion = RunRepository(tx).add(ejecucion)
    log.info("Exploración guardada (%s)", ejecucion.detail)
    return ExplorationOutcome(
        replace(informe, id=id_informe),
        replace(ejecucion, id=id_ejecucion),
        tope.estimate,
        pasada,
        tuple(guardadas),
        tuple(fuera),
        claude.error,
        no_disponibles,
    )


def _extract_candidates(
    client: ClaudeClient, model: str, exploration: str, cancel: threading.Event | None
) -> tuple[list[RawCandidate] | None, AIUsage, bool, str | None]:
    """El paso B: los candidatos de la exploración (solo ideas). Si falla, el motivo."""
    peticion = ExtractRequest(
        model=model,
        max_tokens=EXTRACTION_MAX_TOKENS,
        prompt=render_prompt("explorador_extraccion", {"exploracion": exploration.strip()}),
    )
    try:
        extraido = client.extract(peticion, ExplorerCandidates, cancel)
    except AIError as error:
        if error.kind is AIErrorKind.CANCELLED:
            raise
        log.warning("Exploración: candidatos no disponibles (%s)", error.kind)
        return None, error.usage, error.unmeasured, error.message
    return raw_candidates(extraido.value), extraido.usage, False, None


def _notes(ai: AIResult | None, result: RadarPass | None, dropped: Sequence[DroppedCandidate],
           unavailable: str | None, unmeasured: bool) -> list[str]:
    notas = []
    if ai is not None:
        notas.append(f"{len(ai.sources)} fuentes")
        if ai.continuations:
            notas.append(f"{ai.continuations} reenvíos por pausa")
        if unavailable is not None:
            notas.append(f"candidatos no disponibles: {unavailable}")
    if result is not None:
        candidatos = len(result.verdicts) + len(dropped)
        notas.append(f"{candidatos} candidato{'s' if candidatos != 1 else ''} · "
                     f"{pass_summary(result)}")
    if unmeasured:
        notas.append("coste del paso B sin medir")
    return notas


# -- la lista de vigilancia y las alertas -------------------------------------------------------


class RadarError(ValueError):
    """Lo pedido no se puede hacer. El mensaje (una línea por error) se puede enseñar."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


def _held_tickers(conn: sqlite3.Connection, now: datetime) -> set[str]:
    return {p.ticker.upper() for p in load_valuation(conn, now).positions}


def add_to_watchlist(
    conn: sqlite3.Connection,
    ticker: str,
    name: str,
    yahoo_symbol: str,
    sector: str,
    now: datetime,
    *,
    currency: str | None = None,
    added_by: WatchlistSource = WatchlistSource.USER,
) -> WatchlistItem:
    """Añade un valor a la lista de vigilancia. Va dentro de la transacción de quien llama.

    Hace falta el símbolo de Yahoo (sin él, el filtro no tiene precios). Lo que ya está en la
    lista o en tu cartera no se añade: el radar busca entradas nuevas.
    """
    errores: list[str] = []
    limpio = clean_ticker(ticker)
    if limpio is None:
        errores.append("El ticker no vale: letras, números, «.», «-» o «_», sin espacios.")
    simbolo = clean_symbol(yahoo_symbol)
    if simbolo is None:
        errores.append("Falta el símbolo de Yahoo (sin espacios, por ejemplo CCJ o LDO.MI).")
    nombre = " ".join((name or "").split())
    if not nombre:
        errores.append("Falta el nombre.")
    sector_limpio = clean_sector(sector)
    if limpio is not None:
        if WatchlistRepository(conn).get(limpio) is not None:
            errores.append(f"{limpio} ya está en la lista de vigilancia.")
        elif limpio.upper() in _held_tickers(conn, now):
            errores.append(f"{limpio} ya está en tu cartera: el radar busca entradas nuevas.")
    if errores or limpio is None:
        raise RadarError(errores)
    divisa = normalize_currency(currency or "")
    item = WatchlistItem(limpio, nombre, added_by, now.date(), simbolo, divisa, sector_limpio)
    WatchlistRepository(conn).add(item)
    log.info("%s añadido a la lista de vigilancia", limpio)
    return item


def remove_from_watchlist(conn: sqlite3.Connection, ticker: str) -> bool:
    """Quita un valor de la lista. Sus alertas y descartes se conservan."""
    quitado = WatchlistRepository(conn).remove(ticker)
    if quitado:
        log.info("%s quitado de la lista de vigilancia", ticker)
    return quitado


def watch_candidate(conn: sqlite3.Connection, alert_id: int, now: datetime) -> WatchlistItem:
    """«Vigilar»: un candidato del explorador pasa a la lista de vigilancia con sus datos,
    aunque no haya dado alerta."""
    alerta = RadarAlertRepository(conn).get(alert_id)
    if alerta is None:
        raise RadarError(["Ese candidato ya no existe."])
    return add_to_watchlist(
        conn, alerta.ticker, alerta.name or alerta.ticker, alerta.yahoo_symbol or "",
        alerta.sector or "", now, currency=alerta.currency,
        added_by=(WatchlistSource.EXPLORER if alerta.origin is AlertOrigin.EXPLORER
                  else WatchlistSource.USER),
    )


DISCARD_REASON = "Descartada por ti"


def discard_alert(conn: sqlite3.Connection, alert_id: int, reason: str,
                  now: datetime) -> RadarAlert:
    """«Descartar» una alerta activa, con su motivo. Hasta que habría caducado, el filtro no la
    vuelve a emitir."""
    alertas = RadarAlertRepository(conn)
    alerta = alertas.get(alert_id)
    if alerta is None or alerta.status is not AlertStatus.ACTIVE:
        raise RadarError(["Esa alerta ya no está activa."])
    texto = " ".join(reason.split())
    motivo = f"{DISCARD_REASON} el {now:%d/%m/%Y}" + (f": {texto}" if texto else ".")
    alertas.close(alert_id, AlertStatus.DISCARDED, motivo)
    log.info("Alerta del radar de %s descartada", alerta.ticker)
    return replace(alerta, status=AlertStatus.DISCARDED, reason=motivo)


def last_exploration(conn: sqlite3.Connection) -> Report | None:
    return ReportRepository(conn).latest(ReportKind.EXPLORATION)
