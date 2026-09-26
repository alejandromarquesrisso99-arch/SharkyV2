"""Panel (GUIA §5.10, punto 1, y §7, H6): estado del mandato, patrimonio, efectivo, «Requiere
atención», gráfico del valor por participación y el lateral; y que cuadra con la Cartera.
"""

import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from fakes import FakeMarket
from sharky.core.formatting import format_eur, format_pct
from sharky.core.models import (
    Asset,
    Breach,
    CashKind,
    CashMovement,
    MandateState,
    NavSnapshot,
    PriceSource,
    Trade,
    TradeKind,
)
from sharky.services.market import FxQuote, Quote
from sharky.services.repositories import (
    AssetRepository,
    BreachRepository,
    CashMovementRepository,
    NavSnapshotRepository,
    TradeRepository,
)
from sharky.ui.main_window import MainWindow
from sharky.ui.panel import PanelPage, day_text, short_when
from sharky.ui.portfolio import PortfolioPage, PriceRefresher
from sharky.ui.theme import Theme, ThemeController

MADRID = timezone(timedelta(hours=2))
AHORA = datetime(2026, 9, 25, 18, 5, tzinfo=MADRID)
HOY = AHORA.date()
AYER = HOY - timedelta(days=1)
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


def efectivo(db, *movimientos):
    with db.transaction() as conn:
        for m in movimientos:
            CashMovementRepository(conn).add(m)


def fotos(db, *snapshots):
    with db.transaction() as conn:
        for s in snapshots:
            NavSnapshotRepository(conn).save(s)


def foto(dia, nav, unidades, valor, maximo="100", caida="0", estado=MandateState.OPTIMAL,
         fiable=True):
    return NavSnapshot(dia, D(nav), D(nav), D(unidades), D(valor), D(maximo), D(caida), estado,
                       D("1") if fiable else D("0.5"), fiable)


@pytest.fixture
def cartera(db):
    """2.500 € de efectivo y cuatro posiciones; VUSA sin símbolo va a coste."""
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


@pytest.fixture
def mercado():
    return FakeMarket(COTIZACIONES, CAMBIOS)


def hacer_panel(qtbot, db, tema, mercado=None):
    mercado = mercado or FakeMarket(COTIZACIONES, CAMBIOS)
    actualizador = PriceRefresher(db, mercado, mercado, now=lambda: AHORA)
    pagina = PanelPage(db, tema, actualizador, now=lambda: AHORA)
    qtbot.addWidget(pagina)
    return pagina


def actualizar(qtbot, pagina):
    with qtbot.waitSignal(pagina.refreshFinished, timeout=5000):
        pagina.refresh_button.click()


@pytest.fixture
def ventana(qtbot, cartera, tema, mercado):
    v = MainWindow(tema, "9.9.9", db=cartera, market=mercado, fx=mercado, now=lambda: AHORA)
    qtbot.addWidget(v)
    yield v
    v.shutdown()


# -- cuadra con la Cartera ------------------------------------------------------------------


def test_el_panel_cuadra_con_la_cartera(qtbot, ventana):
    panel, cartera = ventana.page("panel"), ventana.page("cartera")
    assert isinstance(panel, PanelPage) and isinstance(cartera, PortfolioPage)
    actualizar(qtbot, panel)
    v_panel, v_cartera = panel.data.valuation, cartera.valuation
    assert v_panel.nav_eur == v_cartera.nav_eur == D("9964.90")
    assert v_panel.coverage == v_cartera.coverage
    assert [(p.ticker, p.value_eur, p.source) for p in v_panel.positions] == [
        (p.ticker, p.value_eur, p.source) for p in v_cartera.positions
    ]
    assert v_panel.position("AAPL").source is PriceSource.MARKET  # la misma hora de mercado
    assert panel.nav_label.text() == "9.964,90 €"
    assert panel.cash_label.text() == format_eur(v_cartera.cash_eur)
    cobertura = format_pct(v_cartera.coverage, truncate=True)
    assert f"Cobertura de datos: {cobertura} del NAV" in panel.coverage_label.text()
    assert f"Cobertura {cobertura}:" in cartera.footer.text()


def test_actualizar_desde_la_cartera_pone_al_dia_el_panel(qtbot, ventana):
    panel, cartera = ventana.page("panel"), ventana.page("cartera")
    assert not panel.data.valuation.reliable  # sin precios, todo a coste
    with qtbot.waitSignal(panel.refreshFinished, timeout=5000):
        cartera.refresh_button.click()
    assert panel.data.valuation.reliable
    assert panel.data.valuation.market_at == AHORA
    assert panel.data.valuation.nav_eur == cartera.valuation.nav_eur


def test_una_sola_descarga_para_las_dos_pantallas(qtbot, cartera, tema):
    puerta = threading.Event()
    lento = FakeMarket(COTIZACIONES, CAMBIOS, gate=puerta)
    v = MainWindow(tema, "9.9.9", db=cartera, market=lento, fx=lento, now=lambda: AHORA)
    qtbot.addWidget(v)
    v.show()
    panel, pagina_cartera = v.page("panel"), v.page("cartera")
    panel.refresh_button.click()
    assert not panel.refresh_button.isEnabled()
    assert not pagina_cartera.refresh_button.isEnabled()
    assert not panel.progress_box.isHidden() and not pagina_cartera.progress_box.isHidden()
    pagina_cartera.cancel_button.click()
    assert panel.progress_box.label.text() == "Cancelando…"
    with qtbot.waitSignal(panel.refreshFinished, timeout=5000):
        puerta.set()
    assert panel.refresh_button.isEnabled() and pagina_cartera.refresh_button.isEnabled()
    v.shutdown()


# -- la foto y los incumplimientos se guardan al actualizar ---------------------------------


def test_actualizar_guarda_la_foto_del_dia_y_los_incumplimientos(qtbot, cartera, tema):
    pagina = hacer_panel(qtbot, cartera, tema)
    assert NavSnapshotRepository(cartera.connection()).list_all() == []  # abrir no escribe
    actualizar(qtbot, pagina)
    guardada = NavSnapshotRepository(cartera.connection()).get(HOY)
    assert guardada == pagina.data.snapshot
    assert (guardada.unit_value, guardada.reliable) == (D("100"), True)  # la primera fiable
    abiertos = {(b.rule, b.subject) for b in BreachRepository(cartera.connection()).list_open()}
    assert abiertos == {f.key for f in pagina.data.findings}
    assert ("ACTIVO", "IWDA") in abiertos and ("COBERTURA", "") in abiertos


# -- estado del mandato ---------------------------------------------------------------------


def test_estado_alerta_con_su_drawdown(qtbot, db, tema):
    # Ayer, 10.000 € a 100; hoy una comisión de 410 € deja el valor en 95,9: drawdown del 4,1 %.
    efectivo(db, CashMovement(ANTEAYER, CashKind.INITIAL, D("10000")),
             CashMovement(HOY, CashKind.FEE, D("-410")))
    fotos(db, foto(AYER, "10000", "100", "100"))
    pagina = hacer_panel(qtbot, db, tema)
    assert pagina.state_label.text() == "Alerta"
    assert pagina.state_label.objectName() == "stateWarn"
    assert pagina.drawdown_label.text() == "drawdown 4,1 %"
    assert pagina.limits_label.text() == (
        "Tope por activo 5 % · efectivo mínimo 30 % mientras dure la alerta"
    )
    assert pagina.unit_label.text() == "Valor por participación 95,90 · máximo 100,00"
    assert pagina.drawdown_bar.token == "warn"
    assert [texto for _, texto in pagina.drawdown_bar.marks] == ["3 %", "8 %", "20 %"]
    assert pagina.change_label.text() == "−410,00 € hoy (−4,10 %)"
    assert pagina.change_label.objectName() == "dangerText"
    assert pagina.held_label.isHidden()
    # Todo en efectivo: en Alerta no hay techo y el mínimo es el 30 %.
    assert pagina.band_label.text() == "Mínimo del mandato en Alerta: 30 %"
    assert pagina.cash_status_label.text() == "Por encima del mínimo del mandato"
    assert pagina.attention_subtitle.text() == "Nada que mirar: la cartera cumple el mandato."


def test_un_ingreso_no_es_ganancia_en_el_panel(qtbot, db, tema):
    efectivo(db, CashMovement(ANTEAYER, CashKind.INITIAL, D("10000")),
             CashMovement(HOY, CashKind.DEPOSIT, D("5000")))
    fotos(db, foto(AYER, "10000", "100", "100"))
    pagina = hacer_panel(qtbot, db, tema)
    assert pagina.nav_label.text() == "15.000,00 €"
    assert pagina.unit_label.text() == "Valor por participación 100,00 · máximo 100,00"
    assert pagina.change_label.text() == "0,00 € hoy (0,00 %)"
    assert pagina.change_label.objectName() == "muted"
    assert pagina.state_label.text() == "Óptimo"
    # Todo en efectivo en Óptimo: por encima del 30 %.
    assert pagina.cash_status_label.text() == (
        "Sobran unos 10.500 € por encima del máximo del mandato"
    )
    assert pagina.band_label.text() == "Banda del mandato: 15–30 %"


def test_con_cobertura_baja_se_mantiene_el_estado_y_se_avisa(qtbot, cartera, tema):
    fotos(cartera, foto(AYER, "9600", "100", "96", "100", "0.04", MandateState.ALERT))
    pagina = hacer_panel(qtbot, cartera, tema)  # sin actualizar: todo a coste
    assert not pagina.data.valuation.reliable
    assert pagina.state_label.text() == "Alerta"
    assert pagina.drawdown_label.text() == "drawdown 4,0 %"
    assert not pagina.held_label.isHidden()
    assert "se mantienen el estado y el máximo de ayer" in pagina.held_label.text()
    assert pagina.change_label.text() == "Sin variación: la valoración no es fiable"
    primero = pagina.data.items[0]
    assert primero.token == "danger" and primero.title.startswith("Cobertura del")
    assert "no se actualizan" in primero.detail
    assert pagina.chart.points == [(AYER, D("96"))]  # hoy no es fiable: no se dibuja


def test_sin_ninguna_valoracion_fiable_lo_dice(qtbot, cartera, tema):
    pagina = hacer_panel(qtbot, cartera, tema)
    assert "empezará en 100 con la primera valoración fiable" in pagina.held_label.text()
    assert pagina.chart.points == []
    assert "Actualizar precios" in pagina.chart_note.text()


# -- requiere atención ----------------------------------------------------------------------


def test_requiere_atencion_con_incumplimientos_y_posiciones_sin_tesis(qtbot, cartera, tema):
    pagina = hacer_panel(qtbot, cartera, tema)
    actualizar(qtbot, pagina)
    titulos = [i.title for i in pagina.data.items]
    assert "IWDA pesa 44,4 % del patrimonio (límite 10 % en Óptimo)" in titulos
    assert "4 posiciones sin tesis: AAPL, IWDA, SAN y VUSA" in titulos
    assert pagina.attention_subtitle.text() == f"{len(titulos)} cosas que mirar"
    assert [i for i, _ in pagina.attention_rows] == list(pagina.data.items)
    iwda = next(i for i in pagina.data.items if i.ticker == "IWDA")
    assert iwda.detail == "Reducir IWDA al 10 %: vender unos 3.424 € · abierto hoy"
    assert iwda.token == "warn"


def test_un_incumplimiento_escalado_va_en_rojo_y_primero(qtbot, cartera, tema):
    with cartera.transaction() as conn:
        BreachRepository(conn).add(
            Breach("ACTIVO", "AAPL", AHORA - timedelta(days=9), AHORA - timedelta(days=1))
        )
    pagina = hacer_panel(qtbot, cartera, tema)
    actualizar(qtbot, pagina)
    from PySide6.QtWidgets import QLabel

    primero, ver = pagina.attention_rows[0]
    assert primero.ticker == "AAPL" and primero.token == "danger"
    assert primero.detail.endswith("abierto desde hace 9 días (escalado)")
    [punto] = [e for e in ver.parentWidget().findChildren(QLabel)
               if e.objectName().startswith("dot")]
    assert punto.objectName() == "dotDanger"


def test_ver_lleva_a_la_cartera_con_la_fila_elegida(qtbot, ventana):
    panel = ventana.page("panel")
    actualizar(qtbot, panel)
    _, ver = next((i, b) for i, b in panel.attention_rows if i.ticker == "IWDA")
    ver.click()
    assert ventana.current_section == "cartera"
    assert ventana.page("cartera").selected_row().position.ticker == "IWDA"
    _, ver_tesis = next((i, b) for i, b in panel.attention_rows if i.section == "tesis")
    ver_tesis.click()
    assert ventana.current_section == "tesis"


# -- el gráfico -----------------------------------------------------------------------------


def test_el_grafico_solo_dibuja_dias_fiables(qtbot, db, tema):
    efectivo(db, CashMovement(ANTEAYER, CashKind.INITIAL, D("10200")))
    fotos(db, foto(ANTEAYER, "10000", "100", "100"),
          foto(AYER, "9000", "100", "90", fiable=False))
    pagina = hacer_panel(qtbot, db, tema)
    assert pagina.chart.points == [(ANTEAYER, D("100")), (HOY, D("102"))]
    assert pagina.chart_note.isHidden()


# -- el lateral -----------------------------------------------------------------------------


def test_el_lateral_ensena_el_estado_el_control_y_el_recuento(qtbot, ventana):
    panel = ventana.page("panel")
    assert ventana._last_check_label.text() == "Último control: —"  # sin precios todavía
    actualizar(qtbot, panel)
    assert ventana._mandate_label.text() == "Óptimo · drawdown 0,0 %"
    assert ventana._mandate_chip.objectName() == "chipOk"
    assert ventana._last_check_label.text() == "Último control: hoy, 18:05"
    n = len(panel.data.items)
    assert ventana.attention_badge.text() == str(n) and not ventana.attention_badge.isHidden()
    assert ventana.attention_badge.objectName() == "badgeWarn"


def test_sin_nada_que_mirar_no_hay_recuento(qtbot, db, tema):
    efectivo(db, CashMovement(ANTEAYER, CashKind.INITIAL, D("10000")),
             CashMovement(HOY, CashKind.FEE, D("-410")))
    fotos(db, foto(AYER, "10000", "100", "100"))
    mercado = FakeMarket()
    v = MainWindow(tema, "9.9.9", db=db, market=mercado, fx=mercado, now=lambda: AHORA)
    qtbot.addWidget(v)
    assert v.attention_badge.isHidden()
    assert v._mandate_label.text() == "Alerta · drawdown 4,1 %"
    assert v._mandate_chip.objectName() == "chipWarn"


def test_textos_de_fecha():
    assert day_text(HOY, HOY) == "hoy"
    assert day_text(AYER, HOY) == "ayer"
    assert day_text(date(2026, 9, 1), HOY) == "01/09/2026"
    assert short_when(AHORA - timedelta(days=1), AHORA) == "ayer, 18:05"


# -- tema y tarjetas pendientes -------------------------------------------------------------


def test_sin_lanzador_las_tarjetas_de_informes_quedan_para_mas_adelante(qtbot, cartera, tema):
    """Sin `runner`, los tres informes dicen «Próximamente»; la tarjeta del radar (H11) ya
    funciona: lee las alertas activas de la base de datos."""
    from PySide6.QtWidgets import QLabel

    pagina = hacer_panel(qtbot, cartera, tema)
    textos = [e.text() for e in pagina.findChildren(QLabel)]
    for titulo in ("Control diario", "Noticias semanales", "Estudio mensual",
                   "Oportunidades en radar"):
        assert titulo in textos
    assert textos.count("Próximamente") == 3
    assert pagina.radar_card.count_label.text() == "0 activas"


def test_se_pinta_en_los_dos_temas(qtbot, cartera, tema):
    pagina = hacer_panel(qtbot, cartera, tema)
    actualizar(qtbot, pagina)
    pagina.resize(1400, 1000)
    for eleccion in (Theme.LIGHT, Theme.DARK):
        tema.set_theme(eleccion)
        assert not pagina.grab().isNull()
