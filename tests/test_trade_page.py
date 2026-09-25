"""Pantalla Operar (GUIA §5.5, §5.10 y §7, H8): «Revisar» antes de registrar, el primer motivo
del rechazo, «Registrar igualmente» con motivo, la tesis que abre una compra, la que cierra una
venta total, ampliar una posición con tesis, los movimientos de efectivo y que el Panel, la
Cartera y las Tesis cuadran después.

Misma cartera inventada que tests/test_trades.py.
"""

from decimal import Decimal as D

import pytest
from PySide6.QtCore import QCoreApplication, QDate, QEvent

from fakes import FakeMarket
from sharky.core.levels import LevelChoice
from sharky.core.models import CashKind, MandateState, Thesis, ThesisStatus, TradeKind
from sharky.services.market import FxQuote, Quote
from sharky.services.repositories import (
    CashMovementRepository,
    NavSnapshotRepository,
    ThesisRepository,
    TradeRepository,
    create_theses,
    load_valuation,
)
from sharky.ui.main_window import MainWindow
from sharky.ui.theme import Theme, ThemeController
from sharky.ui.trade import TradePage
from test_trades import AHORA, AYER, EFECTIVO, HOY, INICIO, crear_cartera, foto

COTIZACIONES = {
    "SAN.MC": Quote("SAN.MC", D("4"), "EUR", HOY),
    "IWDA.AS": Quote("IWDA.AS", D("90"), "EUR", HOY),
    "AAPL": Quote("AAPL", D("200"), "USD", HOY),
}
CAMBIOS = {"USD": FxQuote("USD", D("0.85"), HOY)}


@pytest.fixture
def cartera(db):
    return crear_cartera(db)


@pytest.fixture
def tema(qapp):
    controlador = ThemeController(qapp, Theme.LIGHT)
    yield controlador
    controlador.set_theme(Theme.LIGHT)


def nueva_ventana(db, tema):
    mercado = FakeMarket(COTIZACIONES, CAMBIOS)
    return MainWindow(tema, "9.9.9", db=db, market=mercado, fx=mercado, now=lambda: AHORA)


def cerrar(ventana):
    """Cierra la ventana y la borra en el acto. Si el borrado espera al bucle de eventos (que
    estos tests casi no hacen correr), las ventanas se acumulan y cada cambio de tema las
    repinta todas."""
    ventana.shutdown()
    ventana.close()
    ventana.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def ventana(qtbot, cartera, tema):
    v = nueva_ventana(cartera, tema)
    v.set_notifier(lambda *_a: None)
    yield v
    cerrar(v)


@pytest.fixture
def pagina(ventana) -> TradePage:
    ventana.show_section("operar")
    return ventana.page("operar")


def alta_tesis(db, ticker, stop, objetivo, entrada=None):
    with db.transaction() as conn:
        create_theses(conn, [Thesis(ticker, "EUR", INICIO,
                                    entry_price=D(entrada) if entrada else None,
                                    stop=D(stop), target=D(objetivo))], AHORA)


def escribir(p: TradePage, **campos):
    """Rellena el formulario: ticker, units, price, fee, stop, target, fx, currency, levels…"""
    nombres = {
        "ticker": p.ticker_edit, "units": p.units_edit, "price": p.price_edit,
        "fee": p.fee_edit, "stop": p.stop_edit, "target": p.target_edit, "fx": p.fx_edit,
        "reason": p.reason_edit, "name": p.name_edit, "isin": p.isin_edit,
        "symbol": p.symbol_edit, "sector": p.sector_edit,
        "quote_currency": p.quote_currency_edit,
    }
    if "ticker" in campos:
        p.ticker_edit.setText(campos.pop("ticker"))
    if "currency" in campos:
        p.currency_combo.setCurrentText(campos.pop("currency"))
    if "levels" in campos:
        p.levels_combo.setCurrentText(campos.pop("levels"))
    for clave, valor in campos.items():
        nombres[clave].setText(valor)


def comprar_asml(p: TradePage, **cambios):
    p.buy_button.click()
    campos = {"ticker": "ASML", "name": "ASML Holding", "symbol": "ASML.AS",
              "sector": "Semiconductores", "units": "1", "price": "700", "fee": "1",
              "stop": "650", "target": "850"}
    escribir(p, **(campos | cambios))


def visible(widget) -> bool:
    return not widget.isHidden()


# -- la pantalla ---------------------------------------------------------------------------------


def test_la_pantalla_ensena_los_limites_y_el_efectivo(pagina):
    assert pagina.state_label.text() == "Estado Óptimo"
    v = pagina.limit_values
    assert (v["Máximo por activo"].text(), v["Máximo por sector"].text()) == ("10 %", "25 %")
    assert (v["Efectivo mínimo"].text(), v["Efectivo máximo"].text()) == ("15 %", "30 %")
    assert v["Riesgo por operación"].text() == "1,5 % del NAV"
    assert v["Ratio mínimo"].text() == "2,0"
    assert pagina.cash_label.text() == "Efectivo: 6.720,00 € (67,2 % del patrimonio)"
    assert pagina.date_edit.date() == QDate(2026, 9, 25)
    assert pagina.date_edit.maximumDate() == QDate(2026, 9, 25)


def test_la_posicion_del_ticker_escrito(pagina, cartera):
    alta_tesis(cartera, "SAN", "3.5", "5.5")
    pagina.reload()
    escribir(pagina, ticker="san")
    assert pagina.position_title.text() == "Tu posición en SAN"
    v = pagina.position_values
    assert (v["Unidades"].text(), v["Valor"].text(), v["Peso"].text()) == (
        "200", "800,00 €", "8,0 %"
    )
    assert v["PnL"].text() == "−100,00 € (−11,1 %)"
    assert v["Stop de su tesis"].text() == "3,50 €"
    escribir(pagina, ticker="ASML")
    assert pagina.position_note.text() == "ASML es nuevo: se da de alta con la compra."
    assert visible(pagina.new_asset_box)


def test_en_cuidados_intensivos_no_se_compra(qtbot, cartera, tema):
    nav = load_valuation(cartera.connection(), AHORA).nav_eur
    with cartera.transaction() as conn:
        NavSnapshotRepository(conn).save(foto(AYER, nav, nav / 90))  # caída del 10 %
    v = nueva_ventana(cartera, tema)
    p = v.page("operar")
    assert p.state is MandateState.INTENSIVE_CARE
    assert p.buying_label.text() == "Compras prohibidas: vender siempre se puede."
    comprar_asml(p)
    p.review_now()
    assert p.result_title.text() == "Rechazada por el mandato"
    assert p.result_message.text().startswith("Compras prohibidas en estado Cuidados intensivos")
    cerrar(v)


# -- una compra ----------------------------------------------------------------------------------


def test_comprar_un_ticker_nuevo_y_completar_su_tesis(pagina, ventana, cartera):
    comprar_asml(pagina)
    revision = pagina.review_now()
    assert revision.validation.ok
    assert pagina.result_box.objectName() == "okBox"
    assert pagina.result_title.text() == "Cumple el mandato"
    assert "✓ Ratio beneficio/riesgo 3,00" in pagina.result_steps.text()
    assert "Se dará de alta el activo ASML (ASML Holding)." in pagina.result_outcome.text()
    assert "ASML no tiene tesis: se abrirá" in pagina.result_outcome.text()
    assert pagina.register_button.text() == "Registrar compra"
    hecho = pagina.register()
    assert hecho is not None and hecho.opened_thesis is not None
    assert pagina.done_label.text() == (
        "Compra registrada: 1 ASML a 700,00 EUR. Su tesis se ha abierto: complétala en Tesis."
    )
    # Se abre Tesis con la nueva, con precio, stop y objetivo ya puestos.
    assert ventana.current_section == "tesis"
    tesis = ventana.page("tesis")
    assert tesis.selected.ticker == "ASML"
    assert (tesis.entry_edit.text(), tesis.stop_edit.text(), tesis.target_edit.text()) == (
        "700,00", "650,00", "850,00"
    )
    # El formulario queda limpio y el efectivo al día.
    assert pagina.ticker_edit.text() == ""
    assert not visible(pagina.result_box)
    assert CashMovementRepository(cartera.connection()).balance() == EFECTIVO - 701


def test_el_panel_la_cartera_y_las_tesis_cuadran_despues(pagina, ventana, cartera):
    ventana.show()  # cada sección se pone al día al enseñarse, como en la app
    comprar_asml(pagina)
    pagina.review_now()
    pagina.register()
    panel = ventana.page("panel")
    assert panel.data.valuation.cash_eur == EFECTIVO - 701
    assert panel.cash_label.text() == "6.019,00 €"
    # ASML todavía no tiene precio: se valora a coste (700 € + 1 € de comisión), y el
    # patrimonio sigue en 10.000 €.
    assert panel.nav_label.text() == "10.000,00 €"
    ventana.show_section("cartera")
    assert "ASML" in [r.position.ticker for r in ventana.page("cartera").rows()]
    assert pagina.cash_label.text().startswith("Efectivo: 6.019,00 €")


def test_rechazada_ensena_el_primer_motivo_y_los_demas_debajo(pagina):
    comprar_asml(pagina, units="2", target="720")  # ratio 0,40 y ASML al 14 %
    pagina.review_now()
    assert pagina.result_box.objectName() == "rejectBox"
    assert pagina.result_title.text() == "Rechazada por el mandato"
    assert pagina.result_message.text().startswith("Ratio beneficio/riesgo 0,40")
    assert pagina.result_also.text().startswith("Además, ASML quedaría al 14,0 %")
    assert visible(pagina.force_check)
    assert pagina.force_check.text() == (
        "Registrar igualmente (queda marcada como forzada y el diario lo menciona)"
    )
    assert not pagina.register_button.isEnabled()


def test_registrar_igualmente_pide_confirmacion_y_motivo(pagina, cartera):
    comprar_asml(pagina, units="2", reason="Resultados")
    pagina.review_now()
    pagina.force_check.setChecked(True)
    assert pagina.register_button.isEnabled()
    assert pagina.register_button.text() == "Registrar igualmente…"
    pedidos = []

    def cancelar(revision, motivo):
        pedidos.append(motivo)
        return None

    pagina.ask_force_reason = cancelar
    assert pagina.register() is None
    assert pedidos == ["Resultados"]
    assert TradeRepository(cartera.connection()).list_for("ASML") == []

    pagina.ask_force_reason = lambda revision, motivo: "Convicción alta tras resultados"
    hecho = pagina.register()
    op = TradeRepository(cartera.connection()).get(hecho.trade.id)
    assert (op.forced, op.reason) == (True, "Convicción alta tras resultados")
    assert "Queda marcada como forzada." in pagina.done_label.text()


def test_lo_que_no_se_puede_forzar_no_ofrece_registrarlo(pagina):
    comprar_asml(pagina)
    escribir(pagina, stop="710")
    pagina.review_now()
    assert pagina.result_title.text() == "No se puede registrar"
    assert pagina.result_message.text().startswith("El stop (710,00 EUR) tiene que quedar")
    assert not visible(pagina.force_check)
    assert not visible(pagina.register_button)


def test_tocar_un_campo_obliga_a_revisar_otra_vez(pagina, cartera):
    comprar_asml(pagina)
    pagina.review_now()
    assert visible(pagina.result_box)
    escribir(pagina, units="2")
    assert not visible(pagina.result_box)
    assert pagina.register() is None
    assert TradeRepository(cartera.connection()).list_for("ASML") == []
    # El motivo no cuenta: se puede escribir después de revisar.
    pagina.review_now()
    escribir(pagina, reason="Ya lo pensaré")
    assert visible(pagina.result_box)


def test_errores_de_formato_y_de_datos(pagina):
    pagina.buy_button.click()
    escribir(pagina, ticker="ASML", units="abc", price="")
    pagina.review_now()
    assert pagina.error_label.text().splitlines()[:2] == [
        "Las unidades: «abc» no es un número.",
        "Falta el precio de ejecución.",
    ]
    assert "Falta el nombre del activo." in pagina.error_label.text()
    assert not visible(pagina.result_box)


def test_comprar_en_usd_propone_el_cambio_guardado(pagina, cartera):
    pagina.buy_button.click()
    escribir(pagina, ticker="AAPL")
    assert pagina.currency_combo.currentText() == "USD"
    assert visible(pagina.fx_row)
    assert pagina.fx_edit.text() == "0,85"
    assert pagina.levels_combo.currentText() == "USD"
    escribir(pagina, fx="0,86", units="1", price="200", fee="1", stop="160", target="220",
             levels="EUR")
    revision = pagina.review_now()
    assert "(comparados en EUR)" in pagina.result_steps.text()
    assert revision.ticket.fx_to_eur == D("0.86")
    hecho = pagina.register()
    assert hecho.trade.amount_eur == D("172.00")
    tesis = ThesisRepository(cartera.connection()).active_for("AAPL")
    assert (tesis.levels_currency, tesis.entry_price) == ("EUR", D("172"))


# -- ampliar una posición con tesis --------------------------------------------------------------


def test_ampliar_propone_los_niveles_de_la_tesis(pagina, cartera):
    alta_tesis(cartera, "SAN", "3.8", "4.6")
    pagina.reload()
    pagina.buy_button.click()
    escribir(pagina, ticker="SAN")
    assert (pagina.stop_edit.text(), pagina.target_edit.text()) == ("3,80", "4,60")
    escribir(pagina, units="20", price="4")
    revision = pagina.review_now()
    assert revision.level_update is None
    pagina.ask_level_choice = lambda _u: pytest.fail("No hay nada que preguntar")
    assert pagina.register() is not None


def test_ampliar_con_otros_niveles_pregunta_y_la_decision_queda(pagina, cartera):
    alta_tesis(cartera, "SAN", "3.5", "5.5", entrada="4.5")
    pagina.reload()
    pagina.buy_button.click()
    escribir(pagina, ticker="SAN", units="20", price="4", stop="3,8", target="4,6")
    pagina.review_now()
    assert "al registrar te pregunto si la actualizas" in pagina.result_outcome.text()
    pagina.ask_level_choice = lambda _u: None  # cancelar no registra nada
    assert pagina.register() is None
    assert len(TradeRepository(cartera.connection()).list_for("SAN")) == 1
    pagina.ask_level_choice = lambda _u: LevelChoice(stop=True)
    hecho = pagina.register()
    assert hecho.updated_thesis is not None
    tesis = ThesisRepository(cartera.connection()).active_for("SAN")
    assert (tesis.entry_price, tesis.stop, tesis.target) == (D("4.5"), D("3.8"), D("5.5"))


def test_el_dialogo_de_ampliar(pagina, cartera):
    from sharky.ui.trade import LevelUpdateDialog

    alta_tesis(cartera, "SAN", "3.5", "5.5", entrada="4.5")
    pagina.reload()
    pagina.buy_button.click()
    escribir(pagina, ticker="SAN", units="20", price="4", stop="3,8", target="4,6")
    revision = pagina.review_now()
    dialogo = LevelUpdateDialog(revision.level_update, pagina)
    assert sorted(dialogo.checks) == ["entrada", "objetivo", "stop"]
    assert dialogo.checks["stop"].text() == "Stop: 3,50 € → 3,80 €"
    dialogo.checks["objetivo"].setChecked(False)
    dialogo.apply()
    assert dialogo.choice == LevelChoice(entry=True, stop=True, target=False)
    dialogo.keep()
    assert dialogo.choice == LevelChoice()
    dialogo.deleteLater()


# -- ventas ---------------------------------------------------------------------------------------


def test_la_venta_parcial_no_cierra_y_la_total_si(pagina, cartera):
    alta_tesis(cartera, "SAN", "3.5", "5.5")
    pagina.reload()
    pagina.sell_button.click()
    assert not visible(pagina.levels_row)
    assert pagina._tickers.stringList() == ["AAPL", "IWDA", "SAN"]
    escribir(pagina, ticker="SAN", units="100", price="5", fee="1")
    pagina.review_now()
    assert "PnL realizado de esta venta: +49,00 €" in pagina.result_outcome.text()
    assert "Venta parcial: su tesis sigue abierta." in pagina.result_outcome.text()
    assert "No sirve para la declaración de la renta" in pagina.result_outcome.text()
    assert pagina.register_button.text() == "Registrar venta"
    pagina.register()
    assert ThesisRepository(cartera.connection()).active_for("SAN") is not None

    escribir(pagina, ticker="SAN")
    pagina.all_button.click()
    assert pagina.units_edit.text() == "100"
    escribir(pagina, price="4", fee="1", reason="Rota la tesis")
    pagina.review_now()
    assert ("Se cerrará su tesis con el PnL realizado de toda la posición: −2,00 €."
            in pagina.result_outcome.text())
    hecho = pagina.register()
    assert hecho.closed_thesis is not None
    tesis = ThesisRepository(cartera.connection()).get(hecho.closed_thesis)
    assert (tesis.status, tesis.realized_pnl_eur) == (ThesisStatus.CLOSED, D("-2"))
    assert pagina.done_label.text().endswith("Su tesis se ha cerrado.")


def test_no_se_vende_mas_de_lo_que_hay(pagina):
    pagina.sell_button.click()
    escribir(pagina, ticker="SAN", units="250", price="4")
    pagina.review_now()
    assert pagina.result_title.text() == "No se puede registrar"
    assert pagina.result_message.text() == "No se pueden vender 250 unidades de SAN: solo hay 200."
    assert not visible(pagina.force_check)


# -- movimientos de efectivo --------------------------------------------------------------------


def rellenar_y_guardar(**campos):
    def run(dialogo):
        if "amount" in campos:
            dialogo.amount_edit.setText(campos["amount"])
        if "note" in campos:
            dialogo.note_edit.setText(campos["note"])
        if "kind" in campos:
            dialogo.kind_combo.setCurrentIndex(dialogo.kind_combo.findData(campos["kind"].value))
        return dialogo.save()

    return run


def test_un_ingreso_desde_la_pantalla(pagina, cartera):
    pagina.run_dialog = rellenar_y_guardar(amount="1.000,50", note="Nómina")
    movimiento = pagina.open_cash_dialog(CashKind.DEPOSIT)
    assert (movimiento.kind, movimiento.amount_eur, movimiento.note) == (
        CashKind.DEPOSIT, D("1000.50"), "Nómina"
    )
    assert pagina.done_label.text() == "Movimiento registrado: ingreso de 1.000,50 €."
    assert pagina.cash_label.text().startswith("Efectivo: 7.720,50 €")


def test_otro_movimiento_elige_su_tipo(pagina):
    pagina.run_dialog = rellenar_y_guardar(amount="3,20", kind=CashKind.TAX)
    movimiento = pagina.open_cash_dialog(CashKind.INTEREST)
    assert (movimiento.kind, movimiento.amount_eur) == (CashKind.TAX, D("-3.20"))
    assert pagina.done_label.text() == "Movimiento registrado: impuesto de 3,20 €."


def test_una_retirada_de_mas_se_queda_en_el_dialogo_con_el_motivo(pagina, cartera):
    dialogos = []

    def run(dialogo):
        dialogos.append(dialogo)
        dialogo.amount_edit.setText("7000")
        return dialogo.save()

    pagina.run_dialog = run
    assert pagina.open_cash_dialog(CashKind.WITHDRAWAL) is None
    assert dialogos[0].error_label.text().startswith("No hay tanto efectivo: hay 6.720,00 €")
    assert CashMovementRepository(cartera.connection()).balance() == EFECTIVO


def test_ajustar_saldo(pagina, cartera):
    from sharky.ui.trade import CashDialog

    dialogo = CashDialog(cartera, lambda: AHORA, None, EFECTIVO, pagina)
    dialogo.amount_edit.setText("6.700,55")
    assert dialogo.hint_label.text() == "Se registrará un ajuste de −19,45 €."
    dialogo.deleteLater()
    pagina.run_dialog = rellenar_y_guardar(amount="6.700,55")
    movimiento = pagina.open_cash_dialog(None)
    assert (movimiento.kind, movimiento.amount_eur) == (CashKind.ADJUSTMENT, D("-19.45"))
    assert pagina.done_label.text() == "Movimiento registrado: ajuste de −19,45 €."


def test_un_movimiento_pone_al_dia_el_panel(pagina, ventana):
    pagina.run_dialog = rellenar_y_guardar(amount="500")
    pagina.open_cash_dialog(CashKind.DIVIDEND)
    assert ventana.page("panel").cash_label.text() == "7.220,00 €"


# -- actualizar precios y temas -----------------------------------------------------------------


def test_actualizar_precios_obliga_a_revisar_otra_vez(qtbot, pagina, ventana):
    comprar_asml(pagina)
    pagina.review_now()
    panel = ventana.page("panel")
    with qtbot.waitSignal(panel.refreshFinished, timeout=5000):
        panel.refresh_button.click()
    assert not visible(pagina.result_box)
    assert pagina.register() is None


def test_se_pinta_en_los_dos_temas(pagina, tema):
    comprar_asml(pagina, units="2")
    pagina.review_now()
    for eleccion in (Theme.DARK, Theme.LIGHT):
        tema.set_theme(eleccion)
        assert not pagina.grab().isNull()


def test_la_operacion_queda_con_la_fecha_elegida(pagina, cartera):
    pagina.sell_button.click()
    escribir(pagina, ticker="IWDA", units="1", price="90")
    pagina.date_edit.setDate(QDate(2026, 9, 24))
    pagina.review_now()
    hecho = pagina.register()
    assert hecho.trade.trade_date.isoformat() == "2026-09-24"
    assert hecho.trade.kind is TradeKind.SELL
