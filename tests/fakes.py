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


#: Lo que contesta el Claude de mentira si no se le ha preparado otra cosa.
RESPUESTA_POR_DEFECTO = (
    "SAN cae y se acerca a su stop; el resto apenas se mueve.\n\n"
    "## Conclusión del día\n"
    "- Vigilar el stop de SAN.\n"
)


def mensaje_claude(texto: str = RESPUESTA_POR_DEFECTO, *, stop: str = "end_turn",
                   entrada: int = 2_000, salida: int = 600, razonamiento: bool = False,
                   busquedas: int = 0, modelo: str = "claude-sonnet-5",
                   bloques: list | None = None) -> SimpleNamespace:
    """Un mensaje final como el de `get_final_message()`: bloques, `stop_reason` y `usage`.
    Con `razonamiento`, lleva un bloque de razonamiento (vacío, como en Sonnet 5). Con
    `bloques`, esos en lugar del texto (para búsquedas y citas: `busqueda()`, `texto_citado()`)."""
    contenido = []
    if razonamiento:
        contenido.append(SimpleNamespace(type="thinking", thinking=""))
    if bloques is not None:
        contenido.extend(bloques)
    elif texto:
        contenido.append(SimpleNamespace(type="text", text=texto, citations=None))
    uso = SimpleNamespace(
        input_tokens=entrada,
        output_tokens=salida,
        server_tool_use=SimpleNamespace(web_search_requests=busquedas) if busquedas else None,
    )
    return SimpleNamespace(content=contenido, stop_reason=stop, usage=uso, model=modelo)


def busqueda(consulta: str, *urls: str) -> list[SimpleNamespace]:
    """Una búsqueda web del servidor: el bloque de la consulta y el de sus resultados."""
    ident = "srvtoolu_" + "".join(c for c in consulta if c.isalnum())[:24]
    return [
        SimpleNamespace(type="server_tool_use", id=ident, name="web_search",
                        input={"query": consulta}),
        SimpleNamespace(type="web_search_tool_result", tool_use_id=ident, content=[
            SimpleNamespace(type="web_search_result", url=u, title=f"Título de {u}",
                            encrypted_content="cifrado", page_age=None)
            for u in urls
        ]),
    ]


def texto_citado(texto: str, *citas: tuple[str, str]) -> SimpleNamespace:
    """Un bloque de texto con sus citas de la búsqueda web: (url, título)."""
    return SimpleNamespace(type="text", text=texto, citations=[
        SimpleNamespace(type="web_search_result_location", url=url, title=titulo,
                        cited_text="…", encrypted_index="cifrado")
        for url, titulo in citas
    ])


def extraccion(valor: object, *, entrada: int = 3_000, salida: int = 400,
               stop: str = "end_turn", modelo: str = "claude-sonnet-5") -> SimpleNamespace:
    """Lo que devuelve `messages.parse`: el objeto ya validado en `parsed_output`."""
    uso = SimpleNamespace(input_tokens=entrada, output_tokens=salida, server_tool_use=None)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="{}")],
                           parsed_output=valor, stop_reason=stop, usage=uso, model=modelo)


class FakeStream:
    """Hace de `MessageStream`: unos cuantos eventos y el mensaje final."""

    def __init__(self, claude: FakeClaude, respuesta: object) -> None:
        self._claude = claude
        self._respuesta = respuesta

    def __enter__(self) -> FakeStream:
        if isinstance(self._respuesta, BaseException):
            raise self._respuesta
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def __iter__(self):
        for n in range(3):
            if self._claude.durante_stream is not None:
                self._claude.durante_stream(n)
            yield SimpleNamespace(type="content_block_delta", index=n)

    def get_final_message(self) -> object:
        return self._respuesta


class FakeClaude:
    """Cliente de Claude de mentira: hace de `anthropic.Anthropic` y nunca sale a la red.

    Se usa como la clase (se llama con los mismos argumentos) y apunta con qué se creó cada
    cliente y qué se le pidió. Con `error`, `models.list` lanza esa excepción.

    `messages.stream(...)` contesta, por orden, lo que haya en `respuestas` (un mensaje de
    `mensaje_claude` o una excepción que se lanza al abrir el streaming); si no queda nada, un
    mensaje con `RESPUESTA_POR_DEFECTO`. `al_llamar(kwargs)` se ejecuta en cada llamada (para
    mirar la base de datos en ese momento) y `durante_stream(n)` con cada evento.

    `messages.parse(...)` contesta, por orden, lo que haya en `extracciones` (un mensaje de
    `extraccion()` o una excepción); si no queda nada, `por_defecto_parse(kwargs)` o, sin él,
    una extracción vacía (sin datos).
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.creados: list[dict] = []
        self.llamadas: list[tuple[str, dict]] = []
        self.cerrados = 0
        self.respuestas: list[object] = []
        self.extracciones: list[object] = []
        self.por_defecto_parse = None
        self.modelos: list[str] = ["claude-sonnet-5"]
        self.al_llamar = None
        self.durante_stream = None

    def __call__(self, **kwargs: object) -> FakeClaude:
        self.creados.append(kwargs)
        return self

    @property
    def models(self) -> FakeClaude:
        return self

    @property
    def messages(self) -> SimpleNamespace:
        return SimpleNamespace(stream=self._stream, parse=self._parse)

    def list(self, **kwargs: object) -> list[SimpleNamespace]:
        self.llamadas.append(("models.list", kwargs))
        if self.error is not None:
            raise self.error
        return [SimpleNamespace(id=m) for m in self.modelos]

    def _stream(self, **kwargs: object) -> FakeStream:
        self.llamadas.append(("messages.stream", kwargs))
        if self.al_llamar is not None:
            self.al_llamar(kwargs)
        respuesta = self.respuestas.pop(0) if self.respuestas else mensaje_claude()
        return FakeStream(self, respuesta)

    def _parse(self, **kwargs: object) -> object:
        self.llamadas.append(("messages.parse", kwargs))
        if self.al_llamar is not None:
            self.al_llamar(kwargs)
        if self.extracciones:
            respuesta = self.extracciones.pop(0)
        elif self.por_defecto_parse is not None:
            respuesta = self.por_defecto_parse(kwargs)
        else:
            respuesta = extraccion(None)
        if isinstance(respuesta, BaseException):
            raise respuesta
        return respuesta

    @property
    def streams(self) -> list[dict]:
        """Lo que se pidió en cada `messages.stream`, por orden."""
        return [kwargs for nombre, kwargs in self.llamadas if nombre == "messages.stream"]

    @property
    def parses(self) -> list[dict]:
        """Lo que se pidió en cada `messages.parse`, por orden."""
        return [kwargs for nombre, kwargs in self.llamadas if nombre == "messages.parse"]

    def close(self) -> None:
        self.cerrados += 1


_PETICION = httpx2.Request("GET", "https://api.anthropic.com/v1/models")


def api_status_error(cls: type[anthropic.APIStatusError], status: int, body: object = None,
                     headers: dict | None = None) -> anthropic.APIStatusError:
    """Un error HTTP del SDK, como los que lanza de verdad (401, 403, 429, 500…)."""
    respuesta = httpx2.Response(status, request=_PETICION, headers=headers)
    return cls(f"HTTP {status}", response=respuesta, body=body)


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=_PETICION)


def timeout_error() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=_PETICION)


# -- series de precios inventadas (radar) ---------------------------------------------------


def dias_habiles(hasta, n):
    """Los `n` últimos días de lunes a viernes que acaban en `hasta` (incluido si lo es)."""
    from datetime import timedelta

    dias = []
    dia = hasta
    while len(dias) < n:
        if dia.weekday() < 5:
            dias.append(dia)
        dia -= timedelta(days=1)
    return list(reversed(dias))


def velas(cierres, *, hasta, rango="1", especiales=None):
    """Velas diarias con esos cierres (del más antiguo al último, que cae en `hasta`): máximo y
    mínimo a `rango` del cierre. `especiales[i] = (máximo, mínimo)` cambia la vela `i` (se puede
    contar desde el final con índices negativos)."""
    from decimal import Decimal

    from sharky.core.radar import DailyBar

    r = Decimal(rango)
    dias = dias_habiles(hasta, len(cierres))
    especiales = {(i % len(cierres)): v for i, v in (especiales or {}).items()}
    barras = []
    for i, (dia, cierre) in enumerate(zip(dias, cierres, strict=True)):
        c = Decimal(str(cierre))
        alto, bajo = especiales.get(i, (c + r, c - r))
        barras.append(DailyBar(dia, Decimal(str(alto)), Decimal(str(bajo)), c))
    return tuple(barras)


def subida_y_caida(hasta, *, inicio="80", pico="100", final="78", dias=300, dia_pico=150,
                   rango="1"):
    """Una acción normal que sube de `inicio` a `pico` y cae hasta `final`, con un rango diario
    de ±`rango` (ATR = 2 × rango): unos 14 meses de cotización."""
    from decimal import Decimal

    a, p, f = Decimal(inicio), Decimal(pico), Decimal(final)
    cierres = []
    for i in range(dias):
        if i <= dia_pico:
            valor = a + (p - a) * i / dia_pico
        else:
            valor = p + (f - p) * (i - dia_pico) / (dias - 1 - dia_pico)
        cierres.append(valor.quantize(Decimal("0.01")))
    return velas(cierres, hasta=hasta, rango=rango)


def historico_yahoo(simbolo, barras, divisa="USD", nombre=None):
    """Lo que «da Yahoo» de un símbolo para el radar (HistoryQuote)."""
    from sharky.services.market import HistoryQuote

    return HistoryQuote(simbolo, divisa, tuple(barras), nombre)


# -- mercado ------------------------------------------------------------------------------


class FakeMarket:
    """Mercado de mentira: hace de `PriceProvider` y de `FxProvider` sin salir a la red.

    - `quotes`: el precio que «da Yahoo» de cada símbolo (sharky.services.market.Quote).
    - `rates`: el cambio a EUR de cada divisa (FxQuote).
    - `offline`: todo falla como si no hubiera conexión.
    - `gate`: si se da, cada descarga espera a que se abra (para ver que la ventana no se
      congela o para cancelar a mitad).

    - `histories`: el histórico diario que «da Yahoo» de cada símbolo (HistoryQuote), para el
      radar.

    Apunta lo que se le pide en `asked`, `asked_fx` y `asked_history`.
    """

    def __init__(self, quotes=None, rates=None, *, offline=False, suggestions=None, gate=None,
                 search_error=None, histories=None):
        self.quotes = dict(quotes or {})
        self.rates = dict(rates or {})
        self.offline = offline
        self.suggestions = dict(suggestions or {})
        self.gate = gate
        self.search_error = search_error
        self.histories = dict(histories or {})
        self.asked: list[list[str]] = []
        self.asked_fx: list[list[str]] = []
        self.asked_history: list[list[str]] = []
        self.searched: list[str] = []

    def fetch_history(self, symbols, since, progress=None, cancel=None):
        from sharky.services.market import FailureKind, FetchFailure, HistoryResult

        simbolos = list(symbols)
        self.asked_history.append(simbolos)
        series, fallos = {}, {}
        for hechos, simbolo in enumerate(simbolos, 1):
            if self.gate is not None:
                self.gate.wait(10)
            if cancel is not None and cancel.is_set():
                for resto in simbolos[hechos - 1:]:
                    fallos[resto] = FetchFailure(resto, FailureKind.CANCELLED, "Cancelada.")
                return HistoryResult(series, fallos, cancelled=True)
            if self.offline:
                fallos[simbolo] = FetchFailure(simbolo, FailureKind.NETWORK,
                                               "No se ha podido conectar con Yahoo.")
            elif simbolo in self.histories:
                series[simbolo] = self.histories[simbolo]
            else:
                fallos[simbolo] = FetchFailure(
                    simbolo, FailureKind.NO_DATA,
                    f"Yahoo no tiene cotizaciones recientes de {simbolo}.",
                )
            if progress is not None:
                progress(hechos, len(simbolos))
        return HistoryResult(series, fallos)

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
    """Hace de `yfinance.Ticker` para un símbolo: cierres (o velas) y metadatos inventados."""

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
            [pandas.Timestamp(fila[0]) for fila in cierres]
        ).tz_localize("Europe/Madrid")
        self.history_metadata = {"currency": divisa, "longName": nombre}
        if cierres and len(cierres[0]) == 4:  # (día, máximo, mínimo, cierre)
            return pandas.DataFrame({
                "High": [f[1] for f in cierres],
                "Low": [f[2] for f in cierres],
                "Close": [f[3] for f in cierres],
            }, index=indice)
        return pandas.DataFrame({"Close": [valor for _, valor in cierres]}, index=indice)


class FakeYahoo:
    """yfinance de mentira para `YahooMarket(ticker_factory=…, search_factory=…, sleep=…)`.

    - `series[símbolo] = ([(fecha, cierre), …], divisa, nombre)`, o con velas
      `([(fecha, máximo, mínimo, cierre), …], divisa, nombre)`.
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
