"""Informes con Claude (GUIA §5.7): todo lo que calcula el código, sin red ni disco.

- **Contexto del control diario.** Solo hoy: estado, patrimonio, posiciones (precio, peso, PnL y
  variación desde el último control), niveles de las tesis, incumplimientos y operaciones
  forzadas. Son los campos del prompt `diario.md`.
- **Semanal de noticias (H10).** Posiciones y sectores, las conclusiones de los diarios de la
  semana y las posiciones prioritarias (±7 % en la semana o con aviso); las fuentes que cita
  Claude van en «Fuentes».
- **Estudio mensual (H10).** El contexto del mes, el estudio de Claude y los veredictos que se
  extraen de él, validados aquí: se añaden a las tesis como revisiones y propuestas, sin tocar
  ningún número.
- **Parte determinista.** La línea de estado de arriba y las tablas «Datos del día» de abajo:
  van en el informe haya o no análisis de Claude.
- **Conclusión.** Se extrae de «## Conclusión del día»; si Claude no la trae (o no hay IA), se
  escribe una con los datos medidos. Es lo único que releerán el semanal y el mensual.
- **Coste.** Lo que cuesta una llamada con los precios de Ajustes, la estimación de cada botón
  (la media de las tres últimas ejecuciones reales; mientras no las haya, la tabla de la guía)
  y el tope de gasto del mes.

Claude nunca cambia un número: aquí se calculan todos y él solo los interpreta.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sharky.core.formatting import (
    format_amount,
    format_eur,
    format_limit_pct,
    format_number,
    format_pct,
    format_price,
    format_signed_amount,
    format_units,
    format_usd,
    format_usd_range,
    pretty_sector,
)
from sharky.core.levels import (
    LevelCheck,
    LevelStatus,
    alert_title,
    format_eur_price,
    from_json,
    levels_of,
    levels_summary,
    proposal_text,
    to_json,
)
from sharky.core.mandate import (
    ALERT_DRAWDOWN,
    INTENSIVE_CARE_DRAWDOWN,
    LOCKDOWN_DRAWDOWN,
    STATE_LABELS,
    DayChange,
    Finding,
    MandateRules,
    age_text,
    days_open,
    is_escalated,
    join_names,
    last_reliable,
)
from sharky.core.models import (
    Asset,
    Author,
    Breach,
    NavSnapshot,
    Price,
    Report,
    Thesis,
    ThesisEvent,
    ThesisEventKind,
    Trade,
    TradeKind,
)
from sharky.core.schedule import Month, parse_day
from sharky.core.valuation import PositionValue, Valuation

ZERO = Decimal("0")
MILLION = Decimal("1000000")
THOUSAND = Decimal("1000")

#: «MOVIMIENTOS DE ±3 % O MÁS DESDE EL ÚLTIMO CONTROL» (decidido en el H9; el semanal usa 7 %).
DAILY_MOVE_THRESHOLD = Decimal("0.03")

DAILY_CONCLUSION = "Conclusión del día"
#: La etiqueta de un informe sin análisis de Claude (GUIA §5.7).
NO_AI_LABEL = "Sin análisis de IA"
#: Cuántas viñetas tiene como mucho la conclusión que escribe Sharky.
MAX_FALLBACK_BULLETS = 3


# -- acciones que llaman a Claude ---------------------------------------------------------


class AIAction(StrEnum):
    """Las acciones que llaman a Claude. El valor es su clave en los ajustes (`ai.daily`…)."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    EXPLORER = "explorer"


#: El nombre de cada acción para la interfaz.
ACTION_LABELS: dict[AIAction, str] = {
    AIAction.DAILY: "Control diario",
    AIAction.WEEKLY: "Noticias semanales",
    AIAction.MONTHLY: "Estudio mensual",
    AIAction.EXPLORER: "Buscar oportunidades",
}

#: El paso de cada acción en el Registro de ejecuciones (`runs.step`).
ACTION_STEPS: dict[AIAction, str] = {
    AIAction.DAILY: "Informe diario",
    AIAction.WEEKLY: "Informe semanal",
    AIAction.MONTHLY: "Estudio mensual",
    AIAction.EXPLORER: "Exploración",
}

#: Coste aproximado de la tabla de la guía, en USD, mientras no haya tres ejecuciones reales.
DEFAULT_COSTS: dict[AIAction, tuple[Decimal, Decimal]] = {
    AIAction.DAILY: (Decimal("0.02"), Decimal("0.05")),
    AIAction.WEEKLY: (Decimal("0.30"), Decimal("0.80")),
    AIAction.MONTHLY: (Decimal("0.20"), Decimal("0.60")),
    AIAction.EXPLORER: (Decimal("0.80"), Decimal("2.50")),
}

#: Cuántas ejecuciones reales hacen falta para estimar con su media (GUIA §5.7).
ESTIMATE_RUNS = 3


# -- coste ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenPrice:
    """Precio de un modelo en USD por millón de tokens."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal


_DATE_SUFFIX = re.compile(r"^\d{8}$")


def find_price[T](prices: Mapping[str, T], model: str) -> T | None:
    """El precio de un modelo. Si la lista de modelos lo da con fecha
    («claude-haiku-4-5-20251001»), vale el del nombre sin ella («claude-haiku-4-5»); nunca el
    de otro modelo que empiece igual («claude-opus-5» no es «claude-opus-5-5»)."""
    if model in prices:
        return prices[model]
    for clave, precio in prices.items():
        if model.startswith(clave + "-") and _DATE_SUFFIX.match(model[len(clave) + 1:]):
            return precio
    return None


def call_cost(
    input_tokens: int,
    output_tokens: int,
    web_searches: int,
    price: TokenPrice,
    search_per_1000: Decimal,
) -> Decimal:
    """Lo que cuesta una llamada: tokens de entrada y de salida (el razonamiento cuenta como
    salida) y búsquedas web."""
    return (
        Decimal(input_tokens) * price.input_per_mtok / MILLION
        + Decimal(output_tokens) * price.output_per_mtok / MILLION
        + Decimal(web_searches) * search_per_1000 / THOUSAND
    )


@dataclass(frozen=True)
class CostEstimate:
    """El coste aproximado de una acción antes de lanzarla."""

    low: Decimal
    high: Decimal
    measured: bool  # media de ejecuciones reales (si no, la tabla de la guía)

    @property
    def ceiling(self) -> Decimal:
        """Lo que se compara con el tope: el extremo alto de la horquilla."""
        return self.high

    @property
    def text(self) -> str:
        """«≈ 0,03 $» o «≈ 0,02–0,05 $»."""
        if self.low == self.high:
            return f"≈ {format_usd(self.low)}"
        return f"≈ {format_usd_range(self.low, self.high)}"


def estimate_cost(action: AIAction, recent_costs: Sequence[Decimal]) -> CostEstimate:
    """La media del coste real de las tres últimas ejecuciones con Claude de esa acción
    (`recent_costs`, de la más reciente a la más antigua); mientras no haya tres, el valor de
    la tabla de la guía."""
    reales = [c for c in recent_costs if c > 0][:ESTIMATE_RUNS]
    if len(reales) < ESTIMATE_RUNS:
        bajo, alto = DEFAULT_COSTS[action]
        return CostEstimate(bajo, alto, measured=False)
    media = sum(reales, ZERO) / len(reales)
    return CostEstimate(media, media, measured=True)


@dataclass(frozen=True)
class BudgetCheck:
    """El gasto del mes frente al tope, con lo que costaría la acción."""

    spent: Decimal
    budget: Decimal
    estimate: CostEstimate

    @property
    def allowed(self) -> bool:
        """La acción cabe en el tope aunque cueste lo más alto de su horquilla."""
        return self.spent + self.estimate.ceiling <= self.budget

    @property
    def spent_text(self) -> str:
        """«1,23 $ de 10,00 $»."""
        return f"{format_usd(self.spent)} de {format_usd(self.budget)}"

    @property
    def reason(self) -> str:
        """Por qué no se lanza (para la etiqueta «Sin análisis de IA: …»)."""
        return (
            f"se superaría el tope de gasto mensual ({self.spent_text} gastados; esta acción "
            f"cuesta {self.estimate.text})"
        )


# -- el mandato para el prompt de sistema --------------------------------------------------


def mandate_text(rules: MandateRules) -> str:
    """El mandato vigente, generado desde los ajustes (la única fuente), para `$mandato`."""
    pct = format_limit_pct
    alerta, cuidados, bloqueo = (
        pct(ALERT_DRAWDOWN), pct(INTENSIVE_CARE_DRAWDOWN), pct(LOCKDOWN_DRAWDOWN),
    )
    return "\n".join([
        f"- Peso máximo por activo: {pct(rules.max_asset_weight_optimal)} en Óptimo; "
        f"{pct(rules.max_asset_weight_other)} en los demás estados.",
        f"- Peso máximo por sector: {pct(rules.max_sector_weight)}.",
        f"- Efectivo: entre {pct(rules.min_cash_optimal)} y {pct(rules.max_cash_optimal)} del "
        f"NAV en Óptimo; mínimo del {pct(rules.min_cash_other)} en los demás estados.",
        f"- Riesgo máximo por operación (hasta el stop): {pct(rules.max_risk_per_trade)} del "
        "NAV.",
        f"- Ratio beneficio/riesgo mínimo: {format_number(rules.min_reward_risk, 1)}.",
        f"- Un incumplimiento abierto {rules.escalation_days} días o más está escalado.",
        "- Estados, según el drawdown del valor por participación frente a su máximo:",
        f"  - Óptimo: menos del {alerta}.",
        f"  - Alerta: desde el {alerta} hasta menos del {cuidados}; compras permitidas, con "
        f"tope del {pct(rules.max_asset_weight_other)} por activo.",
        f"  - Cuidados intensivos: desde el {cuidados} hasta menos del {bloqueo}; compras "
        "prohibidas (vender siempre se puede).",
        f"  - Bloqueo: {bloqueo} o más; compras prohibidas y revisión completa de la cartera.",
    ])


# -- el contexto del control diario -------------------------------------------------------


@dataclass(frozen=True)
class Move:
    """Lo que ha cambiado el precio de una posición desde el último control."""

    fraction: Decimal
    since: date  # el cierre con el que se compara


@dataclass(frozen=True)
class DailyInputs:
    """Todo lo que usa el control diario, ya leído de la base de datos y valorado."""

    today: date
    valuation: Valuation
    snapshot: NavSnapshot  # la foto de hoy
    held_since: date | None  # valoración no fiable: día del estado y el máximo que se mantienen
    change: DayChange | None
    rules: MandateRules
    findings: tuple[Finding, ...]
    open_breaches: Mapping[tuple[str, str], Breach]
    levels: tuple[LevelCheck, ...]
    with_thesis: frozenset[str]
    #: El cierre con el que se compara cada posición («desde el último control»).
    references: Mapping[str, Price] = field(default_factory=dict)
    last_daily: date | None = None  # el día del último control anterior a hoy
    forced: tuple[Trade, ...] = ()

    def move(self, position: PositionValue) -> Move | None:
        return position_move(position, self.references.get(position.ticker))


def position_move(position: PositionValue, reference: Price | None) -> Move | None:
    """La variación del precio de cotización (sin el tipo de cambio) desde el cierre de
    referencia. None si falta alguno de los dos o no son de la misma divisa."""
    actual = position.price
    if actual is None or reference is None or reference.price <= 0:
        return None
    if actual.currency != reference.currency or actual.price_date <= reference.price_date:
        return None
    return Move(actual.price / reference.price - 1, reference.price_date)


def _quote(price: Price) -> str:
    """«5,12 EUR»."""
    return f"{format_price(price.price)} {price.currency}"


def _pnl(p: PositionValue) -> str:
    if p.pnl_eur is None:
        return "sin medir (a coste)"
    porcentaje = format_pct(p.pnl_pct, signed=True) if p.pnl_pct is not None else "—"
    return f"{format_signed_amount(p.pnl_eur)} € ({porcentaje})"


def _move_text(move: Move | None) -> str:
    if move is None:
        return "sin cierre anterior con el que comparar"
    return f"{format_pct(move.fraction, signed=True)} (frente al cierre del {move.since:%d/%m})"


def _position_line(p: PositionValue, move: Move | None, *, with_move: bool = True) -> str:
    partes = [f"{p.ticker} ({p.name}, {p.sector or 'sin sector'})", f"{format_units(p.units)} u."]
    if p.price is not None:
        partes.append(f"precio {_quote(p.price)} (cierre del {p.price.price_date:%d/%m/%Y})")
    else:
        partes.append("sin precio: se valora a coste")
    partes += [
        f"procedencia {p.source.value}",
        f"valor {format_eur(p.value_eur)}",
        f"peso {format_pct(p.weight)}",
        f"PnL {_pnl(p)}",
    ]
    if with_move:
        partes.append(f"desde el último control {_move_text(move)}")
    if not p.reliable and p.note:
        partes.append(f"NO FIABLE: {p.note}")
    return "- " + " · ".join(partes)


def state_line(inputs: DailyInputs) -> str:
    """El estado del día en una línea: estado, drawdown, patrimonio, efectivo y cobertura."""
    foto, v = inputs.snapshot, inputs.valuation
    partes = [
        STATE_LABELS[foto.state],
        f"drawdown {format_pct(foto.drawdown, truncate=True)}",
        f"patrimonio {format_eur(v.nav_eur)}",
    ]
    if inputs.change is not None:
        partes.append(
            f"{format_signed_amount(inputs.change.amount_eur)} € desde el "
            f"{inputs.change.since:%d/%m} ({format_pct(inputs.change.fraction, 2, signed=True)})"
        )
    partes.append(f"efectivo {format_eur(v.cash_eur)} ({format_pct(v.cash_weight)})")
    cobertura = format_pct(foto.coverage, truncate=True)
    if foto.reliable:
        partes.append(f"cobertura {cobertura}")
    elif inputs.held_since is not None:
        partes.append(
            f"cobertura {cobertura}: valoración NO fiable, se mantienen el estado y el máximo "
            f"del {inputs.held_since:%d/%m/%Y}"
        )
    else:
        partes.append(f"cobertura {cobertura}: valoración NO fiable")
    return " · ".join(partes)


def _state_field(inputs: DailyInputs) -> str:
    foto = inputs.snapshot
    return (
        f"{state_line(inputs)} · valor por participación {format_number(foto.unit_value)} "
        f"(máximo {format_number(foto.high_water_mark)})"
    )


def big_moves(inputs: DailyInputs) -> list[tuple[PositionValue, Move]]:
    """Las posiciones que se han movido ±3 % o más desde el último control, la mayor primero."""
    movidas = []
    for p in inputs.valuation.positions:
        m = inputs.move(p)
        if m is not None and abs(m.fraction) >= DAILY_MOVE_THRESHOLD:
            movidas.append((p, m))
    return sorted(movidas, key=lambda pm: -abs(pm[1].fraction))


def _level_line(c: LevelCheck) -> str:
    if c.status is LevelStatus.STOP:
        return f"- {alert_title(c)}. Salida obligatoria del mandato."
    if c.status is LevelStatus.TARGET:
        return f"- {alert_title(c)}. {proposal_text(c)}"
    if c.status is LevelStatus.UNVERIFIABLE:
        return f"- {c.ticker}: NO VERIFICABLE. {c.note or 'Sin precio fiable.'}"
    if c.status is LevelStatus.NO_LEVELS:
        return f"- {c.ticker}: la tesis no tiene stop ni objetivo."
    partes = [f"precio {format_eur_price(c.price_eur)}"] if c.price_eur is not None else []
    if c.stop_eur is not None and c.price_eur:
        partes.append(
            f"stop {format_eur_price(c.stop_eur)} (a {format_pct(1 - c.stop_eur / c.price_eur)} "
            "por debajo)"
        )
    else:
        partes.append("sin stop")
    if c.target_eur is not None and c.price_eur:
        partes.append(
            f"objetivo {format_eur_price(c.target_eur)} (a "
            f"{format_pct(c.target_eur / c.price_eur - 1)} por encima)"
        )
    else:
        partes.append("sin objetivo")
    return f"- {c.ticker}: entre niveles · " + " · ".join(partes)


def _levels_lines(inputs: DailyInputs) -> list[str]:
    orden = {LevelStatus.STOP: 0, LevelStatus.TARGET: 1, LevelStatus.UNVERIFIABLE: 2}
    lineas = [
        _level_line(c)
        for c in sorted(inputs.levels, key=lambda c: (orden.get(c.status, 3), c.ticker))
    ]
    sin_tesis = [
        p.ticker for p in inputs.valuation.positions if p.ticker not in inputs.with_thesis
    ]
    if sin_tesis:
        lineas.append(f"- Sin tesis (sin stop vigilado): {', '.join(sin_tesis)}.")
    return lineas


def _breach_lines(inputs: DailyInputs) -> list[str]:
    lineas = []
    for h in inputs.findings:
        guardado = inputs.open_breaches.get(h.key)
        dias = days_open(guardado.opened_at, inputs.today) if guardado is not None else 0
        lineas.append(
            f"- {h.title}. {h.correction} · {age_text(dias, is_escalated(dias, inputs.rules))}"
        )
    return lineas


def trade_text(trade: Trade) -> str:
    """«24/09 COMPRA SAN: 100 u. a 5,00 EUR · motivo: …»."""
    texto = (
        f"{trade.trade_date:%d/%m} {trade.kind.value} {trade.ticker}: "
        f"{format_units(trade.units)} u. a {format_price(trade.price)} {trade.currency}"
    )
    if trade.reason:
        texto += f" · motivo: «{trade.reason}»"
    return texto


def _forced_lines(inputs: DailyInputs) -> list[str]:
    return [f"- {trade_text(t)}" for t in inputs.forced]


def _or_none(lineas: Sequence[str], nada: str) -> str:
    return "\n" + "\n".join(lineas) if lineas else nada


def daily_prompt_fields(inputs: DailyInputs) -> dict[str, str]:
    """Los campos de `prompts/diario.md`."""
    posiciones = [_position_line(p, inputs.move(p)) for p in inputs.valuation.positions]
    movidas = [
        f"- {p.ticker} {format_pct(m.fraction, signed=True)} (frente al cierre del "
        f"{m.since:%d/%m})"
        for p, m in big_moves(inputs)
    ]
    return {
        "fecha": f"{inputs.today:%d/%m/%Y}",
        "estado": _state_field(inputs),
        "posiciones": _or_none(posiciones, "ninguna: la cartera es solo efectivo."),
        "umbral": format_limit_pct(DAILY_MOVE_THRESHOLD).removesuffix(" %"),
        "movimientos": _or_none(movidas, "ninguno."),
        "niveles": _or_none(_levels_lines(inputs), "no hay tesis activas."),
        "incumplimientos": _or_none(_breach_lines(inputs), "ninguno: se cumple el mandato."),
        "forzadas": _or_none(_forced_lines(inputs), "ninguna."),
    }


# -- lo que se vigila y la conclusión de Sharky -------------------------------------------


def watch_positions(inputs: DailyInputs) -> tuple[str, ...]:
    """Las posiciones a vigilar (para el semanal): con aviso de nivel, con un movimiento de ±3 %
    o más, o con un incumplimiento propio. Primero los stops."""
    orden: list[str] = []

    def poner(ticker: str) -> None:
        if ticker and ticker not in orden:
            orden.append(ticker)

    for estado in (LevelStatus.STOP, LevelStatus.TARGET, LevelStatus.UNVERIFIABLE):
        for c in inputs.levels:
            if c.status is estado:
                poner(c.ticker)
    for p, _m in big_moves(inputs):
        poner(p.ticker)
    for h in inputs.findings:
        poner(h.ticker)
    return tuple(orden)


def fallback_conclusion(inputs: DailyInputs) -> list[str]:
    """De 1 a 3 viñetas con los datos medidos, por orden de prioridad (sin IA, o si Claude no
    trae la conclusión)."""
    vinetas: list[str] = []
    stops = [c.ticker for c in inputs.levels if c.status is LevelStatus.STOP]
    if stops:
        vinetas.append(
            f"Stop alcanzado en {join_names(stops)}: salida obligatoria; liquida en el bróker y "
            "registra la venta."
        )
    if inputs.findings:
        escalados = []
        for h in inputs.findings:
            guardado = inputs.open_breaches.get(h.key)
            if guardado is not None and is_escalated(
                days_open(guardado.opened_at, inputs.today), inputs.rules
            ):
                escalados.append(h.title)
        titulos = "; ".join(h.title for h in inputs.findings[:3])
        resto = len(inputs.findings) - 3
        if resto > 0:
            titulos += f" y {resto} más"
        texto = f"Incumplimientos del mandato abiertos: {titulos}."
        if escalados:
            texto += f" Escalados: {len(escalados)}."
        vinetas.append(texto)
    objetivos = [c.ticker for c in inputs.levels if c.status is LevelStatus.TARGET]
    if objetivos:
        vinetas.append(
            f"Objetivo alcanzado en {join_names(objetivos)}: no obliga a vender; revisa la "
            "propuesta de subir el stop en Tesis."
        )
    movidas = big_moves(inputs)
    if movidas:
        lista = ", ".join(f"{p.ticker} {format_pct(m.fraction, signed=True)}" for p, m in movidas)
        vinetas.append(f"Movimientos de ±{format_limit_pct(DAILY_MOVE_THRESHOLD)} o más desde el "
                       f"último control: {lista}.")
    if not inputs.snapshot.reliable:
        vinetas.append(
            f"Valoración no fiable (cobertura del "
            f"{format_pct(inputs.snapshot.coverage, truncate=True)}): el estado y el máximo no "
            "cambian hasta tener precios fiables."
        )
    if inputs.forced:
        forzadas = join_names([t.ticker for t in inputs.forced])
        vinetas.append(f"Operaciones forzadas fuera del mandato: {forzadas}.")
    sin_verificar = [c.ticker for c in inputs.levels if c.status is LevelStatus.UNVERIFIABLE]
    if sin_verificar:
        vinetas.append(
            f"Niveles sin verificar por falta de precio fiable: {join_names(sin_verificar)}."
        )
    if not vinetas:
        vinetas.append(
            "Sin stops, objetivos ni incumplimientos, y ninguna posición se ha movido un "
            f"±{format_limit_pct(DAILY_MOVE_THRESHOLD)} o más: nada que vigilar hoy."
        )
    return vinetas[:MAX_FALLBACK_BULLETS]


# -- la conclusión de Claude ----------------------------------------------------------------

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BOLD_LINE = re.compile(r"^\s*(\*\*|__)(.+?)(\*\*|__)\s*:?\s*$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")


def _plain(text: str) -> str:
    """Para comparar títulos: sin tildes, sin mayúsculas, sin adornos ni dos puntos."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[*_`:.]", "", sin_tildes).strip().casefold()


def extract_section(markdown: str, heading: str) -> str | None:
    """El contenido de la sección «heading» (un título de cualquier nivel, o una línea que es
    solo ese texto en negrita), hasta el siguiente título del mismo nivel o superior, una línea
    horizontal o el final. Sin distinguir mayúsculas ni tildes. None si no está o está vacía."""
    buscado = _plain(heading)
    lineas = markdown.splitlines()
    for i, linea in enumerate(lineas):
        titulo = _HEADING.match(linea)
        negrita = _BOLD_LINE.match(linea) if titulo is None else None
        texto = titulo.group(2) if titulo else negrita.group(2) if negrita else None
        if texto is None or _plain(texto) != buscado:
            continue
        nivel = len(titulo.group(1)) if titulo else 7
        contenido = []
        for siguiente in lineas[i + 1:]:
            otro = _HEADING.match(siguiente)
            if (otro and len(otro.group(1)) <= nivel) or _RULE.match(siguiente):
                break
            if otro is None and nivel == 7 and _BOLD_LINE.match(siguiente):
                break
            contenido.append(siguiente)
        resultado = "\n".join(contenido).strip()
        return resultado or None
    return None


def bullets(lines: Iterable[str]) -> str:
    return "\n".join(f"- {linea}" for linea in lines)


# -- el informe completo --------------------------------------------------------------------


def _table_row(celdas: Sequence[str]) -> str:
    return "| " + " | ".join(c.replace("|", "\\|") for c in celdas) + " |"


def data_section(inputs: DailyInputs) -> str:
    """«Datos del día»: las tablas que calcula Sharky, con o sin IA."""
    v = inputs.valuation
    partes = [
        "## Datos del día",
        "",
        "*Calculados por Sharky con los precios guardados. Claude no cambia ninguna cifra.*",
        "",
    ]
    if v.positions:
        partes += [
            _table_row(["Posición", "Precio", "Valor", "Peso", "PnL", "Desde el último control",
                        "Dato"]),
            "|---|---:|---:|---:|---:|---:|---|",
        ]
        for p in v.positions:
            m = inputs.move(p)
            partes.append(_table_row([
                p.ticker,
                _quote(p.price) if p.price is not None else "—",
                format_eur(p.value_eur),
                format_pct(p.weight),
                _pnl(p) if p.pnl_eur is not None else "—",
                format_pct(m.fraction, signed=True) if m is not None else "—",
                p.source.value,
            ]))
        # Sin celdas vacías: el lector de Qt las junta con la de al lado.
        partes.append(_table_row(["Efectivo", "—", format_eur(v.cash_eur),
                                  format_pct(v.cash_weight), "—", "—", "—"]))
    else:
        partes.append(f"Sin posiciones: todo es efectivo ({format_eur(v.cash_eur)}).")
    partes += ["", "**Niveles de las tesis**", ""]
    partes += _levels_lines(inputs) or ["No hay tesis activas."]
    partes += ["", "**Incumplimientos del mandato**", ""]
    partes += _breach_lines(inputs) or ["Ninguno: se cumple el mandato."]
    partes += ["", "**Operaciones forzadas desde el último control**", ""]
    partes += _forced_lines(inputs) or ["Ninguna."]
    return "\n".join(partes)


@dataclass(frozen=True)
class ComposedReport:
    markdown: str
    conclusion: str


def compose_daily(
    inputs: DailyInputs,
    analysis: str | None,
    no_ai_reason: str | None = None,
    truncated: bool = False,
) -> ComposedReport:
    """El Markdown del control diario y su conclusión.

    Con análisis: la línea de estado, el texto de Claude (que termina en «## Conclusión del
    día») y los datos. Sin análisis: la etiqueta «Sin análisis de IA: <motivo>» y la conclusión
    de Sharky. Si Claude no trae la conclusión, se añade la de Sharky, dicho así.
    """
    partes = [f"**Estado:** {state_line(inputs)}", ""]
    texto = (analysis or "").strip()
    conclusion = extract_section(texto, DAILY_CONCLUSION) if texto else None
    if texto:
        partes.append(texto)
        if truncated:
            partes += ["", "> El análisis de Claude se cortó por longitud."]
    else:
        motivo = no_ai_reason or "Claude no ha devuelto texto"
        partes.append(f"> **{NO_AI_LABEL}:** {motivo}")
    if conclusion is None:
        conclusion = bullets(fallback_conclusion(inputs))
        partes += ["", f"## {DAILY_CONCLUSION}", ""]
        if texto:
            partes += ["*Escrita por Sharky con los datos medidos: el análisis de Claude no la "
                       "traía.*", ""]
        partes.append(conclusion)
    partes += ["", "---", "", data_section(inputs)]
    return ComposedReport("\n".join(partes).strip() + "\n", conclusion)


def no_ai_text(reason: str) -> str:
    """«Sin análisis de IA: <motivo>»."""
    return f"{NO_AI_LABEL}: {reason}"


def usage_text(input_tokens: int, output_tokens: int, web_searches: int = 0) -> str:
    """«1.234 tokens de entrada · 567 de salida» (y las búsquedas, si las hay)."""
    texto = (
        f"{format_amount(Decimal(input_tokens), 0)} tokens de entrada · "
        f"{format_amount(Decimal(output_tokens), 0)} de salida"
    )
    if web_searches:
        texto += f" · {web_searches} búsqueda{'s' if web_searches != 1 else ''}"
    return texto


def is_forced_since(trade: Trade, last_daily: date | None) -> bool:
    """Una operación forzada que el diario tiene que mencionar: desde el día del último
    control (incluido: pudo registrarse después de él) o todas si no hubo control."""
    if not trade.forced or trade.kind is TradeKind.OPENING:
        return False
    return last_daily is None or trade.trade_date >= last_daily


# == el semanal de noticias (H10) ===========================================================

#: «Posiciones prioritarias: movimiento de ±7 % o con aviso» (GUIA §5.7).
WEEKLY_MOVE_THRESHOLD = Decimal("0.07")
WEEKLY_CONCLUSION = "Conclusión de la semana"
SOURCES_HEADING = "Fuentes"


@dataclass(frozen=True)
class WeeklyInputs:
    """Lo que usa el semanal: el estado de hoy (lo mismo que ve el diario) y la semana."""

    day: DailyInputs
    since: date
    until: date
    assets: Mapping[str, Asset]
    #: El cierre de hace una semana de cada posición (el último anterior a `since`).
    references: Mapping[str, Price] = field(default_factory=dict)
    #: Los controles diarios de la semana, del más antiguo al más reciente.
    dailies: tuple[Report, ...] = ()

    def move(self, position: PositionValue) -> Move | None:
        return position_move(position, self.references.get(position.ticker))


def weekly_moves(inputs: WeeklyInputs) -> list[tuple[PositionValue, Move]]:
    """Las posiciones que se han movido ±7 % o más en la semana, la mayor primero."""
    movidas = []
    for p in inputs.day.valuation.positions:
        m = inputs.move(p)
        if m is not None and abs(m.fraction) >= WEEKLY_MOVE_THRESHOLD:
            movidas.append((p, m))
    return sorted(movidas, key=lambda pm: -abs(pm[1].fraction))


@dataclass(frozen=True)
class Priority:
    """Una posición que el semanal investiga primero, con sus motivos."""

    ticker: str
    reasons: tuple[str, ...]

    @property
    def text(self) -> str:
        return f"{self.ticker} ({'; '.join(self.reasons)})"


def priorities(inputs: WeeklyInputs) -> tuple[Priority, ...]:
    """Las posiciones prioritarias (GUIA §5.7): con aviso de stop u objetivo hoy, las que se han
    movido ±7 % en la semana y las que los controles diarios de la semana dejaron «a vigilar».
    En ese orden y solo las que siguen en cartera."""
    en_cartera = {p.ticker for p in inputs.day.valuation.positions}
    motivos: dict[str, list[str]] = {}

    def poner(ticker: str, motivo: str) -> None:
        if ticker in en_cartera and motivo not in motivos.setdefault(ticker, []):
            motivos[ticker].append(motivo)

    for estado, texto in ((LevelStatus.STOP, "stop alcanzado"),
                          (LevelStatus.TARGET, "objetivo alcanzado")):
        for c in inputs.day.levels:
            if c.status is estado:
                poner(c.ticker, texto)
    for p, m in weekly_moves(inputs):
        poner(p.ticker, f"{format_pct(m.fraction, signed=True)} en la semana")
    vigilados: dict[str, list[date]] = {}
    for diario in inputs.dailies:
        dia = _report_day(diario)
        for ticker in diario.watch_positions:
            if dia is not None:
                vigilados.setdefault(ticker, []).append(dia)
    for ticker, dias in vigilados.items():
        fechas = join_names([f"{d:%d/%m}" for d in sorted(set(dias))], limit=5)
        poner(ticker, f"a vigilar según {'el control' if len(set(dias)) == 1 else 'los controles'}"
                      f" del {fechas}")
    return tuple(Priority(t, tuple(m)) for t, m in motivos.items())


def _report_day(report: Report) -> date | None:
    return parse_day(report.period)


def _dated_conclusions(reports: Sequence[Report], none: str) -> str:
    """Las conclusiones de unos informes, cada una con su fecha."""
    bloques = []
    for informe in reports:
        dia = _report_day(informe)
        cuando = f"{dia:%d/%m/%Y}" if dia is not None else informe.period
        conclusion = informe.conclusion.strip() or "(sin conclusión)"
        bloques.append(f"{cuando}:\n{conclusion}")
    return "\n" + "\n\n".join(bloques) if bloques else none


def _asset_line(p: PositionValue, asset: Asset | None, move: Move | None) -> str:
    partes = [f"{p.ticker} — {p.name}", pretty_sector(p.sector) if p.sector else "sin sector"]
    if asset is not None and asset.yahoo_symbol:
        partes.append(f"Yahoo {asset.yahoo_symbol}")
    if asset is not None and asset.isin:
        partes.append(f"ISIN {asset.isin}")
    partes.append(f"peso {format_pct(p.weight)}")
    if move is not None:
        partes.append(f"en la semana {format_pct(move.fraction, signed=True)} (frente al cierre "
                      f"del {move.since:%d/%m})")
    else:
        partes.append("en la semana: sin cierre de hace una semana con el que comparar")
    return "- " + " · ".join(partes)


def weekly_prompt_fields(inputs: WeeklyInputs) -> dict[str, str]:
    """Los campos de `prompts/semanal.md`. Los activos van en el orden de la cartera, que es
    el de las secciones del informe."""
    activos = [
        _asset_line(p, inputs.assets.get(p.ticker), inputs.move(p))
        for p in inputs.day.valuation.positions
    ]
    prioritarias = [pr.text for pr in priorities(inputs)]
    return {
        "desde": f"{inputs.since:%d/%m/%Y}",
        "hasta": f"{inputs.until:%d/%m/%Y}",
        "activos": "\n".join(activos) if activos else "(ninguno: la cartera es solo efectivo)",
        "conclusiones_diarias": _dated_conclusions(
            inputs.dailies, "no hay controles diarios en estos días."
        ).lstrip("\n"),
        "prioritarias": (
            "; ".join(prioritarias) + "." if prioritarias
            else "ninguna en especial: sigue el orden de la lista."
        ),
    }


def weekly_fallback_conclusion(inputs: WeeklyInputs) -> list[str]:
    """De 1 a 3 viñetas con los datos medidos (sin IA, o si Claude no trae la conclusión)."""
    vinetas: list[str] = []
    stops = [c.ticker for c in inputs.day.levels if c.status is LevelStatus.STOP]
    if stops:
        vinetas.append(f"Stop alcanzado en {join_names(stops)}: salida obligatoria.")
    movidas = weekly_moves(inputs)
    if movidas:
        lista = ", ".join(f"{p.ticker} {format_pct(m.fraction, signed=True)}" for p, m in movidas)
        vinetas.append(f"Movimientos de ±{format_limit_pct(WEEKLY_MOVE_THRESHOLD)} o más en la "
                       f"semana: {lista}.")
    if inputs.day.findings:
        titulos = "; ".join(h.title for h in inputs.day.findings[:3])
        vinetas.append(f"Incumplimientos del mandato abiertos: {titulos}.")
    objetivos = [c.ticker for c in inputs.day.levels if c.status is LevelStatus.TARGET]
    if objetivos:
        vinetas.append(f"Objetivo alcanzado en {join_names(objetivos)}: revisa la propuesta de "
                       "subir el stop en Tesis.")
    if not vinetas:
        vinetas.append(
            "Sin stops, incumplimientos ni movimientos de "
            f"±{format_limit_pct(WEEKLY_MOVE_THRESHOLD)} en la semana."
        )
    return vinetas[:MAX_FALLBACK_BULLETS]


def strip_preamble(text: str) -> str:
    """Lo que Claude escribe antes del primer título «## » («voy a buscar…» entre búsquedas)
    sobra: el semanal empieza por la sección del primer activo. Sin títulos, el texto entero."""
    lineas = text.splitlines()
    for i, linea in enumerate(lineas):
        titulo = _HEADING.match(linea)
        if titulo is not None and len(titulo.group(1)) <= 2:
            return "\n".join(lineas[i:]).strip()
    return text.strip()


def _link_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def sources_section(sources: Sequence[tuple[str, str]]) -> str:
    """«## Fuentes»: las URL citadas por Claude, con su título."""
    partes = [f"## {SOURCES_HEADING}", ""]
    if not sources:
        partes.append("Claude no ha citado fuentes.")
    for url, titulo in sources:
        destino = f"<{url}>" if any(c in url for c in " ()<>") else url
        partes.append(f"- [{_link_text(titulo)}]({destino})")
    return "\n".join(partes)


def weekly_data_section(inputs: WeeklyInputs) -> str:
    """«Datos de la semana»: las tablas que calcula Sharky, con o sin IA."""
    v = inputs.day.valuation
    partes = [
        "## Datos de la semana",
        "",
        f"*Del {inputs.since:%d/%m} al {inputs.until:%d/%m/%Y}, calculados por Sharky con los "
        "precios guardados. Claude no cambia ninguna cifra.*",
        "",
    ]
    if v.positions:
        partes += [
            _table_row(["Posición", "Sector", "Peso", "En la semana", "Precio", "Dato"]),
            "|---|---|---:|---:|---:|---|",
        ]
        for p in v.positions:
            m = inputs.move(p)
            partes.append(_table_row([
                p.ticker,
                pretty_sector(p.sector) if p.sector else "—",
                format_pct(p.weight),
                format_pct(m.fraction, signed=True) if m is not None else "—",
                _quote(p.price) if p.price is not None else "—",
                p.source.value,
            ]))
        partes += ["", "**Exposición por sector**", "",
                   _table_row(["Sector", "Peso", "Tope", "Posiciones"]), "|---|---:|---:|---|"]
        tope = inputs.day.rules.max_sector_weight
        for s in v.sectors:
            peso = format_pct(s.weight)
            if s.exceeds(tope):
                peso += " (supera el tope)"
            partes.append(_table_row([pretty_sector(s.sector), peso, format_limit_pct(tope),
                                      ", ".join(s.tickers)]))
    else:
        partes.append(f"Sin posiciones: todo es efectivo ({format_eur(v.cash_eur)}).")
    partes += ["", "**Posiciones prioritarias**", ""]
    partes += [f"- {pr.text}" for pr in priorities(inputs)] or ["Ninguna."]
    partes += ["", "**Controles diarios de la semana**", ""]
    dias = [d for d in (_report_day(r) for r in inputs.dailies) if d is not None]
    partes.append(
        f"{len(dias)}: {join_names([f'{d:%d/%m}' for d in dias], limit=7)}." if dias
        else "Ninguno."
    )
    return "\n".join(partes)


def compose_weekly(
    inputs: WeeklyInputs,
    analysis: str | None,
    sources: Sequence[tuple[str, str]] = (),
    no_ai_reason: str | None = None,
    incomplete: str | None = None,
) -> ComposedReport:
    """El Markdown del semanal y su conclusión.

    Con análisis: la línea de estado, las secciones de Claude (que terminan en «## Conclusión
    de la semana»), las fuentes que citó y los datos. Sin análisis: la etiqueta «Sin análisis
    de IA: <motivo>» y la conclusión de Sharky. `incomplete` explica por qué el texto puede
    estar a medias (cortado o en pausa).
    """
    partes = [f"**Estado:** {state_line(inputs.day)}", ""]
    texto = strip_preamble(analysis or "")
    conclusion = extract_section(texto, WEEKLY_CONCLUSION) if texto else None
    if texto:
        partes.append(texto)
        if incomplete:
            partes += ["", f"> {incomplete}"]
    else:
        partes.append(f"> **{NO_AI_LABEL}:** {no_ai_reason or 'Claude no ha devuelto texto'}")
    if conclusion is None:
        conclusion = bullets(weekly_fallback_conclusion(inputs))
        partes += ["", f"## {WEEKLY_CONCLUSION}", ""]
        if texto:
            partes += ["*Escrita por Sharky con los datos medidos: el análisis de Claude no la "
                       "traía.*", ""]
        partes.append(conclusion)
    if texto:
        partes += ["", sources_section(sources)]
    partes += ["", "---", "", weekly_data_section(inputs)]
    return ComposedReport("\n".join(partes).strip() + "\n", conclusion)


def weekly_analysis(report: Report) -> str:
    """Lo que escribió Claude en un semanal guardado, sin la línea de estado, sin «Fuentes» y
    sin los datos (para el contexto del mensual). Sin IA, su conclusión."""
    if not report.used_ai:
        return f"(sin análisis de IA)\n{report.conclusion.strip()}"
    lineas = report.markdown.splitlines()
    inicio = 0
    while inicio < len(lineas) and lineas[inicio].strip():
        inicio += 1  # el párrafo de la línea de estado
    fin = len(lineas)
    for i in range(len(lineas) - 1, inicio - 1, -1):
        if lineas[i].strip() == f"## {SOURCES_HEADING}":
            fin = i
            break
    return "\n".join(lineas[inicio:fin]).strip()


# == el estudio mensual (H10) ===============================================================

MONTHLY_CONCLUSION = "Conclusión del mes"


class Verdict(StrEnum):
    """El veredicto de una posición en el estudio mensual (GUIA §5.7)."""

    HOLD = "MANTENER"
    REDUCE = "REDUCIR"
    ADD = "AMPLIAR"
    CLOSE = "CERRAR"


@dataclass(frozen=True)
class ThesisContext:
    """Una tesis activa con su última revisión de Claude y sus propuestas pendientes."""

    thesis: Thesis
    last_review: ThesisEvent | None = None
    pending: tuple[ThesisEvent, ...] = ()


@dataclass(frozen=True)
class MonthChange:
    """Lo que ha cambiado el valor por participación en el mes (fotos fiables)."""

    start: NavSnapshot
    end: NavSnapshot

    @property
    def fraction(self) -> Decimal:
        return self.end.unit_value / self.start.unit_value - 1

    @property
    def text(self) -> str:
        return (
            f"valor por participación {format_number(self.start.unit_value)} el "
            f"{self.start.snapshot_date:%d/%m} → {format_number(self.end.unit_value)} el "
            f"{self.end.snapshot_date:%d/%m} ({format_pct(self.fraction, signed=True)})"
        )


def month_change(snapshots: Iterable[NavSnapshot], month: Month, until: date
                 ) -> MonthChange | None:
    """De la última foto fiable antes del mes (o la primera del mes, si la cartera empezó en
    él) a la última fiable del mes hasta `until`. None si no hay dos fotos distintas."""
    fotos = [s for s in snapshots if s.reliable]
    fin = last_reliable(fotos, until=min(month.last_day, until))
    inicio = last_reliable(fotos, before=month.first_day)
    if inicio is None:
        del_mes = [s for s in fotos if month.first_day <= s.snapshot_date <= month.last_day]
        inicio = min(del_mes, key=lambda s: s.snapshot_date, default=None)
    if inicio is None or fin is None or fin.snapshot_date <= inicio.snapshot_date:
        return None
    if inicio.unit_value <= 0:
        return None
    return MonthChange(inicio, fin)


@dataclass(frozen=True)
class MonthlyInputs:
    """Lo que usa el estudio mensual."""

    day: DailyInputs  # el estado de hoy
    month: Month
    partial: bool  # el mes en curso, a mano
    theses: tuple[ThesisContext, ...] = ()
    trades: tuple[Trade, ...] = ()  # las del mes, sin las APERTURA
    dailies: tuple[Report, ...] = ()
    weeklies: tuple[Report, ...] = ()
    previous: Report | None = None  # el estudio del mes anterior
    change: MonthChange | None = None

    @property
    def title(self) -> str:
        """«agosto de 2026» o «septiembre de 2026 (parcial: hasta el 26/09/2026)»."""
        if self.partial:
            return f"{self.month.label} (parcial: hasta el {self.day.today:%d/%m/%Y})"
        return self.month.label

    def thesis_for(self, ticker: str) -> Thesis | None:
        return next((c.thesis for c in self.theses if c.thesis.ticker == ticker), None)


def _levels_line(thesis: Thesis) -> str:
    return levels_summary(levels_of(thesis)) or f"sin niveles (en {thesis.levels_currency})"


def _thesis_block(ctx: ThesisContext) -> str:
    t = ctx.thesis
    lineas = [f"- {t.ticker} (niveles en {t.levels_currency}): {_levels_line(t)}"]
    for nombre, texto in (("Por qué", t.why), ("Catalizadores", t.catalysts),
                          ("Riesgos", t.risks), ("Qué la invalidaría", t.invalidation)):
        if texto.strip():
            lineas.append(f"  {nombre}: {' '.join(texto.split())}")
    if ctx.last_review is not None:
        lineas.append(f"  Última revisión ({ctx.last_review.created_at:%d/%m/%Y}): "
                      f"{' '.join(ctx.last_review.text.split())}")
    else:
        lineas.append("  Última revisión: ninguna todavía.")
    for p in ctx.pending:
        quien = "Sharky" if p.author is Author.SYSTEM else "Claude"
        lineas.append(f"  Propuesta de {quien} sin aplicar ({p.created_at:%d/%m}): "
                      f"{levels_summary(from_json(p.new_value))}")
    return "\n".join(lineas)


def _theses_field(inputs: MonthlyInputs) -> str:
    bloques = [_thesis_block(c) for c in inputs.theses]
    con_tesis = {c.thesis.ticker for c in inputs.theses}
    sin_tesis = [p.ticker for p in inputs.day.valuation.positions if p.ticker not in con_tesis]
    if sin_tesis:
        bloques.append(f"- Posiciones sin tesis: {', '.join(sin_tesis)}.")
    return "\n" + "\n".join(bloques) if bloques else "no hay tesis activas."


def _trade_lines(trades: Sequence[Trade]) -> list[str]:
    lineas = []
    for t in trades:
        linea = f"- {trade_text(t)}"
        if t.forced:
            linea += " · forzada fuera del mandato"
        lineas.append(linea)
    return lineas


def _valuation_field(inputs: MonthlyInputs) -> str:
    lineas = [_state_field(inputs.day)]
    if inputs.change is not None:
        lineas.append(f"En el mes: {inputs.change.text}.")
    lineas += [_position_line(p, None, with_move=False) for p in inputs.day.valuation.positions]
    if not inputs.day.valuation.positions:
        lineas.append("Sin posiciones: la cartera es solo efectivo.")
    return "\n".join(lineas)


def monthly_prompt_fields(inputs: MonthlyInputs) -> dict[str, str]:
    """Los campos de `prompts/mensual.md`."""
    semanales = []
    for s in inputs.weeklies:
        dia = _report_day(s)
        titulo = (f"Semana del {dia - timedelta(days=6):%d/%m} al {dia:%d/%m}"
                  if dia is not None else s.period)
        semanales.append(f"### {titulo}\n{weekly_analysis(s)}")
    anterior = inputs.previous.conclusion.strip() if inputs.previous is not None else ""
    return {
        "mes": inputs.title,
        "valoracion": "\n" + _valuation_field(inputs),
        "incumplimientos": _or_none(_breach_lines(inputs.day), "ninguno: se cumple el mandato."),
        "tesis": _theses_field(inputs),
        "operaciones": _or_none(_trade_lines(inputs.trades), "ninguna."),
        "conclusiones_diarias": _dated_conclusions(
            inputs.dailies, "no hay controles diarios este mes."
        ),
        "semanales": "\n" + "\n\n".join(semanales) if semanales
        else "no hay informes semanales este mes.",
        "conclusion_anterior": ("\n" + anterior) if anterior
        else "no hay: es el primer estudio mensual.",
    }


def extraction_tickers(inputs: MonthlyInputs) -> str:
    """El campo `$tickers` del paso B: cada posición con la divisa de los niveles de su tesis."""
    partes = []
    for p in inputs.day.valuation.positions:
        tesis = inputs.thesis_for(p.ticker)
        partes.append(f"{p.ticker} (niveles en {tesis.levels_currency})" if tesis is not None
                      else f"{p.ticker} (sin tesis)")
    return ", ".join(partes)


# -- los veredictos --------------------------------------------------------------------------


@dataclass(frozen=True)
class RawVerdict:
    """Un veredicto tal como sale de la extracción, sin validar."""

    ticker: str
    verdict: str
    reason: str = ""
    invalidation: str = ""
    stop: Decimal | None = None
    target: Decimal | None = None


@dataclass(frozen=True)
class PositionVerdict:
    """Un veredicto válido. `stop` y `target` son propuestas en la divisa de los niveles de la
    tesis; `dropped` dice por qué se descartaron las cifras que traía, si fue así."""

    ticker: str
    verdict: Verdict
    reason: str
    invalidation: str
    stop: Decimal | None = None
    target: Decimal | None = None
    dropped: str = ""


@dataclass(frozen=True)
class RejectedVerdict:
    """Lo que llegó de la extracción y no vale, con el motivo."""

    ticker: str
    verdict: str
    reason: str

    @property
    def text(self) -> str:
        return f"«{self.verdict or '—'}» para «{self.ticker or '—'}»: {self.reason}"


@dataclass(frozen=True)
class VerdictCheck:
    verdicts: tuple[PositionVerdict, ...]
    rejected: tuple[RejectedVerdict, ...] = ()
    missing: tuple[str, ...] = ()  # posiciones sin veredicto válido

    def verdict_for(self, ticker: str) -> PositionVerdict | None:
        return next((v for v in self.verdicts if v.ticker == ticker), None)


def parse_verdict(text: str) -> Verdict | None:
    """El veredicto sin distinguir mayúsculas (ni adornos de Markdown alrededor)."""
    limpio = text.strip().strip("*_`.«»\"' ").upper()
    try:
        return Verdict(limpio)
    except ValueError:
        return None


def _clean_numbers(raw: RawVerdict, thesis: Thesis | None) -> tuple[Decimal | None,
                                                                    Decimal | None, str]:
    """Las cifras propuestas que valen y, si se descarta alguna, por qué."""
    stop, objetivo, motivos = raw.stop, raw.target, []
    if stop is not None and stop <= 0:
        stop = None
        motivos.append("el stop propuesto no es mayor que 0")
    if objetivo is not None and objetivo <= 0:
        objetivo = None
        motivos.append("el objetivo propuesto no es mayor que 0")
    stop_final = stop if stop is not None else (thesis.stop if thesis else None)
    objetivo_final = objetivo if objetivo is not None else (thesis.target if thesis else None)
    if (
        (stop is not None or objetivo is not None)
        and stop_final is not None
        and objetivo_final is not None
        and stop_final >= objetivo_final
    ):
        stop = objetivo = None
        motivos.append("el stop no quedaría por debajo del objetivo")
    return stop, objetivo, "; ".join(motivos)


def check_verdicts(
    raw: Sequence[RawVerdict], positions: Sequence[str], theses: Mapping[str, Thesis]
) -> VerdictCheck:
    """Valida lo extraído (GUIA §5.7): el veredicto se compara sin distinguir mayúsculas y tiene
    que ser uno de los cuatro; el ticker, una posición de la cartera y sin repetir. Las cifras
    que no valen (≤ 0, o un stop que no queda por debajo del objetivo) se quitan, pero el
    veredicto se queda."""
    por_nombre = {t.casefold(): t for t in positions}
    validos: list[PositionVerdict] = []
    rechazados: list[RejectedVerdict] = []
    vistos: set[str] = set()
    for r in raw:
        ticker = por_nombre.get(r.ticker.strip().casefold())
        veredicto = parse_verdict(r.verdict)
        if ticker is None:
            rechazados.append(RejectedVerdict(r.ticker, r.verdict,
                                              "no es una posición de la cartera."))
            continue
        if ticker in vistos:
            rechazados.append(RejectedVerdict(r.ticker, r.verdict,
                                              "veredicto repetido: vale el primero."))
            continue
        if veredicto is None:
            rechazados.append(RejectedVerdict(
                r.ticker, r.verdict, "no es MANTENER, REDUCIR, AMPLIAR ni CERRAR."
            ))
            continue
        vistos.add(ticker)
        stop, objetivo, descartadas = _clean_numbers(r, theses.get(ticker))
        validos.append(PositionVerdict(ticker, veredicto, " ".join(r.reason.split()),
                                       " ".join(r.invalidation.split()), stop, objetivo,
                                       descartadas))
    faltan = tuple(t for t in positions if t not in vistos)
    return VerdictCheck(tuple(validos), tuple(rechazados), faltan)


def _verdict_text(v: PositionVerdict, month_title: str) -> str:
    lineas = [f"Estudio mensual de {month_title}: {v.verdict.value}."]
    if v.reason:
        lineas.append(f"Motivo: {v.reason}")
    if v.invalidation:
        lineas.append(f"Qué lo invalidaría: {v.invalidation}")
    return "\n".join(lineas)


def verdict_events(
    v: PositionVerdict, thesis: Thesis, now: datetime, month_title: str, period: str
) -> tuple[ThesisEvent, ...]:
    """Lo que el veredicto añade al historial de su tesis (GUIA §5.7): una REVISIÓN de Claude
    y, si propone un stop o un objetivo distinto del vigente, una PROPUESTA de Claude «no
    aplicada». Ningún número de la tesis cambia: eso solo lo hace el usuario con «Aplicar»."""
    if thesis.id is None:
        return ()
    eventos = [ThesisEvent(
        thesis.id, now, ThesisEventKind.REVIEW, Author.CLAUDE, _verdict_text(v, month_title),
        new_value=to_json({"veredicto": v.verdict.value, "estudio": period}),
    )]
    antes: dict[str, str | None] = {}
    despues: dict[str, str | None] = {}
    for clave, propuesto, vigente in (("stop", v.stop, thesis.stop),
                                      ("objetivo", v.target, thesis.target)):
        if propuesto is not None and propuesto != vigente:
            antes[clave] = str(vigente) if vigente is not None else None
            despues[clave] = str(propuesto)
    if despues:
        texto = f"Estudio mensual de {month_title} · {v.verdict.value}."
        if v.reason:
            texto += f" {v.reason}"
        eventos.append(ThesisEvent(
            thesis.id, now, ThesisEventKind.PROPOSAL, Author.CLAUDE, texto,
            old_value=to_json(antes),
            new_value=to_json({**despues, "divisa": thesis.levels_currency}),
        ))
    return tuple(eventos)


@dataclass(frozen=True)
class VerdictsOutcome:
    """Lo que ha dado el paso B, para el informe: los veredictos o por qué no los hay, y qué se
    añade a cada tesis."""

    check: VerdictCheck | None = None
    unavailable: str | None = None  # motivo, si no hay veredictos
    events: Mapping[str, tuple[ThesisEvent, ...]] = field(default_factory=dict)


def _number_or_dash(value: Decimal | None, currency: str | None) -> str:
    if value is None:
        return "—"
    return f"{format_price(value)} {currency}" if currency else format_price(value)


def verdicts_section(inputs: MonthlyInputs, outcome: VerdictsOutcome) -> str:
    """«Veredictos por posición»: lo extraído del estudio, validado por Sharky."""
    partes = ["## Veredictos por posición", ""]
    if not inputs.day.valuation.positions:
        partes.append("Sin posiciones: no hay veredictos.")
        return "\n".join(partes)
    if outcome.check is None:
        motivo = outcome.unavailable or "no se han extraído."
        partes.append(f"Veredictos no disponibles: {motivo}")
        return "\n".join(partes)
    partes += [
        "*Extraídos del estudio y validados por Sharky. Las cifras son propuestas: solo cambian "
        "la tesis si las aplicas en Tesis.*",
        "",
        _table_row(["Posición", "Veredicto", "Stop propuesto", "Objetivo propuesto",
                    "En la tesis"]),
        "|---|---|---:|---:|---|",
    ]
    for p in inputs.day.valuation.positions:
        v = outcome.check.verdict_for(p.ticker)
        tesis = inputs.thesis_for(p.ticker)
        divisa = tesis.levels_currency if tesis is not None else None
        if v is None:
            partes.append(_table_row([p.ticker, "no disponible", "—", "—", "—"]))
            continue
        eventos = outcome.events.get(p.ticker, ())
        if tesis is None:
            destino = "sin tesis activa: solo aquí"
        elif any(e.kind is ThesisEventKind.PROPOSAL for e in eventos):
            destino = "revisión y propuesta añadidas"
        else:
            destino = "revisión añadida"
        partes.append(_table_row([
            p.ticker, v.verdict.value, _number_or_dash(v.stop, divisa),
            _number_or_dash(v.target, divisa), destino,
        ]))
    motivos = []
    for v in outcome.check.verdicts:
        linea = f"- **{v.ticker} · {v.verdict.value}.** {v.reason or 'Sin motivo.'}"
        if v.invalidation:
            linea += f" *Qué lo invalidaría:* {v.invalidation}"
        if v.dropped:
            linea += f" (Cifras descartadas: {v.dropped}.)"
        motivos.append(linea)
    if motivos:
        partes += ["", "**Motivos**", ""] + motivos
    if outcome.check.rejected:
        partes += ["", "**Descartados por Sharky**", ""]
        partes += [f"- {r.text}" for r in outcome.check.rejected]
    return "\n".join(partes)


def monthly_fallback_conclusion(inputs: MonthlyInputs) -> list[str]:
    """De 1 a 3 viñetas con los datos medidos (sin IA, o si Claude no trae la conclusión)."""
    vinetas = [f"Estado a {inputs.day.today:%d/%m}: {state_line(inputs.day)}."]
    if inputs.change is not None:
        vinetas.append(f"En el mes: {inputs.change.text}.")
    if inputs.day.findings:
        titulos = "; ".join(h.title for h in inputs.day.findings[:3])
        vinetas.append(f"Incumplimientos del mandato abiertos: {titulos}.")
    else:
        vinetas.append(f"Operaciones del mes: {len(inputs.trades)}.")
    return vinetas[:MAX_FALLBACK_BULLETS]


def monthly_data_section(inputs: MonthlyInputs) -> str:
    """«Datos del mes»: las tablas que calcula Sharky, con o sin IA."""
    v = inputs.day.valuation
    partes = [
        "## Datos del mes",
        "",
        "*Calculados por Sharky con los precios guardados. Claude no cambia ninguna cifra.*",
        "",
    ]
    if inputs.change is not None:
        partes += [f"En el mes: {inputs.change.text}.", ""]
    if v.positions:
        partes += [
            _table_row(["Posición", "Precio", "Valor", "Peso", "PnL", "Dato"]),
            "|---|---:|---:|---:|---:|---|",
        ]
        for p in v.positions:
            partes.append(_table_row([
                p.ticker,
                _quote(p.price) if p.price is not None else "—",
                format_eur(p.value_eur),
                format_pct(p.weight),
                _pnl(p) if p.pnl_eur is not None else "—",
                p.source.value,
            ]))
        partes.append(_table_row(["Efectivo", "—", format_eur(v.cash_eur),
                                  format_pct(v.cash_weight), "—", "—"]))
    else:
        partes.append(f"Sin posiciones: todo es efectivo ({format_eur(v.cash_eur)}).")
    partes += ["", "**Incumplimientos del mandato**", ""]
    partes += _breach_lines(inputs.day) or ["Ninguno: se cumple el mandato."]
    partes += ["", "**Operaciones del mes**", ""]
    partes += _trade_lines(inputs.trades) or ["Ninguna."]
    partes += ["", "**Informes que ha releído**", "",
               f"Controles diarios: {len(inputs.dailies)} · semanales: {len(inputs.weeklies)} · "
               f"estudio anterior: {'sí' if inputs.previous is not None else 'no'}."]
    return "\n".join(partes)


def compose_monthly(
    inputs: MonthlyInputs,
    analysis: str | None,
    verdicts: VerdictsOutcome,
    no_ai_reason: str | None = None,
    incomplete: str | None = None,
) -> ComposedReport:
    """El Markdown del estudio mensual y su conclusión: el estudio de Claude (que termina en
    «## Conclusión del mes»), los veredictos por posición y los datos del mes. Sin análisis,
    la etiqueta «Sin análisis de IA: <motivo>» y la conclusión de Sharky."""
    partes = [f"**Estado:** {state_line(inputs.day)}", ""]
    if inputs.partial:
        partes += [f"*Estudio parcial del mes en curso, hasta el {inputs.day.today:%d/%m/%Y}.*",
                   ""]
    texto = (analysis or "").strip()
    conclusion = extract_section(texto, MONTHLY_CONCLUSION) if texto else None
    if texto:
        partes.append(texto)
        if incomplete:
            partes += ["", f"> {incomplete}"]
    else:
        partes.append(f"> **{NO_AI_LABEL}:** {no_ai_reason or 'Claude no ha devuelto texto'}")
    if conclusion is None:
        conclusion = bullets(monthly_fallback_conclusion(inputs))
        partes += ["", f"## {MONTHLY_CONCLUSION}", ""]
        if texto:
            partes += ["*Escrita por Sharky con los datos medidos: el análisis de Claude no la "
                       "traía.*", ""]
        partes.append(conclusion)
    if texto:
        partes += ["", verdicts_section(inputs, verdicts)]
    partes += ["", "---", "", monthly_data_section(inputs)]
    return ComposedReport("\n".join(partes).strip() + "\n", conclusion)
