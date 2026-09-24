"""Valoración de la cartera a precio de mercado (GUIA §5.3).

Pura: recibe las posiciones, los activos, el efectivo, los precios y tipos de cambio que se
conocen y la hora. No descarga nada, no lee la base de datos ni el reloj.

Procedencia de un precio o de un tipo de cambio, por orden de preferencia:

- **MERCADO**: descargado en la última actualización (la de `market_at`).
- **CACHE**: guardado hace menos de 24 horas.
- **ANTIGUO**: el último conocido, más viejo.
- **COSTE**: nunca se obtuvo precio: la posición se valora a su coste medio.

Solo MERCADO y CACHE son fiables. La procedencia guardada en la base de datos no se usa: se
recalcula cada vez con la hora de descarga, porque lo que era MERCADO al guardarlo es CACHE al
día siguiente.

La procedencia de una posición es la peor entre la de su precio y la de su tipo de cambio: un
precio de hoy convertido con un cambio de hace una semana no es fiable. Un precio sin ningún
tipo de cambio conocido no se puede pasar a euros, así que esa posición va a COSTE. Jamás se
inventa un precio ni un cambio.

Divisas: `EUR` no necesita cambio; el resto usa el par de Yahoo `XXXEUR=X`; las acciones de
Londres cotizan en peniques (`GBp`) y su cambio es el de la libra entre 100.

Cobertura = parte del NAV con valor fiable. El efectivo cuenta como fiable: se conoce al
céntimo. La valoración es fiable con una cobertura del 90 % o más.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sharky.core.ledger import Position
from sharky.core.models import Asset, FxRate, Price, PriceSource

ZERO = Decimal("0")

BASE_CURRENCY = "EUR"
PENCE = "GBp"
POUND = "GBP"
PENCE_PER_POUND = Decimal("100")

#: Un precio o un cambio guardado hace menos de esto es CACHE; si no, ANTIGUO.
CACHE_MAX_AGE = timedelta(hours=24)
#: Una valoración es fiable con una cobertura del 90 % o más (GUIA §5.4).
RELIABLE_COVERAGE = Decimal("0.90")

#: Así se agrupan las posiciones sin sector en la exposición por sector.
NO_SECTOR = "Sin sector"

_CURRENCY = re.compile(r"^[A-Za-z]{3}$")
_PREFERENCE: tuple[PriceSource, ...] = (
    PriceSource.MARKET,
    PriceSource.CACHE,
    PriceSource.STALE,
    PriceSource.COST,
)


# -- divisas ----------------------------------------------------------------------------


def normalize_currency(text: str) -> str | None:
    """Divisa de cotización: 3 letras en mayúsculas, salvo los peniques de Londres.

    `GBX` (en cualquier forma) y `GBp` se guardan como `GBp`; `GBP` son libras y se quedan
    igual. Devuelve None si no son 3 letras.
    """
    limpio = text.strip()
    if not _CURRENCY.match(limpio):
        return None
    if limpio == PENCE or limpio.upper() == "GBX":
        return PENCE
    return limpio.upper()


def fx_currency(currency: str) -> str | None:
    """La divisa cuyo cambio a EUR hace falta para `currency`: None si ya es EUR, y la libra
    para los peniques."""
    if currency == BASE_CURRENCY:
        return None
    if currency == PENCE:
        return POUND
    return currency


def fx_symbol(currency: str) -> str:
    """El par de Yahoo con el cambio a EUR: `USD` → `USDEUR=X`."""
    return f"{currency}{BASE_CURRENCY}=X"


def to_eur_rate(currency: str, fx: FxRate | None) -> Decimal | None:
    """Cuántos EUR vale una unidad de `currency` con el cambio `fx` (el de su divisa base).

    EUR vale 1; un penique vale la centésima parte de una libra. None si hace falta un cambio
    y no lo hay.
    """
    if currency == BASE_CURRENCY:
        return Decimal("1")
    if fx is None:
        return None
    if currency == PENCE:
        return fx.rate_to_eur / PENCE_PER_POUND
    return fx.rate_to_eur


# -- procedencia ------------------------------------------------------------------------


def source_of(
    fetched_at: datetime | None, now: datetime, market_at: datetime | None = None
) -> PriceSource:
    """Procedencia de algo que se descargó en `fetched_at`, vista en `now`.

    MERCADO si es de la última actualización (`market_at`); CACHE si tiene menos de 24 h;
    ANTIGUO si es más viejo o no se sabe cuándo se descargó.
    """
    if fetched_at is None:
        return PriceSource.STALE
    if market_at is not None and fetched_at == market_at:
        return PriceSource.MARKET
    if now - fetched_at < CACHE_MAX_AGE:
        return PriceSource.CACHE
    return PriceSource.STALE


def worst_source(*sources: PriceSource | None) -> PriceSource:
    """La menos preferida de varias procedencias (las None no cuentan)."""
    presentes = [s for s in sources if s is not None]
    if not presentes:
        raise ValueError("Hace falta al menos una procedencia")
    return max(presentes, key=_PREFERENCE.index)


# -- resultado --------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionValue:
    """Una posición valorada.

    `price` es el último precio conocido, en la divisa del proveedor; puede existir aunque la
    posición vaya a COSTE (si falta su tipo de cambio). `note` explica por qué no es fiable.
    """

    ticker: str
    name: str
    sector: str | None
    units: Decimal
    cost_eur: Decimal
    value_eur: Decimal
    weight: Decimal
    source: PriceSource
    price: Price | None = None
    price_source: PriceSource = PriceSource.COST
    fx: FxRate | None = None
    fx_source: PriceSource | None = None
    fx_to_eur: Decimal | None = None
    note: str = ""

    @property
    def avg_cost_eur(self) -> Decimal:
        return self.cost_eur / self.units

    @property
    def reliable(self) -> bool:
        return self.source.reliable

    @property
    def at_cost(self) -> bool:
        return self.source is PriceSource.COST

    @property
    def price_currency(self) -> str | None:
        return self.price.currency if self.price is not None else None

    @property
    def pnl_eur(self) -> Decimal | None:
        """Valor − coste. None si se valora a coste: ahí no hay nada que medir."""
        if self.at_cost:
            return None
        return self.value_eur - self.cost_eur

    @property
    def pnl_pct(self) -> Decimal | None:
        """PnL sobre el coste, como fracción."""
        pnl = self.pnl_eur
        if pnl is None or self.cost_eur == 0:
            return None
        return pnl / self.cost_eur


@dataclass(frozen=True)
class SectorExposure:
    """Lo que pesa un sector en el NAV."""

    sector: str
    value_eur: Decimal
    weight: Decimal
    tickers: tuple[str, ...]

    def exceeds(self, limit: Decimal) -> bool:
        """Supera el tope (fracción: 0.25 es un 25 %)."""
        return self.weight > limit


@dataclass(frozen=True)
class Valuation:
    """La cartera valorada en `valued_at`."""

    valued_at: datetime
    cash_eur: Decimal
    nav_eur: Decimal
    coverage: Decimal
    positions: tuple[PositionValue, ...]
    sectors: tuple[SectorExposure, ...]
    market_at: datetime | None = None

    @property
    def reliable(self) -> bool:
        """Cobertura del 90 % o más."""
        return self.coverage >= RELIABLE_COVERAGE

    @property
    def cash_weight(self) -> Decimal:
        return self.cash_eur / self.nav_eur if self.nav_eur > 0 else ZERO

    @property
    def invested_eur(self) -> Decimal:
        return sum((p.value_eur for p in self.positions), ZERO)

    @property
    def at_cost(self) -> tuple[PositionValue, ...]:
        return tuple(p for p in self.positions if p.at_cost)

    @property
    def unreliable(self) -> tuple[PositionValue, ...]:
        return tuple(p for p in self.positions if not p.reliable)

    def position(self, ticker: str) -> PositionValue | None:
        return next((p for p in self.positions if p.ticker == ticker), None)

    def count(self, source: PriceSource) -> int:
        return sum(1 for p in self.positions if p.source is source)


# -- cálculo ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Priced:
    """Lo que sale de mirar el precio y el cambio de una posición, antes de conocer el NAV."""

    value_eur: Decimal
    source: PriceSource
    price: Price | None = None
    price_source: PriceSource = PriceSource.COST
    fx: FxRate | None = None
    fx_source: PriceSource | None = None
    fx_to_eur: Decimal | None = None
    note: str = ""


def _price_position(
    position: Position,
    asset: Asset | None,
    price: Price | None,
    fx_rates: Mapping[str, FxRate],
    now: datetime,
    market_at: datetime | None,
) -> _Priced:
    a_coste = position.cost_eur
    if asset is None or not asset.yahoo_symbol:
        return _Priced(
            a_coste,
            PriceSource.COST,
            note="Sin símbolo de cotización: se valora a coste hasta que se lo asignes.",
        )
    if price is None:
        return _Priced(
            a_coste,
            PriceSource.COST,
            note="Todavía no se ha obtenido ningún precio: se valora a coste.",
        )

    origen_precio = source_of(price.fetched_at, now, market_at)
    base = fx_currency(price.currency)
    cambio = fx_rates.get(base) if base is not None else None
    en_eur = to_eur_rate(price.currency, cambio)
    if en_eur is None:
        return _Priced(
            a_coste,
            PriceSource.COST,
            price=price,
            price_source=origen_precio,
            note=f"Sin tipo de cambio {base}→EUR: se valora a coste.",
        )
    origen_cambio = source_of(cambio.fetched_at, now, market_at) if cambio else None
    origen = worst_source(origen_precio, origen_cambio)
    nota = ""
    if not origen.reliable:
        if not origen_precio.reliable:
            nota = "El último precio tiene más de 24 horas."
        else:
            nota = f"El tipo de cambio {base}→EUR tiene más de 24 horas."
    return _Priced(
        position.units * price.price * en_eur,
        origen,
        price=price,
        price_source=origen_precio,
        fx=cambio,
        fx_source=origen_cambio,
        fx_to_eur=en_eur,
        note=nota,
    )


def value_portfolio(
    positions: Iterable[Position],
    assets: Mapping[str, Asset],
    cash_eur: Decimal,
    prices: Mapping[str, Price],
    fx_rates: Mapping[str, FxRate],
    now: datetime,
    market_at: datetime | None = None,
) -> Valuation:
    """Valora la cartera.

    - `prices`: el último precio conocido de cada ticker (en la divisa del proveedor).
    - `fx_rates`: el último cambio a EUR de cada divisa base (USD, GBP…; nunca GBp).
    - `market_at`: la hora de la última actualización: lo descargado entonces es MERCADO.
    """
    posiciones = sorted(positions, key=lambda p: p.ticker)
    valorados = [
        (
            p,
            assets.get(p.ticker),
            _price_position(
                p, assets.get(p.ticker), prices.get(p.ticker), fx_rates, now, market_at
            ),
        )
        for p in posiciones
    ]
    nav = cash_eur + sum((v.value_eur for _, _, v in valorados), ZERO)

    def peso(valor: Decimal) -> Decimal:
        return valor / nav if nav > 0 else ZERO

    resultado = tuple(
        PositionValue(
            ticker=p.ticker,
            name=activo.name if activo is not None else p.ticker,
            sector=activo.sector if activo is not None else None,
            units=p.units,
            cost_eur=p.cost_eur,
            value_eur=v.value_eur,
            weight=peso(v.value_eur),
            source=v.source,
            price=v.price,
            price_source=v.price_source,
            fx=v.fx,
            fx_source=v.fx_source,
            fx_to_eur=v.fx_to_eur,
            note=v.note,
        )
        for p, activo, v in valorados
    )

    fiable = cash_eur + sum((p.value_eur for p in resultado if p.reliable), ZERO)
    cobertura = fiable / nav if nav > 0 else ZERO

    return Valuation(
        valued_at=now,
        cash_eur=cash_eur,
        nav_eur=nav,
        coverage=cobertura,
        positions=resultado,
        sectors=_sectors(resultado, peso),
        market_at=market_at,
    )


def _sectors(
    positions: Iterable[PositionValue], peso: Callable[[Decimal], Decimal]
) -> tuple[SectorExposure, ...]:
    valores: dict[str, Decimal] = {}
    tickers: dict[str, list[str]] = {}
    for p in positions:
        sector = p.sector or NO_SECTOR
        valores[sector] = valores.get(sector, ZERO) + p.value_eur
        tickers.setdefault(sector, []).append(p.ticker)
    exposicion = [
        SectorExposure(s, valores[s], peso(valores[s]), tuple(tickers[s])) for s in valores
    ]
    exposicion.sort(key=lambda e: (-e.value_eur, e.sector))
    return tuple(exposicion)
