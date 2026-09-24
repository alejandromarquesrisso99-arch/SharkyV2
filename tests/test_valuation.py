"""La valoración (GUIA §5.3): procedencias, divisas, cobertura, pesos, PnL y sectores.

core/valuation.py es pura: aquí se le dan los precios y la hora a mano.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from sharky.core.ledger import Position
from sharky.core.models import Asset, FxRate, Price, PriceSource
from sharky.core.valuation import (
    CACHE_MAX_AGE,
    NO_SECTOR,
    fx_currency,
    fx_symbol,
    normalize_currency,
    source_of,
    to_eur_rate,
    value_portfolio,
    worst_source,
)

MADRID = timezone(timedelta(hours=2))
AHORA = datetime(2026, 9, 24, 18, 0, tzinfo=MADRID)
HOY = date(2026, 9, 24)
MERCADO, CACHE, ANTIGUO, COSTE = (
    PriceSource.MARKET,
    PriceSource.CACHE,
    PriceSource.STALE,
    PriceSource.COST,
)


def activo(ticker, divisa="EUR", simbolo=True, sector=None):
    return Asset(ticker, f"Empresa {ticker}", divisa,
                 yahoo_symbol=f"{ticker}.X" if simbolo else None, sector=sector)


def posicion(ticker, unidades, coste_total):
    return Position(ticker, D(unidades), D(coste_total))


def precio(ticker, valor, divisa="EUR", descargado=AHORA):
    return Price(ticker, HOY, D(valor), divisa, PriceSource.MARKET, descargado)


def cambio(divisa, valor, descargado=AHORA):
    return FxRate(divisa, HOY, D(valor), PriceSource.MARKET, descargado)


def valorar(posiciones, activos, efectivo="0", precios=(), cambios=(), market_at=AHORA):
    return value_portfolio(
        posiciones,
        {a.ticker: a for a in activos},
        D(efectivo),
        {p.ticker: p for p in precios},
        {c.currency: c for c in cambios},
        AHORA,
        market_at,
    )


# -- divisas ------------------------------------------------------------------------------


def test_eur_usd_y_gbp_se_pasan_a_euros():
    v = valorar(
        [posicion("SAN", "200", "900"), posicion("AAPL", "10", "1500"),
         posicion("VOD", "1000", "900")],
        [activo("SAN"), activo("AAPL", "USD"), activo("VOD", "GBp")],
        precios=[precio("SAN", "5.12"), precio("AAPL", "215.40", "USD"),
                 precio("VOD", "125.30", "GBp")],
        cambios=[cambio("USD", "0.85"), cambio("GBP", "1.16")],
    )
    assert v.position("SAN").value_eur == D("1024.00")
    assert v.position("AAPL").value_eur == D("1830.9000")  # 10 × 215,40 × 0,85
    # Los peniques son la centésima parte de una libra: 1000 × 125,30 × 1,16 / 100.
    assert v.position("VOD").value_eur == D("1453.48")
    assert v.position("VOD").fx_to_eur == D("0.0116")
    assert all(p.source is MERCADO for p in v.positions)


def test_eur_no_necesita_tipo_de_cambio():
    v = valorar([posicion("SAN", "10", "40")], [activo("SAN")], precios=[precio("SAN", "5")])
    p = v.position("SAN")
    assert p.fx is None and p.fx_source is None
    assert p.fx_to_eur == D("1")


def test_ayudantes_de_divisas():
    assert fx_currency("EUR") is None
    assert fx_currency("GBp") == "GBP"
    assert fx_currency("USD") == "USD"
    assert fx_symbol("USD") == "USDEUR=X"
    assert fx_symbol("GBP") == "GBPEUR=X"
    assert to_eur_rate("GBp", cambio("GBP", "1.20")) == D("0.012")
    assert to_eur_rate("USD", None) is None
    assert normalize_currency("GBX") == "GBp"
    assert normalize_currency("gbp") == "GBP"
    assert normalize_currency("usd") == "USD"
    assert normalize_currency("dólar") is None


def test_un_precio_sin_tipo_de_cambio_se_valora_a_coste_y_no_se_inventa_el_cambio():
    v = valorar([posicion("AAPL", "10", "1500")], [activo("AAPL", "USD")],
                precios=[precio("AAPL", "215.40", "USD")])
    p = v.position("AAPL")
    assert p.source is COSTE
    assert p.value_eur == D("1500")
    assert p.price is not None and p.price.price == D("215.40")  # se enseña, pero no se usa
    assert "USD→EUR" in p.note
    assert p.pnl_eur is None


# -- procedencias -------------------------------------------------------------------------


def test_sin_simbolo_va_a_coste():
    v = valorar([posicion("VUSA", "12", "1140")], [activo("VUSA", simbolo=False)],
                precios=[precio("VUSA", "99")])  # aunque hubiera un precio guardado
    p = v.position("VUSA")
    assert p.source is COSTE
    assert p.value_eur == D("1140")
    assert "símbolo" in p.note
    assert p.pnl_eur is None and p.pnl_pct is None


def test_con_simbolo_pero_sin_ningun_precio_va_a_coste():
    v = valorar([posicion("SAN", "10", "45")], [activo("SAN")])
    assert v.position("SAN").source is COSTE
    assert v.position("SAN").value_eur == D("45")


def test_mercado_es_lo_descargado_en_la_ultima_actualizacion():
    descarga = AHORA - timedelta(minutes=10)
    assert source_of(descarga, AHORA, market_at=descarga) is MERCADO
    assert source_of(descarga, AHORA, market_at=None) is CACHE
    assert source_of(descarga - timedelta(minutes=1), AHORA, market_at=descarga) is CACHE


@pytest.mark.parametrize(
    ("antiguedad", "esperada"),
    [
        (timedelta(0), CACHE),
        (timedelta(hours=23, minutes=59, seconds=59), CACHE),
        (CACHE_MAX_AGE, ANTIGUO),  # 24 h justas ya no es «menos de 24 h»
        (timedelta(days=3), ANTIGUO),
    ],
)
def test_frontera_de_las_24_horas(antiguedad, esperada):
    assert source_of(AHORA - antiguedad, AHORA) is esperada


def test_sin_hora_de_descarga_es_antiguo():
    assert source_of(None, AHORA) is ANTIGUO


def test_procedencias_por_orden_de_preferencia():
    assert worst_source(MERCADO, CACHE) is CACHE
    assert worst_source(CACHE, ANTIGUO) is ANTIGUO
    assert worst_source(ANTIGUO, COSTE) is COSTE
    assert worst_source(MERCADO, None) is MERCADO
    assert CACHE.reliable and MERCADO.reliable
    assert not ANTIGUO.reliable and not COSTE.reliable


def test_la_posicion_toma_la_peor_procedencia_del_precio_y_del_cambio():
    viejo = AHORA - timedelta(days=2)
    v = valorar(
        [posicion("AAPL", "10", "1500"), posicion("MSFT", "1", "300")],
        [activo("AAPL", "USD"), activo("MSFT", "USD")],
        precios=[precio("AAPL", "200", "USD"), precio("MSFT", "400", "USD", viejo)],
        cambios=[cambio("USD", "0.9", viejo)],
    )
    aapl = v.position("AAPL")
    assert aapl.price_source is MERCADO
    assert aapl.fx_source is ANTIGUO
    assert aapl.source is ANTIGUO  # precio de hoy con un cambio de hace dos días
    assert "tipo de cambio" in aapl.note
    assert aapl.value_eur == D("1800.0")  # se valora con lo último conocido, pero no es fiable
    assert v.position("MSFT").source is ANTIGUO
    assert "precio" in v.position("MSFT").note


def test_un_cambio_guardado_antes_de_la_migracion_no_tiene_hora_y_es_antiguo():
    sin_hora = FxRate("USD", HOY, D("0.9"), PriceSource.MARKET)
    v = valorar([posicion("AAPL", "1", "100")], [activo("AAPL", "USD")],
                precios=[precio("AAPL", "200", "USD")], cambios=[sin_hora])
    assert v.position("AAPL").fx_source is ANTIGUO
    assert v.position("AAPL").source is ANTIGUO


def test_cache_de_menos_de_24_horas_es_fiable():
    hace_un_rato = AHORA - timedelta(hours=5)
    v = valorar([posicion("SAN", "100", "400")], [activo("SAN")],
                precios=[precio("SAN", "5", descargado=hace_un_rato)])
    assert v.position("SAN").source is CACHE
    assert v.position("SAN").reliable


# -- cobertura, pesos y PnL ---------------------------------------------------------------


def test_cobertura_con_el_efectivo_como_fiable():
    v = valorar(
        [posicion("SAN", "100", "400"), posicion("TTE", "10", "600"),
         posicion("VUSA", "1", "100")],
        [activo("SAN"), activo("TTE"), activo("VUSA", simbolo=False)],
        efectivo="1000",
        precios=[precio("SAN", "5"), precio("TTE", "50", descargado=AHORA - timedelta(days=5))],
    )
    # NAV = 1000 + 500 (fiable) + 500 (antiguo) + 100 (coste) = 2100.
    assert v.nav_eur == D("2100")
    assert v.coverage == D("1500") / D("2100")
    assert not v.reliable
    assert [p.ticker for p in v.unreliable] == ["TTE", "VUSA"]
    assert [p.ticker for p in v.at_cost] == ["VUSA"]
    assert v.count(MERCADO) == 1 and v.count(ANTIGUO) == 1 and v.count(COSTE) == 1


def test_cobertura_del_90_por_ciento_es_fiable_y_por_debajo_no():
    justa = valorar([posicion("SAN", "1", "10"), posicion("VUSA", "1", "10")],
                    [activo("SAN"), activo("VUSA", simbolo=False)],
                    efectivo="80", precios=[precio("SAN", "10")])
    assert justa.coverage == D("0.9")
    assert justa.reliable
    corta = valorar([posicion("SAN", "1", "10"), posicion("VUSA", "1", "10.01")],
                    [activo("SAN"), activo("VUSA", simbolo=False)],
                    efectivo="80", precios=[precio("SAN", "10")])
    assert corta.coverage < D("0.9")
    assert not corta.reliable


def test_solo_efectivo_tiene_cobertura_completa():
    v = valorar([], [], efectivo="2500")
    assert v.nav_eur == D("2500")
    assert v.coverage == 1
    assert v.cash_weight == 1


def test_una_cartera_vacia_no_divide_entre_cero():
    v = valorar([], [], efectivo="0")
    assert v.nav_eur == 0
    assert v.coverage == 0
    assert not v.reliable
    assert v.cash_weight == 0


def test_pesos_y_pnl():
    v = valorar(
        [posicion("SAN", "200", "900"), posicion("TTE", "30", "1920")],
        [activo("SAN"), activo("TTE")],
        efectivo="1000",
        precios=[precio("SAN", "5.12"), precio("TTE", "61.80")],
    )
    san, tte = v.position("SAN"), v.position("TTE")
    assert v.nav_eur == D("1000") + D("1024.00") + D("1854.00")
    assert san.weight == D("1024.00") / v.nav_eur
    assert san.weight + tte.weight + v.cash_weight == pytest.approx(1)
    assert san.pnl_eur == D("124.00")
    assert san.pnl_pct == D("124.00") / D("900")
    assert tte.pnl_eur == D("-66.00")
    assert san.avg_cost_eur == D("4.5")


# -- sectores -----------------------------------------------------------------------------


def test_exposicion_por_sector():
    v = valorar(
        [posicion("AAPL", "1", "100"), posicion("MSFT", "1", "100"), posicion("SAN", "1", "100"),
         posicion("RAR", "1", "100")],
        [activo("AAPL", sector="Tecnologia"), activo("MSFT", sector="Tecnologia"),
         activo("SAN", sector="Banca"), activo("RAR")],
        efectivo="100",
        precios=[precio("AAPL", "300"), precio("MSFT", "200"), precio("SAN", "100"),
                 precio("RAR", "50")],
    )
    sectores = {s.sector: s for s in v.sectors}
    assert list(sectores) == ["Tecnologia", "Banca", NO_SECTOR]  # de más a menos peso
    tecnologia = sectores["Tecnologia"]
    assert tecnologia.value_eur == D("500")
    assert tecnologia.weight == D("500") / D("750")
    assert tecnologia.tickers == ("AAPL", "MSFT")
    assert tecnologia.exceeds(D("0.25"))
    assert not sectores[NO_SECTOR].exceeds(D("0.25"))
    assert sum(s.weight for s in v.sectors) + v.cash_weight == pytest.approx(1)


def test_un_sector_justo_en_el_tope_no_lo_supera():
    v = valorar([posicion("SAN", "1", "25")], [activo("SAN", sector="Banca")],
                efectivo="75", precios=[precio("SAN", "25")])
    assert v.sectors[0].weight == D("0.25")
    assert not v.sectors[0].exceeds(D("0.25"))


def test_la_valoracion_no_usa_la_procedencia_guardada():
    """Lo guardado como MERCADO ayer es CACHE o ANTIGUO hoy: se recalcula con la hora."""
    ayer = AHORA - timedelta(days=1, hours=1)
    guardado = Price("SAN", HOY, D("5"), "EUR", PriceSource.MARKET, ayer)
    v = valorar([posicion("SAN", "1", "4")], [activo("SAN")], precios=[guardado])
    assert v.position("SAN").source is ANTIGUO
