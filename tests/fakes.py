"""Dobles de prueba: los tests no tocan la red, ni la clave, ni el Administrador de credenciales."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2
from keyring.errors import PasswordDeleteError


class FakeKeyring:
    """Administrador de credenciales de mentira, solo en memoria.

    Tiene la misma cara que el módulo `keyring` en lo que usa Sharky, así que se puede poner
    en su sitio con monkeypatch. Como el de verdad, borrar lo que no existe da error.
    """

    def __init__(self) -> None:
        self.almacen: dict[tuple[str, str], str] = {}
        self.backend_fijado = False

    def set_keyring(self, backend: object) -> None:
        self.backend_fijado = True

    def get_keyring(self) -> FakeKeyring:
        return self

    def set_password(self, servicio: str, usuario: str, clave: str) -> None:
        self.almacen[(servicio, usuario)] = clave

    def get_password(self, servicio: str, usuario: str) -> str | None:
        return self.almacen.get((servicio, usuario))

    def delete_password(self, servicio: str, usuario: str) -> None:
        if (servicio, usuario) not in self.almacen:
            raise PasswordDeleteError("Password not found")
        del self.almacen[(servicio, usuario)]


class FakeClaude:
    """Cliente de Claude de mentira: hace de `anthropic.Anthropic` y nunca sale a la red.

    Se usa como la clase (se llama con los mismos argumentos) y apunta con qué se creó cada
    cliente y qué se le pidió. Con `error`, `models.list` lanza esa excepción.
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.creados: list[dict] = []
        self.llamadas: list[tuple[str, dict]] = []
        self.cerrados = 0

    def __call__(self, **kwargs: object) -> FakeClaude:
        self.creados.append(kwargs)
        return self

    @property
    def models(self) -> FakeClaude:
        return self

    def list(self, **kwargs: object) -> list[SimpleNamespace]:
        self.llamadas.append(("models.list", kwargs))
        if self.error is not None:
            raise self.error
        return [SimpleNamespace(id="claude-sonnet-5")]

    def close(self) -> None:
        self.cerrados += 1


_PETICION = httpx2.Request("GET", "https://api.anthropic.com/v1/models")


def api_status_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    """Un error HTTP del SDK, como los que lanza de verdad (401, 403, 429, 500…)."""
    return cls(f"HTTP {status}", response=httpx2.Response(status, request=_PETICION), body=None)


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=_PETICION)


def timeout_error() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=_PETICION)


# -- mercado ------------------------------------------------------------------------------


class FakeMarket:
    """Mercado de mentira: hace de `PriceProvider` y de `FxProvider` sin salir a la red.

    - `quotes`: el precio que «da Yahoo» de cada símbolo (sharky.services.market.Quote).
    - `rates`: el cambio a EUR de cada divisa (FxQuote).
    - `offline`: todo falla como si no hubiera conexión.
    - `gate`: si se da, cada descarga espera a que se abra (para ver que la ventana no se
      congela o para cancelar a mitad).

    Apunta lo que se le pide en `asked` y `asked_fx`.
    """

    def __init__(self, quotes=None, rates=None, *, offline=False, suggestions=None, gate=None,
                 search_error=None):
        self.quotes = dict(quotes or {})
        self.rates = dict(rates or {})
        self.offline = offline
        self.suggestions = dict(suggestions or {})
        self.gate = gate
        self.search_error = search_error
        self.asked: list[list[str]] = []
        self.asked_fx: list[list[str]] = []
        self.searched: list[str] = []

    def fetch_quotes(self, symbols, progress=None, cancel=None):
        from sharky.services.market import FailureKind, FetchFailure, FetchResult

        simbolos = list(symbols)
        self.asked.append(simbolos)
        citas, fallos = {}, {}
        for hechos, simbolo in enumerate(simbolos, 1):
            if self.gate is not None:
                self.gate.wait(10)
            if cancel is not None and cancel.is_set():
                for resto in simbolos[hechos - 1:]:
                    fallos[resto] = FetchFailure(resto, FailureKind.CANCELLED, "Cancelada.")
                return FetchResult(citas, fallos, cancelled=True)
            if self.offline:
                fallos[simbolo] = FetchFailure(simbolo, FailureKind.NETWORK,
                                               "No se ha podido conectar con Yahoo.")
            elif simbolo in self.quotes:
                citas[simbolo] = self.quotes[simbolo]
            else:
                fallos[simbolo] = FetchFailure(
                    simbolo, FailureKind.NO_DATA,
                    f"Yahoo no tiene cotizaciones recientes de {simbolo}.",
                )
            if progress is not None:
                progress(hechos, len(simbolos))
        return FetchResult(citas, fallos)

    def fetch_fx(self, currencies, cancel=None):
        from sharky.services.market import FailureKind, FetchFailure, FxResult

        divisas = list(currencies)
        self.asked_fx.append(divisas)
        cambios, fallos = {}, {}
        for divisa in divisas:
            if self.offline:
                fallos[divisa] = FetchFailure(divisa, FailureKind.NETWORK,
                                              "No se ha podido conectar con Yahoo.")
            elif divisa in self.rates:
                cambios[divisa] = self.rates[divisa]
            else:
                fallos[divisa] = FetchFailure(divisa, FailureKind.NO_DATA, "Sin cambio.")
        return FxResult(cambios, fallos)

    def search_isin(self, isin):
        from sharky.services.market import MarketError

        self.searched.append(isin)
        if self.search_error is not None:
            raise MarketError(self.search_error)
        return list(self.suggestions.get(isin, []))


class FakeTicker:
    """Hace de `yfinance.Ticker` para un símbolo: cierres y metadatos inventados."""

    def __init__(self, yahoo, symbol):
        self._yahoo = yahoo
        self.symbol = symbol
        self.history_metadata = {}

    def history(self, **kwargs):
        import pandas

        self._yahoo.calls.append(self.symbol)
        self._yahoo.kwargs.append(kwargs)
        errores = self._yahoo.errors.get(self.symbol)
        if errores:
            raise errores.pop(0)
        serie = self._yahoo.series.get(self.symbol)
        if serie is None:
            return pandas.DataFrame()
        cierres, divisa, nombre = serie
        indice = pandas.DatetimeIndex(
            [pandas.Timestamp(dia) for dia, _ in cierres]
        ).tz_localize("Europe/Madrid")
        self.history_metadata = {"currency": divisa, "longName": nombre}
        return pandas.DataFrame({"Close": [valor for _, valor in cierres]}, index=indice)


class FakeYahoo:
    """yfinance de mentira para `YahooMarket(ticker_factory=…, search_factory=…, sleep=…)`.

    - `series[símbolo] = ([(fecha, cierre), …], divisa, nombre)`.
    - `errors[símbolo] = [excepción, …]`: se lanzan por orden en las primeras peticiones.
    - `search_quotes[consulta] = [dict, …]`: lo que devuelve una búsqueda.

    Apunta cada petición (`calls`) y cada espera (`sleeps`).
    """

    def __init__(self, series=None, errors=None, search_quotes=None):
        self.series = dict(series or {})
        self.errors = {k: list(v) for k, v in (errors or {}).items()}
        self.search_quotes = dict(search_quotes or {})
        self.search_errors: list[BaseException] = []
        self.calls: list[str] = []
        self.kwargs: list[dict] = []
        self.sleeps: list[float] = []
        self.searches: list[tuple[str, dict]] = []

    def ticker(self, symbol):
        return FakeTicker(self, symbol)

    def search(self, query, **kwargs):
        self.searches.append((query, kwargs))
        if self.search_errors:
            raise self.search_errors.pop(0)
        return SimpleNamespace(quotes=list(self.search_quotes.get(query, [])))

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def market(self, **kwargs):
        from sharky.services.market import YahooMarket

        return YahooMarket(
            ticker_factory=self.ticker, search_factory=self.search, sleep=self.sleep, **kwargs
        )
