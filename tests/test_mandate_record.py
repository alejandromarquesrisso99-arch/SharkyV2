"""La foto diaria del NAV y los incumplimientos guardados (GUIA §5.1, §5.4 y §7, H6).

`record_valuation` guarda la foto del día y sincroniza `breaches` con la auditoría; aquí se
comprueba contra una base de datos de verdad (en la carpeta de datos del test).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from sharky.core.csv_import import CsvPosition, build_opening
from sharky.core.ledger import Position
from sharky.core.mandate import days_open
from sharky.core.models import (
    Asset,
    CashKind,
    CashMovement,
    MandateState,
    NavSnapshot,
    Price,
    PriceSource,
)
from sharky.core.valuation import value_portfolio
from sharky.services.repositories import (
    BreachRepository,
    CashMovementRepository,
    NavSnapshotRepository,
    NotInTransactionError,
    create_portfolio,
    record_valuation,
)
from sharky.services.settings import MandateSettings

MADRID = timezone(timedelta(hours=2))
DIA_1 = date(2026, 9, 16)
REGLAS = MandateSettings().rules()


def a_las(dia, hora=18):
    return datetime(dia.year, dia.month, dia.day, hora, 0, tzinfo=MADRID)


def valoracion(efectivo, posiciones, momento, sin_precio=()):
    """{ticker: valor en EUR}; todos con precio de mercado salvo los de `sin_precio`."""
    activos = {t: Asset(t, t, "EUR", yahoo_symbol=f"{t}.MC", sector=f"S{t}") for t in posiciones}
    precios = {
        t: Price(t, momento.date(), D(v), "EUR", PriceSource.MARKET, momento)
        for t, v in posiciones.items()
        if t not in sin_precio
    }
    abiertas = [Position(t, D(1), D(v)) for t, v in posiciones.items()]
    return value_portfolio(abiertas, activos, D(efectivo), precios, {}, momento, momento)


#: 10.000 €: 2.000 € de efectivo y ocho posiciones de 1.000 €. Cumple el mandato.
CUMPLE = {t: "1000" for t in "ABCDEFGH"}


def registrar(db, valoracion_, momento):
    with db.transaction() as conn:
        return record_valuation(conn, valoracion_, REGLAS, momento)


def fotos(db):
    return NavSnapshotRepository(db.connection()).list_all()


def sembrar(db, *snapshots):
    with db.transaction() as conn:
        for s in snapshots:
            NavSnapshotRepository(conn).save(s)


def foto_fiable(dia, nav, unidades, valor, maximo, caida="0", estado=MandateState.OPTIMAL):
    return NavSnapshot(dia, D(nav), D(nav), D(unidades), D(valor), D(maximo), D(caida), estado,
                       D("1"), True)


# -- la foto del día ------------------------------------------------------------------------


def test_la_primera_valoracion_fiable_rehace_la_foto_del_asistente(db):
    posiciones = [CsvPosition(n, t, t, D("1"), D("900"), "EUR", yahoo_symbol=f"{t}.MC")
                  for n, t in enumerate("ABCDEFGH", 2)]
    apertura = build_opening(posiciones, D("2000"), DIA_1)
    with db.transaction() as conn:
        create_portfolio(conn, apertura)
    assert not fotos(db)[0].reliable  # la del asistente va a coste

    registro = registrar(db, valoracion("2000", CUMPLE, a_las(DIA_1)), a_las(DIA_1))
    assert registro.saved
    [hoy] = fotos(db)
    assert hoy == registro.snapshot
    assert (hoy.snapshot_date, hoy.reliable, hoy.unit_value) == (DIA_1, True, D("100"))
    assert hoy.fund_units == D("100")  # 10.000 € a mercado / 100

    manana = DIA_1 + timedelta(days=1)
    subida = {**CUMPLE, "A": "1500"}  # +500 €: +5 %
    registrar(db, valoracion("2000", subida, a_las(manana)), a_las(manana))
    assert [f.unit_value for f in fotos(db)] == [D("100"), D("105")]


def test_una_foto_por_dia_la_ultima_fiable_manda(db):
    sembrar(db, foto_fiable(DIA_1 - timedelta(days=1), "10000", "100", "100", "100"))
    registrar(db, valoracion("2000", CUMPLE, a_las(DIA_1, 10)), a_las(DIA_1, 10))
    tarde = registrar(db, valoracion("2000", {**CUMPLE, "A": "900"}, a_las(DIA_1, 18)),
                      a_las(DIA_1, 18))
    assert tarde.saved
    del_dia = [f for f in fotos(db) if f.snapshot_date == DIA_1]
    assert [f.nav_eur for f in del_dia] == [D("9900")]

    # Una valoración no fiable no sustituye a la fiable del mismo día.
    noche = registrar(
        db, valoracion("2000", CUMPLE, a_las(DIA_1, 21), sin_precio=("A", "B")), a_las(DIA_1, 21)
    )
    assert not noche.saved
    assert NavSnapshotRepository(db.connection()).get(DIA_1).nav_eur == D("9900")


def test_un_ingreso_guardado_no_mueve_el_valor_por_participacion(db):
    sembrar(db, foto_fiable(DIA_1 - timedelta(days=1), "10000", "100", "100", "100"))
    with db.transaction() as conn:
        CashMovementRepository(conn).add(CashMovement(DIA_1, CashKind.DEPOSIT, D("5000")))
    registro = registrar(db, valoracion("7000", CUMPLE, a_las(DIA_1)), a_las(DIA_1))
    assert registro.snapshot.unit_value == D("100")
    assert registro.snapshot.fund_units == D("150")


def test_con_cobertura_baja_se_mantienen_estado_y_maximo(db):
    ayer = foto_fiable(DIA_1 - timedelta(days=1), "10000", "100", "96", "100", "0.04",
                       MandateState.ALERT)
    sembrar(db, ayer)
    registro = registrar(
        db, valoracion("2000", CUMPLE, a_las(DIA_1), sin_precio=("A", "B", "C")), a_las(DIA_1)
    )
    f = registro.snapshot
    assert registro.saved and not f.reliable
    assert (f.state, f.high_water_mark, f.drawdown) == (MandateState.ALERT, D("100"), D("0.04"))
    # La auditoría usa el estado que se mantiene: en Alerta el tope por activo es el 5 %.
    assert ("ACTIVO", "D") in {h.key for h in registro.findings}


def test_sin_transaccion_no_se_guarda_nada(db):
    with pytest.raises(NotInTransactionError):
        record_valuation(db.connection(), valoracion("2000", CUMPLE, a_las(DIA_1)), REGLAS,
                         a_las(DIA_1))


# -- incumplimientos con su antigüedad ------------------------------------------------------


def test_un_incumplimiento_se_abre_envejece_y_se_cierra(db):
    poco_efectivo = {**CUMPLE, "I": "1000"}  # efectivo al 10 %: por debajo del 15 %
    primero = registrar(db, valoracion("1000", poco_efectivo, a_las(DIA_1, 9)), a_las(DIA_1, 9))
    [abierto] = primero.breaches
    assert (abierto.rule, abierto.subject) == ("EFECTIVO", "MINIMO")
    assert abierto.opened_at == a_las(DIA_1, 9)

    dia_9 = DIA_1 + timedelta(days=9)
    despues = registrar(db, valoracion("1000", poco_efectivo, a_las(dia_9)), a_las(dia_9))
    [sigue] = despues.breaches
    assert sigue.id == abierto.id
    assert sigue.opened_at == a_las(DIA_1, 9)  # conserva la fecha en que se abrió
    assert sigue.last_seen_at == a_las(dia_9)
    assert days_open(sigue.opened_at, dia_9) == 9

    dia_10 = dia_9 + timedelta(days=1)
    resuelto = registrar(db, valoracion("2000", CUMPLE, a_las(dia_10)), a_las(dia_10))
    assert resuelto.breaches == ()
    [cerrado] = BreachRepository(db.connection()).list_all()
    assert not cerrado.is_open

    # Si vuelve, es un incumplimiento nuevo que empieza a contar de cero.
    dia_11 = dia_10 + timedelta(days=1)
    vuelve = registrar(db, valoracion("1000", poco_efectivo, a_las(dia_11)), a_las(dia_11))
    [nuevo] = vuelve.breaches
    assert nuevo.id != abierto.id and nuevo.opened_at == a_las(dia_11)


def test_varios_incumplimientos_a_la_vez(db):
    concentrada = {**CUMPLE, "A": "2000"}  # NAV 10.000 €: A al 20 % y efectivo al 10 %
    registro = registrar(db, valoracion("1000", concentrada, a_las(DIA_1)), a_las(DIA_1))
    claves = {(b.rule, b.subject) for b in registro.breaches}
    assert claves == {("EFECTIVO", "MINIMO"), ("ACTIVO", "A")}
    assert {h.key for h in registro.findings} == claves


# -- las reglas salen de los ajustes --------------------------------------------------------


def test_las_reglas_salen_de_los_ajustes_como_fracciones_exactas():
    ajustes = MandateSettings(
        max_asset_weight_optimal_pct=12.5, min_reward_risk=2.5, breach_escalation_days=3
    )
    reglas = ajustes.rules()
    assert reglas.max_asset_weight_optimal == D("0.125")
    assert reglas.max_risk_per_trade == D("0.015")
    assert reglas.min_reward_risk == D("2.5")
    assert reglas.escalation_days == 3
