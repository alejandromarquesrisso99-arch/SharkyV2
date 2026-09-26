"""Pantalla Cartera (GUIA §5.10, punto 2): tabla, procedencia, exposición por sector,
«Actualizar precios» en segundo plano y «Editar activo» con «Probar» y la sugerencia por ISIN.
"""

import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from fakes import FakeMarket
from sharky.core.models import (
    Asset,
    AssetClass,
    CashKind,
    CashMovement,
    Price,
    PriceSource,
    Thesis,
    Trade,
    TradeKind,
)
from sharky.services.market import FxQuote, Quote, SymbolSuggestion
from sharky.services.repositories import (
    AssetRepository,
    CashMovementRepository,
    PriceRepository,
    ThesisRepository,
    TradeRepository,
)
from sharky.ui.main_window import MainWindow
from sharky.ui.panel import PanelPage
from sharky.ui.portfolio import (
    Col,
    EditAssetDialog,
    PortfolioModel,
    PortfolioPage,
    footer_text,
)
from sharky.ui.theme import Theme, ThemeController
from sharky.ui.trade import TradePage

MADRID = timezone(timedelta(hours=2))
AHORA = datetime(2026, 9, 24, 18, 5, tzinfo=MADRID)
HOY = date(2026, 9, 24)

ACTIVOS = (
    Asset("SAN", "Banco Santander", "EUR", isin="ES0113900J37", yahoo_symbol="SAN.MC",
          sector="Banca"),
    Asset("AAPL", "Apple Inc.", "USD", yahoo_symbol="AAPL", sector="Tecnologia"),
    Asset("VOD", "Vodafone Group", "GBp", yahoo_symbol="VOD.L", sector="Tecnologia"),
    Asset("VUSA", "Vanguard S&P 500", "EUR", isin="IE00B3XXRP09", sector="Indices"),
)
COSTES = {"SAN": ("200", "4.5"), "AAPL": ("10", "150"), "VOD": ("1000", "0.9"),
          "VUSA": ("12", "95")}
COTIZACIONES = {
    "SAN.MC": Quote("SAN.MC", D("5.12"), "EUR", HOY, "Banco Santander, S.A."),
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
                Trade(HOY, a.ticker, TradeKind.OPENING, D(unidades), D(coste), "EUR", D(1),
                      D(0), D(unidades) * D(coste))
            )
        CashMovementRepository(conn).add(CashMovement(HOY, CashKind.INITIAL, D("1000")))
    return db


@pytest.fixture
def tema(qapp):
    return ThemeController(qapp, Theme.LIGHT)


@pytest.fixture
def mercado():
    return FakeMarket(COTIZACIONES, CAMBIOS)


@pytest.fixture
def pagina(qtbot, cartera, tema, mercado):
    p = PortfolioPage(cartera, tema, mercado, mercado, now=lambda: AHORA)
    qtbot.addWidget(p)
    return p


def textos(pagina, columna):
    return {f.position.ticker: PortfolioModel.text(f, columna) for f in pagina.rows()}


def actualizar(qtbot, pagina):
    with qtbot.waitSignal(pagina.refreshFinished, timeout=5000):
        pagina.refresh_button.click()


# -- la tabla -----------------------------------------------------------------------------


def test_sin_precios_todo_va_a_coste_y_lo_dice(pagina):
    assert set(textos(pagina, Col.TICKER)) == {"SAN", "AAPL", "VOD", "VUSA"}
    assert set(textos(pagina, Col.SOURCE).values()) == {"Coste"}
    assert textos(pagina, Col.PRICE)["SAN"] == "—"
    assert textos(pagina, Col.PNL_PCT)["SAN"] == "—"
    assert "Pulsa «Actualizar precios»" in pagina.footer.text()
    assert "no es fiable" in pagina.footer.text()


def test_actualizar_precios_llena_la_tabla(qtbot, pagina, mercado):
    actualizar(qtbot, pagina)
    precio = textos(pagina, Col.PRICE)
    assert precio == {"SAN": "5,12 EUR", "AAPL": "215,40 USD", "VOD": "125,30 GBp", "VUSA": "—"}
    assert textos(pagina, Col.VALUE) == {
        "SAN": "1.024,00", "AAPL": "1.830,90", "VOD": "1.453,48", "VUSA": "1.140,00"
    }
    assert textos(pagina, Col.SOURCE) == {
        "SAN": "Mercado", "AAPL": "Mercado", "VOD": "Mercado", "VUSA": "Coste"
    }
    assert textos(pagina, Col.PNL_EUR)["SAN"] == "+124,00"
    assert textos(pagina, Col.PNL_PCT)["SAN"] == "+13,8 %"
    assert textos(pagina, Col.UNITS)["VOD"] == "1.000"
    assert textos(pagina, Col.WEIGHT)["SAN"] == "15,9 %"  # 1.024,00 de 6.448,38
    pie = pagina.footer.text()
    assert "Cobertura" in pie and "3 de 4 posiciones" in pie
    assert "VUSA: Sin símbolo" in pie
    assert "FIFO" in pie
    assert "hoy a las 18:05" in pie
    assert not pagina.messages_box.isVisible()


def test_el_pnl_negativo_lleva_signo_menos(qtbot, cartera, tema):
    mercado = FakeMarket({**COTIZACIONES, "SAN.MC": Quote("SAN.MC", D("4"), "EUR", HOY)},
                         CAMBIOS)
    p = PortfolioPage(cartera, tema, mercado, mercado, now=lambda: AHORA)
    qtbot.addWidget(p)
    actualizar(qtbot, p)
    assert textos(p, Col.PNL_EUR)["SAN"] == "−100,00"
    assert textos(p, Col.PNL_PCT)["SAN"] == "−11,1 %"


def test_la_tabla_se_ordena(qtbot, pagina):
    actualizar(qtbot, pagina)
    pagina.table.sortByColumn(Col.TICKER, Qt.SortOrder.AscendingOrder)
    assert [f.position.ticker for f in pagina.rows()] == ["AAPL", "SAN", "VOD", "VUSA"]
    pagina.table.sortByColumn(Col.VALUE, Qt.SortOrder.DescendingOrder)
    assert [f.position.ticker for f in pagina.rows()] == ["AAPL", "VOD", "VUSA", "SAN"]
    pagina.table.sortByColumn(Col.PNL_PCT, Qt.SortOrder.DescendingOrder)
    assert [f.position.ticker for f in pagina.rows()][-1] == "VUSA"  # sin PnL, al final


def test_stop_y_objetivo_de_la_tesis_activa(cartera, tema, mercado, qtbot):
    with cartera.transaction() as conn:
        ThesisRepository(conn).add(Thesis("SAN", "EUR", HOY, D("4.5"), D("5.2"), D("7")))
        ThesisRepository(conn).add(Thesis("AAPL", "USD", HOY, D("180")))
    p = PortfolioPage(cartera, tema, mercado, mercado, now=lambda: AHORA)
    qtbot.addWidget(p)
    assert textos(p, Col.STOP) == {"SAN": "5,20 EUR", "AAPL": "—", "VOD": "sin tesis",
                                   "VUSA": "sin tesis"}
    assert textos(p, Col.TARGET)["SAN"] == "7,00 EUR"
    assert textos(p, Col.TARGET)["VOD"] == "—"


def test_la_ayuda_emergente_explica_la_procedencia(qtbot, pagina):
    actualizar(qtbot, pagina)
    fila = next(f for f in pagina.rows() if f.position.ticker == "AAPL")
    ayuda = PortfolioModel.tooltip(fila, Col.SOURCE)
    assert "Procedencia: Mercado" in ayuda
    assert "215,40 USD, cierre del 24/09/2026" in ayuda
    assert "Cambio USD→EUR: 0,85" in ayuda


def test_sin_posiciones_lo_dice(qtbot, db, tema, mercado):
    with db.transaction() as conn:
        CashMovementRepository(conn).add(CashMovement(HOY, CashKind.INITIAL, D("500")))
    p = PortfolioPage(db, tema, mercado, mercado, now=lambda: AHORA)
    qtbot.addWidget(p)
    assert p.rows() == []
    assert not p.empty_label.isHidden()
    assert p.table.isHidden()
    assert p.exposure.rows == [("Efectivo", "100,0 %", False)]


# -- exposición por sector ----------------------------------------------------------------


def test_exposicion_por_sector_frente_al_25(qtbot, pagina):
    actualizar(qtbot, pagina)
    filas = {nombre: (texto, supera) for nombre, texto, supera in pagina.exposure.rows}
    assert list(filas)[-1] == "Efectivo"
    assert filas["Tecnologia"][1] is True  # 1.830,90 + 1.453,48 de 6.448,38: más del 25 %
    assert "supera el 25 %" in filas["Tecnologia"][0]
    assert filas["Banca"][1] is False
    assert "25 %" in pagina.exposure.subtitle.text()


def test_el_tope_por_sector_sale_de_los_ajustes(qtbot, cartera, tema, mercado):
    from sharky.services.settings import Settings

    ajustes = Settings()
    ajustes.mandate.max_sector_weight_pct = 60.0
    p = PortfolioPage(cartera, tema, mercado, mercado, settings=ajustes, now=lambda: AHORA)
    qtbot.addWidget(p)
    actualizar(qtbot, p)
    assert not any(supera for _, _, supera in p.exposure.rows)
    assert "60 %" in p.exposure.subtitle.text()


# -- actualizar en segundo plano ----------------------------------------------------------


def test_la_descarga_no_congela_la_ventana(qtbot, cartera, tema):
    puerta = threading.Event()
    mercado = FakeMarket(COTIZACIONES, CAMBIOS, gate=puerta)
    p = PortfolioPage(cartera, tema, mercado, mercado, now=lambda: AHORA)
    qtbot.addWidget(p)
    p.show()
    p.refresh_button.click()
    # La descarga está parada esperando a Yahoo y la interfaz sigue respondiendo.
    assert p.refreshing
    assert not p.refresh_button.isEnabled()
    assert not p.edit_button.isEnabled()
    assert p.progress_box.isVisible()
    qtbot.wait(50)
    assert p.progress_label.text().startswith("Descargando precios")
    with qtbot.waitSignal(p.refreshFinished, timeout=5000):
        puerta.set()
    assert not p.refreshing
    assert p.refresh_button.isEnabled()
    assert not p.progress_box.isVisible()
    assert textos(p, Col.SOURCE)["SAN"] == "Mercado"


def test_el_progreso_llega_a_la_barra(qtbot, pagina):
    avances = []
    pagina.progress_bar.valueChanged.connect(avances.append)
    actualizar(qtbot, pagina)
    assert avances and avances[-1] == 3
    assert pagina.progress_bar.maximum() == 3


def test_cancelar_la_descarga(qtbot, cartera, tema):
    puerta = threading.Event()
    mercado = FakeMarket(COTIZACIONES, CAMBIOS, gate=puerta)
    p = PortfolioPage(cartera, tema, mercado, mercado, now=lambda: AHORA)
    qtbot.addWidget(p)
    p.show()
    p.refresh_button.click()
    p.cancel_button.click()
    assert p.progress_label.text() == "Cancelando…"
    assert not p.cancel_button.isEnabled()
    with qtbot.waitSignal(p.refreshFinished, timeout=5000):
        puerta.set()
    assert p.last_refresh is not None and p.last_refresh.cancelled
    assert "Descarga cancelada" in p.messages_label.text()
    assert mercado.asked_fx == []


def test_sin_red_se_trabaja_con_lo_guardado_y_se_indica(qtbot, cartera, tema):
    with cartera.transaction() as conn:
        PriceRepository(conn).save(Price("SAN", HOY, D("5"), "EUR", PriceSource.MARKET,
                                         AHORA - timedelta(hours=2)))
    sin_red = FakeMarket(COTIZACIONES, CAMBIOS, offline=True)
    p = PortfolioPage(cartera, tema, sin_red, sin_red, now=lambda: AHORA)
    qtbot.addWidget(p)
    p.show()
    actualizar(qtbot, p)
    assert p.messages_box.isVisible()
    assert "Sin conexión" in p.messages_label.text()
    assert textos(p, Col.SOURCE)["SAN"] == "Caché"
    assert textos(p, Col.SOURCE)["AAPL"] == "Coste"


def test_un_error_inesperado_se_ensena_y_la_pagina_sigue_viva(qtbot, pagina, monkeypatch):
    from sharky.ui import portfolio

    def revienta(*_args, **_kwargs):
        raise RuntimeError("la base de datos está ocupada")

    monkeypatch.setattr(portfolio, "refresh_and_value", revienta)
    pagina.show()
    actualizar(qtbot, pagina)
    assert pagina.messages_box.objectName() == "dangerBox"
    assert "ocupada" in pagina.messages_label.text()
    assert pagina.refresh_button.isEnabled()


def test_el_cambio_de_divisa_se_avisa_en_pantalla(qtbot, cartera, tema):
    mercado = FakeMarket({**COTIZACIONES, "AAPL": Quote("AAPL", D("200"), "EUR", HOY)},
                         CAMBIOS)
    p = PortfolioPage(cartera, tema, mercado, mercado, now=lambda: AHORA)
    qtbot.addWidget(p)
    p.show()
    actualizar(qtbot, p)
    assert "Yahoo cotiza en EUR" in p.messages_label.text()
    assert textos(p, Col.PRICE)["AAPL"] == "200,00 EUR"


# -- editar un activo ---------------------------------------------------------------------


def test_editar_activo_necesita_una_fila_elegida(pagina):
    assert not pagina.edit_button.isEnabled()
    assert pagina.select_ticker("VUSA")
    assert pagina.edit_button.isEnabled()
    assert pagina.selected_row().position.ticker == "VUSA"


def test_editar_activo_guarda_y_recarga(qtbot, pagina, cartera, monkeypatch):
    vistos = []

    def rellenar_y_guardar(dialogo):
        vistos.append(dialogo.asset.ticker)
        dialogo.symbol_edit.setText("VUSA.AS")
        dialogo.sector_edit.setText("Renta_Variable")
        dialogo.class_combo.setCurrentIndex(dialogo.class_combo.findData(AssetClass.ETF))
        dialogo.save()
        return dialogo.result() == QDialog.DialogCode.Accepted

    monkeypatch.setattr(pagina, "run_dialog", rellenar_y_guardar)
    pagina.select_ticker("VUSA")
    pagina.edit_button.click()
    assert vistos == ["VUSA"]
    activo = AssetRepository(cartera.connection()).get("VUSA")
    assert (activo.yahoo_symbol, activo.sector, activo.asset_class) == (
        "VUSA.AS", "Renta_Variable", AssetClass.ETF
    )
    assert "Pulsa «Actualizar precios»" in pagina.messages_label.text()
    assert pagina.selected_row().position.ticker == "VUSA"
    assert "Renta Variable" in [n for n, _, _ in pagina.exposure.rows]


def test_cancelar_la_edicion_no_toca_nada(pagina, cartera, monkeypatch):
    monkeypatch.setattr(pagina, "run_dialog", lambda dialogo: False)
    pagina.select_ticker("SAN")
    pagina.edit_selected()
    assert AssetRepository(cartera.connection()).get("SAN") == ACTIVOS[0]


@pytest.fixture
def dialogo(qtbot, cartera, mercado):
    d = EditAssetDialog(cartera, ACTIVOS[0], mercado)
    qtbot.addWidget(d)
    return d


def test_probar_un_simbolo_ensena_su_precio(qtbot, dialogo):
    assert dialogo.symbol_edit.text() == "SAN.MC"
    dialogo.probe_button.click()
    qtbot.waitUntil(lambda: dialogo.probe_status.objectName() != "muted", timeout=5000)
    texto = dialogo.probe_status.text()
    assert dialogo.probe_status.objectName() == "okText"
    assert "5,12 EUR" in texto and "24/09/2026" in texto and "Banco Santander, S.A." in texto


def test_probar_un_simbolo_que_no_existe(qtbot, dialogo):
    dialogo.symbol_edit.setText("NOEXISTE.MC")
    dialogo.probe_button.click()
    qtbot.waitUntil(lambda: dialogo.probe_status.objectName() == "dangerText", timeout=5000)
    assert "NOEXISTE.MC" in dialogo.probe_status.text()


def test_probar_avisa_si_la_divisa_no_coincide(qtbot, cartera, mercado):
    d = EditAssetDialog(cartera, ACTIVOS[1], mercado)  # AAPL, declarada en USD
    qtbot.addWidget(d)
    mercado.quotes["AAPL"] = Quote("AAPL", D("200"), "EUR", HOY)
    d.probe_button.click()
    qtbot.waitUntil(lambda: d.probe_status.objectName() == "warnText", timeout=5000)
    assert "cotiza en EUR" in d.probe_status.text()


def test_sugerir_por_isin(qtbot, cartera):
    mercado = FakeMarket(COTIZACIONES, suggestions={"IE00B3XXRP09": [
        SymbolSuggestion("VUSA.AS", "Vanguard S&P 500 UCITS ETF", "Amsterdam", "ETF"),
        SymbolSuggestion("VUSA.L", "Vanguard S&P 500 UCITS ETF", "London", "ETF"),
    ]})
    d = EditAssetDialog(cartera, ACTIVOS[3], mercado)
    qtbot.addWidget(d)
    assert d.suggest_button is not None
    d.suggest_button.click()
    qtbot.waitUntil(lambda: d.suggestions.count() == 2, timeout=5000)
    assert mercado.searched == ["IE00B3XXRP09"]
    assert d.symbol_edit.text() == "VUSA.AS"  # estaba vacío: se pone la primera
    d._on_suggestion_chosen(1)
    assert d.symbol_edit.text() == "VUSA.L"


def test_sugerir_por_isin_sin_resultados_o_con_error(qtbot, cartera):
    mercado = FakeMarket(search_error="No se ha podido conectar con Yahoo.")
    d = EditAssetDialog(cartera, ACTIVOS[3], mercado)
    qtbot.addWidget(d)
    d.suggest_button.click()
    qtbot.waitUntil(lambda: d.suggest_status.objectName() == "dangerText", timeout=5000)
    assert "conectar" in d.suggest_status.text()
    assert d.suggest_button.isEnabled()


def test_sin_isin_no_hay_sugerencia(qtbot, cartera, mercado):
    d = EditAssetDialog(cartera, ACTIVOS[1], mercado)  # AAPL no tiene ISIN
    qtbot.addWidget(d)
    assert d.suggest_button is None


def test_un_sector_con_espacios_no_se_guarda(dialogo, cartera):
    dialogo.sector_edit.setText("Banca europea")
    dialogo.save()
    assert not dialogo.error_label.isHidden()
    assert "espacios" in dialogo.error_label.text()
    assert dialogo.result() != QDialog.DialogCode.Accepted
    assert AssetRepository(cartera.connection()).get("SAN").sector == "Banca"


# -- tema, pintado y ventana --------------------------------------------------------------


def test_se_pinta_en_los_dos_temas(qtbot, pagina, tema):
    actualizar(qtbot, pagina)
    pagina.resize(1200, 800)
    for eleccion in (Theme.LIGHT, Theme.DARK):
        tema.set_theme(eleccion)
        imagen = pagina.grab()
        assert not imagen.isNull()
    tema.set_theme(Theme.LIGHT)


def test_la_ventana_con_mercado_tiene_cartera_y_sus_botones(qtbot, qapp, cartera, mercado):
    ventana = MainWindow(ThemeController(qapp, Theme.LIGHT), "9.9.9", db=cartera,
                         market=mercado, fx=mercado, now=lambda: AHORA)
    qtbot.addWidget(ventana)
    ventana.show()
    pagina = ventana.page("cartera")
    assert isinstance(pagina, PortfolioPage)
    ventana.show_section("cartera")
    assert pagina.header_actions.isVisible()
    ventana.show_section("panel")
    assert not pagina.header_actions.isVisible()
    panel = ventana.page("panel")
    assert isinstance(panel, PanelPage)
    assert panel.header_actions.isVisible()
    # Panel y Cartera comparten la misma descarga de precios.
    assert pagina.refresher is ventana.refresher
    assert isinstance(ventana.page("operar"), TradePage)
    from sharky.ui.radar import RadarPage

    assert isinstance(ventana.page("radar"), RadarPage)
    ventana.shutdown()


def test_el_pie_con_cobertura_fiable(qtbot, pagina, cartera):
    with cartera.transaction() as conn:
        from sharky.services.market import edit_asset

        edit_asset(conn, "VUSA", yahoo_symbol="VUSA.AS", sector="Indices",
                   asset_class=AssetClass.ETF)
    pagina._prices.quotes["VUSA.AS"] = Quote("VUSA.AS", D("98.6"), "EUR", HOY)
    actualizar(qtbot, pagina)
    pie = footer_text(pagina.valuation, AHORA)
    assert "Cobertura 100,0 %: 4 de 4 posiciones" in pie
    assert "no es fiable" not in pie
    assert "Patrimonio (NAV):" in pie
