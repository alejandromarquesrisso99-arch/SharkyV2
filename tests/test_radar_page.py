"""Pantalla Radar y tarjeta del Panel (GUIA §5.8 y §5.10, punto 5; §7, H11), con pytest-qt.

- «Pasar el filtro ahora (gratis)»: alertas y descartes en pantalla, el número del lateral y la
  tarjeta del Panel.
- «Comprar» abre Operar con los niveles de la alerta; al registrar la compra real, la alerta
  queda EJECUTADA y la tesis se abre con los niveles de la compra.
- «Descartar», «Añadir valor…» (con «Probar»), quitar un valor y «Vigilar» un candidato.
- «Buscar oportunidades nuevas» enseña su precio antes, corre en segundo plano y deja el informe
  en Informes con su coste real.

Misma cartera inventada que tests/test_trades.py; históricos inventados de tests/fakes.py.
"""

from datetime import timedelta
from decimal import Decimal as D

import pytest
from PySide6.QtCore import QCoreApplication, QEvent

from fakes import FakeMarket, extraccion, historico_yahoo, subida_y_caida
from sharky.core.models import (
    AlertOrigin,
    AlertStatus,
    MandateState,
    RadarAlert,
    ReportKind,
)
from sharky.core.reports import AIAction
from sharky.services import secrets
from sharky.services.ai import ClaudeClient
from sharky.services.explorer import add_to_watchlist
from sharky.services.market import FxQuote, Quote
from sharky.services.repositories import (
    RadarAlertRepository,
    ThesisRepository,
    WatchlistRepository,
)
from sharky.services.settings import Settings, SettingsStore
from sharky.ui import radar as radar_module
from sharky.ui.main_window import MainWindow
from sharky.ui.radar import AddWatchDialog, RadarPage
from sharky.ui.theme import Theme, ThemeController
from sharky.ui.trade import TradePage
from test_explorer import CANDIDATOS, exploracion
from test_trades import AHORA, HOY, crear_cartera

CLAVE = "sk-ant-inventada-0000"
COTIZACIONES = {
    "SAN.MC": Quote("SAN.MC", D("4"), "EUR", HOY),
    "IWDA.AS": Quote("IWDA.AS", D("90"), "EUR", HOY),
    "AAPL": Quote("AAPL", D("200"), "USD", HOY),
    "CCJ": Quote("CCJ", D("78"), "USD", HOY, "Cameco Corporation"),
}
CAMBIOS = {"USD": FxQuote("USD", D("0.85"), HOY)}
HISTORICOS = {
    "CCJ": historico_yahoo("CCJ", subida_y_caida(HOY), nombre="Cameco"),
    "NVO": historico_yahoo("NVO", subida_y_caida(HOY, final="88")),
}


@pytest.fixture
def cartera(db):
    return crear_cartera(db)


@pytest.fixture
def tema(qapp):
    controlador = ThemeController(qapp, Theme.LIGHT)
    yield controlador
    controlador.set_theme(Theme.LIGHT)


def cerrar(ventana):
    """Cierra y borra en el acto (si no, las ventanas se acumulan entre tests)."""
    ventana.shutdown()
    ventana.close()
    ventana.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def ventana(qtbot, cartera, tema):
    mercado = FakeMarket(COTIZACIONES, CAMBIOS, histories=HISTORICOS)
    v = MainWindow(
        tema, "9.9.9", db=cartera, market=mercado, fx=mercado, settings=Settings(),
        now=lambda: AHORA, store=SettingsStore(),
        runner_options={"client_factory": lambda k: ClaudeClient(k, wait=lambda _s, _c: None)},
    )
    v.set_notifier(lambda *_a: None)
    yield v
    cerrar(v)


@pytest.fixture
def con_clave(keyring_falso):
    secrets.save_api_key(CLAVE)


def radar(ventana) -> RadarPage:
    pagina = ventana.page("radar")
    assert isinstance(pagina, RadarPage)
    return pagina


def vigilar(db, *tickers):
    datos = {"CCJ": ("Cameco", "CCJ", "Energia"), "NVO": ("Novo Nordisk", "NVO", "Salud")}
    with db.transaction() as conn:
        for t in tickers:
            add_to_watchlist(conn, t, *datos[t], AHORA)


def pasar_filtro(qtbot, ventana):
    pagina = radar(ventana)
    with qtbot.waitSignal(ventana.radar_runner.stopped, timeout=10_000):
        pagina.filter_button.click()
    return pagina


# -- la pantalla -----------------------------------------------------------------------------------


def test_sin_nada_todavia(ventana):
    pagina = radar(ventana)
    assert pagina.alert_rows == [] and not pagina.alerts_empty.isHidden()
    assert pagina.alerts_empty.text().startswith("No hay alertas activas.")
    assert pagina.last_label.text() == "Todavía no hay ninguna exploración"
    assert pagina.watch_subtitle.text().startswith("Vacía.")
    assert pagina.history_empty.text() == "Todavía no hay alertas cerradas."
    # El botón que llama a Claude lleva su precio; el filtro dice que es gratis.
    assert pagina.explore_button.text() == "Buscar oportunidades nuevas (≈ 0,80–2,50 $)"
    assert pagina.filter_button.text() == "Pasar el filtro ahora (gratis)"


def test_pasar_el_filtro_ahora_gratis(qtbot, ventana, cartera, claude_falso):
    vigilar(cartera, "CCJ", "NVO")
    ventana.show_section("radar")
    pagina = pasar_filtro(qtbot, ventana)
    [fila] = pagina.alert_rows
    assert fila.alert.ticker == "CCJ" and fila.watch_button is None  # de la vigilancia
    textos = [e.text() for e in pagina.findChildren(radar_module.QLabel)]
    for esperado in ("78,00 USD", "71,76 USD", "101,00 USD", "3,6", "−22,8 %", "10,0 %"):
        assert esperado in textos
    [(descarte, _boton)] = pagina.rejected_rows
    assert descarte.ticker == "NVO"
    assert "Ratio 1,8 · el mínimo del mandato es 2,0" in textos
    assert pagina.message_label.text() == "Filtro pasado: 1 alerta nueva (CCJ) · 1 descarte."
    assert set(pagina.watch_chips) == {"CCJ", "NVO"}
    # El número del lateral y la tarjeta del Panel.
    assert ventana.radar_badge.text() == "1" and not ventana.radar_badge.isHidden()
    tarjeta = ventana.page("panel").radar_card
    assert tarjeta.count_label.text() == "1 activa"
    assert tarjeta.lines_label.text() == "• CCJ · R:R 3,6 · −22,8 % desde máx."
    assert claude_falso.llamadas == []  # gratis: nada de Claude


def test_la_tarjeta_del_panel_abre_el_radar(ventana):
    ventana.page("panel").radar_card.open_button.click()
    assert ventana.current_section == "radar"


def test_comprar_desde_una_alerta_abre_operar_con_sus_niveles_y_la_deja_ejecutada(
    qtbot, ventana, cartera
):
    """Criterio del H11: «Comprar» abre Operar con los niveles de la alerta; al registrar la
    compra real (con otros niveles), la alerta queda EJECUTADA y la tesis se abre con los de la
    compra, no con los de la alerta."""
    vigilar(cartera, "CCJ")
    pagina = pasar_filtro(qtbot, ventana)
    [fila] = pagina.alert_rows
    fila.buy_button.click()
    assert ventana.current_section == "operar"
    operar = ventana.page("operar")
    assert isinstance(operar, TradePage)
    assert operar.is_buy and operar.ticker_edit.text() == "CCJ"
    assert (operar.stop_edit.text(), operar.target_edit.text()) == ("71,76", "101")
    assert operar.levels_combo.currentText() == "USD"
    assert operar.currency_combo.currentText() == "USD"
    assert operar.fx_edit.text() == "0,85"  # el último cambio guardado
    # Activo nuevo: sus datos salen de la alerta.
    assert not operar.new_asset_box.isHidden()
    assert (operar.name_edit.text(), operar.symbol_edit.text(), operar.sector_edit.text(),
            operar.quote_currency_edit.text()) == ("Cameco", "CCJ", "Energia", "USD")
    assert "Desde la alerta del radar del 25/09/2026" in operar.alert_note.text()
    assert operar.units_edit.text() == "" and operar.price_edit.text() == ""
    # La compra real, con otros niveles.
    operar.units_edit.setText("10")
    operar.price_edit.setText("77,5")
    operar.stop_edit.setText("70")
    operar.target_edit.setText("105")
    revision = operar.review_now()
    assert revision is not None and revision.validation.ok, operar.error_label.text()
    hecho = operar.register()
    assert hecho is not None and hecho.executed_alert == fila.alert.id
    assert "Su alerta del radar queda como ejecutada." in operar.done_label.text()
    alerta = RadarAlertRepository(cartera.connection()).get(fila.alert.id)
    assert alerta.status is AlertStatus.EXECUTED
    tesis = ThesisRepository(cartera.connection()).active_for("CCJ")
    assert (tesis.stop, tesis.target, tesis.levels_currency) == (D("70"), D("105"), "USD")
    # El Radar y el lateral se ponen al día.
    assert pagina.alert_rows == []
    assert [a.status for a in pagina.history_rows] == [AlertStatus.EXECUTED]
    assert ventana.radar_badge.isHidden()
    assert pagina.watch_chips["CCJ"].text() == "CCJ (en cartera)"


def test_descartar_una_alerta(qtbot, ventana, cartera):
    vigilar(cartera, "CCJ")
    pagina = pasar_filtro(qtbot, ventana)
    pagina.ask_discard_reason = lambda alerta: "cara"
    pagina.alert_rows[0].discard_button.click()
    assert pagina.alert_rows == []
    [cerrada] = pagina.history_rows
    assert cerrada.status is AlertStatus.DISCARDED
    assert cerrada.reason == "Descartada por ti el 25/09/2026: cara"
    assert "no la volverá a emitir" in pagina.message_label.text()
    assert ventana.page("panel").radar_card.count_label.text() == "0 activas"


def test_cancelar_el_descarte_no_toca_nada(qtbot, ventana, cartera):
    vigilar(cartera, "CCJ")
    pagina = pasar_filtro(qtbot, ventana)
    pagina.ask_discard_reason = lambda alerta: None
    pagina.alert_rows[0].discard_button.click()
    assert len(pagina.alert_rows) == 1


def test_anadir_un_valor_con_probar(qtbot, ventana, cartera):
    pagina = radar(ventana)
    dialogos = []

    def rellenar(dialogo):
        assert isinstance(dialogo, AddWatchDialog)
        dialogos.append(dialogo)
        dialogo.ticker_edit.setText("CCJ")
        dialogo.symbol_edit.setText("CCJ")
        with qtbot.waitSignal(dialogo.probe_button.clicked, timeout=1000):
            dialogo.probe_button.click()
        qtbot.waitUntil(lambda: dialogo.currency == "USD", timeout=5000)
        assert dialogo.name_edit.text() == "Cameco Corporation"
        assert "78,00 USD" in dialogo.probe_label.text()
        dialogo.sector_edit.setText("Energía nuclear")
        return dialogo.save()

    pagina.run_dialog = rellenar
    item = pagina.add_value()
    assert item is not None and item.currency == "USD" and item.sector == "Energía_nuclear"
    assert list(pagina.watch_chips) == ["CCJ"]
    assert pagina.watch_subtitle.text().startswith("1 valor.")


def test_anadir_un_valor_que_no_vale_ensena_los_errores(ventana, cartera):
    pagina = radar(ventana)

    def rellenar(dialogo):
        dialogo.ticker_edit.setText("SAN")  # ya está en cartera
        dialogo.symbol_edit.setText("SAN.MC")
        dialogo.name_edit.setText("Santander")
        assert not dialogo.save()
        assert "ya está en tu cartera" in dialogo.error_label.text()
        return False

    pagina.run_dialog = rellenar
    assert pagina.add_value() is None
    assert WatchlistRepository(cartera.connection()).list_all() == []


def test_quitar_un_valor_de_la_lista(ventana, cartera):
    vigilar(cartera, "NVO")
    pagina = radar(ventana)
    pagina.reload()
    pagina.confirm_remove = lambda item: item.ticker == "NVO"
    pagina.watch_chips["NVO"].click()
    assert pagina.watch_chips == {}
    assert WatchlistRepository(cartera.connection()).list_all() == []


def test_vigilar_un_candidato_del_explorador_aunque_no_diera_alerta(ventana, cartera):
    with cartera.transaction() as conn:
        RadarAlertRepository(conn).add(RadarAlert(
            HOY, "NVO", AlertOrigin.EXPLORER, AlertStatus.DISCARDED, D("88"), "USD",
            reason="Ratio 1,8 · el mínimo del mandato es 2,0", summary="Nuevos fármacos",
            name="Novo Nordisk", yahoo_symbol="NVO", sector="Salud",
        ))
    pagina = radar(ventana)
    pagina.reload()
    [(descarte, boton)] = pagina.rejected_rows
    assert descarte.ticker == "NVO" and boton is not None
    boton.click()
    [item] = WatchlistRepository(cartera.connection()).list_all()
    assert (item.ticker, item.name, item.yahoo_symbol, item.sector) == (
        "NVO", "Novo Nordisk", "NVO", "Salud")
    assert pagina.rejected_rows[0][1] is None  # ya vigilado: sin botón


def test_en_cuidados_intensivos_avisa_de_que_no_se_puede_comprar(qtbot, ventana, cartera,
                                                                monkeypatch):
    vigilar(cartera, "CCJ")
    pagina = pasar_filtro(qtbot, ventana)
    monkeypatch.setattr(radar_module, "current_state",
                        lambda *_a: MandateState.INTENSIVE_CARE)
    pagina.reload()
    assert not pagina.buying_label.isHidden()
    assert pagina.buying_label.text().startswith("Compras prohibidas en Cuidados intensivos")


# -- buscar oportunidades nuevas -------------------------------------------------------------------


def test_buscar_oportunidades_ensena_el_precio_y_deja_el_informe_con_su_coste(
    qtbot, con_clave, ventana, cartera, claude_falso
):
    claude_falso.respuestas = [exploracion()]
    claude_falso.extracciones = [extraccion(CANDIDATOS, modelo="claude-opus-5")]
    pagina = radar(ventana)
    ventana.show_section("radar")
    vistos = []

    def confirmar(plan):
        vistos.append(plan)
        return True

    pagina.confirm_explore = confirmar
    assert pagina.explore_button.text() == "Buscar oportunidades nuevas (≈ 0,80–2,50 $)"
    with qtbot.waitSignal(ventana.reports_runner.stopped, timeout=10_000):
        pagina.explore_button.click()
    [plan] = vistos
    assert plan.action is AIAction.EXPLORER and plan.uses_ai
    assert plan.model == "claude-opus-5" and plan.effort == "high"
    assert "Coste real: 0,34 $" in pagina.message_label.text()
    assert "1 alerta nueva: CCJ." in pagina.message_label.text()
    [fila] = pagina.alert_rows
    assert fila.alert.origin is AlertOrigin.EXPLORER and fila.watch_button is not None
    assert pagina.last_label.text() == "Última exploración: 25/09 · 0,34 $"
    # En Informes, con su coste real.
    informes = ventana.page("informes")
    assert informes.current.kind is ReportKind.EXPLORATION
    assert informes.meta_label.text() == "claude-opus-5 · esfuerzo high · 0,34 $"
    assert ventana.radar_badge.text() == "1"


def test_sin_clave_no_se_lanza_y_se_dice_por_que(ventana, claude_falso):
    pagina = radar(ventana)
    errores = []
    pagina.show_error = errores.append
    pagina.confirm_explore = lambda plan: pytest.fail("no debería preguntar")
    pagina.explore_button.click()
    assert "no hay clave de Claude" in errores[0]
    assert claude_falso.llamadas == []


def test_cancelar_la_confirmacion_no_lanza_nada(con_clave, ventana, claude_falso):
    pagina = radar(ventana)
    pagina.confirm_explore = lambda plan: False
    pagina.explore_button.click()
    assert not ventana.reports_runner.running and claude_falso.llamadas == []


def test_se_pinta_en_los_dos_temas(qtbot, ventana, cartera, tema):
    vigilar(cartera, "CCJ", "NVO")
    pagina = pasar_filtro(qtbot, ventana)
    ventana.show_section("radar")
    ventana.resize(1400, 1000)
    for eleccion in (Theme.LIGHT, Theme.DARK):
        tema.set_theme(eleccion)
        assert not pagina.grab().isNull()
    assert not ventana.page("panel").grab().isNull()


def test_una_alerta_antigua_caduca_al_pasar_el_filtro(qtbot, ventana, cartera):
    vigilar(cartera, "NVO")
    with cartera.transaction() as conn:
        RadarAlertRepository(conn).add(RadarAlert(
            HOY - timedelta(days=31), "CCJ", AlertOrigin.WATCHLIST, AlertStatus.ACTIVE,
            D("70"), "USD", D("60"), D("140"), D("3"), D("0.3"), D("0.1"), yahoo_symbol="CCJ",
        ))
    pagina = radar(ventana)
    pagina.reload()
    assert len(pagina.alert_rows) == 1
    pasar_filtro(qtbot, ventana)
    assert pagina.alert_rows == []
    assert pagina.history_rows[0].status is AlertStatus.EXPIRED
