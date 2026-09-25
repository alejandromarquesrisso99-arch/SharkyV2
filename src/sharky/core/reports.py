"""Informes con Claude (GUIA §5.7): todo lo que calcula el código, sin red ni disco.

- **Contexto del control diario.** Solo hoy: estado, patrimonio, posiciones (precio, peso, PnL y
  variación desde el último control), niveles de las tesis, incumplimientos y operaciones
  forzadas. Son los campos del prompt `diario.md`.
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
from datetime import date
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
)
from sharky.core.levels import (
    LevelCheck,
    LevelStatus,
    alert_title,
    format_eur_price,
    proposal_text,
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
)
from sharky.core.models import Breach, NavSnapshot, Price, Trade, TradeKind
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


def _position_line(p: PositionValue, move: Move | None) -> str:
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
        f"desde el último control {_move_text(move)}",
    ]
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
