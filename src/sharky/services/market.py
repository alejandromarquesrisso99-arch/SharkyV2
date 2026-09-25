"""Precios y tipos de cambio (GUIA §5.3 y §7, H5).

`PriceProvider` y `FxProvider` son lo que el resto del programa sabe del mercado; `YahooMarket`
los cumple con yfinance. Los tests usan dobles sin red (tests/fakes.py).

**Por lotes y con la divisa real.** `yf.download` no devuelve la divisa y se traga el error de
límite de Yahoo, así que no sirve. Cada símbolo se pide con `Ticker.history(period="5d")`, que
en la misma petición trae los cierres de los últimos 5 días y los metadatos con la divisa en la
que cotiza de verdad. Los símbolos van en lotes de 10, con una pausa entre lotes.

**Reintentos.** Si Yahoo limita las peticiones, los símbolos limitados de ese lote se reintentan
hasta 3 veces, esperando 2, 4 y 8 segundos. Sin red no se insiste: al primer fallo de conexión
sin ningún precio conseguido, se da la descarga por perdida y la app trabaja con lo guardado.

**La divisa la manda el proveedor.** Si Yahoo dice que un activo cotiza en otra divisa, el
precio se guarda en la de Yahoo, el activo se corrige y se avisa.

La caché de yfinance vive en la carpeta de datos (`cache\\`). Todo lo de aquí se ejecuta en un
hilo de trabajo: nunca desde el hilo de la interfaz.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from sqlite3 import Connection
from typing import Any, Protocol

from sharky import paths
from sharky.core.ledger import build_ledger
from sharky.core.models import Asset, AssetClass, FxRate, Price, PriceSource
from sharky.core.valuation import (
    BASE_CURRENCY,
    Valuation,
    fx_currency,
    fx_symbol,
    normalize_currency,
    value_portfolio,
)
from sharky.services.db import Database
from sharky.services.repositories import (
    AssetRepository,
    CashMovementRepository,
    FxRateRepository,
    PriceRepository,
    ThesisRepository,
    TradeRepository,
)

log = logging.getLogger(__name__)

LOT_SIZE = 10
LOT_PAUSE_S = 1.0
#: Hasta 3 reintentos si Yahoo limita, con estas esperas.
RATE_LIMIT_WAITS_S: tuple[float, ...] = (2.0, 4.0, 8.0)
HISTORY_PERIOD = "5d"
REQUEST_TIMEOUT_S = 15
SEARCH_RESULTS = 8
#: Yahoo sirve los precios con la precisión de un float de 32 bits (unas 7 cifras): lo que
#: venga detrás es ruido (125.30000305… es 125,3).
SIGNIFICANT_DIGITS = 7

_SYMBOL = re.compile(r"^\S{1,32}$")
_SECTOR = re.compile(r"^\S{1,60}$")

#: `progress(hechos, total)`, desde el hilo de trabajo.
Progress = Callable[[int, int], None]


def local_now() -> datetime:
    """El reloj de la app: la hora local, con su desfase horario."""
    return datetime.now().astimezone()


# -- lo que devuelve un proveedor -------------------------------------------------------


class FailureKind(StrEnum):
    RATE_LIMIT = "LIMITE"
    NETWORK = "SIN_CONEXION"
    NO_DATA = "SIN_DATOS"
    CANCELLED = "CANCELADO"


@dataclass(frozen=True)
class Quote:
    """El último cierre de un símbolo, en la divisa en la que cotiza según el proveedor."""

    symbol: str
    price: Decimal
    currency: str
    close_date: date
    name: str | None = None


@dataclass(frozen=True)
class FxQuote:
    """Cuántos EUR vale una unidad de la divisa, según el último cierre de su par."""

    currency: str
    rate_to_eur: Decimal
    rate_date: date


@dataclass(frozen=True)
class FetchFailure:
    """Por qué no se ha obtenido un símbolo. El mensaje se puede enseñar."""

    symbol: str
    kind: FailureKind
    message: str


@dataclass(frozen=True)
class FetchResult:
    """Lo que ha dado pedir unos símbolos."""

    quotes: dict[str, Quote] = field(default_factory=dict)
    failures: dict[str, FetchFailure] = field(default_factory=dict)
    cancelled: bool = False

    @property
    def offline(self) -> bool:
        """No se ha conseguido nada y todo ha fallado por la conexión."""
        return (
            not self.quotes
            and bool(self.failures)
            and all(f.kind is FailureKind.NETWORK for f in self.failures.values())
        )


@dataclass(frozen=True)
class FxResult:
    """Lo que ha dado pedir unos tipos de cambio, por divisa (USD, GBP…)."""

    rates: dict[str, FxQuote] = field(default_factory=dict)
    failures: dict[str, FetchFailure] = field(default_factory=dict)
    cancelled: bool = False


@dataclass(frozen=True)
class SymbolSuggestion:
    """Un símbolo de Yahoo que corresponde a un ISIN."""

    symbol: str
    name: str
    exchange: str
    quote_type: str


class MarketError(Exception):
    """El mercado no ha podido responder. El mensaje se puede enseñar."""


class PriceProvider(Protocol):
    def fetch_quotes(
        self,
        symbols: Sequence[str],
        progress: Progress | None = None,
        cancel: threading.Event | None = None,
    ) -> FetchResult: ...

    def search_isin(self, isin: str) -> list[SymbolSuggestion]: ...


class FxProvider(Protocol):
    def fetch_fx(
        self, currencies: Sequence[str], cancel: threading.Event | None = None
    ) -> FxResult: ...


# -- yfinance ---------------------------------------------------------------------------


def configure_yfinance(cache_dir: Path | None = None) -> Path:
    """Caché de yfinance en la carpeta de datos y errores a la vista (para distinguir «sin red»
    de «Yahoo no tiene ese símbolo»). Devuelve la carpeta de la caché."""
    import yfinance

    carpeta = Path(cache_dir) if cache_dir is not None else paths.cache_dir()
    carpeta.mkdir(parents=True, exist_ok=True)
    yfinance.set_tz_cache_location(str(carpeta))
    yfinance.config.debug.hide_exceptions = False
    return carpeta


class _NoData(Exception):
    """Yahoo ha respondido, pero sin lo que hace falta."""


def _significant(value: Any) -> Decimal:
    """Un número de Yahoo como Decimal, sin el ruido del float."""
    try:
        numero = Decimal(repr(float(value)))
    except (TypeError, ValueError, InvalidOperation) as error:
        raise _NoData(f"precio ilegible ({value!r})") from error
    if not numero.is_finite() or numero <= 0:
        raise _NoData(f"precio no válido ({value!r})")
    return numero.quantize(Decimal(1).scaleb(numero.adjusted() - SIGNIFICANT_DIGITS + 1))


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return value.date()  # pandas.Timestamp


def _chunks(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for inicio in range(0, len(items), size):
        yield list(items[inicio : inicio + size])


def _classify(error: BaseException) -> FailureKind:
    from yfinance.exceptions import YFException, YFRateLimitError

    if isinstance(error, YFRateLimitError):
        return FailureKind.RATE_LIMIT
    if isinstance(error, _NoData | YFException):
        return FailureKind.NO_DATA
    if isinstance(error, OSError | TimeoutError):  # curl_cffi: sus errores son OSError
        return FailureKind.NETWORK
    return FailureKind.NO_DATA


def _describe(kind: FailureKind, symbol: str, error: BaseException | None = None) -> str:
    if kind is FailureKind.RATE_LIMIT:
        return (
            f"Yahoo ha limitado las peticiones y se ha reintentado {len(RATE_LIMIT_WAITS_S)} "
            "veces sin éxito."
        )
    if kind is FailureKind.NETWORK:
        return "No se ha podido conectar con Yahoo."
    if kind is FailureKind.CANCELLED:
        return "Descarga cancelada."
    if isinstance(error, _NoData):
        return f"Yahoo no ha dado un precio válido de {symbol}: {error}."
    return f"Yahoo no tiene cotizaciones recientes de {symbol}."


class YahooMarket:
    """`PriceProvider` y `FxProvider` con yfinance.

    `ticker_factory`, `search_factory` y `sleep` se pueden sustituir (los tests lo hacen para
    no salir a la red ni esperar de verdad). Con los de verdad, yfinance se importa y se
    configura la primera vez que se usa.
    """

    def __init__(
        self,
        *,
        ticker_factory: Callable[[str], Any] | None = None,
        search_factory: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        cache_dir: Path | None = None,
        lot_size: int = LOT_SIZE,
        lot_pause_s: float = LOT_PAUSE_S,
        rate_limit_waits_s: Sequence[float] = RATE_LIMIT_WAITS_S,
    ) -> None:
        self._ticker_factory = ticker_factory
        self._search_factory = search_factory
        self._cache_dir = cache_dir
        self._ready = threading.Lock()
        self._sleep = sleep
        self.lot_size = max(1, lot_size)
        self.lot_pause_s = lot_pause_s
        self.rate_limit_waits_s = tuple(rate_limit_waits_s)

    # -- yfinance, la primera vez que hace falta ---------------------------------------

    def _factories(self) -> tuple[Callable[[str], Any], Callable[..., Any]]:
        """Importar yfinance (y pandas) cuesta un par de segundos: se hace en el hilo de
        trabajo que lo necesita por primera vez, no al arrancar la app."""
        with self._ready:
            if self._ticker_factory is None or self._search_factory is None:
                import yfinance

                configure_yfinance(self._cache_dir)
                self._ticker_factory = self._ticker_factory or yfinance.Ticker
                self._search_factory = self._search_factory or yfinance.Search
            return self._ticker_factory, self._search_factory

    # -- esperas ---------------------------------------------------------------------

    def _wait(self, seconds: float, cancel: threading.Event | None) -> None:
        if seconds <= 0:
            return
        if self._sleep is not None:
            self._sleep(seconds)
        elif cancel is not None:
            cancel.wait(seconds)  # «Cancelar» corta la espera
        else:
            time.sleep(seconds)

    # -- precios ---------------------------------------------------------------------

    def _fetch_one(self, symbol: str) -> Quote:
        fabrica, _ = self._factories()
        ticker = fabrica(symbol)
        datos = ticker.history(
            period=HISTORY_PERIOD,
            interval="1d",
            auto_adjust=False,
            actions=False,
            timeout=REQUEST_TIMEOUT_S,
        )
        if datos is None or getattr(datos, "empty", True) or "Close" not in datos:
            raise _NoData("sin cotizaciones en los últimos 5 días")
        cierres = datos["Close"].dropna()
        if cierres.empty:
            raise _NoData("sin cotizaciones en los últimos 5 días")
        metadatos = ticker.history_metadata or {}
        divisa = normalize_currency(str(metadatos.get("currency") or ""))
        if divisa is None:
            raise _NoData("no dice en qué divisa cotiza")
        nombre = metadatos.get("longName") or metadatos.get("shortName") or None
        return Quote(
            symbol=symbol,
            price=_significant(cierres.iloc[-1]),
            currency=divisa,
            close_date=_as_date(cierres.index[-1]),
            name=str(nombre) if nombre else None,
        )

    def fetch_quotes(
        self,
        symbols: Sequence[str],
        progress: Progress | None = None,
        cancel: threading.Event | None = None,
    ) -> FetchResult:
        """El último cierre de cada símbolo, por lotes y con reintentos. Nunca lanza."""
        simbolos = list(dict.fromkeys(s.strip() for s in symbols if s and s.strip()))
        total = len(simbolos)
        citas: dict[str, Quote] = {}
        fallos: dict[str, FetchFailure] = {}
        hechos = 0

        def avanzar() -> None:
            nonlocal hechos
            hechos += 1
            if progress is not None:
                progress(hechos, total)

        def cancelado() -> bool:
            return cancel is not None and cancel.is_set()

        def abandonar(resto: Sequence[str], kind: FailureKind) -> None:
            for s in resto:
                if s not in citas and s not in fallos:
                    fallos[s] = FetchFailure(s, kind, _describe(kind, s))

        for numero, lote in enumerate(_chunks(simbolos, self.lot_size)):
            if numero:
                self._wait(self.lot_pause_s, cancel)
            por_intentar = lote
            for intento in range(len(self.rate_limit_waits_s) + 1):
                if intento:
                    log.info("Yahoo limita: reintento %d de %s dentro de %.0f s",
                             intento, ", ".join(por_intentar),
                             self.rate_limit_waits_s[intento - 1])
                    self._wait(self.rate_limit_waits_s[intento - 1], cancel)
                limitados: list[str] = []
                for simbolo in por_intentar:
                    if cancelado():
                        abandonar(simbolos, FailureKind.CANCELLED)
                        return FetchResult(citas, fallos, cancelled=True)
                    try:
                        citas[simbolo] = self._fetch_one(simbolo)
                    except Exception as error:
                        tipo = _classify(error)
                        if tipo is FailureKind.RATE_LIMIT:
                            limitados.append(simbolo)
                            continue
                        log.info("Sin precio de %s: %s: %s", simbolo, type(error).__name__,
                                 error)
                        fallos[simbolo] = FetchFailure(simbolo, tipo, _describe(tipo, simbolo,
                                                                                error))
                        if tipo is FailureKind.NETWORK and not citas:
                            log.warning("Sin conexión con Yahoo: se usan los precios guardados")
                            abandonar(simbolos, FailureKind.NETWORK)
                            return FetchResult(citas, fallos)
                    avanzar()
                por_intentar = limitados
                if not por_intentar:
                    break
            for simbolo in por_intentar:
                fallos[simbolo] = FetchFailure(
                    simbolo, FailureKind.RATE_LIMIT, _describe(FailureKind.RATE_LIMIT, simbolo)
                )
                avanzar()
        return FetchResult(citas, fallos)

    # -- tipos de cambio -------------------------------------------------------------

    def fetch_fx(
        self, currencies: Sequence[str], cancel: threading.Event | None = None
    ) -> FxResult:
        """El cambio a EUR de cada divisa (USD, GBP…) con el par `XXXEUR=X`. Nunca lanza."""
        pares = {fx_symbol(c): c for c in dict.fromkeys(currencies) if c != BASE_CURRENCY}
        resultado = self.fetch_quotes(list(pares), cancel=cancel)
        cambios: dict[str, FxQuote] = {}
        fallos: dict[str, FetchFailure] = {}
        for par, divisa in pares.items():
            cita = resultado.quotes.get(par)
            if cita is None:
                fallo = resultado.failures.get(par)
                if fallo is not None:
                    fallos[divisa] = replace(fallo, symbol=divisa)
            elif cita.currency != BASE_CURRENCY:
                fallos[divisa] = FetchFailure(
                    divisa, FailureKind.NO_DATA,
                    f"El par {par} no viene en EUR sino en {cita.currency}.",
                )
            else:
                cambios[divisa] = FxQuote(divisa, cita.price, cita.close_date)
        return FxResult(cambios, fallos, resultado.cancelled)

    # -- sugerencia por ISIN ---------------------------------------------------------

    def search_isin(self, isin: str) -> list[SymbolSuggestion]:
        """Los símbolos de Yahoo de un ISIN. Lanza MarketError si Yahoo no responde."""
        _, buscar = self._factories()
        for intento in range(len(self.rate_limit_waits_s) + 1):
            try:
                busqueda = buscar(
                    isin,
                    max_results=SEARCH_RESULTS,
                    news_count=0,
                    lists_count=0,
                    include_cb=False,
                    recommended=0,
                    timeout=REQUEST_TIMEOUT_S,
                    raise_errors=True,
                )
                citas = list(getattr(busqueda, "quotes", None) or [])
                break
            except Exception as error:
                tipo = _classify(error)
                if tipo is FailureKind.RATE_LIMIT and intento < len(self.rate_limit_waits_s):
                    self._wait(self.rate_limit_waits_s[intento], None)
                    continue
                log.info("Búsqueda por ISIN fallida: %s: %s", type(error).__name__, error)
                if tipo is FailureKind.NO_DATA:
                    raise MarketError("Yahoo no ha sabido buscar ese ISIN ahora.") from error
                raise MarketError(_describe(tipo, isin)) from error
        return [
            SymbolSuggestion(
                symbol=str(c["symbol"]),
                name=str(c.get("longname") or c.get("shortname") or ""),
                exchange=str(c.get("exchDisp") or c.get("exchange") or ""),
                quote_type=str(c.get("typeDisp") or c.get("quoteType") or ""),
            )
            for c in citas
            if isinstance(c, dict) and c.get("symbol")
        ]


# -- actualizar precios ----------------------------------------------------------------


@dataclass(frozen=True)
class CurrencyChange:
    """Yahoo dice que un activo cotiza en otra divisa que la declarada: manda Yahoo."""

    ticker: str
    declared: str
    provider: str


@dataclass(frozen=True)
class MarketRefresh:
    """Lo que ha dado «Actualizar precios». Lo guardado lleva la hora `fetched_at`."""

    fetched_at: datetime
    requested: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    fx_updated: tuple[str, ...] = ()
    without_symbol: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    currency_changes: tuple[CurrencyChange, ...] = ()
    offline: bool = False
    cancelled: bool = False

    @property
    def messages(self) -> list[str]:
        """Avisos para enseñar, por orden de importancia."""
        avisos: list[str] = []
        if self.offline:
            avisos.append("Sin conexión con Yahoo: se usan los precios guardados.")
        if self.cancelled:
            avisos.append("Descarga cancelada: lo descargado hasta entonces se ha guardado.")
        for c in self.currency_changes:
            avisos.append(
                f"{c.ticker}: la divisa declarada era {c.declared}, pero Yahoo cotiza en "
                f"{c.provider}. Se usa {c.provider}."
            )
        if not self.offline:
            avisos.extend(self.failures)
        return avisos


def _open_positions(conn: Connection) -> tuple[dict[str, Asset], list[str]]:
    activos = {a.ticker: a for a in AssetRepository(conn).list_all()}
    libro = build_ledger(TradeRepository(conn).list_all())
    return activos, sorted(libro.positions)


def refresh_market(
    db: Database,
    prices: PriceProvider,
    fx: FxProvider,
    now: datetime,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
) -> MarketRefresh:
    """Descarga los precios de las posiciones abiertas y los cambios que hagan falta, y lo
    guarda todo en una transacción. Se ejecuta en un hilo de trabajo."""
    ahora = now.replace(microsecond=0)  # así se guarda: MERCADO es «fetched_at == esta hora»
    conn = db.connection()
    activos, abiertas = _open_positions(conn)
    por_simbolo: dict[str, list[str]] = {}
    sin_simbolo: list[str] = []
    for ticker in abiertas:
        activo = activos.get(ticker)
        if activo is not None and activo.yahoo_symbol:
            por_simbolo.setdefault(activo.yahoo_symbol, []).append(ticker)
        else:
            sin_simbolo.append(ticker)

    resultado = prices.fetch_quotes(sorted(por_simbolo), progress, cancel)

    # Los cambios que hacen falta: los de la divisa del precio nuevo, o del guardado, o la
    # declarada si todavía no hay ninguno.
    guardados = PriceRepository(conn).latest_all()
    divisas: set[str] = set()
    for simbolo, tickers in por_simbolo.items():
        for ticker in tickers:
            cita = resultado.quotes.get(simbolo)
            if cita is not None:
                divisa = cita.currency
            elif ticker in guardados:
                divisa = guardados[ticker].currency
            else:
                divisa = activos[ticker].currency
            base = fx_currency(divisa)
            if base is not None:
                divisas.add(base)
    # Y los de las divisas de los niveles de las tesis (GUIA §5.6): un stop en USD de una
    # acción que cotiza en EUR se compara en EUR con el cambio del dólar.
    for tesis in ThesisRepository(conn).list_active():
        base = fx_currency(tesis.levels_currency)
        if tesis.ticker in abiertas and base is not None:
            divisas.add(base)
    cancelado = resultado.cancelled or (cancel is not None and cancel.is_set())
    cambios = FxResult()
    if divisas and not resultado.offline and not cancelado:
        cambios = fx.fetch_fx(sorted(divisas), cancel)
        cancelado = cancelado or cambios.cancelled

    actualizados: list[str] = []
    cambios_divisa: list[CurrencyChange] = []
    with db.transaction() as tx:
        rep_precios = PriceRepository(tx)
        rep_activos = AssetRepository(tx)
        for simbolo, cita in resultado.quotes.items():
            for ticker in por_simbolo.get(simbolo, []):
                rep_precios.save(Price(ticker, cita.close_date, cita.price, cita.currency,
                                       PriceSource.MARKET, ahora))
                actualizados.append(ticker)
                activo = activos[ticker]
                if cita.currency != activo.currency:
                    rep_activos.update(replace(activo, currency=cita.currency))
                    cambios_divisa.append(CurrencyChange(ticker, activo.currency, cita.currency))
                    log.warning("%s: la divisa declarada era %s y Yahoo cotiza en %s; manda "
                                "Yahoo", ticker, activo.currency, cita.currency)
        rep_cambios = FxRateRepository(tx)
        for divisa, cambio in cambios.rates.items():
            rep_cambios.save(FxRate(divisa, cambio.rate_date, cambio.rate_to_eur,
                                    PriceSource.MARKET, ahora))

    fallos: list[str] = []
    for simbolo, fallo in resultado.failures.items():
        if fallo.kind is FailureKind.CANCELLED:
            continue
        for ticker in por_simbolo.get(simbolo, []):
            fallos.append(f"{ticker} ({simbolo}): {fallo.message}")
    for divisa, fallo in cambios.failures.items():
        if fallo.kind is not FailureKind.CANCELLED:
            fallos.append(f"Tipo de cambio {divisa}→EUR: {fallo.message}")

    refresco = MarketRefresh(
        fetched_at=ahora,
        requested=tuple(sorted(t for ts in por_simbolo.values() for t in ts)),
        updated=tuple(sorted(actualizados)),
        fx_updated=tuple(sorted(cambios.rates)),
        without_symbol=tuple(sin_simbolo),
        failures=tuple(fallos),
        currency_changes=tuple(cambios_divisa),
        offline=resultado.offline,
        cancelled=cancelado,
    )
    log.info(
        "Precios actualizados: %d de %d posiciones con símbolo, %d cambios%s%s",
        len(refresco.updated), len(refresco.requested), len(refresco.fx_updated),
        " (sin conexión)" if refresco.offline else "",
        " (cancelado)" if refresco.cancelled else "",
    )
    return refresco


def load_valuation(conn: Connection, now: datetime, market_at: datetime | None = None) -> Valuation:
    """La cartera valorada con lo que hay guardado. `market_at` es la hora de la última
    actualización de esta sesión: lo descargado entonces cuenta como MERCADO."""
    libro = build_ledger(TradeRepository(conn).list_all())
    return value_portfolio(
        libro.positions.values(),
        {a.ticker: a for a in AssetRepository(conn).list_all()},
        CashMovementRepository(conn).balance(),
        PriceRepository(conn).latest_all(),
        FxRateRepository(conn).latest_all(),
        now,
        market_at,
    )


# -- editar un activo y probar un símbolo ------------------------------------------------


class AssetEditError(ValueError):
    """Los datos del activo no valen. El mensaje se puede enseñar."""


def edit_asset(
    conn: Connection,
    ticker: str,
    *,
    yahoo_symbol: str | None,
    sector: str | None,
    asset_class: AssetClass,
) -> Asset:
    """«Editar activo»: símbolo, sector y clase. Va dentro de la transacción de quien llama.

    Si cambia el símbolo, los precios guardados de ese ticker se borran: eran de otro valor.
    """
    simbolo = (yahoo_symbol or "").strip() or None
    sector_limpio = (sector or "").strip() or None
    if simbolo is not None and not _SYMBOL.match(simbolo):
        raise AssetEditError("El símbolo no puede llevar espacios (por ejemplo, SAN.MC).")
    if sector_limpio is not None and not _SECTOR.match(sector_limpio):
        raise AssetEditError("El sector no puede llevar espacios (usa «_»: Renta_Variable).")
    activos = AssetRepository(conn)
    actual = activos.get(ticker)
    if actual is None:
        raise AssetEditError(f"No existe el activo {ticker}.")
    nuevo = replace(actual, yahoo_symbol=simbolo, sector=sector_limpio, asset_class=asset_class)
    activos.update(nuevo)
    if nuevo.yahoo_symbol != actual.yahoo_symbol:
        borrados = PriceRepository(conn).delete_for(ticker)
        log.info("%s: símbolo %s → %s (%d precios guardados borrados)",
                 ticker, actual.yahoo_symbol, nuevo.yahoo_symbol, borrados)
    return nuevo


def probe_symbol(prices: PriceProvider, symbol: str) -> Quote:
    """«Probar»: el último cierre de un símbolo. Lanza MarketError con el motivo si no hay."""
    simbolo = symbol.strip()
    if not simbolo:
        raise MarketError("Escribe un símbolo.")
    resultado = prices.fetch_quotes([simbolo])
    cita = resultado.quotes.get(simbolo)
    if cita is not None:
        return cita
    fallo = resultado.failures.get(simbolo)
    raise MarketError(fallo.message if fallo else f"Yahoo no reconoce {simbolo}.")
