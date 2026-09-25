"""El mandato de riesgo (GUIA §5.4) y la validación de las operaciones (GUIA §5.5).

Pura: recibe la valoración, las fotos del NAV guardadas, los movimientos de efectivo y las
reglas; no lee la base de datos, ni los ajustes, ni el reloj.

**Participaciones.** El drawdown se mide como en un fondo: sobre el valor por participación
frente a su máximo, que empieza en 100. Ingresos y retiradas cambian el número de
participaciones, no su valor: meter o sacar dinero no es ganar ni perder. Dividendos,
intereses, comisiones, impuestos y ajustes sí cuentan, porque no son dinero que entre o salga
de la cartera sino lo que la cartera gana o pierde.

Cada foto parte de la última anterior a su día (la base). Los ingresos y retiradas fechados
después de la base (F) se valoran al valor por participación de hoy sin ellos:

    valor = (NAV − F) / participaciones de la base
    participaciones = participaciones de la base + F / valor

Mientras no haya ninguna foto fiable (la primera, la del asistente, suele valorar a coste), cada
foto vuelve a fijar el valor en 100: así lo ganado o perdido antes de Sharky no cuenta como
drawdown desde el primer día.

**Estados.** El estado y el máximo solo se actualizan con una valoración fiable (cobertura del
90 % o más). Si no, las participaciones siguen a los ingresos y retiradas, pero el máximo, el
drawdown y el estado son los de la última foto fiable.

Las fronteras son exactas: un drawdown de 0,0299 es Óptimo y uno de 0,03 es Alerta. Nada se
redondea antes de comparar.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from enum import StrEnum

from sharky.core.formatting import (
    format_eur,
    format_limit_pct,
    format_number,
    format_pct,
    format_price,
    format_units,
    pretty_sector,
)
from sharky.core.models import CashKind, CashMovement, MandateState, NavSnapshot
from sharky.core.valuation import NO_SECTOR, Valuation

ZERO = Decimal("0")
ONE = Decimal("1")

#: El valor por participación el primer día.
INITIAL_UNIT_VALUE = Decimal("100")

#: Drawdown desde el que empieza cada estado (GUIA §5.4).
ALERT_DRAWDOWN = Decimal("0.03")
INTENSIVE_CARE_DRAWDOWN = Decimal("0.08")
LOCKDOWN_DRAWDOWN = Decimal("0.20")

#: Movimientos que cambian las participaciones y no su valor.
FLOW_KINDS = frozenset({CashKind.DEPOSIT, CashKind.WITHDRAWAL})

STATE_LABELS: dict[MandateState, str] = {
    MandateState.OPTIMAL: "Óptimo",
    MandateState.ALERT: "Alerta",
    MandateState.INTENSIVE_CARE: "Cuidados intensivos",
    MandateState.LOCKDOWN: "Bloqueo",
}

#: El color de estado de cada estado del mandato (verde, ámbar o rojo). Siempre va con su
#: etiqueta, nunca solo el color.
STATE_TOKENS: dict[MandateState, str] = {
    MandateState.OPTIMAL: "ok",
    MandateState.ALERT: "warn",
    MandateState.INTENSIVE_CARE: "danger",
    MandateState.LOCKDOWN: "danger",
}


# -- reglas -----------------------------------------------------------------------------


@dataclass(frozen=True)
class MandateRules:
    """Las reglas del mandato, como fracciones (0.10 es un 10 %).

    Salen de los ajustes, que son la única fuente: aquí no hay valores por defecto.
    """

    max_asset_weight_optimal: Decimal
    max_asset_weight_other: Decimal
    max_sector_weight: Decimal
    min_cash_optimal: Decimal
    max_cash_optimal: Decimal
    min_cash_other: Decimal
    max_risk_per_trade: Decimal
    min_reward_risk: Decimal
    escalation_days: int

    def max_asset_weight(self, state: MandateState) -> Decimal:
        """10 % en Óptimo; 5 % en los demás estados (por defecto)."""
        if state is MandateState.OPTIMAL:
            return self.max_asset_weight_optimal
        return self.max_asset_weight_other

    def min_cash(self, state: MandateState) -> Decimal:
        if state is MandateState.OPTIMAL:
            return self.min_cash_optimal
        return self.min_cash_other

    def max_cash(self, state: MandateState) -> Decimal | None:
        """Solo Óptimo tiene techo de efectivo; en los demás estados, cuanto más, mejor."""
        return self.max_cash_optimal if state is MandateState.OPTIMAL else None


# -- estados ----------------------------------------------------------------------------


def state_for(drawdown: Decimal) -> MandateState:
    """El estado que corresponde a un drawdown (fracción), con las fronteras exactas."""
    if drawdown >= LOCKDOWN_DRAWDOWN:
        return MandateState.LOCKDOWN
    if drawdown >= INTENSIVE_CARE_DRAWDOWN:
        return MandateState.INTENSIVE_CARE
    if drawdown >= ALERT_DRAWDOWN:
        return MandateState.ALERT
    return MandateState.OPTIMAL


def buying_allowed(state: MandateState) -> bool:
    """En Cuidados intensivos y en Bloqueo no se compra (vender siempre se puede)."""
    return state in (MandateState.OPTIMAL, MandateState.ALERT)


def drawdown_of(unit_value: Decimal, high_water_mark: Decimal) -> Decimal:
    """Caída del valor por participación desde su máximo, como fracción."""
    if high_water_mark <= 0 or unit_value >= high_water_mark:
        return ZERO
    return (high_water_mark - unit_value) / high_water_mark


def state_limits_text(state: MandateState, rules: MandateRules) -> str:
    """Lo que el estado permite, en una línea."""
    tope = format_limit_pct(rules.max_asset_weight(state))
    minimo = format_limit_pct(rules.min_cash(state))
    if state is MandateState.OPTIMAL:
        maximo = format_limit_pct(rules.max_cash_optimal)
        return f"Tope por activo {tope} · efectivo entre {minimo} y {maximo}"
    if state is MandateState.ALERT:
        return f"Tope por activo {tope} · efectivo mínimo {minimo} mientras dure la alerta"
    if state is MandateState.INTENSIVE_CARE:
        return f"Compras prohibidas; vender siempre se puede · efectivo mínimo {minimo}"
    return "Compras prohibidas · toca una revisión completa de la cartera"


# -- la foto del NAV --------------------------------------------------------------------


def external_flows(
    movements: Iterable[CashMovement], after: date | None, until: date
) -> Decimal:
    """Ingresos menos retiradas fechados después de `after` (sin límite si es None) y hasta
    `until`, incluido."""
    return sum(
        (
            m.amount_eur
            for m in movements
            if m.kind in FLOW_KINDS
            and m.movement_date <= until
            and (after is None or m.movement_date > after)
        ),
        ZERO,
    )


def last_reliable(
    snapshots: Iterable[NavSnapshot], *, before: date | None = None, until: date | None = None
) -> NavSnapshot | None:
    """La última foto fiable anterior a `before` o, si se da `until`, de ese día o antes."""
    candidatas = [
        s
        for s in snapshots
        if s.reliable
        and (before is None or s.snapshot_date < before)
        and (until is None or s.snapshot_date <= until)
    ]
    return max(candidatas, key=lambda s: s.snapshot_date, default=None)


def snapshot_for(
    day: date,
    valuation: Valuation,
    snapshots: Sequence[NavSnapshot],
    movements: Iterable[CashMovement],
) -> NavSnapshot:
    """La foto del NAV de `day` con esta valoración.

    `snapshots` son las fotos guardadas (puede incluir una de ese mismo día, que esta
    sustituiría); `movements`, todos los movimientos de efectivo.
    """
    nav = valuation.nav_eur
    fiable = valuation.reliable
    anteriores = [s for s in snapshots if s.snapshot_date < day]
    base = max(anteriores, key=lambda s: s.snapshot_date, default=None)
    ancla = last_reliable(anteriores)

    if base is None or ancla is None:
        # Todavía no hay ninguna foto fiable: el valor vuelve a empezar en 100.
        return NavSnapshot(
            snapshot_date=day,
            nav_eur=nav,
            cash_eur=valuation.cash_eur,
            fund_units=nav / INITIAL_UNIT_VALUE if nav > 0 else ZERO,
            unit_value=INITIAL_UNIT_VALUE,
            high_water_mark=INITIAL_UNIT_VALUE,
            drawdown=ZERO,
            state=MandateState.OPTIMAL,
            coverage=valuation.coverage,
            reliable=fiable,
        )

    flujos = external_flows(movements, base.snapshot_date, day)
    sin_flujos = nav - flujos
    if base.fund_units > 0 and sin_flujos > 0:
        valor = sin_flujos / base.fund_units
    else:
        valor = base.unit_value  # no queda nada que medir: el valor se conserva
    participaciones = base.fund_units + flujos / valor if valor > 0 else ZERO
    if participaciones < 0:
        participaciones = ZERO

    if fiable:
        maximo = max(ancla.high_water_mark, valor)
        caida = drawdown_of(valor, maximo)
        estado = state_for(caida)
    else:
        referencia = last_reliable(snapshots, until=day) or ancla
        maximo, caida, estado = (
            referencia.high_water_mark,
            referencia.drawdown,
            referencia.state,
        )

    return NavSnapshot(
        snapshot_date=day,
        nav_eur=nav,
        cash_eur=valuation.cash_eur,
        fund_units=participaciones,
        unit_value=valor,
        high_water_mark=maximo,
        drawdown=caida,
        state=estado,
        coverage=valuation.coverage,
        reliable=fiable,
    )


def held_since(snapshot: NavSnapshot, snapshots: Iterable[NavSnapshot]) -> date | None:
    """Si la foto no es fiable, el día de la foto fiable de la que salen su estado y su
    máximo. None si es fiable (o si nunca ha habido una fiable)."""
    if snapshot.reliable:
        return None
    referencia = last_reliable(snapshots, until=snapshot.snapshot_date)
    return referencia.snapshot_date if referencia is not None else None


@dataclass(frozen=True)
class DayChange:
    """Lo que ha cambiado el patrimonio desde la última foto fiable anterior, sin contar los
    ingresos ni las retiradas."""

    amount_eur: Decimal
    fraction: Decimal  # variación del valor por participación
    since: date


def day_change(
    current: NavSnapshot,
    snapshots: Iterable[NavSnapshot],
    movements: Iterable[CashMovement],
) -> DayChange | None:
    """La variación desde la última foto fiable anterior. None si la de ahora no es fiable o
    no hay con qué comparar."""
    if not current.reliable:
        return None
    anterior = last_reliable(snapshots, before=current.snapshot_date)
    if anterior is None or anterior.unit_value <= 0:
        return None
    flujos = external_flows(movements, anterior.snapshot_date, current.snapshot_date)
    return DayChange(
        amount_eur=current.nav_eur - anterior.nav_eur - flujos,
        fraction=current.unit_value / anterior.unit_value - ONE,
        since=anterior.snapshot_date,
    )


def unit_value_series(
    snapshots: Iterable[NavSnapshot], current: NavSnapshot | None = None
) -> list[tuple[date, Decimal]]:
    """El valor por participación de cada día con foto fiable, por fecha. La foto de ahora,
    si es fiable, sustituye a la guardada de su mismo día."""
    puntos = {s.snapshot_date: s.unit_value for s in snapshots if s.reliable}
    if current is not None and current.reliable:
        puntos[current.snapshot_date] = current.unit_value
    return sorted(puntos.items())


# -- auditoría --------------------------------------------------------------------------


class BreachRule(StrEnum):
    """Las reglas que audita el mandato. Con el sujeto, identifican un incumplimiento."""

    ASSET = "ACTIVO"
    SECTOR = "SECTOR"
    CASH = "EFECTIVO"
    COVERAGE = "COBERTURA"


#: Sujetos del incumplimiento de efectivo.
CASH_BELOW = "MINIMO"
CASH_ABOVE = "MAXIMO"


@dataclass(frozen=True)
class Finding:
    """Un incumplimiento del mandato, con la corrección en euros."""

    rule: BreachRule
    subject: str
    title: str
    correction: str
    amount_eur: Decimal  # euros de la corrección, redondeados al euro hacia arriba
    ticker: str = ""  # el activo al que se refiere, si es uno

    @property
    def key(self) -> tuple[str, str]:
        """(regla, sujeto), como se guarda en `breaches`."""
        return (self.rule.value, self.subject)


def _euros_up(value: Decimal) -> Decimal:
    """«Unos 712 €»: al euro, hacia arriba, para que la corrección baste."""
    return value.quantize(ONE, rounding=ROUND_CEILING)


def _pct_against(fraction: Decimal, limit: Decimal) -> str:
    """El porcentaje con los decimales que hagan falta para no confundirse con el límite: un
    10,04 % frente a un tope del 10 % se ve «10,04 %», no «10,0 %»."""
    decimales = 1
    for decimales in (1, 2, 3, 4):
        visto = (fraction * 100).quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_HALF_UP)
        if visto != limit * 100:
            break
    return format_pct(fraction, decimales)


def join_names(items: Sequence[str], limit: int = 4) -> str:
    """Una lista corta de tickers para un texto: «IWDA», «IWDA y VUSA», «IWDA, VUSA, SAN y 2
    más»."""
    if len(items) <= 1:
        return "".join(items)
    if len(items) <= limit:
        return f"{', '.join(items[:-1])} y {items[-1]}"
    return f"{', '.join(items[:limit - 1])} y {len(items) - limit + 1} más"


def audit(valuation: Valuation, state: MandateState, rules: MandateRules) -> tuple[Finding, ...]:
    """Los incumplimientos de la cartera en este estado, uno por regla y sujeto (GUIA §5.4).

    Quedar justo en el límite no es incumplir. Las posiciones sin sector cuentan juntas como
    si fueran un sector: el mandato no se afloja por falta de datos.
    """
    nav = valuation.nav_eur
    if nav <= 0:
        return ()
    hallazgos: list[Finding] = []
    nombre_estado = STATE_LABELS[state]

    # Efectivo, frente a la banda del estado.
    minimo = rules.min_cash(state)
    maximo = rules.max_cash(state)
    peso_efectivo = valuation.cash_weight
    if peso_efectivo < minimo:
        falta = _euros_up(minimo * nav - valuation.cash_eur)
        hallazgos.append(
            Finding(
                BreachRule.CASH,
                CASH_BELOW,
                f"Efectivo al {_pct_against(peso_efectivo, minimo)}, por debajo del mínimo "
                f"del {format_limit_pct(minimo)}",
                f"Liberar unos {format_eur(falta, 0)}",
                falta,
            )
        )
    elif maximo is not None and peso_efectivo > maximo:
        sobra = _euros_up(valuation.cash_eur - maximo * nav)
        hallazgos.append(
            Finding(
                BreachRule.CASH,
                CASH_ABOVE,
                f"Efectivo al {_pct_against(peso_efectivo, maximo)}, por encima del máximo "
                f"del {format_limit_pct(maximo)}",
                f"Invertir unos {format_eur(sobra, 0)}",
                sobra,
            )
        )

    # Cada activo, frente al tope del estado.
    tope = rules.max_asset_weight(state)
    for p in sorted(valuation.positions, key=lambda p: (-p.weight, p.ticker)):
        if p.weight > tope:
            vender = _euros_up(p.value_eur - tope * nav)
            hallazgos.append(
                Finding(
                    BreachRule.ASSET,
                    p.ticker,
                    f"{p.ticker} pesa {_pct_against(p.weight, tope)} del patrimonio (límite "
                    f"{format_limit_pct(tope)} en {nombre_estado})",
                    f"Reducir {p.ticker} al {format_limit_pct(tope)}: vender unos "
                    f"{format_eur(vender, 0)}",
                    vender,
                    ticker=p.ticker,
                )
            )

    # Cada sector, frente a su tope.
    tope_sector = rules.max_sector_weight
    for s in valuation.sectors:
        if not s.exceeds(tope_sector):
            continue
        vender = _euros_up(s.value_eur - tope_sector * nav)
        peso = _pct_against(s.weight, tope_sector)
        limite = format_limit_pct(tope_sector)
        if s.sector == NO_SECTOR:
            titulo = f"Las posiciones sin sector pesan {peso} del patrimonio (límite {limite})"
            correccion = (
                f"Asigna su sector en Cartera → «Editar activo» ({join_names(s.tickers)}); si "
                f"fueran de un mismo sector, habría que vender unos {format_eur(vender, 0)}"
            )
        else:
            nombre = pretty_sector(s.sector)
            titulo = f"El sector {nombre} pesa {peso} del patrimonio (límite {limite})"
            correccion = f"Reducir {nombre} al {limite}: vender unos {format_eur(vender, 0)}"
        hallazgos.append(Finding(BreachRule.SECTOR, s.sector, titulo, correccion, vender))

    # Cobertura: lo que no tiene precio fiable.
    if valuation.coverage < ONE and valuation.positions:
        sin_precio = valuation.unreliable
        euros = _euros_up(sum((p.value_eur for p in sin_precio), ZERO))
        n = len(sin_precio)
        cuantas = "1 posición" if n == 1 else f"{n} posiciones"
        titulo = (
            f"Cobertura del {format_pct(valuation.coverage, truncate=True)}: {cuantas} sin "
            f"precio fiable ({join_names([p.ticker for p in sin_precio])})"
        )
        correccion = (
            f"Unos {format_eur(euros, 0)} del patrimonio sin precio fiable: actualiza los "
            "precios o asigna su símbolo"
        )
        if not valuation.reliable:
            correccion += "; por debajo del 90 % el estado y el máximo no se actualizan"
        hallazgos.append(Finding(BreachRule.COVERAGE, "", titulo, correccion, euros))

    return tuple(hallazgos)


def days_open(opened_at: datetime, today: date) -> int:
    """Días que lleva abierto un incumplimiento (0 si se abrió hoy)."""
    return max(0, (today - opened_at.date()).days)


def is_escalated(days: int, rules: MandateRules) -> bool:
    """Abierto 7 días o más (por defecto): incumplimiento escalado."""
    return days >= rules.escalation_days


def age_text(days: int, escalated: bool) -> str:
    """«abierto hoy», «abierto desde ayer», «abierto desde hace 9 días (escalado)»."""
    if days <= 0:
        texto = "abierto hoy"
    elif days == 1:
        texto = "abierto desde ayer"
    else:
        texto = f"abierto desde hace {days} días"
    return f"{texto} (escalado)" if escalated else texto


# -- validación de operaciones ----------------------------------------------------------


class Check(StrEnum):
    """Los pasos de la validación de una compra, en su orden (GUIA §5.5), y el de la venta."""

    STATE = "ESTADO"
    AMOUNTS = "IMPORTES"
    LEVELS = "NIVELES"
    REWARD_RISK = "RATIO"
    ASSET_WEIGHT = "PESO_ACTIVO"
    TRADE_RISK = "RIESGO"
    SECTOR_WEIGHT = "PESO_SECTOR"
    CASH = "EFECTIVO"
    HOLDING = "UNIDADES"


#: El orden de la validación de una compra.
BUY_CHECKS: tuple[Check, ...] = (
    Check.STATE,
    Check.AMOUNTS,
    Check.LEVELS,
    Check.REWARD_RISK,
    Check.ASSET_WEIGHT,
    Check.TRADE_RISK,
    Check.SECTOR_WEIGHT,
    Check.CASH,
)


@dataclass(frozen=True)
class CheckResult:
    check: Check
    passed: bool
    message: str


@dataclass(frozen=True)
class OrderValidation:
    """El resultado de validar una operación, paso a paso.

    Los pasos van en su orden. Los que no se pueden calcular porque falla uno anterior del
    que dependen (sin precio no hay ratio) no aparecen.
    """

    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failure(self) -> CheckResult | None:
        """El primer paso que falla: el motivo que se enseña."""
        return next((r for r in self.results if not r.passed), None)

    @property
    def also_failing(self) -> tuple[CheckResult, ...]:
        """Los demás pasos que fallan, después del primero («Además, …»)."""
        fallos = [r for r in self.results if not r.passed]
        return tuple(fallos[1:])

    def result(self, check: Check) -> CheckResult | None:
        return next((r for r in self.results if r.check is check), None)


@dataclass(frozen=True)
class BuyOrder:
    """Una compra, tal como se ha ejecutado en el bróker.

    `price_to_eur` y `levels_to_eur` son los EUR que vale una unidad de la divisa del precio y
    de la de los niveles. Si las dos divisas coinciden, los niveles se comparan con el precio
    tal cual, sin convertir.
    """

    ticker: str
    units: Decimal
    price: Decimal
    currency: str
    price_to_eur: Decimal
    fee_eur: Decimal
    stop: Decimal | None
    target: Decimal | None
    levels_currency: str
    levels_to_eur: Decimal
    sector: str | None = None

    @property
    def amount_eur(self) -> Decimal:
        """Importe bruto en EUR, sin la comisión."""
        return self.units * self.price * self.price_to_eur

    @property
    def same_currency(self) -> bool:
        return self.levels_currency == self.currency

    def comparable(self, value: Decimal, *, level: bool) -> Decimal:
        """El precio o un nivel en la moneda en la que se comparan: la suya si las dos divisas
        coinciden; si no, EUR."""
        if self.same_currency:
            return value
        return value * (self.levels_to_eur if level else self.price_to_eur)


def _level(value: Decimal, currency: str) -> str:
    return f"{format_price(value)} {currency}"


def validate_buy(
    order: BuyOrder, valuation: Valuation, state: MandateState, rules: MandateRules
) -> OrderValidation:
    """Valida una compra con los 8 pasos de GUIA §5.5, en su orden.

    `valuation` es la cartera antes de la compra y `state`, el estado vigente del mandato.
    """
    if order.price_to_eur <= 0 or order.levels_to_eur <= 0:
        raise ValueError("Hace falta el cambio a EUR de la divisa del precio y de los niveles")
    resultados: list[CheckResult] = []
    nombre_estado = STATE_LABELS[state]

    # 1. Estado del mandato.
    if buying_allowed(state):
        texto = f"Estado {nombre_estado}: compras permitidas"
        if state is not MandateState.OPTIMAL:
            texto += f", con tope del {format_limit_pct(rules.max_asset_weight(state))} por activo"
        resultados.append(CheckResult(Check.STATE, True, texto + "."))
    else:
        texto = f"Compras prohibidas en estado {nombre_estado}: vender siempre se puede."
        if state is MandateState.LOCKDOWN:
            texto += " Toca una revisión completa de la cartera."
        resultados.append(CheckResult(Check.STATE, False, texto))

    # 2. Precio y unidades.
    if order.price <= 0 or order.units <= 0:
        que = "el precio y las unidades" if order.price <= 0 and order.units <= 0 else (
            "el precio" if order.price <= 0 else "las unidades"
        )
        resultados.append(
            CheckResult(Check.AMOUNTS, False, f"Hace falta que {que} sean mayores que 0.")
        )
        return OrderValidation(tuple(resultados))  # sin importe no hay nada más que medir
    resultados.append(CheckResult(Check.AMOUNTS, True, "Precio y unidades mayores que 0."))

    nav = valuation.nav_eur
    importe = order.amount_eur
    nav_despues = nav - order.fee_eur

    # 3. Stop < precio < objetivo, en la misma divisa.
    # Si están bien, (precio, stop, objetivo) en la moneda en que se comparan.
    niveles: tuple[Decimal, Decimal, Decimal] | None = None
    precio_txt = _level(order.price, order.currency)
    if order.stop is None or order.target is None:
        falta = "el stop y el objetivo" if order.stop is None and order.target is None else (
            "el stop" if order.stop is None else "el objetivo"
        )
        resultados.append(CheckResult(Check.LEVELS, False, f"Falta {falta}."))
    else:
        precio = order.comparable(order.price, level=False)
        stop = order.comparable(order.stop, level=True)
        objetivo = order.comparable(order.target, level=True)
        en_eur = "" if order.same_currency else " (comparados en EUR)"
        stop_txt = _level(order.stop, order.levels_currency)
        objetivo_txt = _level(order.target, order.levels_currency)
        if stop >= precio:
            resultados.append(CheckResult(
                Check.LEVELS, False,
                f"El stop ({stop_txt}) tiene que quedar por debajo del precio de compra "
                f"({precio_txt}){en_eur}.",
            ))
        elif objetivo <= precio:
            resultados.append(CheckResult(
                Check.LEVELS, False,
                f"El objetivo ({objetivo_txt}) tiene que quedar por encima del precio de "
                f"compra ({precio_txt}){en_eur}.",
            ))
        else:
            niveles = (precio, stop, objetivo)
            resultados.append(CheckResult(
                Check.LEVELS, True,
                f"Stop {stop_txt} < precio {precio_txt} < objetivo {objetivo_txt}{en_eur}.",
            ))

    # 4. Ratio beneficio/riesgo.
    riesgo_unitario_eur = ZERO
    if niveles is not None:
        precio, stop, objetivo = niveles
        ratio = (objetivo - precio) / (precio - stop)
        minimo = rules.min_reward_risk
        texto = (
            f"Ratio beneficio/riesgo {format_number(ratio, 2, truncate=True)} "
            f"(mínimo {format_number(minimo, 1)})"
        )
        resultados.append(CheckResult(
            Check.REWARD_RISK,
            ratio >= minimo,
            texto + ("." if ratio >= minimo else ": no compensa el riesgo hasta el stop."),
        ))
        # En la misma divisa, la distancia hasta el stop se pasa a EUR; si no, ya lo está.
        riesgo_unitario_eur = (precio - stop) * (
            order.price_to_eur if order.same_currency else ONE
        )

    # 5. Peso final del activo frente al tope del estado.
    tope = rules.max_asset_weight(state)
    actual = valuation.position(order.ticker)
    ya_invertido = actual.value_eur if actual is not None else ZERO
    if nav_despues > 0:
        peso = (ya_invertido + importe) / nav_despues
        cabe = max(ZERO, tope * nav_despues - ya_invertido)
        bien = peso <= tope
        texto = (
            f"{order.ticker} quedaría al {_pct_against(peso, tope)} del patrimonio y el tope en "
            f"estado {nombre_estado} es del {format_limit_pct(tope)}."
        )
        if not bien:
            texto += f" Máximo invertible ahora: {format_eur(cabe)}."
        resultados.append(CheckResult(Check.ASSET_WEIGHT, bien, texto))
    else:
        resultados.append(CheckResult(
            Check.ASSET_WEIGHT, False, "Sin patrimonio no se puede medir el peso de la compra."
        ))

    # 6. Riesgo hasta el stop, en EUR, frente al máximo por operación.
    if niveles is not None:
        riesgo = riesgo_unitario_eur * order.units
        limite = rules.max_risk_per_trade * nav
        bien = riesgo <= limite
        texto = (
            f"Riesgo hasta el stop: {format_eur(riesgo)}; el máximo por operación es "
            f"{format_eur(limite)} ({format_limit_pct(rules.max_risk_per_trade)} del patrimonio)."
        )
        resultados.append(CheckResult(Check.TRADE_RISK, bien, texto))

    # 7. Peso final del sector.
    sector = order.sector or NO_SECTOR
    exposicion = next((s for s in valuation.sectors if s.sector == sector), None)
    en_sector = exposicion.value_eur if exposicion is not None else ZERO
    tope_sector = rules.max_sector_weight
    if nav_despues > 0:
        peso = (en_sector + importe) / nav_despues
        bien = peso <= tope_sector
        quien = (
            "Las posiciones sin sector quedarían"
            if sector == NO_SECTOR
            else f"El sector {pretty_sector(sector)} quedaría"
        )
        texto = (
            f"{quien} al {_pct_against(peso, tope_sector)} del patrimonio (tope "
            f"{format_limit_pct(tope_sector)})."
        )
        resultados.append(CheckResult(Check.SECTOR_WEIGHT, bien, texto))

    # 8. Efectivo después de la compra y la comisión.
    efectivo_despues = valuation.cash_eur - importe - order.fee_eur
    minimo = rules.min_cash(state)
    if efectivo_despues < 0:
        resultados.append(CheckResult(
            Check.CASH, False,
            f"No hay efectivo suficiente: la compra y la comisión suman "
            f"{format_eur(importe + order.fee_eur)} y hay {format_eur(valuation.cash_eur)}.",
        ))
    elif nav_despues > 0:
        peso = efectivo_despues / nav_despues
        bien = peso >= minimo
        if bien:
            texto = (
                f"La liquidez quedaría al {_pct_against(peso, minimo)} (mínimo "
                f"{format_limit_pct(minimo)} en estado {nombre_estado})."
            )
        else:
            texto = (
                f"La liquidez caería al {_pct_against(peso, minimo)}, por debajo del mínimo del "
                f"{format_limit_pct(minimo)} que exige el estado {nombre_estado}."
            )
        resultados.append(CheckResult(Check.CASH, bien, texto))

    return OrderValidation(tuple(resultados))


def validate_sell(
    ticker: str, units: Decimal, price: Decimal, held_units: Decimal
) -> OrderValidation:
    """Una venta solo exige precio y unidades mayores que 0 y no vender más de lo que hay."""
    if price <= 0 or units <= 0:
        que = "el precio y las unidades" if price <= 0 and units <= 0 else (
            "el precio" if price <= 0 else "las unidades"
        )
        return OrderValidation(
            (CheckResult(Check.AMOUNTS, False, f"Hace falta que {que} sean mayores que 0."),)
        )
    resultados = [CheckResult(Check.AMOUNTS, True, "Precio y unidades mayores que 0.")]
    if units > held_units:
        resultados.append(CheckResult(
            Check.HOLDING, False,
            f"No se pueden vender {format_units(units)} unidades de {ticker}: solo hay "
            f"{format_units(held_units)}.",
        ))
    else:
        resultados.append(CheckResult(
            Check.HOLDING, True,
            f"Se venden {format_units(units)} de las {format_units(held_units)} unidades de "
            f"{ticker}.",
        ))
    return OrderValidation(tuple(resultados))
