"""El libro: coste medio ponderado, PnL realizado y efectivo (GUIA §5.1). Datos inventados."""

from datetime import date
from decimal import Decimal as D

import pytest

from sharky.core.ledger import (
    TAX_WARNING,
    LedgerError,
    build_ledger,
    cash_balance,
    trade_cash_amount,
)
from sharky.core.models import CashKind, CashMovement, Trade, TradeKind


def op(dia, kind, units, price, fee="0", ticker="ACME", fx="1", trade_id=None):
    """Una operación inventada; el importe es unidades × precio × cambio."""
    units, price, fx = D(units), D(price), D(fx)
    return Trade(
        trade_date=date(2026, 1, dia),
        ticker=ticker,
        kind=kind,
        units=units,
        price=price,
        currency="EUR" if fx == 1 else "USD",
        fx_to_eur=fx,
        fee_eur=D(fee),
        amount_eur=units * price * fx,
        id=trade_id,
    )


def mov(kind, importe, dia=2):
    return CashMovement(date(2026, 1, dia), kind, D(importe))


# -- coste medio ------------------------------------------------------------------------


def test_la_comision_de_compra_suma_al_coste():
    libro = build_ledger([op(2, TradeKind.BUY, "10", "10", fee="1")])
    posicion = libro.position("ACME")
    assert posicion.units == D("10")
    assert posicion.cost_eur == D("101")
    assert posicion.avg_cost_eur == D("10.1")


def test_el_coste_medio_pondera_las_compras():
    libro = build_ledger(
        [
            op(2, TradeKind.OPENING, "10", "8"),  # 80 €
            op(3, TradeKind.BUY, "30", "12", fee="2"),  # 362 €
        ]
    )
    posicion = libro.position("ACME")
    assert posicion.units == D("40")
    assert posicion.cost_eur == D("442")
    assert posicion.avg_cost_eur == D("11.05")


def test_el_coste_se_lleva_a_eur_con_el_cambio_de_la_operacion():
    libro = build_ledger([op(2, TradeKind.BUY, "5", "100", fee="1", fx="0.9")])
    assert libro.position("ACME").cost_eur == D("451.0")


def test_unidades_con_decimales():
    libro = build_ledger([op(2, TradeKind.BUY, "0.123456", "1000")])
    assert libro.position("ACME").units == D("0.123456")
    assert libro.position("ACME").cost_eur == D("123.456")


# -- PnL realizado ----------------------------------------------------------------------


def test_pnl_realizado_de_una_venta_parcial():
    libro = build_ledger(
        [
            op(2, TradeKind.BUY, "10", "10", fee="1"),  # coste medio 10,10
            op(5, TradeKind.SELL, "4", "12", fee="1"),  # neto 47; coste 40,40
        ]
    )
    (venta,) = libro.sales
    assert venta.net_proceeds_eur == D("47")
    assert venta.cost_basis_eur == D("40.4")
    assert venta.pnl_eur == D("6.6")
    assert not venta.closes_position
    # Lo que queda conserva el coste medio.
    posicion = libro.position("ACME")
    assert posicion.units == D("6")
    assert posicion.cost_eur == D("60.6")
    assert posicion.avg_cost_eur == D("10.1")


def test_el_ejemplo_completo_con_compras_y_ventas():
    libro = build_ledger(
        [
            op(2, TradeKind.BUY, "10", "10", fee="1"),  # 10 u · coste 101
            op(5, TradeKind.SELL, "4", "12", fee="1"),  # PnL +6,6 · quedan 6 u · coste 60,6
            op(8, TradeKind.BUY, "4", "15", fee="1"),  # 10 u · coste 121,6 · medio 12,16
            op(12, TradeKind.SELL, "10", "14", fee="2"),  # neto 138 · PnL +16,4
        ]
    )
    assert libro.position("ACME") is None  # vendida entera
    assert [v.pnl_eur for v in libro.sales] == [D("6.6"), D("16.4")]
    assert libro.sales[-1].closes_position
    assert libro.realized_pnl_eur() == D("23.0")
    assert libro.realized_pnl_eur("ACME") == D("23.0")
    assert libro.realized_pnl_eur("OTRA") == D("0")


def test_una_venta_con_perdidas():
    libro = build_ledger(
        [op(2, TradeKind.BUY, "20", "5"), op(3, TradeKind.SELL, "20", "4", fee="1")]
    )
    assert libro.sales[0].pnl_eur == D("-21")


def test_vender_todo_no_deja_restos_de_redondeo():
    """Con un coste medio periódico (10/3), la venta total se lleva justo lo que queda."""
    libro = build_ledger(
        [
            op(2, TradeKind.BUY, "3", "3.333333333333333333333333333333", fee="0"),
            op(3, TradeKind.SELL, "1", "4"),
            op(4, TradeKind.SELL, "2", "4"),
        ]
    )
    base_total = sum(v.cost_basis_eur for v in libro.sales)
    compra = D("3") * D("3.333333333333333333333333333333")
    assert base_total == compra
    assert libro.position("ACME") is None


def test_tras_cerrar_una_posicion_la_siguiente_empieza_de_cero():
    libro = build_ledger(
        [
            op(2, TradeKind.BUY, "10", "10"),
            op(3, TradeKind.SELL, "10", "11"),
            op(4, TradeKind.BUY, "5", "20"),
        ]
    )
    assert libro.position("ACME").avg_cost_eur == D("20")


def test_no_se_puede_vender_mas_de_lo_que_hay():
    with pytest.raises(LedgerError, match=r"vender 11 unidades de ACME: solo hay 10"):
        build_ledger([op(2, TradeKind.BUY, "10", "10"), op(3, TradeKind.SELL, "11", "10")])


def test_no_se_puede_vender_lo_que_no_se_tiene():
    with pytest.raises(LedgerError, match="solo hay 0"):
        build_ledger([op(2, TradeKind.SELL, "1", "10")])


def test_las_operaciones_se_ordenan_por_fecha():
    desordenadas = [op(5, TradeKind.SELL, "5", "12"), op(2, TradeKind.BUY, "10", "10")]
    libro = build_ledger(desordenadas)
    assert libro.position("ACME").units == D("5")


def test_varios_tickers_por_separado():
    libro = build_ledger(
        [
            op(2, TradeKind.BUY, "10", "10", ticker="ACME"),
            op(2, TradeKind.BUY, "2", "50", ticker="BETA"),
            op(3, TradeKind.SELL, "1", "60", ticker="BETA"),
        ]
    )
    assert sorted(libro.positions) == ["ACME", "BETA"]
    assert libro.position("BETA").units == D("1")
    assert libro.realized_pnl_eur("BETA") == D("10")
    assert libro.realized_pnl_eur("ACME") == D("0")


def test_el_aviso_de_la_renta_habla_de_fifo():
    assert "FIFO" in TAX_WARNING
    assert "renta" in TAX_WARNING


# -- efectivo ---------------------------------------------------------------------------


def test_el_efectivo_es_la_suma_de_los_movimientos():
    movimientos = [
        mov(CashKind.INITIAL, "1000.00"),
        mov(CashKind.DEPOSIT, "250.10"),
        mov(CashKind.WITHDRAWAL, "-100.20"),
        mov(CashKind.DIVIDEND, "3.33"),
        mov(CashKind.FEE, "-1.00"),
        mov(CashKind.TAX, "-0.63"),
        mov(CashKind.INTEREST, "0.01"),
        mov(CashKind.ADJUSTMENT, "-0.01"),
    ]
    assert cash_balance(movimientos) == D("1151.60")


def test_sin_movimientos_no_hay_efectivo():
    assert cash_balance([]) == D("0")


def test_la_suma_es_exacta_al_centimo():
    """Con float, diez ingresos de 0,10 no suman 1,00. Con Decimal, sí."""
    assert cash_balance([mov(CashKind.DEPOSIT, "0.10")] * 10) == D("1.00")


def test_lo_que_mueve_cada_operacion_en_el_efectivo():
    assert trade_cash_amount(op(2, TradeKind.BUY, "10", "10", fee="1")) == D("-101")
    assert trade_cash_amount(op(2, TradeKind.SELL, "10", "12", fee="1")) == D("119")
    assert trade_cash_amount(op(2, TradeKind.OPENING, "10", "10")) is None
