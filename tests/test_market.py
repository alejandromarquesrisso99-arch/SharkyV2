"""Precios y tipos de cambio (GUIA §5.3): lotes, reintentos, divisa real, GBp, caché y
«Actualizar precios» con lo guardado cuando falla la descarga. Sin red: yfinance falso."""

import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from yfinance.exceptions import YFRateLimitError

from fakes import FakeMarket, FakeYahoo
from sharky import paths
from sharky.core.models import (
    Asset,
    AssetClass,
    CashKind,
    CashMovement,
    FxRate,
    Price,
    PriceSource,
    Trade,
    TradeKind,
)
from sharky.services import market
from sharky.services.db import MIGRATIONS, Database
from sharky.services.market import (
    AssetEditError,
    FailureKind,
    FxQuote,
    MarketError,
    Quote,
    YahooMarket,
    configure_yfinance,
    edit_asset,
    load_valuation,
    probe_symbol,
    refresh_market,
)
from sharky.services.repositories import (
    AssetRepository,
    CashMovementRepository,
    FxRateRepository,
    PriceRepository,
    TradeRepository,
)

MADRID = timezone(timedelta(hours=2))
AHORA = datetime(2026, 9, 24, 18, 5, 30, 123456, tzinfo=MADRID)
HOY = date(2026, 9, 24)
AYER = date(2026, 9, 23)


def serie(*cierres, divisa="EUR", nombre="Empresa inventada"):
    """Cierres de los últimos días para el yfinance falso."""
    dias = [date(2026, 9, 24 - len(cierres) + 1 + i) for i in range(len(cierres))]
    return (list(zip(dias, cierres, strict=True)), divisa, nombre)


# -- YahooMarket: lotes, divisa y reintentos ----------------------------------------------


def test_un_lote_devuelve_el_ultimo_cierre_y_la_divisa_real():
    yahoo = FakeYahoo({
        "SAN.MC": serie(12.1, 12.3, 12.527999877929688),
        "VOD.L": serie(124.0, 125.30000305175781, divisa="GBp"),
        "AAPL": serie(215.4, divisa="USD", nombre="Apple Inc."),
    })
    resultado = yahoo.market().fetch_quotes(["SAN.MC", "VOD.L", "AAPL"])
    assert not resultado.failures
    san = resultado.quotes["SAN.MC"]
    assert san.price == D("12.528")  # sin el ruido del float de Yahoo
    assert san.currency == "EUR"
    assert san.close_date == HOY
    assert resultado.quotes["VOD.L"].price == D("125.3")
    assert resultado.quotes["VOD.L"].currency == "GBp"
    assert resultado.quotes["AAPL"].name == "Apple Inc."
    # Últimos 5 días, cierre sin ajustar.
    assert all(k["period"] == "5d" and k["auto_adjust"] is False for k in yahoo.kwargs)


def test_los_huecos_sin_cierre_no_cuentan():
    yahoo = FakeYahoo({"SAN.MC": serie(12.0, float("nan"))})
    cita = yahoo.market().fetch_quotes(["SAN.MC"]).quotes["SAN.MC"]
    assert cita.price == D("12")
    assert cita.close_date == AYER


def test_gbx_se_guarda_como_peniques():
    yahoo = FakeYahoo({"VOD.L": serie(125.3, divisa="GBX")})
    assert yahoo.market().fetch_quotes(["VOD.L"]).quotes["VOD.L"].currency == "GBp"


def test_lotes_de_diez_con_pausa_entre_lotes_y_progreso():
    simbolos = [f"S{i}.MC" for i in range(23)]
    yahoo = FakeYahoo({s: serie(10.0) for s in simbolos})
    avances = []
    resultado = yahoo.market().fetch_quotes(simbolos, progress=lambda h, t: avances.append((h, t)))
    assert len(resultado.quotes) == 23
    assert yahoo.sleeps == [market.LOT_PAUSE_S, market.LOT_PAUSE_S]  # 3 lotes, 2 pausas
    assert avances[-1] == (23, 23)
    assert [h for h, _ in avances] == list(range(1, 24))


def test_si_yahoo_limita_se_reintenta_con_espera():
    yahoo = FakeYahoo(
        {"SAN.MC": serie(12.0), "BBVA.MC": serie(9.0)},
        errors={"SAN.MC": [YFRateLimitError(), YFRateLimitError()]},
    )
    resultado = yahoo.market().fetch_quotes(["SAN.MC", "BBVA.MC"])
    assert set(resultado.quotes) == {"SAN.MC", "BBVA.MC"}
    assert yahoo.sleeps == [2.0, 4.0]
    assert yahoo.calls.count("SAN.MC") == 3
    assert yahoo.calls.count("BBVA.MC") == 1  # solo se reintenta lo limitado


def test_hasta_tres_reintentos_y_luego_se_rinde():
    yahoo = FakeYahoo({"SAN.MC": serie(12.0)}, errors={"SAN.MC": [YFRateLimitError()] * 4})
    resultado = yahoo.market().fetch_quotes(["SAN.MC"])
    assert not resultado.quotes
    fallo = resultado.failures["SAN.MC"]
    assert fallo.kind is FailureKind.RATE_LIMIT
    assert "3 veces" in fallo.message
    assert yahoo.sleeps == [2.0, 4.0, 8.0]
    assert yahoo.calls.count("SAN.MC") == 4  # 1 + 3 reintentos


def test_sin_red_no_se_insiste():
    simbolos = ["A.MC", "B.MC", "C.MC", "D.MC"]
    yahoo = FakeYahoo({s: serie(1.0) for s in simbolos},
                      errors={"A.MC": [ConnectionError("sin red")]})
    resultado = yahoo.market().fetch_quotes(simbolos)
    assert resultado.offline
    assert yahoo.calls == ["A.MC"]  # no se prueba con los demás
    assert all(f.kind is FailureKind.NETWORK for f in resultado.failures.values())
    assert set(resultado.failures) == set(simbolos)


def test_un_fallo_de_red_suelto_no_es_estar_sin_conexion():
    yahoo = FakeYahoo({"A.MC": serie(1.0), "B.MC": serie(2.0), "C.MC": serie(3.0)},
                      errors={"B.MC": [TimeoutError("lento")]})
    resultado = yahoo.market().fetch_quotes(["A.MC", "B.MC", "C.MC"])
    assert not resultado.offline
    assert set(resultado.quotes) == {"A.MC", "C.MC"}
    assert resultado.failures["B.MC"].kind is FailureKind.NETWORK


def test_un_simbolo_que_yahoo_no_conoce():
    yahoo = FakeYahoo({"SAN.MC": serie(12.0)})
    resultado = yahoo.market().fetch_quotes(["SAN.MC", "NOEXISTE.MC"])
    assert set(resultado.quotes) == {"SAN.MC"}
    assert resultado.failures["NOEXISTE.MC"].kind is FailureKind.NO_DATA
    assert "NOEXISTE.MC" in resultado.failures["NOEXISTE.MC"].message
    assert not resultado.offline


class ErrorHttp(OSError):
    """Como el `HTTPError` de curl_cffi (que hereda de OSError): lleva la respuesta."""

    def __init__(self, codigo):
        super().__init__(f"HTTP Error {codigo}: ")
        self.response = SimpleNamespace(status_code=codigo)


def test_un_404_de_yahoo_no_es_estar_sin_conexion():
    """Yahoo contesta 404 a un símbolo que no existe. Aunque sea el primero de la lista, no se
    abandona la descarga como si no hubiera red (H11: un símbolo mal escrito del explorador)."""
    yahoo = FakeYahoo({"B.MC": serie(2.0)}, errors={"AAAA": [ErrorHttp(404)]})
    resultado = yahoo.market().fetch_quotes(["AAAA", "B.MC"])
    assert not resultado.offline
    assert set(resultado.quotes) == {"B.MC"}
    assert resultado.failures["AAAA"].kind is FailureKind.NO_DATA
    assert resultado.failures["AAAA"].message == "Yahoo no tiene cotizaciones recientes de AAAA."


def test_un_429_de_yahoo_es_un_limite_y_se_reintenta():
    yahoo = FakeYahoo({"A.MC": serie(1.0)}, errors={"A.MC": [ErrorHttp(429)]})
    resultado = yahoo.market().fetch_quotes(["A.MC"])
    assert set(resultado.quotes) == {"A.MC"} and yahoo.sleeps == [2.0]


def test_sin_divisa_no_hay_precio():
    yahoo = FakeYahoo({"RARO": ([(HOY, 5.0)], "", "Raro")})
    resultado = yahoo.market().fetch_quotes(["RARO"])
    assert resultado.failures["RARO"].kind is FailureKind.NO_DATA
    assert "divisa" in resultado.failures["RARO"].message


def test_cancelar_antes_de_empezar():
    yahoo = FakeYahoo({"SAN.MC": serie(12.0)})
    cancelar = threading.Event()
    cancelar.set()
    resultado = yahoo.market().fetch_quotes(["SAN.MC"], cancel=cancelar)
    assert resultado.cancelled
    assert yahoo.calls == []


def test_tipos_de_cambio_con_el_par_xxxeur():
    yahoo = FakeYahoo({"USDEUR=X": serie(0.8523), "GBPEUR=X": serie(1.1613),
                       "CHFEUR=X": serie(1.07, divisa="USD")})
    resultado = yahoo.market().fetch_fx(["USD", "GBP", "EUR", "CHF"])
    assert resultado.rates["USD"] == FxQuote("USD", D("0.8523"), HOY)
    assert resultado.rates["GBP"].rate_to_eur == D("1.1613")
    assert "EUR" not in resultado.rates
    assert "EUREUR=X" not in yahoo.calls  # el euro no necesita cambio
    assert "no viene en EUR" in resultado.failures["CHF"].message


def test_sugerencia_de_simbolo_por_isin():
    yahoo = FakeYahoo(search_quotes={"ES0113900J37": [
        {"symbol": "SAN.MC", "exchDisp": "Madrid", "longname": "Banco Santander, S.A.",
         "quoteType": "EQUITY"},
        {"symbol": "BSD2.F", "exchange": "FRA", "shortname": "BANCO SANTANDER"},
        {"exchange": "sin símbolo: no cuenta"},
    ]})
    sugerencias = yahoo.market().search_isin("ES0113900J37")
    assert [s.symbol for s in sugerencias] == ["SAN.MC", "BSD2.F"]
    assert sugerencias[0].name == "Banco Santander, S.A."
    assert sugerencias[0].exchange == "Madrid"
    consulta, argumentos = yahoo.searches[0]
    assert consulta == "ES0113900J37"
    assert argumentos["news_count"] == 0


def test_la_busqueda_por_isin_sin_red_da_un_mensaje():
    yahoo = FakeYahoo()
    yahoo.search_errors = [ConnectionError("sin red")]
    with pytest.raises(MarketError, match="conectar"):
        yahoo.market().search_isin("ES0113900J37")


def test_la_cache_de_yfinance_va_a_la_carpeta_de_datos(monkeypatch):
    import yfinance

    fijadas = []
    monkeypatch.setattr(yfinance, "set_tz_cache_location", fijadas.append)
    antes = yfinance.config.debug.hide_exceptions
    try:
        carpeta = configure_yfinance()
        assert carpeta == paths.cache_dir()
        assert carpeta.is_dir()
        assert fijadas == [str(paths.cache_dir())]
        assert yfinance.config.debug.hide_exceptions is False
    finally:
        yfinance.config.debug.hide_exceptions = antes


def test_yahoo_market_configura_yfinance_la_primera_vez_que_lo_usa(monkeypatch):
    import yfinance

    yahoo = FakeYahoo({"SAN.MC": serie(12.0)})
    fijadas = []
    monkeypatch.setattr(yfinance, "set_tz_cache_location", fijadas.append)
    monkeypatch.setattr(yfinance, "Ticker", yahoo.ticker)
    monkeypatch.setattr(yfinance, "Search", yahoo.search)
    antes = yfinance.config.debug.hide_exceptions
    try:
        mercado = YahooMarket(sleep=yahoo.sleep)
        assert fijadas == []  # crear el mercado no carga nada
        assert mercado.fetch_quotes(["SAN.MC"]).quotes["SAN.MC"].price == D("12")
        assert fijadas == [str(paths.cache_dir())]
    finally:
        yfinance.config.debug.hide_exceptions = antes


# -- actualizar precios y valorar ---------------------------------------------------------

ACTIVOS = (
    Asset("SAN", "Banco Santander", "EUR", yahoo_symbol="SAN.MC", sector="Banca"),
    Asset("AAPL", "Apple Inc.", "USD", yahoo_symbol="AAPL", sector="Tecnologia"),
    Asset("VOD", "Vodafone Group", "GBp", yahoo_symbol="VOD.L", sector="Telecomunicaciones"),
    Asset("VUSA", "Vanguard S&P 500", "EUR", isin="IE00B3XXRP09", sector="Indices"),
)
COSTES = {"SAN": ("200", "4.5"), "AAPL": ("10", "150"), "VOD": ("1000", "0.9"),
          "VUSA": ("12", "95")}

COTIZACIONES = {
    "SAN.MC": Quote("SAN.MC", D("5.12"), "EUR", HOY),
    "AAPL": Quote("AAPL", D("215.4"), "USD", HOY),
    "VOD.L": Quote("VOD.L", D("125.3"), "GBp", HOY),
}
CAMBIOS = {"USD": FxQuote("USD", D("0.85"), HOY), "GBP": FxQuote("GBP", D("1.16"), HOY)}


@pytest.fixture
def cartera(db):
    with db.transaction() as conn:
        for a in ACTIVOS:
            AssetRepository(conn).add(a)
            unidades, coste = COSTES[a.ticker]
            TradeRepository(conn).add(
                Trade(AYER, a.ticker, TradeKind.OPENING, D(unidades), D(coste), "EUR", D(1),
                      D(0), D(unidades) * D(coste))
            )
        CashMovementRepository(conn).add(CashMovement(AYER, CashKind.INITIAL, D("1000")))
    return db


def test_actualizar_guarda_precios_y_cambios_y_valora_a_mercado(cartera):
    mercado = FakeMarket(COTIZACIONES, CAMBIOS)
    refresco = refresh_market(cartera, mercado, mercado, AHORA)

    assert mercado.asked == [["AAPL", "SAN.MC", "VOD.L"]]  # VUSA no tiene símbolo
    assert mercado.asked_fx == [["GBP", "USD"]]  # EUR no necesita cambio; GBp usa la libra
    assert refresco.updated == ("AAPL", "SAN", "VOD")
    assert refresco.fx_updated == ("GBP", "USD")
    assert refresco.without_symbol == ("VUSA",)
    assert refresco.fetched_at == AHORA.replace(microsecond=0)
    assert not refresco.offline and not refresco.messages

    conn = cartera.connection()
    guardado = PriceRepository(conn).latest("SAN")
    assert guardado == Price("SAN", HOY, D("5.12"), "EUR", PriceSource.MARKET,
                             refresco.fetched_at)
    assert FxRateRepository(conn).latest("GBP") == FxRate(
        "GBP", HOY, D("1.16"), PriceSource.MARKET, refresco.fetched_at
    )

    v = load_valuation(conn, AHORA, refresco.fetched_at)
    assert v.position("SAN").source is PriceSource.MARKET
    assert v.position("SAN").value_eur == D("1024.00")
    assert v.position("AAPL").value_eur == D("1830.900")
    assert v.position("VOD").value_eur == D("1453.48")
    assert v.position("VUSA").source is PriceSource.COST
    assert v.position("VUSA").value_eur == D("1140")
    nav = D("1000") + D("1024.00") + D("1830.900") + D("1453.48") + D("1140")
    assert v.nav_eur == nav
    assert v.coverage == (nav - D("1140")) / nav


def test_si_falla_la_descarga_se_usa_lo_guardado_cache_o_antiguo(cartera):
    with cartera.transaction() as conn:
        PriceRepository(conn).save(Price("SAN", AYER, D("5"), "EUR", PriceSource.MARKET,
                                         AHORA - timedelta(hours=3)))
        PriceRepository(conn).save(Price("AAPL", date(2026, 9, 20), D("200"), "USD",
                                         PriceSource.MARKET, AHORA - timedelta(days=4)))
        FxRateRepository(conn).save(FxRate("USD", AYER, D("0.86"), PriceSource.MARKET,
                                           AHORA - timedelta(hours=3)))
    sin_red = FakeMarket(COTIZACIONES, CAMBIOS, offline=True)
    refresco = refresh_market(cartera, sin_red, sin_red, AHORA)

    assert refresco.offline
    assert refresco.updated == ()
    assert sin_red.asked_fx == []  # sin red, no se insiste con los cambios
    assert refresco.messages == ["Sin conexión con Yahoo: se usan los precios guardados."]

    v = load_valuation(cartera.connection(), AHORA, refresco.fetched_at)
    assert v.position("SAN").source is PriceSource.CACHE  # guardado hace 3 horas
    assert v.position("SAN").value_eur == D("1000")
    assert v.position("AAPL").source is PriceSource.STALE  # guardado hace 4 días
    assert v.position("AAPL").value_eur == D("1720.00")  # 10 × 200 × 0,86
    assert v.position("VOD").source is PriceSource.COST  # nunca tuvo precio
    assert v.position("VUSA").source is PriceSource.COST


def test_un_simbolo_que_falla_conserva_su_ultimo_precio_y_se_avisa(cartera):
    with cartera.transaction() as conn:
        PriceRepository(conn).save(Price("SAN", AYER, D("5"), "EUR", PriceSource.MARKET,
                                         AHORA - timedelta(hours=1)))
    cotizaciones = {k: v for k, v in COTIZACIONES.items() if k != "SAN.MC"}
    mercado = FakeMarket(cotizaciones, CAMBIOS)
    refresco = refresh_market(cartera, mercado, mercado, AHORA)
    assert "SAN" not in refresco.updated
    assert any(m.startswith("SAN (SAN.MC):") for m in refresco.messages)
    v = load_valuation(cartera.connection(), AHORA, refresco.fetched_at)
    assert v.position("SAN").source is PriceSource.CACHE
    assert v.position("AAPL").source is PriceSource.MARKET


def test_la_divisa_del_proveedor_manda_y_se_avisa(cartera):
    cotizaciones = dict(COTIZACIONES)
    cotizaciones["AAPL"] = Quote("AAPL", D("200"), "EUR", HOY)  # el activo dice USD
    mercado = FakeMarket(cotizaciones, CAMBIOS)
    refresco = refresh_market(cartera, mercado, mercado, AHORA)

    assert [(c.ticker, c.declared, c.provider) for c in refresco.currency_changes] == [
        ("AAPL", "USD", "EUR")
    ]
    assert any("Yahoo cotiza en EUR" in m for m in refresco.messages)
    conn = cartera.connection()
    assert AssetRepository(conn).get("AAPL").currency == "EUR"
    assert PriceRepository(conn).latest("AAPL").currency == "EUR"
    assert mercado.asked_fx == [["GBP"]]  # ya no hace falta el dólar
    v = load_valuation(conn, AHORA, refresco.fetched_at)
    assert v.position("AAPL").value_eur == D("2000")


def test_cancelar_guarda_lo_descargado_y_no_pide_cambios(cartera):
    cancelar = threading.Event()

    class CancelaTrasElPrimero(FakeMarket):
        def fetch_quotes(self, symbols, progress=None, cancel=None):
            return super().fetch_quotes(
                symbols, progress=lambda h, t: cancelar.set(), cancel=cancel
            )

    mercado = CancelaTrasElPrimero(COTIZACIONES, CAMBIOS)
    refresco = refresh_market(cartera, mercado, mercado, AHORA, cancel=cancelar)
    assert refresco.cancelled
    assert refresco.updated == ("AAPL",)
    assert mercado.asked_fx == []
    assert "Descarga cancelada" in refresco.messages[0]


def test_sin_posiciones_no_se_pide_nada(db):
    with db.transaction() as conn:
        CashMovementRepository(conn).add(CashMovement(AYER, CashKind.INITIAL, D("500")))
    mercado = FakeMarket()
    refresco = refresh_market(db, mercado, mercado, AHORA)
    assert mercado.asked == [[]]
    assert mercado.asked_fx == []
    v = load_valuation(db.connection(), AHORA, refresco.fetched_at)
    assert v.nav_eur == D("500") and v.coverage == 1


def test_despues_de_un_dia_lo_descargado_ya_no_es_de_mercado(cartera):
    mercado = FakeMarket(COTIZACIONES, CAMBIOS)
    refresco = refresh_market(cartera, mercado, mercado, AHORA)
    manana = AHORA + timedelta(days=1)
    v = load_valuation(cartera.connection(), manana)  # otra sesión: sin descarga nueva
    assert v.position("SAN").source is PriceSource.STALE
    v = load_valuation(cartera.connection(), AHORA + timedelta(hours=2))
    assert v.position("SAN").source is PriceSource.CACHE
    assert refresco.fetched_at < AHORA + timedelta(hours=2)


# -- editar un activo y probar un símbolo -------------------------------------------------


def test_editar_activo_cambia_simbolo_sector_y_clase_y_borra_los_precios_viejos(cartera):
    with cartera.transaction() as conn:
        PriceRepository(conn).save(Price("SAN", AYER, D("5"), "EUR", PriceSource.MARKET, AHORA))
        nuevo = edit_asset(conn, "SAN", yahoo_symbol=" SAN.MC2 ", sector="Banca_Europea",
                           asset_class=AssetClass.ETF)
    conn = cartera.connection()
    assert nuevo == AssetRepository(conn).get("SAN")
    assert (nuevo.yahoo_symbol, nuevo.sector, nuevo.asset_class) == (
        "SAN.MC2", "Banca_Europea", AssetClass.ETF
    )
    assert nuevo.currency == "EUR" and nuevo.name == "Banco Santander"
    assert PriceRepository(conn).latest("SAN") is None  # eran de otro valor


def test_editar_sin_cambiar_el_simbolo_conserva_los_precios(cartera):
    with cartera.transaction() as conn:
        PriceRepository(conn).save(Price("SAN", AYER, D("5"), "EUR", PriceSource.MARKET, AHORA))
        edit_asset(conn, "SAN", yahoo_symbol="SAN.MC", sector="", asset_class=AssetClass.STOCK)
    conn = cartera.connection()
    assert PriceRepository(conn).latest("SAN") is not None
    assert AssetRepository(conn).get("SAN").sector is None


def test_asignar_simbolo_a_un_activo_que_no_tenia(cartera):
    with cartera.transaction() as conn:
        edit_asset(conn, "VUSA", yahoo_symbol="VUSA.AS", sector="Indices",
                   asset_class=AssetClass.ETF)
    mercado = FakeMarket({**COTIZACIONES, "VUSA.AS": Quote("VUSA.AS", D("98.6"), "EUR", HOY)},
                         CAMBIOS)
    refresco = refresh_market(cartera, mercado, mercado, AHORA)
    assert "VUSA" in refresco.updated
    v = load_valuation(cartera.connection(), AHORA, refresco.fetched_at)
    assert v.position("VUSA").source is PriceSource.MARKET
    assert v.coverage == 1


@pytest.mark.parametrize(
    ("simbolo", "sector", "mensaje"),
    [("SAN MC", "Banca", "símbolo"), ("SAN.MC", "Banca europea", "sector")],
)
def test_editar_activo_valida(cartera, simbolo, sector, mensaje):
    with pytest.raises(AssetEditError, match=mensaje), cartera.transaction() as conn:
        edit_asset(conn, "SAN", yahoo_symbol=simbolo, sector=sector,
                   asset_class=AssetClass.STOCK)
    assert AssetRepository(cartera.connection()).get("SAN") == ACTIVOS[0]


def test_editar_un_activo_que_no_existe(cartera):
    with pytest.raises(AssetEditError, match="No existe"), cartera.transaction() as conn:
        edit_asset(conn, "NADA", yahoo_symbol=None, sector=None, asset_class=AssetClass.STOCK)


def test_probar_un_simbolo():
    mercado = FakeMarket(COTIZACIONES)
    assert probe_symbol(mercado, " SAN.MC ") == COTIZACIONES["SAN.MC"]
    with pytest.raises(MarketError, match="NOEXISTE"):
        probe_symbol(mercado, "NOEXISTE")
    with pytest.raises(MarketError, match="Escribe"):
        probe_symbol(mercado, "  ")


# -- migración 2 --------------------------------------------------------------------------


def test_la_migracion_2_da_hora_a_los_tipos_de_cambio(tmp_path):
    ruta = tmp_path / "vieja.db"
    vieja = Database(ruta, MIGRATIONS[:1])
    try:
        vieja.migrate()
        with vieja.transaction() as conn:
            conn.execute(
                "INSERT INTO fx_rates (currency, rate_date, rate_to_eur, source) "
                "VALUES ('USD', '2026-09-20', '0.9', 'MERCADO')"
            )
    finally:
        vieja.close_all()
    nueva = Database(ruta, MIGRATIONS[:2])
    try:
        assert nueva.migrate() == 1
        conn = nueva.connection()
        assert FxRateRepository(conn).latest("USD") == FxRate(
            "USD", date(2026, 9, 20), D("0.9"), PriceSource.MARKET, None
        )
        with nueva.transaction() as tx:
            FxRateRepository(tx).save(FxRate("USD", HOY, D("0.85"), PriceSource.MARKET, AHORA))
        assert FxRateRepository(conn).latest("USD").fetched_at == AHORA.replace(microsecond=0)
    finally:
        nueva.close_all()
