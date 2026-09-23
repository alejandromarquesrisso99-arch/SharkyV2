"""Repositorios: cada tabla guarda y devuelve lo mismo, y las reglas del esquema se cumplen."""

import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from sharky.core.ledger import build_ledger, trade_cash_amount
from sharky.core.models import (
    AlertOrigin,
    AlertStatus,
    Asset,
    AssetClass,
    Author,
    Breach,
    CashKind,
    CashMovement,
    FxRate,
    LevelAlert,
    LevelAlertKind,
    MandateState,
    NavSnapshot,
    Price,
    PriceSource,
    RadarAlert,
    Report,
    ReportKind,
    Run,
    RunStatus,
    Thesis,
    ThesisEvent,
    ThesisEventKind,
    ThesisStatus,
    Trade,
    TradeKind,
    WatchlistItem,
    WatchlistSource,
)
from sharky.services.repositories import (
    AssetRepository,
    BreachRepository,
    CashMovementRepository,
    FxRateRepository,
    LevelAlertRepository,
    NavSnapshotRepository,
    NotInTransactionError,
    PriceRepository,
    RadarAlertRepository,
    ReportRepository,
    RunRepository,
    ThesisEventRepository,
    ThesisRepository,
    TradeRepository,
    WatchlistRepository,
)

MADRID = timezone(timedelta(hours=2))
HOY = date(2026, 9, 23)
AHORA = datetime(2026, 9, 23, 20, 30, 15, tzinfo=MADRID)
ACME = Asset(
    "ACME", "Acme Inventada S.A.", "USD", AssetClass.STOCK, "US0000000001", "ACME", "Tecnologia"
)
BETA = Asset("BETA", "Beta Fondo Ficticio", "GBp", AssetClass.ETF, sector="Renta_Variable")


def compra(units="10", price="10", fee="1", kind=TradeKind.BUY, dia=HOY, ticker="ACME"):
    units, price = D(units), D(price)
    return Trade(dia, ticker, kind, units, price, "EUR", D("1"), D(fee), units * price)


@pytest.fixture
def con_activos(db):
    with db.transaction() as conn:
        AssetRepository(conn).add(ACME)
        AssetRepository(conn).add(BETA)
    return db


# -- ida y vuelta -----------------------------------------------------------------------


def test_activos(con_activos):
    repo = AssetRepository(con_activos.connection())
    assert repo.get("ACME") == ACME
    assert repo.get("BETA") == BETA
    assert repo.get("NADA") is None
    assert [a.ticker for a in repo.list_all()] == ["ACME", "BETA"]


def test_operaciones_con_todos_sus_campos(con_activos):
    original = Trade(
        trade_date=HOY,
        ticker="ACME",
        kind=TradeKind.BUY,
        units=D("12.345678"),
        price=D("101.25"),
        currency="USD",
        fx_to_eur=D("0.912345"),
        fee_eur=D("1.00"),
        amount_eur=D("1140.44"),
        stop=D("90"),
        target=D("130.5"),
        levels_currency="USD",
        forced=True,
        reason="Fuera del mandato a sabiendas",
    )
    with con_activos.transaction() as conn:
        nuevo_id = TradeRepository(conn).add(original)
    leido = TradeRepository(con_activos.connection()).get(nuevo_id)
    assert leido == Trade(**{**original.__dict__, "id": nuevo_id})
    assert isinstance(leido.units, D)
    assert leido.forced is True


def test_los_decimales_se_guardan_exactos(con_activos):
    with con_activos.transaction() as conn:
        nuevo_id = TradeRepository(conn).add(compra(units="0.000001", price="98765.4321"))
    crudo = con_activos.connection().execute(
        "SELECT units, price FROM trades WHERE id = ?", (nuevo_id,)
    ).fetchone()
    assert tuple(crudo) == ("0.000001", "98765.4321")  # texto, sin pasar por float


def test_las_operaciones_salen_en_orden_y_el_libro_cuadra(con_activos):
    with con_activos.transaction() as conn:
        repo = TradeRepository(conn)
        repo.add(compra(units="4", price="12", kind=TradeKind.SELL, dia=date(2026, 9, 25)))
        repo.add(compra(units="10", price="10", dia=date(2026, 9, 22)))
    operaciones = TradeRepository(con_activos.connection()).list_all()
    assert [o.trade_date.day for o in operaciones] == [22, 25]
    libro = build_ledger(operaciones)
    assert libro.position("ACME").units == D("6")
    assert libro.sales[0].pnl_eur == D("6.6")


def test_una_operacion_de_un_activo_desconocido_no_entra(db):
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        TradeRepository(conn).add(compra(ticker="NOEXISTE"))


def test_el_efectivo_es_la_suma_de_los_movimientos_guardados(con_activos):
    with con_activos.transaction() as conn:
        movimientos = CashMovementRepository(conn)
        movimientos.add(CashMovement(HOY, CashKind.INITIAL, D("5000.00"), note="Saldo inicial"))
        movimientos.add(CashMovement(HOY, CashKind.DEPOSIT, D("1000.10")))
        movimientos.add(CashMovement(HOY, CashKind.DIVIDEND, D("12.34")))
        movimientos.add(CashMovement(HOY, CashKind.WITHDRAWAL, D("-500.05")))
        # Una compra y su movimiento, en la misma transacción.
        operacion = compra(units="10", price="100", fee="1")
        id_op = TradeRepository(conn).add(operacion)
        movimientos.add(
            CashMovement(HOY, CashKind.TRADE, trade_cash_amount(operacion), trade_id=id_op)
        )
    repo = CashMovementRepository(con_activos.connection())
    assert repo.balance() == D("4511.39")
    assert repo.balance() == sum(m.amount_eur for m in repo.list_all())


def test_el_movimiento_de_una_operacion_tiene_que_apuntar_a_ella(con_activos):
    with pytest.raises(sqlite3.IntegrityError), con_activos.transaction() as conn:
        CashMovementRepository(conn).add(CashMovement(HOY, CashKind.TRADE, D("-10")))
    with pytest.raises(sqlite3.IntegrityError), con_activos.transaction() as conn:
        id_op = TradeRepository(conn).add(compra())
        CashMovementRepository(conn).add(
            CashMovement(HOY, CashKind.DEPOSIT, D("10"), trade_id=id_op)
        )


def test_precios_y_cambios(db):
    precio = Price("ACME", HOY, D("101.25"), "USD", PriceSource.MARKET, AHORA)
    cambio = FxRate("USD", HOY, D("0.912345"), PriceSource.MARKET)
    with db.transaction() as conn:
        PriceRepository(conn).save(Price("ACME", date(2026, 9, 22), D("99"), "USD",
                                         PriceSource.MARKET, AHORA))
        PriceRepository(conn).save(precio)
        PriceRepository(conn).save(precio)  # repetir el mismo día sustituye
        FxRateRepository(conn).save(cambio)
    conn = db.connection()
    assert PriceRepository(conn).latest("ACME") == precio
    assert PriceRepository(conn).latest("ACME").fetched_at.utcoffset() == timedelta(hours=2)
    assert len(PriceRepository(conn).list_for("ACME")) == 2
    assert FxRateRepository(conn).latest("USD") == cambio


def _foto(dia=HOY, nav="10000", fiable=True, estado=MandateState.OPTIMAL):
    return NavSnapshot(dia, D(nav), D("2000"), D("100"), D(nav) / 100, D("100"), D("0.03"),
                       estado, D("0.95"), fiable)


def test_una_foto_del_nav_por_dia_y_la_fiable_manda(db):
    with db.transaction() as conn:
        repo = NavSnapshotRepository(conn)
        assert repo.save(_foto(nav="10000", fiable=True))
        assert not repo.save(_foto(nav="9000", fiable=False))  # no sustituye a una fiable
        assert repo.save(_foto(nav="10100", fiable=True))  # la última fiable, sí
        assert repo.save(_foto(dia=HOY + timedelta(days=1), nav="9900", fiable=False))
        assert repo.save(_foto(dia=HOY + timedelta(days=1), nav="9950", fiable=False))
    repo = NavSnapshotRepository(db.connection())
    assert repo.get(HOY).nav_eur == D("10100")
    assert len(repo.list_all()) == 2
    assert repo.latest().nav_eur == D("9950")
    assert repo.latest(reliable_only=True).snapshot_date == HOY


def test_tesis_y_su_historial(con_activos):
    tesis = Thesis("ACME", "USD", HOY, entry_price=D("100"), stop=D("90"), target=D("130"),
                   conviction=7, why="Foso inventado", catalysts="Resultados",
                   risks="Competencia", invalidation="Pierde cuota")
    with con_activos.transaction() as conn:
        id_tesis = ThesisRepository(conn).add(tesis)
        eventos = ThesisEventRepository(conn)
        creacion = eventos.add(ThesisEvent(id_tesis, AHORA, ThesisEventKind.CREATED, Author.USER))
        propuesta = eventos.add(ThesisEvent(id_tesis, AHORA, ThesisEventKind.PROPOSAL,
                                            Author.CLAUDE, "Subir el stop", '{"stop": "90"}',
                                            '{"stop": "100"}'))
        eventos.add(ThesisEvent(id_tesis, AHORA, ThesisEventKind.LEVELS_CHANGED, Author.USER,
                                "Propuesta aplicada", '{"stop": "90"}', '{"stop": "100"}',
                                ref_event_id=propuesta))
    conn = con_activos.connection()
    assert ThesisRepository(conn).get(id_tesis) == Thesis(**{**tesis.__dict__, "id": id_tesis})
    assert ThesisRepository(conn).active_for("ACME").id == id_tesis
    historial = ThesisEventRepository(conn).list_for(id_tesis)
    assert [e.kind for e in historial] == [
        ThesisEventKind.CREATED, ThesisEventKind.PROPOSAL, ThesisEventKind.LEVELS_CHANGED,
    ]
    assert historial[0].id == creacion
    assert historial[2].ref_event_id == propuesta


def test_el_historial_de_una_tesis_solo_admite_anadir(con_activos):
    with con_activos.transaction() as conn:
        id_tesis = ThesisRepository(conn).add(Thesis("ACME", "EUR", HOY))
        id_evento = ThesisEventRepository(conn).add(
            ThesisEvent(id_tesis, AHORA, ThesisEventKind.CREATED, Author.USER, "Creada")
        )
    for orden in (
        "UPDATE thesis_events SET text = 'reescrito' WHERE id = ?",
        "DELETE FROM thesis_events WHERE id = ?",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="solo admite añadir"):
            with con_activos.transaction() as conn:
                conn.execute(orden, (id_evento,))
    evento = ThesisEventRepository(con_activos.connection()).list_for(id_tesis)[0]
    assert evento.text == "Creada"


def test_una_sola_tesis_activa_por_ticker(con_activos):
    with con_activos.transaction() as conn:
        ThesisRepository(conn).add(
            Thesis("ACME", "EUR", date(2026, 1, 5), status=ThesisStatus.CLOSED,
                   closed_on=date(2026, 3, 1), realized_pnl_eur=D("12.5"), close_reason="Stop")
        )
        ThesisRepository(conn).add(Thesis("ACME", "EUR", HOY))  # cerrada + activa: bien
    with pytest.raises(sqlite3.IntegrityError), con_activos.transaction() as conn:
        ThesisRepository(conn).add(Thesis("ACME", "EUR", HOY))  # segunda activa: no


def test_una_tesis_cerrada_lleva_fecha_de_cierre(con_activos):
    with pytest.raises(sqlite3.IntegrityError), con_activos.transaction() as conn:
        ThesisRepository(conn).add(Thesis("ACME", "EUR", HOY, status=ThesisStatus.CLOSED))


def test_la_conviccion_va_de_1_a_10(con_activos):
    with pytest.raises(sqlite3.IntegrityError), con_activos.transaction() as conn:
        ThesisRepository(conn).add(Thesis("ACME", "EUR", HOY, conviction=11))


def test_un_aviso_de_nivel_por_ticker_tipo_y_dia(con_activos):
    aviso = LevelAlert(HOY, "ACME", LevelAlertKind.STOP, D("88.5"), D("90"),
                       proposed_stop=None, criterion=None)
    with con_activos.transaction() as conn:
        repo = LevelAlertRepository(conn)
        assert repo.add(aviso) is not None
        assert repo.add(aviso) is None  # el mismo día, ya estaba
        assert repo.add(LevelAlert(HOY, "ACME", LevelAlertKind.TARGET, D("131"), D("130"),
                                   D("100"), "break-even")) is not None
    avisos = LevelAlertRepository(con_activos.connection()).list_for_date(HOY)
    assert [a.kind for a in avisos] == [LevelAlertKind.STOP, LevelAlertKind.TARGET]
    assert avisos[1].proposed_stop == D("100")
    assert avisos[0].notified is False


def test_incumplimientos(db):
    with db.transaction() as conn:
        repo = BreachRepository(conn)
        repo.add(Breach("ACTIVO", "ACME", AHORA, AHORA))
        repo.add(Breach("EFECTIVO", "", AHORA, AHORA, is_open=False))
        repo.add(Breach("EFECTIVO", "", AHORA, AHORA))  # otra abierta: la anterior se cerró
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        BreachRepository(conn).add(Breach("ACTIVO", "ACME", AHORA, AHORA))  # ya está abierta
    abiertos = BreachRepository(db.connection()).list_open()
    assert [(b.rule, b.subject) for b in abiertos] == [("ACTIVO", "ACME"), ("EFECTIVO", "")]


def test_lista_de_vigilancia(db):
    item = WatchlistItem("GAMMA", "Gamma Ficticia", WatchlistSource.EXPLORER, HOY, "GMA.DE",
                         "EUR", "Industria")
    with db.transaction() as conn:
        WatchlistRepository(conn).add(item)
        WatchlistRepository(conn).add(WatchlistItem("DELTA", "Delta", WatchlistSource.USER, HOY))
    assert WatchlistRepository(db.connection()).list_all()[1] == item
    with db.transaction() as conn:
        assert WatchlistRepository(conn).remove("DELTA")
        assert not WatchlistRepository(conn).remove("DELTA")
    assert [i.ticker for i in WatchlistRepository(db.connection()).list_all()] == ["GAMMA"]


def test_alertas_del_radar_con_su_informe(db):
    exploracion = Report(ReportKind.EXPLORATION, "2026-09-23", AHORA, "# Exploración", True)
    with db.transaction() as conn:
        id_informe = ReportRepository(conn).save(exploracion)
        repo = RadarAlertRepository(conn)
        repo.add(RadarAlert(HOY, "GAMMA", AlertOrigin.EXPLORER, AlertStatus.ACTIVE, D("50"),
                            "EUR", D("45"), D("62"), D("2.4"), D("0.19"), D("0.10"),
                            "Foso inventado", report_id=id_informe))
        repo.add(RadarAlert(HOY, "DELTA", AlertOrigin.WATCHLIST, AlertStatus.DISCARDED,
                            reason="Caída desde máximos del 4 %: fuera del 10–40 %"))
    conn = db.connection()
    activas = RadarAlertRepository(conn).list_by_status(AlertStatus.ACTIVE)
    assert activas[0].ratio == D("2.4")
    assert activas[0].report_id == id_informe
    descartadas = RadarAlertRepository(conn).list_by_status(AlertStatus.DISCARDED)
    assert descartadas[0].price is None
    assert "fuera" in descartadas[0].reason
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        RadarAlertRepository(conn).add(
            RadarAlert(HOY, "GAMMA", AlertOrigin.WATCHLIST, AlertStatus.ACTIVE)
        )  # ya hay una activa de GAMMA


def test_informes_uno_por_periodo(db):
    diario = Report(ReportKind.DAILY, "2026-09-23", AHORA, "# Diario", True,
                    conclusion="- Vigilar ACME", watch_positions=("ACME", "BETA"),
                    model="claude-sonnet-5", effort="low", input_tokens=1200,
                    output_tokens=800, cost_usd=D("0.0104"))
    with db.transaction() as conn:
        repo = ReportRepository(conn)
        repo.save(Report(ReportKind.DAILY, "2026-09-23", AHORA, "# Sin IA", False,
                         error="Sin análisis de IA: no hay clave"))
        id_nuevo = repo.save(diario)  # «Ejecutar ahora» sustituye al del mismo día
        repo.save(Report(ReportKind.EXPLORATION, "2026-09-23", AHORA, "# Uno", True))
        repo.save(Report(ReportKind.EXPLORATION, "2026-09-23", AHORA, "# Dos", True))
    conn = db.connection()
    assert ReportRepository(conn).get(id_nuevo) == Report(**{**diario.__dict__, "id": id_nuevo})
    assert ReportRepository(conn).latest(ReportKind.DAILY).watch_positions == ("ACME", "BETA")
    tipos = sorted(r.kind for r in ReportRepository(conn).list_all())
    assert tipos == [ReportKind.DAILY, ReportKind.EXPLORATION, ReportKind.EXPLORATION]


def test_registro_de_ejecuciones(db):
    with db.transaction() as conn:
        RunRepository(conn).add(Run(AHORA, "arranque", "Valorar", RunStatus.OK, "12 posiciones",
                                    AHORA + timedelta(seconds=4)))
        RunRepository(conn).add(Run(AHORA + timedelta(minutes=1), "arranque", "Diario",
                                    RunStatus.SKIPPED, "Ya había uno de hoy"))
    recientes = RunRepository(db.connection()).list_recent()
    assert [r.step for r in recientes] == ["Diario", "Valorar"]
    assert recientes[1].finished_at == AHORA + timedelta(seconds=4)


def test_una_hora_en_utc_vuelve_igual(db):
    en_utc = datetime(2026, 9, 23, 18, 30, tzinfo=UTC)
    with db.transaction() as conn:
        RunRepository(conn).add(Run(en_utc, "manual", "Copia", RunStatus.OK))
    assert RunRepository(db.connection()).list_recent()[0].started_at == en_utc


# -- una transacción por operación ------------------------------------------------------


def test_escribir_fuera_de_una_transaccion_se_rechaza(db):
    with pytest.raises(NotInTransactionError):
        AssetRepository(db.connection()).add(ACME)
    assert AssetRepository(db.connection()).list_all() == []


def test_varias_escrituras_en_una_transaccion_entran_juntas_o_ninguna(con_activos):
    with pytest.raises(sqlite3.IntegrityError), con_activos.transaction() as conn:
        id_op = TradeRepository(conn).add(compra())
        CashMovementRepository(conn).add(
            CashMovement(HOY, CashKind.TRADE, D("-101"), trade_id=id_op)
        )
        CashMovementRepository(conn).add(  # el mismo movimiento dos veces: falla
            CashMovement(HOY, CashKind.TRADE, D("-101"), trade_id=id_op)
        )
    conn = con_activos.connection()
    assert TradeRepository(conn).list_all() == []
    assert CashMovementRepository(conn).list_all() == []
