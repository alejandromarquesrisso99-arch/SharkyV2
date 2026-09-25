"""Operaciones y efectivo (GUIA §5.5 y §7, H8): el registro en una transacción, la validación
en su orden, las operaciones forzadas, la tesis que abre una compra y la que cierra una venta
total, y los movimientos de efectivo.

Datos inventados. Cartera de partida (NAV 10.000 €):

    SAN    200 × 4,00 €        =   800 €  (8 %)   Banca          coste 4,50 €
    IWDA    20 × 90,00 €       = 1.800 €  (18 %)  Renta_Variable coste 80 €
    AAPL     4 × 200 USD × 0,85 =   680 €  (6,8 %) Tecnologia    coste 150 €
    efectivo                     6.720 €
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from sharky.core.csv_import import asset_from_fields
from sharky.core.ledger import (
    build_ledger,
    cycle_realized_pnl,
    signed_cash_amount,
    trade_date_error,
)
from sharky.core.levels import (
    LevelChoice,
    buy_entry,
    chosen_levels,
    close_reason,
    history,
    level_update,
)
from sharky.core.mandate import (
    Check,
    apply_flow,
    cash_date_error,
    day_change,
    snapshot_for,
)
from sharky.core.models import (
    Asset,
    CashKind,
    CashMovement,
    FxRate,
    LevelAlert,
    LevelAlertKind,
    MandateState,
    NavSnapshot,
    Price,
    PriceSource,
    Thesis,
    ThesisEventKind,
    ThesisStatus,
    Trade,
    TradeKind,
)
from sharky.services import repositories
from sharky.services.repositories import (
    AssetRepository,
    CashMovementRepository,
    LevelAlertRepository,
    NavSnapshotRepository,
    ThesisEventRepository,
    ThesisRepository,
    TradeError,
    TradeRepository,
    TradeTicket,
    create_theses,
    load_valuation,
    record_balance_adjustment,
    record_cash_movement,
    record_trade,
    record_valuation,
    review_trade,
)
from sharky.services.settings import MandateSettings

MADRID = timezone(timedelta(hours=2))
AHORA = datetime(2026, 9, 25, 18, 5, tzinfo=MADRID)
HOY = AHORA.date()
AYER = HOY - timedelta(days=1)
INICIO = HOY - timedelta(days=5)
REGLAS = MandateSettings().rules()

#: ticker: (nombre, divisa, símbolo, sector, unidades, coste medio EUR, precio)
POSICIONES = {
    "SAN": ("Banco Santander", "EUR", "SAN.MC", "Banca", "200", "4.5", "4"),
    "IWDA": ("iShares MSCI World", "EUR", "IWDA.AS", "Renta_Variable", "20", "80", "90"),
    "AAPL": ("Apple Inc.", "USD", "AAPL", "Tecnologia", "4", "150", "200"),
}
EFECTIVO = D("6720")


def crear_cartera(db):
    """La cartera de partida, con precios de hoy y el cambio del dólar (datos inventados)."""
    with db.transaction() as conn:
        for ticker, (nombre, divisa, simbolo, sector, unidades, coste, precio) in (
            POSICIONES.items()
        ):
            AssetRepository(conn).add(
                Asset(ticker, nombre, divisa, yahoo_symbol=simbolo, sector=sector)
            )
            TradeRepository(conn).add(Trade(INICIO, ticker, TradeKind.OPENING, D(unidades),
                                            D(coste), "EUR", D(1), D(0),
                                            D(unidades) * D(coste)))
            repositories.PriceRepository(conn).save(
                Price(ticker, HOY, D(precio), divisa, PriceSource.MARKET, AHORA)
            )
        repositories.FxRateRepository(conn).save(
            FxRate("USD", HOY, D("0.85"), PriceSource.MARKET, AHORA)
        )
        CashMovementRepository(conn).add(CashMovement(INICIO, CashKind.INITIAL, EFECTIVO))
    return db


@pytest.fixture
def cartera(db):
    return crear_cartera(db)


def compra(ticker="ASML", units="1", price="700", currency="EUR", fx="1", fee="1",
           stop="650", target="850", levels="EUR", day=HOY, reason="", new_asset=None):
    def d(x):
        return D(x) if x is not None else None

    if new_asset is None and ticker == "ASML":
        new_asset = Asset("ASML", "ASML Holding", "EUR", yahoo_symbol="ASML.AS",
                          sector="Semiconductores")
    return TradeTicket(TradeKind.BUY, ticker, day, D(units), D(price), currency, D(fx), D(fee),
                       d(stop), d(target), levels, reason, new_asset)


def venta(ticker="SAN", units="100", price="5", fee="1", day=HOY, reason="", currency="EUR"):
    return TradeTicket(TradeKind.SELL, ticker, day, D(units), D(price), currency, D(1), D(fee),
                       reason=reason)


def registrar(db, ticket, **kwargs):
    with db.transaction() as conn:
        return record_trade(conn, ticket, REGLAS, AHORA, **kwargs)


def revisar(db, ticket):
    return review_trade(db.connection(), ticket, REGLAS, AHORA)


def efectivo(db):
    return CashMovementRepository(db.connection()).balance()


def posicion(db, ticker):
    return build_ledger(TradeRepository(db.connection()).list_all()).position(ticker)


def alta_tesis(db, ticker, stop, objetivo, entrada=None, divisa="EUR"):
    with db.transaction() as conn:
        return create_theses(conn, [Thesis(ticker, divisa, INICIO,
                                           entry_price=D(entrada) if entrada else None,
                                           stop=D(stop), target=D(objetivo))], AHORA)[0]


def foto(dia, nav, unidades, valor="100", maximo="100", fiable=True, efectivo_eur="6720"):
    return NavSnapshot(dia, D(nav), D(efectivo_eur), D(unidades), D(valor), D(maximo), D(0),
                       MandateState.OPTIMAL, D(1), fiable)


def guardar_foto(db, snapshot):
    with db.transaction() as conn:
        NavSnapshotRepository(conn).save(snapshot)


# -- una compra ---------------------------------------------------------------------------------


def test_una_compra_de_un_ticker_nuevo_lo_guarda_todo_junto(cartera):
    hecho = registrar(cartera, compra())
    conn = cartera.connection()
    assert AssetRepository(conn).get("ASML").sector == "Semiconductores"
    op = TradeRepository(conn).get(hecho.trade.id)
    assert (op.kind, op.units, op.amount_eur, op.fee_eur) == (TradeKind.BUY, 1, 700, 1)
    assert (op.stop, op.target, op.levels_currency, op.forced) == (650, 850, "EUR", False)
    # Su movimiento de efectivo apunta a ella y saca importe + comisión.
    movimiento = [m for m in CashMovementRepository(conn).list_all() if m.trade_id == op.id]
    assert [(m.kind, m.amount_eur) for m in movimiento] == [(CashKind.TRADE, D("-701"))]
    assert efectivo(cartera) == EFECTIVO - 701


def test_comprar_un_ticker_sin_tesis_la_abre_con_precio_stop_y_objetivo(cartera):
    hecho = registrar(cartera, compra())
    tesis = ThesisRepository(cartera.connection()).get(hecho.opened_thesis)
    assert (tesis.ticker, tesis.status, tesis.opened_on) == ("ASML", ThesisStatus.ACTIVE, HOY)
    assert (tesis.entry_price, tesis.stop, tesis.target, tesis.levels_currency) == (
        700, 650, 850, "EUR"
    )
    eventos = ThesisEventRepository(cartera.connection()).list_for(tesis.id)
    assert [e.kind for e in eventos] == [ThesisEventKind.CREATED]
    assert eventos[0].text == "Tesis abierta con la compra del 25/09/2026: 1 a 700,00 EUR"


def test_coste_medio_ponderado_con_las_comisiones_de_compra(cartera):
    alta_tesis(cartera, "SAN", "3.8", "4.6")
    registrar(cartera, compra("SAN", units="20", price="4", fee="2", stop="3.8", target="4.6"))
    p = posicion(cartera, "SAN")
    # 200 × 4,50 + 20 × 4,00 + 2 de comisión.
    assert (p.units, p.cost_eur) == (220, D("982"))
    assert p.avg_cost_eur == D("982") / 220


def test_compra_en_usd_con_niveles_en_eur(cartera):
    # 1 AAPL a 200 USD con el cambio del bróker (0,86): 172 € de importe.
    hecho = registrar(cartera, compra("AAPL", price="200", currency="USD", fx="0.86", fee="1",
                                      stop="160", target="220", levels="EUR"))
    assert hecho.trade.amount_eur == D("172.00")
    assert hecho.movement.amount_eur == D("-173.00")
    revision = hecho.review
    # Los niveles en EUR se comparan con el precio en EUR (172 €): ratio (220−172)/(172−160) = 4.
    assert "(comparados en EUR)" in revision.validation.result(Check.LEVELS).message
    assert "Ratio beneficio/riesgo 4,00" in revision.validation.result(Check.REWARD_RISK).message
    # Riesgo hasta el stop: (172 − 160) × 1 = 12 €.
    assert "12,00 €" in revision.validation.result(Check.TRADE_RISK).message
    tesis = ThesisRepository(cartera.connection()).get(hecho.opened_thesis)
    assert (tesis.entry_price, tesis.levels_currency) == (D("172"), "EUR")


def test_compra_en_eur_con_niveles_en_usd_usa_el_cambio_guardado(cartera):
    revision = revisar(cartera, compra("AAPL", price="170", stop="190", target="260",
                                       levels="USD"))
    # 170 € frente a stop 190 USD × 0,85 = 161,50 € y objetivo 221 €.
    assert revision.levels_to_eur == D("0.85")
    assert revision.validation.result(Check.LEVELS).passed


def test_niveles_en_una_divisa_sin_cambio_guardado(cartera):
    with pytest.raises(TradeError, match="No hay ningún cambio GBP→EUR"):
        revisar(cartera, compra(levels="GBp"))


def test_divisa_distinta_de_eur_necesita_su_cambio(cartera):
    with pytest.raises(TradeError, match="Falta el cambio a EUR: cuántos euros vale 1 USD"):
        revisar(cartera, compra("AAPL", currency="USD", fx="0", levels="USD"))


def test_un_ticker_se_encuentra_sin_distinguir_mayusculas(cartera):
    revision = revisar(cartera, venta("san", units="10"))
    assert revision.ticket.ticker == "SAN"
    assert revision.validation.ok


def test_un_ticker_nuevo_necesita_sus_datos(cartera):
    with pytest.raises(TradeError, match="NVDA es nuevo: faltan sus datos"):
        revisar(cartera, compra("NVDA"))
    with pytest.raises(TradeError, match="No tienes NVDA"):
        revisar(cartera, venta("NVDA"))


def test_los_datos_de_un_activo_nuevo_se_validan_como_en_el_csv():
    activo, errores = asset_from_fields("NV DA", "", "dolares", isin="US123",
                                        sector="Semi conductores", yahoo_symbol="NV DA")
    assert activo is None
    assert len(errores) == 6
    activo, errores = asset_from_fields("VOD", "Vodafone", "gbx", isin="gb00bh4hkS39")
    assert errores == []
    assert (activo.currency, activo.isin) == ("GBp", "GB00BH4HKS39")


def test_fechas_de_una_operacion(cartera):
    with pytest.raises(TradeError, match="La fecha no puede ser futura"):
        revisar(cartera, compra(day=HOY + timedelta(days=1)))
    with pytest.raises(TradeError, match="La cartera empieza el 20/09/2026"):
        revisar(cartera, compra(day=INICIO - timedelta(days=1)))
    registrar(cartera, venta("SAN", units="10"))
    with pytest.raises(TradeError, match="La última operación de SAN es del 25/09/2026"):
        revisar(cartera, venta("SAN", units="10", day=AYER))
    # Otro ticker sí puede ir con fecha de ayer.
    assert revisar(cartera, venta("IWDA", units="1", day=AYER)).validation.ok


def test_trade_date_error_en_sus_fronteras():
    assert trade_date_error(HOY, HOY, "SAN", INICIO, HOY) is None
    assert trade_date_error(INICIO, HOY, "SAN", INICIO, None) is None


# -- la validación, en su orden -------------------------------------------------------------------


def estado_en_caida(db, caida):
    """Una foto fiable de ayer con el máximo en 100 y las participaciones justas para que hoy
    el valor por participación caiga `caida`."""
    nav = load_valuation(db.connection(), AHORA).nav_eur
    valor = 100 * (1 - D(caida))
    guardar_foto(db, foto(AYER, nav, nav / valor))


@pytest.mark.parametrize(
    ("cambios", "caida", "paso", "motivo"),
    [
        ({}, "0.10", Check.STATE, "Compras prohibidas en estado Cuidados intensivos"),
        ({"target": "720"}, "0", Check.REWARD_RISK, "Ratio beneficio/riesgo 0,40"),
        ({"units": "2"}, "0", Check.ASSET_WEIGHT, "ASML quedaría al 14,0 %"),
        ({"stop": "500", "target": "1200"}, "0", Check.TRADE_RISK, "Riesgo hasta el stop: 200"),
    ],
)
def test_cada_paso_del_mandato_rechaza_con_su_motivo(cartera, cambios, caida, paso, motivo):
    if caida != "0":
        estado_en_caida(cartera, caida)
    revision = revisar(cartera, compra(**cambios))
    fallo = revision.validation.failure
    assert fallo.check is paso
    assert motivo in fallo.message
    assert fallo.forceable
    with pytest.raises(TradeError, match="No cumple el mandato"):
        registrar(cartera, compra(**cambios))


def test_paso_7_peso_final_del_sector(cartera):
    # IWDA (Renta_Variable, 18 %) + 900 € de otro fondo del mismo sector: 27 % > 25 %.
    otro = Asset("VWCE", "Vanguard All-World", "EUR", sector="Renta_Variable")
    revision = revisar(cartera, compra("VWCE", units="9", price="100", stop="95", target="120",
                                       new_asset=otro))
    assert revision.validation.result(Check.ASSET_WEIGHT).passed
    assert revision.validation.failure.check is Check.SECTOR_WEIGHT
    assert revision.validation.failure.message.startswith("El sector Renta Variable quedaría")


def test_paso_8_efectivo_minimo_del_estado(cartera):
    # Una retirada deja el efectivo en 720 € (18 % de 4.000 €); la compra lo baja al 10,5 %.
    with cartera.transaction() as conn:
        record_cash_movement(conn, CashKind.WITHDRAWAL, HOY, D("6000"), AHORA)
    revision = revisar(cartera, compra(units="1", price="300", stop="285", target="340"))
    assert revision.validation.failure.check is Check.CASH
    assert "La liquidez caería" in revision.validation.failure.message
    assert revision.validation.failure.forceable


def test_se_ensena_la_primera_que_falla(cartera):
    # En Cuidados intensivos, con un ratio malo y demasiado peso: manda el estado (paso 1).
    estado_en_caida(cartera, "0.10")
    revision = revisar(cartera, compra(units="2", target="720"))
    assert revision.validation.failure.check is Check.STATE
    assert [r.check for r in revision.validation.also_failing][:2] == [
        Check.REWARD_RISK, Check.ASSET_WEIGHT
    ]


@pytest.mark.parametrize(
    ("ticket", "motivo"),
    [
        (compra(units="0"), "Hace falta que las unidades sean mayores que 0"),
        (compra(price="0"), "Hace falta que el precio sea mayor que 0"),
        (compra(stop=None), "Falta el stop"),
        (compra(stop="710"), "El stop (710,00 EUR) tiene que quedar por debajo"),
        (compra(target="690"), "El objetivo (690,00 EUR) tiene que quedar por encima"),
        (compra(units="10", price="700", stop="690", target="800"), "No hay efectivo suficiente"),
        (venta("SAN", units="250"), "No se pueden vender 250 unidades de SAN: solo hay 200"),
        (venta("SAN", units="0"), "Hace falta que las unidades sean mayores que 0"),
    ],
)
def test_lo_que_no_se_puede_forzar(cartera, ticket, motivo):
    revision = revisar(cartera, ticket)
    assert revision.validation.blocking is not None
    assert not revision.validation.forceable
    with pytest.raises(TradeError) as error:
        registrar(cartera, ticket, forced=True)
    assert motivo in str(error.value)
    # No se ha guardado nada: ni la operación ni el activo nuevo.
    conn = cartera.connection()
    assert [t.kind for t in TradeRepository(conn).list_all()] == [TradeKind.OPENING] * 3
    assert AssetRepository(conn).get("ASML") is None


def test_operacion_forzada_exige_motivo_y_queda_marcada(cartera):
    malo = compra(units="2")  # ASML al 14 %: tope del 10 %
    assert revisar(cartera, malo).validation.forceable
    with pytest.raises(TradeError, match="hace falta escribir el motivo"):
        registrar(cartera, malo, forced=True)
    assert AssetRepository(cartera.connection()).get("ASML") is None
    motivo = "Convicción alta tras resultados"
    hecho = registrar(cartera, compra(units="2", reason=motivo), forced=True)
    op = TradeRepository(cartera.connection()).get(hecho.trade.id)
    assert (op.forced, op.reason) == (True, motivo)
    assert hecho.forced


def test_forzar_una_que_cumple_no_la_marca(cartera):
    hecho = registrar(cartera, compra(reason="Por si acaso"), forced=True)
    assert not TradeRepository(cartera.connection()).get(hecho.trade.id).forced


# -- la transacción -----------------------------------------------------------------------------


def test_la_operacion_y_su_efectivo_entran_juntos_o_no_entra_nada(cartera, monkeypatch):
    def falla(*_args, **_kwargs):
        raise RuntimeError("se va la luz")

    monkeypatch.setattr(repositories, "create_theses", falla)
    antes = efectivo(cartera)
    with pytest.raises(RuntimeError):
        registrar(cartera, compra())
    conn = cartera.connection()
    assert AssetRepository(conn).get("ASML") is None
    assert TradeRepository(conn).list_for("ASML") == []
    assert efectivo(cartera) == antes


def test_escribir_fuera_de_una_transaccion_se_rechaza(cartera):
    with pytest.raises(repositories.NotInTransactionError):
        record_trade(cartera.connection(), compra(), REGLAS, AHORA)


# -- ventas y cierre de la tesis ------------------------------------------------------------------


def test_pnl_realizado_de_una_venta_parcial_y_la_tesis_sigue_abierta(cartera):
    id_tesis = alta_tesis(cartera, "SAN", "3.5", "5.5")
    hecho = registrar(cartera, venta("SAN", units="100", price="5", fee="1"))
    # 100 × 5 − 1 − 100 × 4,50 = 49 €.
    assert hecho.review.sale_pnl_eur == D("49")
    assert not hecho.review.closes_position
    assert hecho.closed_thesis is None
    assert hecho.movement.amount_eur == D("499")
    assert ThesisRepository(cartera.connection()).get(id_tesis).status is ThesisStatus.ACTIVE
    assert posicion(cartera, "SAN").units == 100


def test_vender_todo_cierra_la_tesis_con_el_pnl_de_toda_la_posicion(cartera):
    id_tesis = alta_tesis(cartera, "SAN", "3.5", "5.5")
    registrar(cartera, venta("SAN", units="100", price="5", fee="1"))  # +49
    hecho = registrar(cartera, venta("SAN", units="100", price="4", fee="1",
                                     reason="Rota la tesis"))  # 399 − 450 = −51
    assert hecho.review.closes_position
    assert hecho.closed_thesis == id_tesis
    tesis = ThesisRepository(cartera.connection()).get(id_tesis)
    assert (tesis.status, tesis.closed_on, tesis.realized_pnl_eur) == (
        ThesisStatus.CLOSED, HOY, D("-2")
    )
    assert tesis.close_reason == "Venta total el 25/09/2026. Motivo: Rota la tesis"
    eventos = ThesisEventRepository(cartera.connection()).list_for(id_tesis)
    assert eventos[-1].kind is ThesisEventKind.CLOSED
    assert eventos[-1].text.startswith("PnL realizado −2,00 €. Venta total el 25/09/2026.")
    assert posicion(cartera, "SAN") is None


def test_la_venta_total_dice_si_hoy_salto_su_stop(cartera):
    id_tesis = alta_tesis(cartera, "SAN", "4.2", "5.5")  # cotiza a 4 €: stop saltado
    with cartera.transaction() as conn:
        LevelAlertRepository(conn).add(
            LevelAlert(HOY, "SAN", LevelAlertKind.STOP, D("4"), D("4.2"))
        )
    registrar(cartera, venta("SAN", units="200", price="4"))
    tesis = ThesisRepository(cartera.connection()).get(id_tesis)
    assert "Hoy saltó su stop: cotizaba a 4,00 € con el stop en 4,20 €." in tesis.close_reason


def test_sin_aviso_guardado_el_stop_se_mira_con_los_precios_de_ahora(cartera):
    id_tesis = alta_tesis(cartera, "SAN", "4.2", "5.5")
    registrar(cartera, venta("SAN", units="200", price="4"))
    assert "Hoy saltó su stop" in ThesisRepository(cartera.connection()).get(id_tesis).close_reason


def test_vender_todo_sin_tesis_no_cierra_nada(cartera):
    hecho = registrar(cartera, venta("IWDA", units="20", price="90"))
    assert hecho.review.closes_position
    assert hecho.closed_thesis is None


def test_volver_a_comprar_tras_cerrar_abre_una_tesis_nueva(cartera):
    vieja = alta_tesis(cartera, "SAN", "3.5", "5.5")
    registrar(cartera, venta("SAN", units="200", price="4"))
    hecho = registrar(cartera, compra("SAN", units="10", price="4", stop="3.8", target="4.6"))
    assert hecho.opened_thesis not in (None, vieja)
    assert ThesisRepository(cartera.connection()).get(vieja).status is ThesisStatus.CLOSED


def test_cycle_realized_pnl_empieza_tras_el_ultimo_cierre():
    def op(kind, units, price, fee="0"):
        u, p = D(units), D(price)
        return Trade(HOY, "X", kind, u, p, "EUR", D(1), D(fee), u * p)

    libro = build_ledger([
        op(TradeKind.BUY, "10", "10"), op(TradeKind.SELL, "10", "12"),  # +20, cierra
        op(TradeKind.BUY, "10", "10"), op(TradeKind.SELL, "5", "11"),  # +5
        op(TradeKind.SELL, "5", "9", fee="1"),  # −6, cierra
    ])
    assert cycle_realized_pnl(libro, "X") == D("-1")
    assert cycle_realized_pnl(libro, "Y") is None


# -- ampliar una posición con tesis ---------------------------------------------------------------


def test_ampliar_con_los_mismos_niveles_no_pregunta(cartera):
    alta_tesis(cartera, "SAN", "3.8", "4.6")
    revision = revisar(cartera, compra("SAN", units="20", price="4", stop="3.8", target="4.6"))
    assert revision.level_update is None
    hecho = registrar(cartera, compra("SAN", units="20", price="4", stop="3.8", target="4.6"))
    assert hecho.updated_thesis is None


def test_ampliar_con_otros_niveles_hay_que_decidir(cartera):
    alta_tesis(cartera, "SAN", "3.5", "5.5", entrada="4.5")
    ampliar = compra("SAN", units="20", price="4", fee="2", stop="3.8", target="4.6")
    actualizacion = revisar(cartera, ampliar).level_update
    assert (actualizacion.stop_changes, actualizacion.target_changes) == (True, True)
    assert actualizacion.entry == (D("982") / 220).quantize(D("0.000001"))
    with pytest.raises(TradeError, match="falta decidir si la actualizas"):
        registrar(cartera, ampliar)
    assert posicion(cartera, "SAN").units == 200


def test_ampliar_y_actualizar_entrada_y_stop(cartera):
    id_tesis = alta_tesis(cartera, "SAN", "3.5", "5.5", entrada="4.5")
    hecho = registrar(cartera, compra("SAN", units="20", price="4", fee="2", stop="3.8",
                                      target="4.6"),
                      choice=LevelChoice(entry=True, stop=True, target=False))
    assert hecho.updated_thesis == id_tesis
    tesis = ThesisRepository(cartera.connection()).get(id_tesis)
    assert (tesis.entry_price, tesis.stop, tesis.target) == (D("4.463636"), D("3.8"), D("5.5"))
    evento = ThesisEventRepository(cartera.connection()).list_for(id_tesis)[-1]
    assert evento.kind is ThesisEventKind.LEVELS_CHANGED
    assert evento.text == "Ampliación: compra del 25/09/2026: 20 a 4,00 EUR"


def test_ampliar_y_mantener_los_niveles_queda_en_el_historial(cartera):
    id_tesis = alta_tesis(cartera, "SAN", "3.5", "5.5", entrada="4.5")
    registrar(cartera, compra("SAN", units="20", price="4", stop="3.8", target="4.6"),
              choice=LevelChoice())
    tesis = ThesisRepository(cartera.connection()).get(id_tesis)
    assert (tesis.entry_price, tesis.stop, tesis.target) == (D("4.5"), D("3.5"), D("5.5"))
    eventos = ThesisEventRepository(cartera.connection()).list_for(id_tesis)
    assert eventos[-1].kind is ThesisEventKind.REVIEW
    linea = history(eventos)[0]
    assert linea.title == "Ampliación: mantienes los niveles de la tesis"
    assert linea.detail == (
        "En la compra del 25/09/2026: 20 a 4,00 EUR, con stop 3,80 € y objetivo 4,60 €."
    )


def test_ampliar_con_niveles_en_otra_divisa_va_todo_junto():
    tesis = Thesis("AAPL", "EUR", INICIO, entry_price=D("150"), stop=D("140"), target=D("200"),
                   id=1)
    actualizacion = level_update(tesis, D("160"), D("260"), "USD", D("170"), D("0.85"))
    assert actualizacion.currency_changes
    assert actualizacion.entry == D("200.000000")
    todo = chosen_levels(actualizacion, LevelChoice(stop=True))
    assert (todo["divisa"], todo["entrada"], todo["stop"], todo["objetivo"]) == (
        "USD", "200.000000", "160", "260"
    )
    assert chosen_levels(actualizacion, LevelChoice())["divisa"] == "EUR"


def test_buy_entry_en_la_divisa_de_los_niveles():
    assert buy_entry(D("200"), "USD", D("0.86"), "USD", D("0.86")) == D("200")
    assert buy_entry(D("200"), "USD", D("0.86"), "EUR", D("1")) == D("172.000000")
    assert buy_entry(D("170"), "EUR", D("1"), "USD", D("0.85")) == D("200.000000")


def test_close_reason():
    assert close_reason(HOY, None) == "Venta total el 25/09/2026."
    assert close_reason(HOY, (D("5.12"), D("5.2")), " Salta el stop ") == (
        "Venta total el 25/09/2026. Hoy saltó su stop: cotizaba a 5,12 € con el stop en 5,20 €."
        " Motivo: Salta el stop"
    )


# -- movimientos de efectivo --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tipo", "signo"),
    [
        (CashKind.DEPOSIT, 1),
        (CashKind.WITHDRAWAL, -1),
        (CashKind.DIVIDEND, 1),
        (CashKind.INTEREST, 1),
        (CashKind.FEE, -1),
        (CashKind.TAX, -1),
    ],
)
def test_cada_movimiento_con_su_signo(cartera, tipo, signo):
    with cartera.transaction() as conn:
        movimiento = record_cash_movement(conn, tipo, HOY, D("12.34"), AHORA, " nota ")
    assert (movimiento.amount_eur, movimiento.note) == (D("12.34") * signo, "nota")
    assert efectivo(cartera) == EFECTIVO + D("12.34") * signo


def test_el_efectivo_es_la_suma_de_todos_los_movimientos(cartera):
    registrar(cartera, compra())  # −701
    registrar(cartera, venta("SAN", units="100", price="5", fee="1"))  # +499
    with cartera.transaction() as conn:
        record_cash_movement(conn, CashKind.DEPOSIT, HOY, D("1000"), AHORA)
        record_cash_movement(conn, CashKind.DIVIDEND, HOY, D("7.5"), AHORA)
        record_cash_movement(conn, CashKind.TAX, HOY, D("1.43"), AHORA)
    movimientos = CashMovementRepository(cartera.connection()).list_all()
    assert efectivo(cartera) == sum(m.amount_eur for m in movimientos)
    assert efectivo(cartera) == EFECTIVO - 701 + 499 + 1000 + D("7.5") - D("1.43")


def test_ningun_movimiento_deja_el_efectivo_en_negativo(cartera):
    with pytest.raises(TradeError, match="No hay tanto efectivo: hay 6.720,00 €"):
        with cartera.transaction() as conn:
            record_cash_movement(conn, CashKind.WITHDRAWAL, HOY, D("6720.01"), AHORA)
    with cartera.transaction() as conn:
        record_cash_movement(conn, CashKind.WITHDRAWAL, HOY, D("6720"), AHORA)
    assert efectivo(cartera) == 0


def test_el_importe_tiene_que_ser_positivo(cartera):
    with pytest.raises(TradeError, match="El importe tiene que ser mayor que 0"):
        with cartera.transaction() as conn:
            record_cash_movement(conn, CashKind.DEPOSIT, HOY, D("0"), AHORA)
    with pytest.raises(ValueError):
        signed_cash_amount(CashKind.ADJUSTMENT, D("1"))


def test_ajustar_saldo_crea_el_ajuste_que_cuadra(cartera):
    with cartera.transaction() as conn:
        movimiento = record_balance_adjustment(conn, HOY, D("6700.55"), AHORA)
    assert (movimiento.kind, movimiento.amount_eur) == (CashKind.ADJUSTMENT, D("-19.45"))
    assert movimiento.note == "Saldo real 6.700,55 €"
    assert efectivo(cartera) == D("6700.55")
    with pytest.raises(TradeError, match="no hace falta ningún ajuste"):
        with cartera.transaction() as conn:
            record_balance_adjustment(conn, HOY, D("6700.55"), AHORA)
    with pytest.raises(TradeError, match="no puede ser negativo"):
        with cartera.transaction() as conn:
            record_balance_adjustment(conn, HOY, D("-1"), AHORA)


def test_fechas_de_los_movimientos_frente_a_la_ultima_foto(cartera):
    guardar_foto(cartera, foto(AYER, "10000", "100"))
    for dia, motivo in (
        (HOY + timedelta(days=1), "La fecha no puede ser futura"),
        (AYER, "La última foto del patrimonio es del 24/09/2026"),
    ):
        with pytest.raises(TradeError, match=motivo):
            with cartera.transaction() as conn:
                record_cash_movement(conn, CashKind.DEPOSIT, dia, D("10"), AHORA)
    guardar_foto(cartera, foto(HOY, "10000", "100"))
    with pytest.raises(TradeError, match="Hoy ya hay foto del patrimonio"):
        with cartera.transaction() as conn:
            record_cash_movement(conn, CashKind.DIVIDEND, AYER, D("10"), AHORA)
    with cartera.transaction() as conn:
        record_cash_movement(conn, CashKind.DIVIDEND, HOY, D("10"), AHORA)


def test_cash_date_error_en_sus_fronteras():
    assert cash_date_error(HOY, HOY, HOY) is None
    assert cash_date_error(AYER, HOY, AYER - timedelta(days=1)) is None
    assert cash_date_error(AYER, HOY, None) is None
    assert cash_date_error(AYER, HOY, AYER) is not None


def test_un_ingreso_de_hoy_no_mueve_el_valor_por_participacion(cartera):
    # La foto de hoy ya está hecha cuando llega el ingreso.
    conn = cartera.connection()
    with cartera.transaction() as tx:
        record_valuation(tx, load_valuation(conn, AHORA), REGLAS, AHORA)
    antes = NavSnapshotRepository(conn).get(HOY)
    with cartera.transaction() as tx:
        record_cash_movement(tx, CashKind.DEPOSIT, HOY, D("1000"), AHORA)
    hoy = NavSnapshotRepository(conn).get(HOY)
    assert hoy.unit_value == antes.unit_value
    assert hoy.nav_eur == antes.nav_eur + 1000
    assert hoy.fund_units == antes.fund_units + D("1000") / antes.unit_value
    # Mañana, con los mismos precios, el valor sigue igual: el ingreso no es ganancia.
    manana = AHORA + timedelta(hours=12)  # otro día, precios de menos de 24 h
    valoracion = load_valuation(conn, manana, manana)
    fotos = NavSnapshotRepository(conn).list_all()
    movimientos = CashMovementRepository(conn).list_all()
    siguiente = snapshot_for(manana.date(), valoracion, fotos, movimientos)
    assert siguiente.unit_value == hoy.unit_value
    assert day_change(siguiente, fotos, movimientos).amount_eur == 0


def test_apply_flow_con_una_retirada():
    base = foto(HOY, "10000", "100", efectivo_eur="3000")
    despues = apply_flow(base, D("-1000"))
    assert (despues.nav_eur, despues.cash_eur, despues.fund_units) == (9000, 2000, 90)
    assert (despues.unit_value, despues.state, despues.reliable) == (100, base.state, True)


def test_un_dividendo_si_cuenta_como_ganancia(cartera):
    conn = cartera.connection()
    with cartera.transaction() as tx:
        record_valuation(tx, load_valuation(conn, AHORA), REGLAS, AHORA)
    antes = NavSnapshotRepository(conn).get(HOY)
    with cartera.transaction() as tx:
        record_cash_movement(tx, CashKind.DIVIDEND, HOY, D("100"), AHORA)
    # La foto guardada no cambia; la de mañana lo verá como ganancia.
    assert NavSnapshotRepository(conn).get(HOY) == antes
    manana = AHORA + timedelta(hours=12)  # otro día, precios de menos de 24 h
    siguiente = snapshot_for(manana.date(), load_valuation(conn, manana, manana),
                             NavSnapshotRepository(conn).list_all(),
                             CashMovementRepository(conn).list_all())
    assert siguiente.unit_value > antes.unit_value


def test_la_fecha_de_hoy_es_la_del_reloj_inyectado(cartera):
    otro_dia = datetime(2026, 10, 1, 9, 0, tzinfo=MADRID)
    with cartera.transaction() as conn:
        movimiento = record_cash_movement(conn, CashKind.DEPOSIT, date(2026, 10, 1), D("5"),
                                          otro_dia)
    assert movimiento.movement_date == date(2026, 10, 1)
