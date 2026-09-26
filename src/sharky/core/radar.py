"""Radar de oportunidades (GUIA §5.8): el filtro cuantitativo y la vida de las alertas.

Lógica pura: recibe la serie diaria de precios de cada valor (la descarga services) y decide con
números. Claude no entra aquí: sus candidatos llegan como ideas (ticker, símbolo, nombre,
sector, tesis e invalidación), sin ningún precio, y los niveles salen solo de la serie real.

**El filtro**, para cada valor que no esté en cartera:

1. Cotización fiable (descargada ahora o hace menos de 24 h, con un cierre de los últimos 7
   días) y al menos 12 meses de histórico; si no, NO VERIFICABLE.
2. Caída desde el máximo de 52 semanas de entre el 10 % y el 40 % (ambos incluidos).
3. Stop = el mayor de: el mínimo de las últimas 8 semanas, o el precio menos 1,5 × ATR(14);
   a una caída de entre el 8 % y el 15 % del precio (decidido en el H11): si queda más cerca
   que el 8 %, se baja hasta el 8 %; si queda más lejos que el 15 %, se descarta.
4. Objetivo = el máximo de 52 semanas.
5. Ratio (objetivo − precio) / (precio − stop) ≥ el ratio mínimo del mandato.
6. Peso máximo sugerido = el tope por activo del estado del mandato, y nunca más.

Máximos y mínimos con los del día (no solo los cierres); ATR(14) con el suavizado de Wilder. Los
umbrales son coherentes entre sí: con el stop al 8 %, el ratio de 2,0 se alcanza desde una caída
del 13,8 %; con el stop al 15 %, desde el 23,1 %. Por debajo del 13,8 % nada puede pasar.

**Caducidad**, siempre antes del filtro (así un ticker que caduca hoy puede volver a alertar hoy
con niveles de hoy): por edad (30 días por defecto), porque un cierre rompió el stop antes de
entrar o porque alcanzó el objetivo sin ti. Los dos criterios de precio, solo con cotización
fiable y en la divisa que declaró la alerta.

**Qué se guarda.** Lo que pasa el filtro, como alerta ACTIVA con sus niveles y el precio con el
que se calcularon. Lo que no, como DESCARTADA con su motivo y sin peso máximo (así se distingue
de una alerta que pasó el filtro y descartaste tú). Mientras un ticker tiene una alerta activa,
el filtro no lo vuelve a mirar: la alerta conserva sus niveles.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum

from sharky.core.formatting import (
    format_eur,
    format_limit_pct,
    format_number,
    format_pct,
    pretty_sector,
)
from sharky.core.levels import format_level, round_level
from sharky.core.mandate import STATE_LABELS, MandateRules, buying_allowed
from sharky.core.models import (
    AlertOrigin,
    AlertStatus,
    MandateState,
    PriceSource,
    RadarAlert,
    WatchlistItem,
)
from sharky.core.reports import (
    NO_AI_LABEL,
    ComposedReport,
    bullets,
    extract_section,
    sources_section,
    strip_preamble,
)
from sharky.core.valuation import NO_SECTOR, Valuation

ZERO = Decimal("0")
ONE = Decimal("1")

#: ATR(14) y su múltiplo para el stop (GUIA §5.8).
ATR_PERIOD = 14
ATR_MULTIPLE = Decimal("1.5")
#: Las ventanas del máximo (objetivo y caída) y del mínimo (stop).
HIGH_WINDOW = timedelta(weeks=52)
LOW_WINDOW = timedelta(weeks=8)
#: Una cotización sin ningún cierre en estos días no es fiable (valor suspendido o sin datos).
RECENT_QUOTE = timedelta(days=7)
#: Cuántos días de histórico se piden: algo más de 13 meses, para poder exigir 12.
HISTORY_DAYS = 400
#: El explorador aporta como mucho 8 candidatos (GUIA §5.8).
MAX_CANDIDATES = 8

UNVERIFIABLE_LABEL = "No verificable"
EXPLORATION_CONCLUSION = "Conclusión de la exploración"

_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SPACES = re.compile(r"\s+")
_QUANTUM = Decimal("0.0001")


# -- la serie de precios ---------------------------------------------------------------------


@dataclass(frozen=True)
class DailyBar:
    """Un día de cotización: máximo, mínimo y cierre, en la divisa del valor."""

    day: date
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class PriceHistory:
    """El histórico diario de un símbolo de Yahoo, del día más antiguo al más reciente.

    `source` es su procedencia (GUIA §5.3): MERCADO si se acaba de descargar, CACHE si se guardó
    hace menos de 24 h, ANTIGUO si es más viejo.
    """

    symbol: str
    currency: str
    bars: tuple[DailyBar, ...]
    fetched_at: datetime
    source: PriceSource

    @property
    def reliable(self) -> bool:
        return self.source.reliable

    @property
    def last(self) -> DailyBar | None:
        return self.bars[-1] if self.bars else None


def one_year_before(day: date) -> date:
    """El mismo día del año anterior (el 29 de febrero, el 28)."""
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def has_twelve_months(bars: Sequence[DailyBar]) -> bool:
    """El histórico cubre al menos 12 meses hasta su último cierre (y da para un ATR)."""
    if len(bars) < ATR_PERIOD + 1:
        return False
    return bars[0].day <= one_year_before(bars[-1].day)


def high_52w(bars: Sequence[DailyBar]) -> Decimal:
    """El máximo de las últimas 52 semanas, contando el último día."""
    desde = bars[-1].day - HIGH_WINDOW
    return max(b.high for b in bars if b.day > desde)


def low_8w(bars: Sequence[DailyBar]) -> Decimal:
    """El mínimo de las últimas 8 semanas, contando el último día."""
    desde = bars[-1].day - LOW_WINDOW
    return min(b.low for b in bars if b.day > desde)


def true_range(bar: DailyBar, previous_close: Decimal | None) -> Decimal:
    """El rango verdadero de un día: el mayor de máximo − mínimo y las distancias al cierre
    anterior."""
    rango = bar.high - bar.low
    if previous_close is None:
        return rango
    return max(rango, abs(bar.high - previous_close), abs(bar.low - previous_close))


def average_true_range(bars: Sequence[DailyBar], period: int = ATR_PERIOD) -> Decimal:
    """ATR con el suavizado de Wilder: la media de los `period` primeros rangos verdaderos y,
    después, ATR = (ATR anterior × (period − 1) + rango del día) / period. Necesita al menos
    `period + 1` días (el primero solo aporta su cierre)."""
    if len(bars) < period + 1:
        raise ValueError(f"Hacen falta al menos {period + 1} días para el ATR({period})")
    rangos = [true_range(b, bars[i].close) for i, b in enumerate(bars[1:])]
    atr = sum(rangos[:period], ZERO) / period
    for rango in rangos[period:]:
        atr = (atr * (period - 1) + rango) / period
    return atr


# -- los umbrales ------------------------------------------------------------------------------


@dataclass(frozen=True)
class RadarRules:
    """Los umbrales del filtro, como fracciones (0.10 es un 10 %). Salen de los ajustes (Radar
    y Mandato) y del estado del mandato, que fija el peso máximo sugerido."""

    min_drop: Decimal
    max_drop: Decimal
    min_stop: Decimal
    max_stop: Decimal
    min_ratio: Decimal
    max_weight: Decimal
    validity_days: int = 30

    @classmethod
    def from_mandate(
        cls,
        rules: MandateRules,
        state: MandateState,
        *,
        min_drop: Decimal,
        max_drop: Decimal,
        min_stop: Decimal,
        max_stop: Decimal,
        validity_days: int,
    ) -> RadarRules:
        return cls(min_drop, max_drop, min_stop, max_stop, rules.min_reward_risk,
                   rules.max_asset_weight(state), validity_days)


# -- el filtro ---------------------------------------------------------------------------------


class FilterFailure(StrEnum):
    """El punto del filtro que no se cumple."""

    UNVERIFIABLE = "NO_VERIFICABLE"
    DROP = "CAIDA"
    STOP = "STOP"
    RATIO = "RATIO"


@dataclass(frozen=True)
class FilterResult:
    """Lo que ha dado el filtro con un valor: si pasa, sus niveles; si no, el motivo y lo que se
    llegó a calcular."""

    passed: bool
    reason: str | None = None
    failure: FilterFailure | None = None
    price: Decimal | None = None
    currency: str | None = None
    price_date: date | None = None
    high: Decimal | None = None  # máximo de 52 semanas
    low: Decimal | None = None  # mínimo de 8 semanas
    atr: Decimal | None = None
    drop: Decimal | None = None  # caída desde el máximo de 52 semanas, como fracción
    raw_stop: Decimal | None = None  # el stop antes de acotarlo
    stop: Decimal | None = None
    widened: bool = False  # el stop calculado quedaba a menos del mínimo y se ha bajado
    target: Decimal | None = None
    ratio: Decimal | None = None
    max_weight: Decimal | None = None

    @property
    def raw_stop_distance(self) -> Decimal | None:
        if self.raw_stop is None or self.price is None:
            return None
        return (self.price - self.raw_stop) / self.price


def _pct_up(fraction: Decimal) -> str:
    """Un porcentaje redondeado hacia arriba (para lo que se pasa de un tope: 40,01 % → 40,1 %,
    nunca «40,0 %»)."""
    return f"{format_number((fraction * 100).quantize(Decimal('0.1'), ROUND_CEILING), 1)} %"


def _band(low: Decimal, high: Decimal) -> str:
    """«8–15 %»."""
    return f"{format_limit_pct(low).removesuffix(' %')}–{format_limit_pct(high)}"


def drop_reason(drop: Decimal, rules: RadarRules) -> str:
    cifra = format_pct(drop, truncate=True) if drop < rules.min_drop else _pct_up(drop)
    return (
        f"Caída desde máximos del {cifra} · el filtro pide entre el "
        f"{format_limit_pct(rules.min_drop)} y el {format_limit_pct(rules.max_drop)}"
    )


def stop_reason(distance: Decimal, rules: RadarRules) -> str:
    return (
        f"El stop calculado queda a un {_pct_up(distance)} · el filtro lo acota al "
        f"{_band(rules.min_stop, rules.max_stop)}"
    )


def ratio_reason(ratio: Decimal, rules: RadarRules) -> str:
    return (
        f"Ratio {format_number(ratio, 1, truncate=True)} · el mínimo del mandato es "
        f"{format_number(rules.min_ratio, 1)}"
    )


def unverifiable_reason(detail: str) -> str:
    return f"{UNVERIFIABLE_LABEL}: {detail}"


def round_target(value: Decimal) -> Decimal:
    """El objetivo calculado, con los decimales de un nivel (2, o 4 por debajo de 1)."""
    decimales = 2 if value >= ONE else 4
    return value.quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_HALF_UP)


def apply_filter(
    history: PriceHistory | None,
    today: date,
    rules: RadarRules,
    missing: str | None = None,
) -> FilterResult:
    """Los seis puntos del filtro (GUIA §5.8) sobre el histórico de un valor. `missing` explica
    por qué no hay histórico, si no lo hay."""

    def no_verificable(detalle: str) -> FilterResult:
        return FilterResult(False, unverifiable_reason(detalle), FilterFailure.UNVERIFIABLE)

    if history is None or not history.bars:
        return no_verificable(missing or "Yahoo no ha dado su histórico")
    if not history.reliable:
        return no_verificable(
            f"el histórico guardado es del {history.fetched_at:%d/%m/%Y} y no se ha podido "
            "descargar otro"
        )
    barras = history.bars
    ultimo = barras[-1]
    if ultimo.day < today - RECENT_QUOTE:
        return no_verificable(f"sin cotización reciente (último cierre del {ultimo.day:%d/%m/%Y})")
    if not has_twelve_months(barras):
        return no_verificable(
            f"sin 12 meses de histórico fiable (empieza el {barras[0].day:%d/%m/%Y})"
        )

    # 2. La caída desde el máximo de 52 semanas.
    precio = ultimo.close
    maximo = high_52w(barras)
    minimo = low_8w(barras)
    atr = average_true_range(barras)
    caida = (maximo - precio) / maximo
    base = FilterResult(
        False, price=precio, currency=history.currency, price_date=ultimo.day, high=maximo,
        low=minimo, atr=atr, drop=caida,
    )
    if not rules.min_drop <= caida <= rules.max_drop:
        return replace(base, reason=drop_reason(caida, rules), failure=FilterFailure.DROP)

    # 3. El stop, acotado a la banda: más cerca, se baja; más lejos, se descarta.
    bruto = max(minimo, precio - ATR_MULTIPLE * atr)
    distancia = (precio - bruto) / precio
    if distancia > rules.max_stop:
        return replace(base, raw_stop=bruto, reason=stop_reason(distancia, rules),
                       failure=FilterFailure.STOP)
    bajado = distancia < rules.min_stop
    stop = round_level(precio * (ONE - rules.min_stop) if bajado else bruto)

    # 4 y 5. El objetivo y el ratio, con los niveles tal como se guardan.
    objetivo = round_target(maximo)
    ratio = (objetivo - precio) / (precio - stop)
    niveles = replace(base, raw_stop=bruto, stop=stop, widened=bajado, target=objetivo,
                      ratio=ratio)
    if ratio < rules.min_ratio:
        return replace(niveles, reason=ratio_reason(ratio, rules), failure=FilterFailure.RATIO)

    # 6. El peso máximo sugerido: el tope por activo del estado.
    return replace(niveles, passed=True, max_weight=rules.max_weight)


# -- la caducidad ------------------------------------------------------------------------------


class ExpiryKind(StrEnum):
    AGE = "EDAD"
    STOP = "STOP"
    TARGET = "OBJETIVO"


@dataclass(frozen=True)
class Expiry:
    """Una alerta activa que caduca hoy, y por qué."""

    alert: RadarAlert
    kind: ExpiryKind
    reason: str


def expires_on(alert: RadarAlert, validity_days: int) -> date:
    """El día en que una alerta caduca por edad."""
    return alert.created_on + timedelta(days=validity_days)


def price_expiry(alert: RadarAlert, history: PriceHistory | None) -> Expiry | None:
    """Si un cierre desde el día de la alerta rompió su stop o alcanzó su objetivo. Solo con
    cotización fiable y en la divisa de la alerta; si no, nada."""
    if (
        history is None
        or not history.reliable
        or history.currency != alert.currency
        or alert.stop is None
        or alert.target is None
        or alert.currency is None
    ):
        return None
    for barra in history.bars:
        if barra.day < alert.created_on:
            continue
        cierre = format_level(barra.close, alert.currency)
        if barra.close <= alert.stop:
            return Expiry(alert, ExpiryKind.STOP, (
                f"Rompió su stop antes de que entraras: cierre de {cierre} el "
                f"{barra.day:%d/%m/%Y} (stop {format_level(alert.stop, alert.currency)})."
            ))
        if barra.close >= alert.target:
            return Expiry(alert, ExpiryKind.TARGET, (
                f"Alcanzó el objetivo sin ti: cierre de {cierre} el {barra.day:%d/%m/%Y} "
                f"(objetivo {format_level(alert.target, alert.currency)})."
            ))
    return None


def expire_alerts(
    alerts: Iterable[RadarAlert],
    today: date,
    validity_days: int,
    histories: Mapping[str, PriceHistory],
) -> list[Expiry]:
    """Las alertas activas que caducan hoy (GUIA §5.8). `histories`, por ticker. El precio va
    primero: si rompió el stop, se dice aunque además haya cumplido los días."""
    caducadas = []
    for alerta in alerts:
        if alerta.status is not AlertStatus.ACTIVE:
            continue
        por_precio = price_expiry(alerta, histories.get(alerta.ticker))
        if por_precio is not None:
            caducadas.append(por_precio)
        elif today >= expires_on(alerta, validity_days):
            caducadas.append(Expiry(alerta, ExpiryKind.AGE, (
                f"Caducada por edad: {validity_days} días desde el "
                f"{alerta.created_on:%d/%m/%Y} sin entrar."
            )))
    return caducadas


# -- los candidatos ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """Un valor que pasa por el filtro: de la lista de vigilancia o del explorador. Del
    explorador llega solo la idea: ningún precio."""

    ticker: str
    name: str
    origin: AlertOrigin
    yahoo_symbol: str | None = None
    sector: str | None = None
    summary: str = ""  # la idea: por qué hay foso o catalizador
    invalidation: str = ""


def watchlist_candidate(item: WatchlistItem) -> Candidate:
    return Candidate(item.ticker, item.name, AlertOrigin.WATCHLIST, item.yahoo_symbol,
                     item.sector)


@dataclass(frozen=True)
class RawCandidate:
    """Un candidato tal como lo extrae Claude (paso B), antes de comprobarlo."""

    ticker: str
    yahoo_symbol: str
    name: str
    sector: str
    thesis: str
    invalidation: str


def _clean(text: str) -> str:
    return _SPACES.sub(" ", text or "").strip()


def clean_ticker(text: str) -> str | None:
    """Un ticker como los de Sharky: sin espacios («SAAB B» → «SAAB-B»). None si no vale."""
    limpio = _SPACES.sub("-", (text or "").strip())
    return limpio if _TICKER.match(limpio) else None


def clean_symbol(text: str) -> str | None:
    limpio = (text or "").strip()
    return limpio if limpio and not _SPACES.search(limpio) else None


def clean_sector(text: str) -> str | None:
    """Los sectores van sin espacios: «Defensa europea» → «Defensa_europea»."""
    limpio = _SPACES.sub("_", (text or "").strip())
    return limpio or None


@dataclass(frozen=True)
class DroppedCandidate:
    """Un candidato del explorador que no llega al filtro, y por qué."""

    ticker: str
    reason: str


def check_candidates(
    raw: Sequence[RawCandidate],
) -> tuple[list[Candidate], list[DroppedCandidate]]:
    """Los candidatos del explorador, limpios: como mucho 8 (los primeros), sin repetir y con un
    ticker válido. Solo ideas: aquí no hay ningún precio que copiar."""
    candidatos: list[Candidate] = []
    fuera: list[DroppedCandidate] = []
    vistos: set[str] = set()
    for c in raw[:MAX_CANDIDATES]:
        ticker = clean_ticker(c.ticker)
        if ticker is None:
            fuera.append(DroppedCandidate(_clean(c.ticker) or "(sin ticker)",
                                          "ticker no válido"))
            continue
        if ticker.upper() in vistos:
            continue
        vistos.add(ticker.upper())
        candidatos.append(Candidate(
            ticker=ticker,
            name=_clean(c.name) or ticker,
            origin=AlertOrigin.EXPLORER,
            yahoo_symbol=clean_symbol(c.yahoo_symbol),
            sector=clean_sector(c.sector),
            summary=_clean(c.thesis),
            invalidation=_clean(c.invalidation),
        ))
    return candidatos, fuera


# -- lo guardado del radar ---------------------------------------------------------------------


def passed_filter(alert: RadarAlert) -> bool:
    """La fila es de una alerta que pasó el filtro (activa, ejecutada, caducada o descartada
    por ti): solo esas tienen peso máximo sugerido."""
    return alert.max_weight is not None


def is_filter_rejection(alert: RadarAlert) -> bool:
    """La fila es de un candidato que no pasó el filtro."""
    return alert.status is AlertStatus.DISCARDED and not passed_filter(alert)


def _order(alert: RadarAlert) -> tuple[date, int]:
    return alert.created_on, alert.id or 0


def latest_by_ticker(alerts: Iterable[RadarAlert]) -> dict[str, RadarAlert]:
    """La fila más reciente de cada ticker (sin distinguir mayúsculas)."""
    ultimas: dict[str, RadarAlert] = {}
    for alerta in sorted(alerts, key=_order):
        ultimas[alerta.ticker.upper()] = alerta
    return ultimas


def rejected_candidates(alerts: Iterable[RadarAlert]) -> list[RadarAlert]:
    """«Candidatos que no pasaron el filtro»: los tickers cuyo último resultado es un descarte
    del filtro, del más reciente al más antiguo."""
    ultimas = latest_by_ticker(alerts).values()
    return sorted((a for a in ultimas if is_filter_rejection(a)), key=_order, reverse=True)


def alert_history(alerts: Iterable[RadarAlert]) -> list[RadarAlert]:
    """El historial: alertas caducadas, ejecutadas y descartadas por ti, la más reciente
    primero."""
    return sorted(
        (a for a in alerts
         if a.status in (AlertStatus.EXPIRED, AlertStatus.EXECUTED)
         or (a.status is AlertStatus.DISCARDED and passed_filter(a))),
        key=_order, reverse=True,
    )


def active_alerts(alerts: Iterable[RadarAlert]) -> list[RadarAlert]:
    """Las alertas activas, la más reciente primero."""
    return sorted((a for a in alerts if a.status is AlertStatus.ACTIVE), key=_order,
                  reverse=True)


def idea_for(ticker: str, alerts: Iterable[RadarAlert]) -> tuple[str, str]:
    """La última idea (y su invalidación) que se conoce de un ticker: la del explorador."""
    for alerta in sorted(alerts, key=_order, reverse=True):
        if alerta.ticker.upper() == ticker.upper() and alerta.summary.strip():
            return alerta.summary, alerta.invalidation
    return "", ""


def discarded_until(
    ticker: str, alerts: Iterable[RadarAlert], validity_days: int
) -> date | None:
    """Si descartaste una alerta de este ticker, el día en que habría caducado: hasta entonces
    el filtro no la vuelve a emitir (si no, reaparecería al día siguiente)."""
    dias = [
        expires_on(a, validity_days) for a in alerts
        if a.ticker.upper() == ticker.upper()
        and a.status is AlertStatus.DISCARDED and passed_filter(a)
    ]
    return max(dias, default=None)


@dataclass(frozen=True)
class Holdings:
    """Lo que ya está en cartera: tickers y símbolos de Yahoo de las posiciones abiertas."""

    tickers: frozenset[str] = frozenset()
    symbols: frozenset[str] = frozenset()

    @classmethod
    def of(cls, pairs: Iterable[tuple[str, str | None]]) -> Holdings:
        tickers, simbolos = set(), set()
        for ticker, simbolo in pairs:
            tickers.add(ticker.upper())
            if simbolo:
                simbolos.add(simbolo.upper())
        return cls(frozenset(tickers), frozenset(simbolos))

    def holds(self, candidate: Candidate) -> bool:
        return candidate.ticker.upper() in self.tickers or bool(
            candidate.yahoo_symbol and candidate.yahoo_symbol.upper() in self.symbols
        )


IN_PORTFOLIO = "Ya está en tu cartera: el radar busca entradas nuevas."
ALREADY_ACTIVE = "Ya tiene una alerta activa: conserva sus niveles."


@dataclass(frozen=True)
class CandidateVerdict:
    """Lo que ha pasado con un candidato: se ha quedado fuera antes del filtro (`skipped`) o ha
    pasado por él (`result`), con la fila que se guarda (`alert`)."""

    candidate: Candidate
    result: FilterResult | None = None
    alert: RadarAlert | None = None
    skipped: str | None = None


@dataclass(frozen=True)
class RadarPass:
    """Una pasada del radar: lo que caduca y lo que dice el filtro de cada candidato."""

    today: date
    rules: RadarRules
    expired: tuple[Expiry, ...] = ()
    verdicts: tuple[CandidateVerdict, ...] = ()

    @property
    def new_alerts(self) -> list[RadarAlert]:
        return [v.alert for v in self.verdicts
                if v.alert is not None and v.alert.status is AlertStatus.ACTIVE]

    @property
    def rejections(self) -> list[RadarAlert]:
        return [v.alert for v in self.verdicts
                if v.alert is not None and v.alert.status is AlertStatus.DISCARDED]

    @property
    def skipped(self) -> list[CandidateVerdict]:
        return [v for v in self.verdicts if v.skipped is not None]


def new_alert(candidate: Candidate, result: FilterResult, today: date) -> RadarAlert:
    """La fila del filtro: ACTIVA si pasa, DESCARTADA con su motivo si no."""

    def cuatro(valor: Decimal | None) -> Decimal | None:
        return valor.quantize(_QUANTUM, rounding=ROUND_DOWN) if valor is not None else None

    return RadarAlert(
        created_on=today,
        ticker=candidate.ticker,
        origin=candidate.origin,
        status=AlertStatus.ACTIVE if result.passed else AlertStatus.DISCARDED,
        price=result.price,
        currency=result.currency,
        stop=result.stop,
        target=result.target,
        ratio=cuatro(result.ratio),
        drawdown_from_high=cuatro(result.drop),
        max_weight=result.max_weight if result.passed else None,
        summary=candidate.summary,
        reason=None if result.passed else result.reason,
        name=candidate.name,
        yahoo_symbol=candidate.yahoo_symbol,
        sector=candidate.sector,
        invalidation=candidate.invalidation,
    )


def radar_pass(
    candidates: Sequence[Candidate],
    alerts: Sequence[RadarAlert],
    holdings: Holdings,
    histories: Mapping[str, PriceHistory],
    failures: Mapping[str, str],
    today: date,
    rules: RadarRules,
) -> RadarPass:
    """Una pasada del radar (GUIA §5.8): primero caduca lo que toca y después pasa el filtro a
    cada candidato que no esté en cartera ni tenga una alerta activa. `histories` y `failures`
    van por símbolo de Yahoo. Solo calcula: guardar es cosa de services."""
    activas = [a for a in alerts if a.status is AlertStatus.ACTIVE]
    por_ticker = {
        a.ticker: histories[a.yahoo_symbol] for a in activas
        if a.yahoo_symbol and a.yahoo_symbol in histories
    }
    caducadas = expire_alerts(activas, today, rules.validity_days, por_ticker)
    ids_caducadas = {e.alert.id for e in caducadas}
    siguen = {a.ticker.upper() for a in activas if a.id not in ids_caducadas}

    veredictos: list[CandidateVerdict] = []
    vistos: set[str] = set()
    for c in candidates:
        if c.ticker.upper() in vistos:
            continue
        vistos.add(c.ticker.upper())
        if holdings.holds(c):
            veredictos.append(CandidateVerdict(c, skipped=IN_PORTFOLIO))
            continue
        if c.ticker.upper() in siguen:
            veredictos.append(CandidateVerdict(c, skipped=ALREADY_ACTIVE))
            continue
        hasta = discarded_until(c.ticker, alerts, rules.validity_days)
        if hasta is not None and today < hasta:
            veredictos.append(CandidateVerdict(
                c, skipped=f"La descartaste: no se vuelve a mirar hasta el {hasta:%d/%m/%Y}."
            ))
            continue
        if c.origin is AlertOrigin.WATCHLIST and not c.summary:
            idea, invalida = idea_for(c.ticker, alerts)
            c = replace(c, summary=idea, invalidation=c.invalidation or invalida)
        if c.yahoo_symbol:
            historia = histories.get(c.yahoo_symbol)
            falta = failures.get(c.yahoo_symbol)
        else:
            historia, falta = None, "sin símbolo de Yahoo"
        resultado = apply_filter(historia, today, rules, falta)
        veredictos.append(CandidateVerdict(c, resultado, new_alert(c, resultado, today)))
    return RadarPass(today, rules, tuple(caducadas), tuple(veredictos))


# -- textos para la interfaz y los informes ------------------------------------------------------


def ratio_text(ratio: Decimal) -> str:
    return format_number(ratio, 1, truncate=True)


def drop_text(drop: Decimal) -> str:
    """«−28,7 %»: la caída desde el máximo, con su signo."""
    return format_pct(-drop, signed=True)


def pass_summary(result: RadarPass) -> str:
    """Una línea para el Registro y los avisos: «2 alertas nuevas (CCJ, LDO) · 3 descartes ·
    1 caducada»."""
    partes = []
    nuevas = result.new_alerts
    if nuevas:
        tickers = ", ".join(a.ticker for a in nuevas)
        partes.append(f"{len(nuevas)} alerta{'s' if len(nuevas) != 1 else ''} "
                      f"nueva{'s' if len(nuevas) != 1 else ''} ({tickers})")
    else:
        partes.append("ninguna alerta nueva")
    descartes = result.rejections
    if descartes:
        partes.append(f"{len(descartes)} descarte{'s' if len(descartes) != 1 else ''}")
    if result.expired:
        n = len(result.expired)
        partes.append(f"{n} caducada{'s' if n != 1 else ''}")
    return " · ".join(partes)


# -- el explorador: el contexto ------------------------------------------------------------------


@dataclass(frozen=True)
class SaturatedSector:
    sector: str
    weight: Decimal


def saturated_sectors(
    valuation: Valuation, rules: MandateRules, state: MandateState
) -> list[SaturatedSector]:
    """Los sectores en los que ya no cabe una posición nueva del tamaño sugerido: su peso más el
    tope por activo del estado supera el tope por sector. Las posiciones sin sector no cuentan
    como un sector de verdad."""
    hueco = rules.max_asset_weight(state)
    return [
        SaturatedSector(s.sector, s.weight) for s in valuation.sectors
        if s.sector != NO_SECTOR and s.weight + hueco > rules.max_sector_weight
    ]


@dataclass(frozen=True)
class ExplorationInputs:
    """Lo que usa el explorador: la cartera (para no repetirla), la lista de vigilancia, las
    alertas activas y el mandato."""

    today: date
    valuation: Valuation
    state: MandateState
    rules: MandateRules
    radar: RadarRules
    watchlist: tuple[WatchlistItem, ...] = ()
    active: tuple[RadarAlert, ...] = ()
    symbols: Mapping[str, str] = field(default_factory=dict)  # ticker de cartera → Yahoo


def exploration_prompt_fields(inputs: ExplorationInputs, mandate: str) -> dict[str, str]:
    """Los campos de `prompts/explorador.md`."""
    cartera = []
    for p in inputs.valuation.positions:
        partes = [f"{p.ticker} — {p.name}"]
        simbolo = inputs.symbols.get(p.ticker)
        if simbolo:
            partes.append(f"Yahoo {simbolo}")
        partes.append(pretty_sector(p.sector) if p.sector else "sin sector")
        partes.append(f"peso {format_pct(p.weight)}")
        cartera.append("- " + " · ".join(partes))
    vigilancia = [
        f"- {w.ticker} — {w.name}" + (f" · Yahoo {w.yahoo_symbol}" if w.yahoo_symbol else "")
        for w in inputs.watchlist
    ]
    alertas = [f"- {a.ticker}" + (f" — {a.name}" if a.name else "") for a in inputs.active]
    saturados = [
        f"- {pretty_sector(s.sector)}: {format_pct(s.weight)} de la cartera (tope "
        f"{format_limit_pct(inputs.rules.max_sector_weight)})"
        for s in saturated_sectors(inputs.valuation, inputs.rules, inputs.state)
    ]
    return {
        "cartera": "\n" + "\n".join(cartera) if cartera else "ninguna posición (solo efectivo)",
        "vigilancia": "\n" + "\n".join(vigilancia) if vigilancia else "ninguno",
        "alertas": "\n" + "\n".join(alertas) if alertas else "ninguna",
        "mandato": "\n" + mandate,
        "sectores_saturados": "\n" + "\n".join(saturados) if saturados else "ninguno",
    }


# -- el informe de exploración -------------------------------------------------------------------


def _row(celdas: Sequence[str]) -> str:
    return "| " + " | ".join(c.replace("|", "\\|") for c in celdas) + " |"


def _level(value: Decimal | None, currency: str | None) -> str:
    if value is None or currency is None:
        return "—"
    return format_level(value, currency)


def state_text(inputs: ExplorationInputs) -> str:
    """La línea de arriba: el estado y el peso máximo que sugerirá el filtro."""
    texto = (
        f"{STATE_LABELS[inputs.state]} · peso máximo sugerido por alerta "
        f"{format_limit_pct(inputs.radar.max_weight)} · efectivo "
        f"{format_eur(inputs.valuation.cash_eur)}"
    )
    if not buying_allowed(inputs.state):
        texto += " · compras prohibidas en este estado: Operar rechazará la compra"
    return texto


def candidates_section(
    result: RadarPass | None,
    dropped: Sequence[DroppedCandidate] = (),
    unavailable: str | None = None,
) -> str:
    """«Candidatos y filtro»: lo que ha dicho el filtro de cada candidato, con precios reales."""
    partes = [
        "## Candidatos y filtro",
        "",
        "*Niveles calculados por Sharky con el histórico real de cada valor. Claude no propone "
        "precios, stops ni objetivos.*",
        "",
    ]
    if unavailable is not None:
        partes.append(f"Candidatos no disponibles: {unavailable}")
        return "\n".join(partes)
    veredictos = result.verdicts if result is not None else ()
    if not veredictos and not dropped:
        partes.append("Claude no ha propuesto ningún candidato.")
        return "\n".join(partes)
    partes += [
        _row(["Candidato", "Sector", "Resultado", "Precio", "Stop", "Objetivo", "R:R",
              "Desde máx."]),
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for v in veredictos:
        c = v.candidate
        nombre = f"{c.ticker} — {c.name}" if c.name and c.name != c.ticker else c.ticker
        sector = pretty_sector(c.sector) if c.sector else "—"
        a = v.alert
        if v.skipped is not None or a is None:
            partes.append(_row([nombre, sector, f"Fuera: {v.skipped}", "—", "—", "—", "—", "—"]))
            continue
        resultado = "Alerta" if a.status is AlertStatus.ACTIVE else f"Descartado: {a.reason}"
        partes.append(_row([
            nombre,
            sector,
            resultado,
            _level(a.price, a.currency),
            _level(a.stop, a.currency),
            _level(a.target, a.currency),
            ratio_text(a.ratio) if a.ratio is not None else "—",
            drop_text(a.drawdown_from_high) if a.drawdown_from_high is not None else "—",
        ]))
    for d in dropped:
        partes.append(_row([d.ticker, "—", f"Fuera: {d.reason}", "—", "—", "—", "—", "—"]))
    if result is not None and result.expired:
        partes += ["", "**Alertas caducadas antes de pasar el filtro**", ""]
        partes += [f"- {e.alert.ticker}: {e.reason}" for e in result.expired]
    return "\n".join(partes)


def exploration_fallback_conclusion(
    result: RadarPass | None, unavailable: str | None
) -> list[str]:
    """De 1 a 3 viñetas con lo medido (sin IA, o si Claude no trae la conclusión)."""
    if unavailable is not None or result is None:
        return ["Sin candidatos que pasar por el filtro."]
    evaluados = [v for v in result.verdicts if v.alert is not None]
    nuevas = result.new_alerts
    vinetas = [
        f"{len(evaluados)} candidato{'s' if len(evaluados) != 1 else ''} por el filtro: "
        f"{len(nuevas)} alerta{'s' if len(nuevas) != 1 else ''}."
    ]
    if nuevas:
        vinetas.append("Alertas: " + ", ".join(
            f"{a.ticker} (ratio {ratio_text(a.ratio)})" for a in nuevas if a.ratio is not None
        ) + ".")
    elif evaluados:
        vinetas.append("Ningún candidato ofrece hoy asimetría con precios reales: es el filtro "
                       "haciendo su trabajo.")
    return vinetas


def compose_exploration(
    inputs: ExplorationInputs,
    analysis: str | None,
    sources: Sequence[tuple[str, str]] = (),
    no_ai_reason: str | None = None,
    incomplete: str | None = None,
    result: RadarPass | None = None,
    dropped: Sequence[DroppedCandidate] = (),
    unavailable: str | None = None,
) -> ComposedReport:
    """El Markdown de la exploración y su conclusión: el texto de Claude (que termina en
    «## Conclusión de la exploración»), sus fuentes y, aparte, lo que ha dicho el filtro."""
    partes = [f"**Mandato:** {state_text(inputs)}", ""]
    texto = strip_preamble(analysis or "")
    conclusion = extract_section(texto, EXPLORATION_CONCLUSION) if texto else None
    if texto:
        partes.append(texto)
        if incomplete:
            partes += ["", f"> {incomplete}"]
    else:
        partes.append(f"> **{NO_AI_LABEL}:** {no_ai_reason or 'Claude no ha devuelto texto'}")
    if conclusion is None:
        conclusion = bullets(exploration_fallback_conclusion(result, unavailable))
        partes += ["", f"## {EXPLORATION_CONCLUSION}", ""]
        if texto:
            partes += ["*Escrita por Sharky con los datos medidos: la exploración de Claude no "
                       "la traía.*", ""]
        partes.append(conclusion)
    if texto:
        partes += ["", sources_section(sources)]
    partes += ["", "---", "", candidates_section(result, dropped, unavailable)]
    return ComposedReport("\n".join(partes).strip() + "\n", conclusion)
