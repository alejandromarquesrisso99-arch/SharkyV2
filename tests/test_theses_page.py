"""Pantalla Tesis y avisos de niveles (GUIA §5.6, §5.10 y §7, H7): lista, editor, historial,
propuestas, «Alta rápida», la ventana de aviso de stop (una vez al día), la notificación del
objetivo y lo que enseña el Panel.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from fakes import FakeMarket
from sharky.core.models import (
    Asset,
    CashKind,
    CashMovement,
    Thesis,
    ThesisStatus,
    Trade,
    TradeKind,
)
from sharky.services.market import FxQuote, Quote
from sharky.services.repositories import (
    AssetRepository,
    CashMovementRepository,
    LevelAlertRepository,
    ThesisRepository,
    TradeRepository,
    create_theses,
)
from sharky.ui.main_window import MainWindow
from sharky.ui.portfolio import PriceRefresher
from sharky.ui.theme import Theme, ThemeController
from sharky.ui.theses import QuickAddDialog, ThesesPage

MADRID = timezone(timedelta(hours=2))
AHORA = datetime(2026, 9, 25, 18, 5, tzinfo=MADRID)
HOY = AHORA.date()
ANTEAYER = HOY - timedelta(days=2)

#: ticker: (nombre, divisa, símbolo, sector, unidades, coste medio en EUR)
POSICIONES = {
    "SAN": ("Banco Santander", "EUR", "SAN.MC", "Banca", "200", "4.5"),
    "AAPL": ("Apple Inc.", "USD", "AAPL", "Tecnologia", "10", "150"),
    "IWDA": ("iShares MSCI World", "EUR", "IWDA.AS", "Renta_Variable_Global", "50", "80"),
    "VUSA": ("Vanguard S&P 500", "EUR", None, "Indices", "2", "95"),  # sin símbolo: a coste
}
COTIZACIONES = {
    "SAN.MC": Quote("SAN.MC", D("5.12"), "EUR", HOY),
    "AAPL": Quote("AAPL", D("215.4"), "USD", HOY),
    "IWDA.AS": Quote("IWDA.AS", D("88.4"), "EUR", HOY),
}
CAMBIOS = {"USD": FxQuote("USD", D("0.85"), HOY)}


@pytest.fixture
def cartera(db):
    with db.transaction() as conn:
        for ticker, (nombre, divisa, simbolo, sector, unidades, coste) in POSICIONES.items():
            AssetRepository(conn).add(Asset(ticker, nombre, divisa, yahoo_symbol=simbolo,
                                            sector=sector))
            TradeRepository(conn).add(Trade(ANTEAYER, ticker, TradeKind.OPENING, D(unidades),
                                            D(coste), "EUR", D(1), D(0),
                                            D(unidades) * D(coste)))
        CashMovementRepository(conn).add(CashMovement(ANTEAYER, CashKind.INITIAL, D("2500")))
    return db


@pytest.fixture
def tema(qapp):
    controlador = ThemeController(qapp, Theme.LIGHT)
    yield controlador
    controlador.set_theme(Theme.LIGHT)


def alta(db, *tesis):
    with db.transaction() as conn:
        return create_theses(conn, list(tesis), AHORA)


def tesis(ticker, stop=None, objetivo=None, entrada=None, divisa="EUR"):
    def d(x):
        return D(x) if x is not None else None

    return Thesis(ticker, divisa, HOY, entry_price=d(entrada), stop=d(stop), target=d(objetivo))


@pytest.fixture
def ventana(qtbot, cartera, tema):
    mercado = FakeMarket(COTIZACIONES, CAMBIOS)
    v = MainWindow(tema, "9.9.9", db=cartera, market=mercado, fx=mercado, now=lambda: AHORA)
    qtbot.addWidget(v)
    v.notificaciones = []
    v.set_notifier(lambda titulo, texto, critica: v.notificaciones.append(
        (titulo, texto, critica)))
    yield v
    v.shutdown()


def actualizar(qtbot, ventana):
    panel = ventana.page("panel")
    with qtbot.waitSignal(panel.refreshFinished, timeout=5000):
        panel.refresh_button.click()


def pagina_sola(qtbot, db, tema):
    mercado = FakeMarket(COTIZACIONES, CAMBIOS)
    actualizador = PriceRefresher(db, mercado, mercado, now=lambda: AHORA)
    pagina = ThesesPage(db, tema, actualizador, now=lambda: AHORA)
    qtbot.addWidget(pagina)
    return pagina


# -- la lista y el alta rápida -------------------------------------------------------------------


def test_sin_tesis_lo_explica_y_ofrece_el_alta_rapida(qtbot, cartera, tema):
    pagina = pagina_sola(qtbot, cartera, tema)
    assert pagina.items == []
    assert not pagina.empty_card.isHidden() and pagina.editor_card.isHidden()
    assert pagina.quick_add_button.text() == "Alta rápida:\n4 posiciones sin tesis"
    assert pagina.quick_add_button.isEnabled()


def test_alta_rapida_desde_la_tabla(qtbot, cartera, tema):
    pagina = pagina_sola(qtbot, cartera, tema)
    senales = []
    pagina.levelsChanged.connect(lambda: senales.append(1))

    def rellenar(dialogo: QuickAddDialog) -> bool:
        assert [r.position.ticker for r in dialogo.rows] == ["AAPL", "IWDA", "SAN", "VUSA"]
        san = dialogo.row("SAN")
        assert san.entry_edit.text() == "4,50"  # la entrada, el coste medio en EUR
        assert san.currency_combo.currentData() == "EUR"
        san.stop_edit.setText("4,2")
        san.target_edit.setText("7")
        aapl = dialogo.row("AAPL")
        aapl.currency_combo.setCurrentIndex(aapl.currency_combo.findData("USD"))
        assert aapl.entry_edit.text() == ""  # el coste medio en EUR no vale como entrada en USD
        aapl.stop_edit.setText("180")
        aapl.target_edit.setText("300.5")
        return dialogo.save()

    pagina.run_dialog = rellenar
    pagina.quick_add_button.click()
    activas = ThesisRepository(cartera.connection()).list_active()
    assert [(t.ticker, t.levels_currency, t.entry_price, t.stop, t.target) for t in activas] == [
        ("AAPL", "USD", None, D("180"), D("300.5")),
        ("SAN", "EUR", D("4.50"), D("4.2"), D("7")),
    ]
    assert [i.thesis.ticker for i in pagina.items] == ["AAPL", "SAN"]
    assert pagina.quick_add_button.text() == "Alta rápida:\n2 posiciones sin tesis"
    assert senales == [1]
    assert pagina.history_rows[0].entry.title == "Tesis creada con el alta rápida"


def test_alta_rapida_ensena_todos_los_errores_y_no_guarda_nada(qtbot, cartera, tema):
    pagina = pagina_sola(qtbot, cartera, tema)
    dialogo = QuickAddDialog(cartera, pagina.without_thesis(), lambda: AHORA)
    qtbot.addWidget(dialogo)
    assert not dialogo.save()
    assert dialogo.error_label.text() == "Pon el stop o el objetivo de al menos una posición."
    dialogo.row("SAN").stop_edit.setText("8")
    dialogo.row("SAN").target_edit.setText("7")
    dialogo.row("IWDA").stop_edit.setText("ochenta")
    assert not dialogo.save()
    assert dialogo.error_label.text().splitlines() == [
        "IWDA: el stop: «ochenta» no es un número.",
    ]
    dialogo.row("IWDA").stop_edit.setText("75")
    assert not dialogo.save()
    assert dialogo.error_label.text() == (
        "SAN: El stop (8,00) tiene que quedar por debajo del objetivo (7,00)."
    )
    assert ThesisRepository(cartera.connection()).list_active() == []


def test_la_lista_ensena_el_estado_de_cada_tesis(qtbot, ventana, cartera):
    alta(cartera, tesis("SAN", "5.20", "7"), tesis("IWDA", "70", "85", "80"),
         tesis("AAPL", "150", "300", divisa="USD"), tesis("VUSA", "80", "120"))
    with cartera.transaction() as conn:
        ThesisRepository(conn).add(Thesis("SAN", "EUR", ANTEAYER, stop=D("4"),
                                          status=ThesisStatus.CLOSED, closed_on=ANTEAYER))
    actualizar(qtbot, ventana)
    pagina = ventana.page("tesis")
    lineas = {(i.thesis.ticker, i.state_label.text()): i.status_label.text()
              for i in pagina.items}
    assert lineas == {
        ("AAPL", "Activa"): "entre niveles",
        ("IWDA", "Activa"): "objetivo alcanzado",
        ("SAN", "Activa"): "stop alcanzado",
        ("VUSA", "Activa"): "niveles sin verificar",
        ("SAN", "Cerrada"): f"cerrada el {ANTEAYER:%d/%m/%Y}",
    }
    [san] = [i for i in pagina.items if i.thesis.ticker == "SAN" and i.state_label.text() ==
             "Activa"]
    assert san.status_label.objectName() == "dangerText"
    assert [i.thesis.status for i in pagina.items][-1] is ThesisStatus.CLOSED  # al final


# -- el editor ---------------------------------------------------------------------------------


def test_cambiar_el_stop_pide_motivo_y_queda_en_el_historial(qtbot, cartera, tema):
    alta(cartera, tesis("SAN", "4", "7", "4.5"))
    pagina = pagina_sola(qtbot, cartera, tema)
    assert not pagina.save_button.isEnabled()
    pagina.ask_reason = lambda _t: "el mínimo de agosto aguantó"
    senales = []
    pagina.levelsChanged.connect(lambda: senales.append(1))
    pagina.stop_edit.setText("4,20")
    assert pagina.save_button.isEnabled() and pagina.dirty
    assert pagina.save()
    assert ThesisRepository(cartera.connection()).active_for("SAN").stop == D("4.20")
    primera = pagina.history_rows[0]
    assert primera.entry.title == "Stop cambiado por ti: 4,00 → 4,20"
    assert primera.entry.detail == "Motivo: el mínimo de agosto aguantó"
    assert senales == [1]
    assert not pagina.dirty


def test_cancelar_el_motivo_no_guarda(qtbot, cartera, tema):
    alta(cartera, tesis("SAN", "4", "7"))
    pagina = pagina_sola(qtbot, cartera, tema)
    pagina.ask_reason = lambda _t: None
    pagina.stop_edit.setText("4,5")
    assert not pagina.save()
    assert ThesisRepository(cartera.connection()).active_for("SAN").stop == D("4")


def test_los_textos_se_guardan_sin_pedir_motivo(qtbot, cartera, tema):
    alta(cartera, tesis("SAN", "4", "7"))
    pagina = pagina_sola(qtbot, cartera, tema)
    pagina.ask_reason = lambda _t: pytest.fail("no hay números que cambien")
    pagina.text_edits["why"].setPlainText("Banca europea barata")
    assert pagina.save()
    assert ThesisRepository(cartera.connection()).active_for("SAN").why == "Banca europea barata"
    assert pagina.history_rows[0].entry.title == "Textos editados por ti: por qué la tengo"


def test_un_error_se_ensena_y_no_guarda(qtbot, cartera, tema):
    alta(cartera, tesis("SAN", "4", "7"))
    pagina = pagina_sola(qtbot, cartera, tema)
    pagina.ask_reason = lambda _t: ""
    pagina.stop_edit.setText("9")
    assert not pagina.save()
    assert "por debajo del objetivo" in pagina.error_label.text()
    pagina.stop_edit.setText("cuatro")
    assert not pagina.save()
    assert pagina.error_label.text() == "Stop-loss: «cuatro» no es un número."


def test_los_cambios_sin_guardar_sobreviven_a_una_actualizacion(qtbot, ventana, cartera):
    alta(cartera, tesis("SAN", "4", "7"))
    pagina = ventana.page("tesis")
    pagina.reload()
    pagina.target_edit.setText("7,50")
    actualizar(qtbot, ventana)
    assert pagina.target_edit.text() == "7,50" and pagina.dirty
    pagina.revert()
    assert pagina.target_edit.text() == "7,00" and not pagina.dirty


# -- propuestas ---------------------------------------------------------------------------------


def test_aplicar_y_descartar_la_propuesta_del_objetivo(qtbot, ventana, cartera):
    alta(cartera, tesis("IWDA", "70", "85", "80"))
    actualizar(qtbot, ventana)  # IWDA a 88,40: pasa del objetivo de 85
    pagina = ventana.page("tesis")
    pagina.select_ticker("IWDA")
    fila = pagina.history_rows[0]
    assert fila.entry.title == "Propuesta de Sharky: stop 80,00 EUR"  # break-even
    assert fila.apply_button.isEnabled() and fila.note_label.text() == "Propuesta no aplicada"
    # Con cambios sin guardar en el editor, la propuesta espera.
    pagina.target_edit.setText("95")
    assert not fila.apply_button.isEnabled()
    pagina.revert()
    pagina.history_rows[0].apply_button.click()
    assert ThesisRepository(cartera.connection()).active_for("IWDA").stop == D("80")
    assert pagina.history_rows[0].entry.title == "Propuesta aplicada por ti: stop 70,00 → 80,00"
    assert pagina.history_rows[1].apply_button is None  # ya no está pendiente


def test_bajar_el_objetivo_deja_la_propuesta_a_la_vista(qtbot, ventana, cartera):
    alta(cartera, tesis("IWDA", "70", "95", "80"))
    actualizar(qtbot, ventana)  # IWDA a 88,40: entre niveles
    assert ventana.notificaciones == []
    pagina = ventana.page("tesis")
    pagina.select_ticker("IWDA")
    pagina.ask_reason = lambda _t: ""
    pagina.target_edit.setText("85")
    assert pagina.save()
    assert pagina.history_rows[0].entry.title == "Propuesta de Sharky: stop 80,00 EUR"
    assert pagina.items[0].status_label.text() == "objetivo alcanzado"
    assert ventana.notificaciones[-1][0] == "Objetivo alcanzado en IWDA"


def test_descartar_una_propuesta(qtbot, ventana, cartera):
    alta(cartera, tesis("IWDA", "70", "85", "80"))
    actualizar(qtbot, ventana)
    pagina = ventana.page("tesis")
    pagina.select_ticker("IWDA")
    pagina.history_rows[0].discard_button.click()
    assert ThesisRepository(cartera.connection()).active_for("IWDA").stop == D("70")
    assert pagina.history_rows[0].entry.title == "Propuesta descartada por ti"


# -- los avisos ---------------------------------------------------------------------------------


def test_el_aviso_de_stop_sale_una_sola_vez_al_dia(qtbot, ventana, cartera):
    alta(cartera, tesis("SAN", "5.20", "7"))
    actualizar(qtbot, ventana)
    [dialogo] = ventana.stop_dialogs
    assert dialogo.isVisible() and dialogo.isModal()
    assert [c.ticker for c in dialogo.checks] == ["SAN"]
    textos = " ".join(e.text() for e in dialogo.findChildren(type(dialogo.title_label)))
    assert "SAN cotiza a 5,12 € y su tesis fija el stop en 5,20 €." in textos
    assert "Valor de la posición: 1.024,00 € · PnL +124,00 €" in textos
    assert ventana.notificaciones == [(
        "Stop-loss alcanzado en SAN",
        "SAN cotiza a 5,12 € y su tesis fija el stop en 5,20 €. Salida obligatoria del mandato: "
        "liquida en tu bróker y registra la venta.",
        True,
    )]
    assert [a.notified for a in LevelAlertRepository(cartera.connection()).list_for_date(HOY)] == [
        True
    ]
    dialogo.close_button.click()
    qtbot.waitUntil(lambda: not ventana.stop_dialogs)
    # Otra actualización el mismo día: ni ventana ni notificación, pero el Panel lo sigue
    # marcando en rojo.
    actualizar(qtbot, ventana)
    assert ventana.stop_dialogs == [] and len(ventana.notificaciones) == 1
    primero = ventana.page("panel").data.items[0]
    assert primero.token == "danger"
    assert primero.title == "Stop alcanzado: SAN a 5,12 € (stop 5,20 €)"
    assert primero.detail == "El mandato exige liquidar. Ejecuta en tu bróker y regístralo."


def test_poner_el_stop_por_encima_del_precio_avisa_al_momento_y_una_vez(qtbot, ventana, cartera):
    """El criterio manual del H7, de punta a punta: con precios ya descargados, subir el stop por
    encima del precio en Tesis abre la ventana de aviso una sola vez en el día."""
    alta(cartera, tesis("SAN", "4", "7"))
    actualizar(qtbot, ventana)
    assert ventana.stop_dialogs == []
    pagina = ventana.page("tesis")
    pagina.select_ticker("SAN")
    pagina.ask_reason = lambda _t: "prueba del aviso"
    pagina.stop_edit.setText("5,50")
    assert pagina.save()
    [dialogo] = ventana.stop_dialogs
    assert "fija el stop en 5,50 €" in dialogo.findChildren(type(dialogo.title_label))[2].text()
    assert ventana.page("panel").data.items[0].title.startswith("Stop alcanzado: SAN")
    assert ventana.attention_badge.objectName() == "badgeDanger"
    dialogo.close()
    qtbot.waitUntil(lambda: not ventana.stop_dialogs)
    # Tocar otra vez los números el mismo día no repite la ventana.
    pagina.stop_edit.setText("5,60")
    assert pagina.save()
    actualizar(qtbot, ventana)
    assert ventana.stop_dialogs == []
    assert [n[0] for n in ventana.notificaciones] == ["Stop-loss alcanzado en SAN"]


def test_abrir_sharky_desde_el_aviso_lleva_al_panel(qtbot, ventana, cartera):
    alta(cartera, tesis("SAN", "5.20", "7"))
    ventana.show_section("cartera")
    actualizar(qtbot, ventana)
    [dialogo] = ventana.stop_dialogs
    dialogo.open_button.click()
    assert ventana.current_section == "panel"
    qtbot.waitUntil(lambda: not ventana.stop_dialogs)


def test_varios_stops_en_una_sola_ventana(qtbot, ventana, cartera):
    alta(cartera, tesis("SAN", "5.20", "7"), tesis("IWDA", "90", "120"))
    actualizar(qtbot, ventana)
    [dialogo] = ventana.stop_dialogs
    assert [c.ticker for c in dialogo.checks] == ["IWDA", "SAN"]
    assert dialogo.title_label.text() == "2 stop-loss alcanzados"
    assert [n[2] for n in ventana.notificaciones] == [True, True]


def test_el_objetivo_solo_notifica(qtbot, ventana, cartera):
    alta(cartera, tesis("IWDA", "70", "85", "80"))
    actualizar(qtbot, ventana)
    assert ventana.stop_dialogs == []
    assert ventana.notificaciones == [(
        "Objetivo alcanzado en IWDA",
        "No obliga a vender. Sharky propone subir el stop a 80,00 € (break-even).",
        False,
    )]
    [objetivo] = [i for i in ventana.page("panel").data.items if i.ticker == "IWDA"
                  and i.section == "tesis"]
    assert objetivo.token == "warn"
    assert objetivo.title == "Objetivo alcanzado: IWDA a 88,40 € (objetivo 85,00 €)"


def test_niveles_sin_verificar_en_el_panel(qtbot, ventana, cartera):
    alta(cartera, tesis("VUSA", "80", "120"))
    actualizar(qtbot, ventana)
    [item] = [i for i in ventana.page("panel").data.items if i.title.startswith("Niveles")]
    assert item.title == "Niveles sin verificar: VUSA"
    assert item.token == "warn"
    assert ventana.notificaciones == []


def test_ver_lleva_a_la_tesis(qtbot, ventana, cartera):
    alta(cartera, tesis("AAPL", "150", "300", divisa="USD"), tesis("SAN", "5.20", "7"))
    actualizar(qtbot, ventana)
    for dialogo in list(ventana.stop_dialogs):
        dialogo.close()
    panel = ventana.page("panel")
    _, ver = next((i, b) for i, b in panel.attention_rows if i.title.startswith("Stop"))
    ver.click()
    assert ventana.current_section == "tesis"
    assert ventana.page("tesis").selected.ticker == "SAN"


def test_se_pinta_en_los_dos_temas(qtbot, ventana, cartera, tema):
    alta(cartera, tesis("IWDA", "70", "85", "80"))
    actualizar(qtbot, ventana)
    pagina = ventana.page("tesis")
    pagina.resize(1200, 900)
    for eleccion in (Theme.LIGHT, Theme.DARK):
        tema.set_theme(eleccion)
        assert not pagina.grab().isNull()
