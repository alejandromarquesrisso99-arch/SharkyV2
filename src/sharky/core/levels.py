"""Tesis y niveles (GUIA §5.6): la vigilancia del stop y del objetivo de cada tesis.

Pura: recibe las tesis, la valoración, los tipos de cambio y el historial; no lee la base de
datos, ni los ajustes, ni el reloj.

**Vigilancia.** Para cada tesis ACTIVA con posición abierta, en cada valoración y sin IA:

- Sin precio fiable (el de la posición, GUIA §5.3) o sin un tipo de cambio fiable para la
  divisa de los niveles → **NO VERIFICABLE**. Nunca «a salvo»: no se sabe.
- Todo se compara en EUR: el precio con su cambio y los niveles con el de su divisa (los
  peniques de Londres, a la centésima parte de la libra).
- Precio ≤ stop → **STOP**: salida obligatoria, y manda aunque también se haya pasado el
  objetivo. Precio ≥ objetivo → **OBJETIVO**: aviso, no obliga a vender. Tocar el nivel ya
  cuenta.

**Stop propuesto en OBJETIVO**, en la divisa de los niveles: el mayor de la entrada
(break-even) y el precio actual − el riesgo inicial; sin entrada o sin stop, el precio actual −
8 %. Nunca por debajo del stop vigente: si ninguno lo sube, no hay nada que proponer. Un
candidato que no quede por debajo del precio actual no vale (saltaría el stop en el acto). El
riesgo inicial es entrada − stop la primera vez que la tesis tuvo los dos en su divisa de
niveles (se lee del historial). No es el stop vigente: tras aplicar una subida, entrada − stop
vigente daría un stop por encima del precio.

**Historial.** Los números de una tesis solo los cambia el usuario, y cada cambio queda en
`thesis_events` con el valor anterior y el nuevo, en JSON (`levels_of`, `to_json`). Las
propuestas (de Sharky o de Claude) son eventos PROPUESTA; aplicarlas o descartarlas son eventos
nuevos que apuntan a ellas (`ref_event_id`): nada se reescribe.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from sharky.core.formatting import format_eur, format_price, format_signed_amount, format_units
from sharky.core.models import (
    Author,
    FxRate,
    LevelAlert,
    LevelAlertKind,
    Thesis,
    ThesisEvent,
    ThesisEventKind,
    ThesisStatus,
)
from sharky.core.valuation import (
    BASE_CURRENCY,
    PositionValue,
    Valuation,
    fx_currency,
    source_of,
    to_eur_rate,
)

ZERO = Decimal("0")
ONE = Decimal("1")

#: Sin entrada o sin stop, el stop propuesto es el precio actual menos esto (GUIA §5.6).
FALLBACK_DROP = Decimal("0.08")

MIN_CONVICTION = 1
MAX_CONVICTION = 10


# -- estados y criterios ---------------------------------------------------------------------


class LevelStatus(StrEnum):
    """Lo que dice la vigilancia de una tesis."""

    STOP = "STOP"
    TARGET = "OBJETIVO"
    UNVERIFIABLE = "NO_VERIFICABLE"
    WITHIN = "ENTRE_NIVELES"
    NO_LEVELS = "SIN_NIVELES"


STATUS_LABELS: dict[LevelStatus, str] = {
    LevelStatus.STOP: "Stop alcanzado",
    LevelStatus.TARGET: "Objetivo alcanzado",
    LevelStatus.UNVERIFIABLE: "No verificable",
    LevelStatus.WITHIN: "Entre niveles",
    LevelStatus.NO_LEVELS: "Sin stop ni objetivo",
}

#: El color de estado de cada resultado. Siempre con su etiqueta, nunca solo el color.
STATUS_TOKENS: dict[LevelStatus, str] = {
    LevelStatus.STOP: "danger",
    LevelStatus.TARGET: "warn",
    LevelStatus.UNVERIFIABLE: "warn",
    LevelStatus.WITHIN: "ok",
    LevelStatus.NO_LEVELS: "warn",
}


class StopCriterion(StrEnum):
    """Qué criterio da el stop propuesto (columna `criterion` de `level_alerts`)."""

    BREAK_EVEN = "BREAK_EVEN"
    TRAILING = "TRAILING"
    MINUS_8 = "MENOS_8"
    CURRENT_STOP = "STOP_VIGENTE"


CRITERION_LABELS: dict[StopCriterion, str] = {
    StopCriterion.BREAK_EVEN: "break-even",
    StopCriterion.TRAILING: "mantiene el riesgo inicial",
    StopCriterion.MINUS_8: "precio − 8 %",
    StopCriterion.CURRENT_STOP: "stop vigente",
}


# -- cifras para mostrar ---------------------------------------------------------------------


def _display(value: Decimal) -> Decimal:
    """Un importe convertido de divisa, redondeado para leerlo (2 decimales; 4 por debajo de
    1). Solo para mostrar: las comparaciones nunca se redondean."""
    decimales = 2 if abs(value) >= ONE else 4
    return value.quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_HALF_UP)


def format_eur_price(value: Decimal) -> str:
    """Un precio por unidad en euros: «5,12 €»."""
    return f"{format_price(_display(value))} €"


def format_level(value: Decimal, currency: str) -> str:
    """Un nivel en su divisa: «5,20 €» o «110,00 USD»."""
    return f"{format_price(value)} {'€' if currency == BASE_CURRENCY else currency}"


def format_level_eur(value: Decimal, currency: str, value_eur: Decimal | None) -> str:
    """Un nivel con su valor en euros si no está en euros: «110,00 USD (93,50 €)»."""
    texto = format_level(value, currency)
    if currency == BASE_CURRENCY or value_eur is None:
        return texto
    return f"{texto} ({format_eur_price(value_eur)})"


def round_level(value: Decimal) -> Decimal:
    """Un stop calculado (no escrito por el usuario), a 2 decimales (4 por debajo de 1) y hacia
    abajo."""
    decimales = 2 if value >= ONE else 4
    return value.quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_DOWN)


# -- el stop propuesto -----------------------------------------------------------------------


@dataclass(frozen=True)
class StopProposal:
    """El stop que se propone al llegar al objetivo, en la divisa de los niveles."""

    stop: Decimal
    criterion: StopCriterion
    current: Decimal | None  # el stop vigente

    @property
    def raises(self) -> bool:
        """Sube el stop vigente (si no, no hay nada que aplicar)."""
        return self.current is None or self.stop > self.current


def propose_stop(
    price: Decimal,
    entry: Decimal | None,
    stop: Decimal | None,
    initial_risk: Decimal | None,
) -> StopProposal | None:
    """El stop propuesto en OBJETIVO (GUIA §5.6), todo en la divisa de los niveles.

    - Con entrada y stop: el mayor de la entrada (break-even) y `price − initial_risk`.
    - Sin entrada o sin stop: `price − 8 %`.
    - Nunca por debajo del stop vigente: si nada lo sube, se devuelve el vigente con el
      criterio STOP_VIGENTE (`raises` es False).

    None solo si no hay stop vigente ni ningún candidato válido.
    """
    candidatos: list[tuple[Decimal, StopCriterion]] = []
    if entry is None or stop is None:
        candidatos.append((round_level(price * (ONE - FALLBACK_DROP)), StopCriterion.MINUS_8))
    else:
        candidatos.append((entry, StopCriterion.BREAK_EVEN))
        if initial_risk is not None and initial_risk > 0:
            candidatos.append((round_level(price - initial_risk), StopCriterion.TRAILING))
    validos = [(valor, criterio) for valor, criterio in candidatos if ZERO < valor < price]
    # En un empate gana el primero: el break-even.
    mejor = max(validos, key=lambda c: c[0], default=None)
    if stop is not None and (mejor is None or mejor[0] <= stop):
        return StopProposal(stop, StopCriterion.CURRENT_STOP, stop)
    if mejor is None:
        return None
    return StopProposal(mejor[0], mejor[1], stop)


# -- la vigilancia ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LevelCheck:
    """El resultado de vigilar una tesis con su posición valorada.

    `price_eur`, `stop_eur` y `target_eur` son por unidad y en EUR; `price_in_levels`, el
    precio en la divisa de los niveles. Faltan (None) si la tesis no se puede verificar.
    """

    thesis: Thesis
    position: PositionValue
    status: LevelStatus
    price_eur: Decimal | None = None
    stop_eur: Decimal | None = None
    target_eur: Decimal | None = None
    price_in_levels: Decimal | None = None
    proposal: StopProposal | None = None
    note: str = ""  # por qué no se puede verificar

    @property
    def ticker(self) -> str:
        return self.thesis.ticker

    @property
    def currency(self) -> str:
        return self.thesis.levels_currency

    def alert(self, day: date) -> LevelAlert | None:
        """El aviso que se guarda en `level_alerts` (solo STOP y OBJETIVO)."""
        if self.price_eur is None:
            return None
        if self.status is LevelStatus.STOP and self.stop_eur is not None:
            return LevelAlert(day, self.ticker, LevelAlertKind.STOP, self.price_eur, self.stop_eur)
        if self.status is LevelStatus.TARGET and self.target_eur is not None:
            propuesta = self.proposal
            return LevelAlert(
                day,
                self.ticker,
                LevelAlertKind.TARGET,
                self.price_eur,
                self.target_eur,
                proposed_stop=propuesta.stop if propuesta is not None else None,
                criterion=propuesta.criterion.value if propuesta is not None else None,
            )
        return None


def _levels_rate(
    currency: str,
    position: PositionValue,
    fx_rates: Mapping[str, FxRate],
    now: datetime,
    market_at: datetime | None,
) -> tuple[Decimal | None, str]:
    """Cuántos EUR vale una unidad de la divisa de los niveles, o None y por qué no se sabe."""
    if position.price is not None and currency == position.price.currency:
        return position.fx_to_eur, ""  # el mismo cambio que el precio, ya fiable
    base = fx_currency(currency)
    if base is None:
        return ONE, ""
    cambio = fx_rates.get(base)
    valor = to_eur_rate(currency, cambio)
    if valor is None or cambio is None:
        return None, f"Sin tipo de cambio {base}→EUR para los niveles en {currency}."
    if not source_of(cambio.fetched_at, now, market_at).reliable:
        return None, f"El tipo de cambio {base}→EUR de los niveles tiene más de 24 horas."
    return valor, ""


def check_thesis(
    thesis: Thesis,
    position: PositionValue,
    fx_rates: Mapping[str, FxRate],
    now: datetime,
    market_at: datetime | None = None,
    initial_risk: Decimal | None = None,
) -> LevelCheck:
    """Vigila una tesis con su posición valorada (GUIA §5.6).

    `fx_rates` son los últimos cambios a EUR de cada divisa base (los de la valoración);
    `now` y `market_at`, la hora de la valoración y la de la última actualización, para saber
    si el cambio de los niveles es fiable.
    """
    if thesis.stop is None and thesis.target is None:
        return LevelCheck(thesis, position, LevelStatus.NO_LEVELS)
    if not position.reliable or position.price is None or position.fx_to_eur is None:
        motivo = position.note or "Sin precio fiable."
        return LevelCheck(thesis, position, LevelStatus.UNVERIFIABLE, note=motivo)
    cambio, motivo = _levels_rate(thesis.levels_currency, position, fx_rates, now, market_at)
    if cambio is None:
        return LevelCheck(thesis, position, LevelStatus.UNVERIFIABLE, note=motivo)

    precio_eur = position.price.price * position.fx_to_eur
    stop_eur = thesis.stop * cambio if thesis.stop is not None else None
    objetivo_eur = thesis.target * cambio if thesis.target is not None else None
    if thesis.levels_currency == position.price.currency:
        en_niveles = position.price.price
    else:
        en_niveles = precio_eur / cambio

    propuesta = None
    if stop_eur is not None and precio_eur <= stop_eur:
        estado = LevelStatus.STOP  # manda aunque también se haya pasado el objetivo
    elif objetivo_eur is not None and precio_eur >= objetivo_eur:
        estado = LevelStatus.TARGET
        propuesta = propose_stop(en_niveles, thesis.entry_price, thesis.stop, initial_risk)
    else:
        estado = LevelStatus.WITHIN
    return LevelCheck(
        thesis,
        position,
        estado,
        price_eur=precio_eur,
        stop_eur=stop_eur,
        target_eur=objetivo_eur,
        price_in_levels=en_niveles,
        proposal=propuesta,
    )


def evaluate_levels(
    theses: Iterable[Thesis],
    valuation: Valuation,
    fx_rates: Mapping[str, FxRate],
    histories: Mapping[int, Sequence[ThesisEvent]] | None = None,
) -> tuple[LevelCheck, ...]:
    """Vigila cada tesis ACTIVA con posición abierta, en el orden de la valoración.

    `histories` es el historial de cada tesis (por su id), de donde sale el riesgo inicial.
    """
    activas = {t.ticker: t for t in theses if t.status is ThesisStatus.ACTIVE}
    historiales = histories or {}
    resultado: list[LevelCheck] = []
    for posicion in valuation.positions:
        tesis = activas.get(posicion.ticker)
        if tesis is None:
            continue
        eventos = historiales.get(tesis.id, ()) if tesis.id is not None else ()
        resultado.append(
            check_thesis(
                tesis,
                posicion,
                fx_rates,
                valuation.valued_at,
                valuation.market_at,
                initial_risk(eventos, tesis.levels_currency),
            )
        )
    return tuple(resultado)


# -- los números de una tesis en el historial -----------------------------------------------

#: Las claves de los números de una tesis en el historial, y cómo se llaman al enseñarlas.
LEVEL_NAMES: dict[str, str] = {
    "entrada": "entrada",
    "stop": "stop",
    "objetivo": "objetivo",
    "conviccion": "convicción",
    "divisa": "divisa de los niveles",
}

#: Las claves de los textos de una tesis, y cómo se llaman al enseñarlos.
TEXT_NAMES: dict[str, str] = {
    "por_que": "por qué la tengo",
    "catalizadores": "catalizadores",
    "riesgos": "riesgos",
    "invalidacion": "qué la invalidaría",
}


def _text_decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def to_decimal(value: Any) -> Decimal | None:
    """Un número guardado en el historial (texto) como Decimal; None si falta o no vale."""
    if value is None or value == "":
        return None
    try:
        numero = Decimal(str(value))
    except InvalidOperation:
        return None
    return numero if numero.is_finite() else None


def levels_of(thesis: Thesis) -> dict[str, Any]:
    """Los números de una tesis (y la divisa de los niveles), tal como van al historial."""
    return {
        "entrada": _text_decimal(thesis.entry_price),
        "stop": _text_decimal(thesis.stop),
        "objetivo": _text_decimal(thesis.target),
        "conviccion": thesis.conviction,
        "divisa": thesis.levels_currency,
    }


def texts_of(thesis: Thesis) -> dict[str, str]:
    return {
        "por_que": thesis.why,
        "catalizadores": thesis.catalysts,
        "riesgos": thesis.risks,
        "invalidacion": thesis.invalidation,
    }


def changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> tuple[dict, dict]:
    """Lo que cambia de `old` a `new`: (valores anteriores, valores nuevos), solo esas claves.

    Los números se comparan por su valor: «5.2» y «5.20» son lo mismo.
    """
    antes: dict[str, Any] = {}
    despues: dict[str, Any] = {}
    for clave, nuevo in new.items():
        viejo = old.get(clave)
        if clave in ("entrada", "stop", "objetivo"):
            igual = to_decimal(viejo) == to_decimal(nuevo)
        else:
            igual = viejo == nuevo
        if not igual:
            antes[clave] = viejo
            despues[clave] = nuevo
    return antes, despues


def to_json(values: Mapping[str, Any]) -> str:
    return json.dumps(dict(values), ensure_ascii=False)


def from_json(text: str | None) -> dict[str, Any]:
    """Lo guardado en `old_value` o `new_value`; un texto que no sea un objeto JSON, vacío."""
    if not text:
        return {}
    try:
        valor = json.loads(text)
    except ValueError:
        return {}
    return valor if isinstance(valor, dict) else {}


def initial_risk(events: Iterable[ThesisEvent], currency: str) -> Decimal | None:
    """Entrada − stop la primera vez que la tesis tuvo los dos en la divisa `currency`.

    Se reconstruyen los números con el historial (la creación y cada cambio de niveles, por
    orden). Si la divisa de los niveles cambia, se vuelve a empezar: el riesgo en otra divisa
    no vale. None si nunca los tuvo o si el stop no quedaba por debajo de la entrada.
    """
    estado: dict[str, Any] = {}
    riesgo: Decimal | None = None
    fijado = False
    for evento in events:
        if evento.kind is ThesisEventKind.CREATED:
            estado = from_json(evento.new_value)
            riesgo, fijado = None, False
        elif evento.kind is ThesisEventKind.LEVELS_CHANGED:
            nuevo = from_json(evento.new_value)
            if "divisa" in nuevo and nuevo["divisa"] != estado.get("divisa"):
                riesgo, fijado = None, False
            estado.update(nuevo)
        else:
            continue
        if fijado or estado.get("divisa") != currency:
            continue
        entrada, stop = to_decimal(estado.get("entrada")), to_decimal(estado.get("stop"))
        if entrada is not None and stop is not None:
            riesgo, fijado = entrada - stop, True
    return riesgo if riesgo is not None and riesgo > 0 else None


# -- validación ------------------------------------------------------------------------------


def validate_levels(
    entry: Decimal | None,
    stop: Decimal | None,
    target: Decimal | None,
    conviction: int | None,
    *,
    check_order: bool = True,
) -> list[str]:
    """Los errores de los números de una tesis, todos a la vez (vacío si valen).

    Los que faltan valen: una tesis puede no tener entrada, stop u objetivo. Un stop por
    encima del precio actual no es un error: saltará el aviso, que es lo que toca.

    `check_order` exige stop < objetivo. Se comprueba al escribir el stop o el objetivo, no al
    aplicar una propuesta: pasado el objetivo, el stop propuesto puede quedar por encima de él.
    """
    errores: list[str] = []
    for nombre, valor in (("La entrada", entry), ("El stop", stop), ("El objetivo", target)):
        if valor is not None and valor <= 0:
            errores.append(f"{nombre} tiene que ser mayor que 0.")
    if check_order and stop is not None and target is not None and ZERO < target <= stop:
        errores.append(
            f"El stop ({format_price(stop)}) tiene que quedar por debajo del objetivo "
            f"({format_price(target)})."
        )
    if conviction is not None and not MIN_CONVICTION <= conviction <= MAX_CONVICTION:
        errores.append(f"La convicción va de {MIN_CONVICTION} a {MAX_CONVICTION}.")
    return errores


# -- propuestas ------------------------------------------------------------------------------


class ProposalState(StrEnum):
    PENDING = "PENDIENTE"
    APPLIED = "APLICADA"
    DISCARDED = "DESCARTADA"
    SUPERSEDED = "SUSTITUIDA"


def proposal_states(events: Sequence[ThesisEvent]) -> dict[int, ProposalState]:
    """El estado de cada propuesta del historial de una tesis, por su id.

    Aplicada si un cambio de niveles apunta a ella; descartada si lo hace otro evento; si no,
    pendiente. De las pendientes de un mismo autor solo cuenta la más reciente: las anteriores
    quedan sustituidas.
    """
    respuestas = {e.ref_event_id: e for e in events if e.ref_event_id is not None}
    estados: dict[int, ProposalState] = {}
    ultima: dict[Author, int] = {}
    for evento in events:
        if evento.kind is not ThesisEventKind.PROPOSAL or evento.id is None:
            continue
        respuesta = respuestas.get(evento.id)
        if respuesta is not None:
            estados[evento.id] = (
                ProposalState.APPLIED
                if respuesta.kind is ThesisEventKind.LEVELS_CHANGED
                else ProposalState.DISCARDED
            )
            continue
        anterior = ultima.get(evento.author)
        if anterior is not None and estados.get(anterior) is ProposalState.PENDING:
            estados[anterior] = ProposalState.SUPERSEDED
        estados[evento.id] = ProposalState.PENDING
        ultima[evento.author] = evento.id
    return estados


def proposal_blocker(proposal: ThesisEvent, thesis: Thesis) -> str:
    """Por qué no se puede aplicar ahora una propuesta pendiente («» si se puede)."""
    if thesis.status is not ThesisStatus.ACTIVE:
        return "La tesis está cerrada."
    nuevo = from_json(proposal.new_value)
    divisa = nuevo.get("divisa")
    if divisa is not None and divisa != thesis.levels_currency:
        return (
            f"La propuesta está en {divisa} y los niveles de la tesis ahora van en "
            f"{thesis.levels_currency}."
        )
    stop = to_decimal(nuevo.get("stop"))
    if (
        proposal.author is Author.SYSTEM
        and stop is not None
        and thesis.stop is not None
        and stop <= thesis.stop
    ):
        return "Tu stop ya es igual o más alto que el propuesto."
    return ""


def proposal_levels(proposal: ThesisEvent) -> dict[str, Any]:
    """Los números que cambia una propuesta (solo stop, objetivo y convicción)."""
    nuevo = from_json(proposal.new_value)
    return {k: v for k, v in nuevo.items() if k in ("stop", "objetivo", "conviccion")}


def target_proposal_event(
    check: LevelCheck, thesis_id: int, now: datetime
) -> ThesisEvent | None:
    """La propuesta de Sharky al llegar al objetivo, para el historial (None si no sube el
    stop)."""
    propuesta = check.proposal
    if (
        check.status is not LevelStatus.TARGET
        or propuesta is None
        or not propuesta.raises
        or check.price_in_levels is None
    ):
        return None
    divisa = check.currency
    texto = (
        f"Objetivo alcanzado a {format_level(_display(check.price_in_levels), divisa)}. "
        f"Criterio: {CRITERION_LABELS[propuesta.criterion]}."
    )
    return ThesisEvent(
        thesis_id,
        now,
        ThesisEventKind.PROPOSAL,
        Author.SYSTEM,
        texto,
        old_value=to_json({"stop": _text_decimal(propuesta.current)}),
        new_value=to_json({"stop": str(propuesta.stop), "divisa": divisa}),
    )


# -- textos de los avisos --------------------------------------------------------------------


def alert_title(check: LevelCheck) -> str:
    """La línea de «Requiere atención»: «Stop alcanzado: SAN a 5,12 € (stop 5,20 €)»."""
    precio = format_eur_price(check.price_eur) if check.price_eur is not None else "—"
    if check.status is LevelStatus.STOP and check.thesis.stop is not None:
        nivel = format_level_eur(check.thesis.stop, check.currency, check.stop_eur)
        return f"Stop alcanzado: {check.ticker} a {precio} (stop {nivel})"
    if check.status is LevelStatus.TARGET and check.thesis.target is not None:
        nivel = format_level_eur(check.thesis.target, check.currency, check.target_eur)
        return f"Objetivo alcanzado: {check.ticker} a {precio} (objetivo {nivel})"
    return f"{STATUS_LABELS[check.status]}: {check.ticker}"


STOP_ACTION = "Salida obligatoria del mandato: liquida en tu bróker y registra la venta."


def proposal_text(check: LevelCheck) -> str:
    """Lo que acompaña a un OBJETIVO: que no obliga a vender y el stop propuesto."""
    propuesta = check.proposal
    if propuesta is None:
        return "No obliga a vender."
    if propuesta.raises:
        return (
            f"No obliga a vender. Sharky propone subir el stop a "
            f"{format_level(propuesta.stop, check.currency)} "
            f"({CRITERION_LABELS[propuesta.criterion]})."
        )
    return (
        f"No obliga a vender. Tu stop ({format_level(propuesta.stop, check.currency)}) ya "
        "protege más que lo que se propondría."
    )


def stop_message(check: LevelCheck) -> str:
    """El texto del aviso de stop: «SAN cotiza a 5,12 € y su tesis fija el stop en 5,20 €…»."""
    precio = format_eur_price(check.price_eur) if check.price_eur is not None else "—"
    stop = (
        format_level_eur(check.thesis.stop, check.currency, check.stop_eur)
        if check.thesis.stop is not None
        else "—"
    )
    return f"{check.ticker} cotiza a {precio} y su tesis fija el stop en {stop}. {STOP_ACTION}"


def position_line(check: LevelCheck) -> str:
    """«Valor de la posición: 1.024,00 € · PnL +124,00 €»."""
    p = check.position
    texto = f"Valor de la posición: {format_eur(p.value_eur)}"
    if p.pnl_eur is not None:
        texto += f" · PnL {format_signed_amount(p.pnl_eur)} €"
    return texto


def notification(check: LevelCheck) -> tuple[str, str]:
    """Título y texto de la notificación de un aviso."""
    if check.status is LevelStatus.STOP:
        return f"Stop-loss alcanzado en {check.ticker}", stop_message(check)
    return f"Objetivo alcanzado en {check.ticker}", proposal_text(check)


# -- el historial para leer ------------------------------------------------------------------


def _value_text(key: str, value: Any) -> str:
    if value is None or value == "":
        return "—"
    if key in ("entrada", "stop", "objetivo"):
        numero = to_decimal(value)
        return format_price(numero) if numero is not None else str(value)
    return str(value)


def levels_summary(values: Mapping[str, Any]) -> str:
    """«Entrada 628,00 EUR · stop 610,00 · objetivo 950,00 · convicción 8»."""
    partes: list[str] = []
    divisa = values.get("divisa") or ""
    if values.get("entrada") is not None:
        partes.append(f"Entrada {_value_text('entrada', values['entrada'])} {divisa}".strip())
    elif divisa:
        partes.append(f"Niveles en {divisa}")
    for clave in ("stop", "objetivo"):
        if values.get(clave) is not None:
            partes.append(f"{LEVEL_NAMES[clave]} {_value_text(clave, values[clave])}")
    if values.get("conviccion") is not None:
        partes.append(f"convicción {values['conviccion']}")
    return " · ".join(partes)


def _changes_text(old: Mapping[str, Any], new: Mapping[str, Any]) -> str:
    return " · ".join(
        f"{LEVEL_NAMES.get(k, k)} {_value_text(k, old.get(k))} → {_value_text(k, v)}"
        for k, v in new.items()
    )


@dataclass(frozen=True)
class HistoryEntry:
    """Una línea del historial de una tesis, lista para leer."""

    event: ThesisEvent
    title: str
    detail: str = ""
    state: ProposalState | None = None  # solo en las propuestas


def describe_event(event: ThesisEvent, state: ProposalState | None = None) -> HistoryEntry:
    """El título y el detalle de un evento del historial."""
    antes, despues = from_json(event.old_value), from_json(event.new_value)
    kind = event.kind
    if kind is ThesisEventKind.CREATED:
        return HistoryEntry(event, event.text or "Tesis creada", levels_summary(despues))
    if kind is ThesisEventKind.LEVELS_CHANGED:
        if event.ref_event_id is not None:
            titulo = f"Propuesta aplicada por ti: {_changes_text(antes, despues)}"
            return HistoryEntry(event, titulo)
        if len(despues) == 1:
            clave = next(iter(despues))
            nombre = LEVEL_NAMES.get(clave, clave)
            titulo = (
                f"{nombre[0].upper()}{nombre[1:]} cambiado por ti: "
                f"{_value_text(clave, antes.get(clave))} → {_value_text(clave, despues[clave])}"
            )
            if clave in ("entrada", "divisa", "conviccion"):
                titulo = titulo.replace("cambiado", "cambiada")
        else:
            titulo = f"Niveles cambiados por ti: {_changes_text(antes, despues)}"
        return HistoryEntry(event, titulo, f"Motivo: {event.text}" if event.text else "")
    if kind is ThesisEventKind.PROPOSAL:
        quien = "Sharky" if event.author is Author.SYSTEM else "Claude"
        cambios = " y ".join(
            f"{LEVEL_NAMES.get(k, k)} {_value_text(k, v)}"
            for k, v in despues.items()
            if k in ("stop", "objetivo", "conviccion") and v is not None
        )
        divisa = despues.get("divisa")
        titulo = f"Propuesta de {quien}: {cambios}" if cambios else f"Propuesta de {quien}"
        if divisa and cambios:
            titulo += f" {divisa}"
        return HistoryEntry(event, titulo, event.text, state or ProposalState.PENDING)
    if kind is ThesisEventKind.REVIEW:
        if event.ref_event_id is not None:
            return HistoryEntry(event, "Propuesta descartada por ti", event.text)
        if event.author is Author.USER and not despues and event.text:
            # Una nota tuya (por ejemplo, al ampliar sin cambiar los niveles): la primera línea
            # es el título y el resto, el detalle.
            titulo, _, detalle = event.text.partition("\n")
            return HistoryEntry(event, titulo, detalle)
        if event.author is Author.USER:
            nombres = ", ".join(TEXT_NAMES.get(k, k) for k in despues)
            return HistoryEntry(event, f"Textos editados por ti: {nombres}" if nombres else
                                "Textos editados por ti")
        quien = "Claude" if event.author is Author.CLAUDE else "Sharky"
        return HistoryEntry(event, f"Revisión de {quien}", event.text)
    return HistoryEntry(event, "Tesis cerrada", event.text)


def history(events: Sequence[ThesisEvent]) -> list[HistoryEntry]:
    """El historial de una tesis para leer, del más reciente al más antiguo."""
    estados = proposal_states(events)
    return [
        describe_event(e, estados.get(e.id) if e.id is not None else None)
        for e in reversed(list(events))
    ]


# -- las tesis y las operaciones (GUIA §5.5, H8) ----------------------------------------------

#: Decimales de un nivel calculado al pasarlo de divisa.
LEVEL_DECIMALS = 6


def to_levels(value_eur: Decimal, levels_to_eur: Decimal) -> Decimal:
    """Un importe por unidad en EUR pasado a la divisa de los niveles (a 6 decimales)."""
    if levels_to_eur <= 0:
        raise ValueError("Hace falta el cambio a EUR de la divisa de los niveles")
    return (value_eur / levels_to_eur).quantize(
        Decimal(1).scaleb(-LEVEL_DECIMALS), rounding=ROUND_HALF_UP
    )


def buy_entry(
    price: Decimal,
    currency: str,
    price_to_eur: Decimal,
    levels_currency: str,
    levels_to_eur: Decimal,
) -> Decimal:
    """La entrada de la tesis que abre una compra: el precio de compra en la divisa de los
    niveles (tal cual si coinciden; si no, pasando por EUR con los cambios de la compra)."""
    if currency == levels_currency:
        return price
    return to_levels(price * price_to_eur, levels_to_eur)


def trade_text(day: date, units: Decimal, price: Decimal, currency: str) -> str:
    """«compra del 25/09/2026: 2 a 742,10 EUR»."""
    return f"compra del {day:%d/%m/%Y}: {format_units(units)} a {format_price(price)} {currency}"


def opened_by_buy_text(day: date, units: Decimal, price: Decimal, currency: str) -> str:
    """El texto del evento de creación de una tesis que abre una compra."""
    return f"Tesis abierta con la {trade_text(day, units, price, currency)}"


@dataclass(frozen=True)
class LevelUpdate:
    """Ampliar una posición con tesis cuando el stop o el objetivo de la compra no coinciden
    con los de la tesis (GUIA §5.5): lo que se puede actualizar, en la divisa de los niveles de
    la compra. La entrada es el nuevo coste medio en esa divisa.

    Si la divisa cambia, los tres números van juntos: una entrada en otra divisa no valdría.
    """

    thesis: Thesis
    currency: str
    entry: Decimal
    stop: Decimal
    target: Decimal

    @property
    def currency_changes(self) -> bool:
        return self.currency != self.thesis.levels_currency

    @property
    def stop_changes(self) -> bool:
        return self.currency_changes or self.stop != self.thesis.stop

    @property
    def target_changes(self) -> bool:
        return self.currency_changes or self.target != self.thesis.target

    @property
    def entry_changes(self) -> bool:
        return self.currency_changes or self.entry != self.thesis.entry_price


def level_update(
    thesis: Thesis,
    stop: Decimal,
    target: Decimal,
    currency: str,
    avg_cost_eur: Decimal,
    levels_to_eur: Decimal,
) -> LevelUpdate | None:
    """Lo que se pregunta al ampliar una posición con tesis. None si el stop y el objetivo de
    la compra coinciden con los de la tesis (en su misma divisa): no hay nada que preguntar."""
    if (
        currency == thesis.levels_currency
        and stop == thesis.stop
        and target == thesis.target
    ):
        return None
    return LevelUpdate(thesis, currency, to_levels(avg_cost_eur, levels_to_eur), stop, target)


@dataclass(frozen=True)
class LevelChoice:
    """Qué decide actualizar el usuario al ampliar (con otra divisa, o todo o nada)."""

    entry: bool = False
    stop: bool = False
    target: bool = False

    @property
    def any(self) -> bool:
        return self.entry or self.stop or self.target


def chosen_levels(update: LevelUpdate, choice: LevelChoice) -> dict[str, Any]:
    """Los números de la tesis tras la decisión: entrada, stop, objetivo y divisa."""
    t = update.thesis
    if update.currency_changes:
        if not choice.any:
            return levels_of(t)
        return {**levels_of(t), "entrada": str(update.entry), "stop": str(update.stop),
                "objetivo": str(update.target), "divisa": update.currency}
    return {
        **levels_of(t),
        "entrada": str(update.entry) if choice.entry else _text_decimal(t.entry_price),
        "stop": str(update.stop) if choice.stop else _text_decimal(t.stop),
        "objetivo": str(update.target) if choice.target else _text_decimal(t.target),
    }


def enlarge_reason(day: date, units: Decimal, price: Decimal, currency: str) -> str:
    """El motivo del cambio de niveles al ampliar: «Ampliación: compra del 25/09/2026: …»."""
    return f"Ampliación: {trade_text(day, units, price, currency)}"


def kept_levels_text(update: LevelUpdate, day: date, units: Decimal, price: Decimal,
                     currency: str) -> str:
    """La nota del historial cuando el usuario amplía y mantiene los niveles de la tesis."""
    return (
        "Ampliación: mantienes los niveles de la tesis\n"
        f"En la {trade_text(day, units, price, currency)}, con stop "
        f"{format_level(update.stop, update.currency)} y objetivo "
        f"{format_level(update.target, update.currency)}."
    )


def close_reason(
    day: date, stop_hit: tuple[Decimal, Decimal] | None, user_reason: str = ""
) -> str:
    """El motivo del cierre de una tesis al venderlo todo (GUIA §5.5). `stop_hit`, si hoy saltó
    su stop: (precio, stop) por unidad y en EUR."""
    texto = f"Venta total el {day:%d/%m/%Y}."
    if stop_hit is not None:
        precio, stop = stop_hit
        texto += (
            f" Hoy saltó su stop: cotizaba a {format_eur_price(precio)} con el stop en "
            f"{format_eur_price(stop)}."
        )
    if user_reason.strip():
        texto += f" Motivo: {user_reason.strip()}"
    return texto


def close_event_text(pnl_eur: Decimal, reason: str) -> str:
    """El texto del evento de cierre: el PnL realizado y el motivo."""
    return f"PnL realizado {format_signed_amount(pnl_eur)} €. {reason}"
