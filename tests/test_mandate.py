"""El mandato (GUIA §5.4 y §5.5): estados en sus fronteras exactas, participaciones, cobertura,
auditoría con la corrección en euros y validación de compras y ventas.

core/mandate.py es pura: aquí se le dan la valoración, las fotos y los movimientos a mano.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from sharky.core.formatting import format_pct
from sharky.core.ledger import Position
from sharky.core.mandate import (
    BUY_CHECKS,
    BreachRule,
    BuyOrder,
    Check,
    age_text,
    audit,
    buying_allowed,
    day_change,
    days_open,
    drawdown_of,
    external_flows,
    held_since,
    is_escalated,
    join_names,
    snapshot_for,
    state_for,
    state_limits_text,
    unit_value_series,
    validate_buy,
    validate_sell,
)
from sharky.core.models import (
    Asset,
    CashKind,
    CashMovement,
    MandateState,
    NavSnapshot,
    Price,
    PriceSource,
)
from sharky.core.valuation import Valuation, value_portfolio
from sharky.services.settings import MandateSettings

MADRID = timezone(timedelta(hours=2))
AHORA = datetime(2026, 9, 25, 18, 0, tzinfo=MADRID)
HOY = date(2026, 9, 25)
AYER = date(2026, 9, 24)
ANTEAYER = date(2026, 9, 23)

OPTIMO = MandateState.OPTIMAL
ALERTA = MandateState.ALERT
CUIDADOS = MandateState.INTENSIVE_CARE
BLOQUEO = MandateState.LOCKDOWN

#: Los valores por defecto de los ajustes, que son la única fuente de las reglas.
REGLAS = MandateSettings().rules()


def valoracion(nav, efectivo=None, cobertura="1"):
    """Una valoración sin posiciones a la vista: para la foto solo cuentan NAV y cobertura."""
    return Valuation(
        valued_at=AHORA,
        cash_eur=D(efectivo if efectivo is not None else nav),
        nav_eur=D(nav),
        coverage=D(cobertura),
        positions=(),
        sectors=(),
    )


def foto(dia, nav, unidades, valor, *, maximo=None, caida="0", estado=OPTIMO, fiable=True,
         cobertura="1"):
    return NavSnapshot(
        snapshot_date=dia,
        nav_eur=D(nav),
        cash_eur=D(nav),
        fund_units=D(unidades),
        unit_value=D(valor),
        high_water_mark=D(maximo if maximo is not None else valor),
        drawdown=D(caida),
        state=estado,
        coverage=D(cobertura),
        reliable=fiable,
    )


def movimiento(tipo, importe, dia=HOY):
    return CashMovement(dia, tipo, D(importe))


#: Ayer, valor 100 con 100 participaciones: un NAV de 10.000 €.
BASE = foto(AYER, "10000", "100", "100")
#: Ayer, en Alerta: valor 96 frente a un máximo de 100.
BASE_ALERTA = foto(AYER, "9600", "100", "96", maximo="100", caida="0.04", estado=ALERTA)


# -- estados en sus fronteras -------------------------------------------------------------


@pytest.mark.parametrize(
    ("caida", "estado"),
    [
        ("0", OPTIMO),
        ("0.0299", OPTIMO),
        ("0.03", ALERTA),
        ("0.0799", ALERTA),
        ("0.08", CUIDADOS),
        ("0.1999", CUIDADOS),
        ("0.20", BLOQUEO),
        ("0.5", BLOQUEO),
    ],
)
def test_estado_de_cada_drawdown(caida, estado):
    assert state_for(D(caida)) is estado


@pytest.mark.parametrize(
    ("nav", "caida", "estado", "visto"),
    [
        ("9701", "0.0299", OPTIMO, "2,9 %"),
        ("9700", "0.03", ALERTA, "3,0 %"),
        ("9201", "0.0799", ALERTA, "7,9 %"),
        ("9200", "0.08", CUIDADOS, "8,0 %"),
        ("8001", "0.1999", CUIDADOS, "19,9 %"),
        ("8000", "0.20", BLOQUEO, "20,0 %"),
    ],
)
def test_fronteras_exactas_desde_el_valor_por_participacion(nav, caida, estado, visto):
    """Valor 97,01 / 97 / 92,01 / 92 / 80,01 / 80 frente a un máximo de 100: drawdown de
    2,99 / 3,00 / 7,99 / 8,00 / 19,99 / 20,00 %."""
    f = snapshot_for(HOY, valoracion(nav), [BASE], [])
    assert f.unit_value == D(nav) / 100
    assert f.high_water_mark == D("100")
    assert f.drawdown == D(caida)
    assert f.state is estado
    # Se enseña cortado, sin redondear: un 2,99 % nunca se ve como «3,0 %».
    assert format_pct(f.drawdown, truncate=True) == visto


def test_solo_se_compra_en_optimo_y_en_alerta():
    assert buying_allowed(OPTIMO) and buying_allowed(ALERTA)
    assert not buying_allowed(CUIDADOS) and not buying_allowed(BLOQUEO)


def test_drawdown_sobre_el_maximo():
    assert drawdown_of(D("95"), D("100")) == D("0.05")
    assert drawdown_of(D("105"), D("100")) == 0
    assert drawdown_of(D("100"), D("100")) == 0


def test_reglas_por_estado():
    assert REGLAS.max_asset_weight(OPTIMO) == D("0.10")
    assert REGLAS.max_asset_weight(ALERTA) == REGLAS.max_asset_weight(BLOQUEO) == D("0.05")
    assert (REGLAS.min_cash(OPTIMO), REGLAS.max_cash(OPTIMO)) == (D("0.15"), D("0.30"))
    assert (REGLAS.min_cash(CUIDADOS), REGLAS.max_cash(CUIDADOS)) == (D("0.30"), None)
    assert state_limits_text(ALERTA, REGLAS) == (
        "Tope por activo 5 % · efectivo mínimo 30 % mientras dure la alerta"
    )
    assert "entre 15 % y 30 %" in state_limits_text(OPTIMO, REGLAS)
    assert "revisión completa" in state_limits_text(BLOQUEO, REGLAS)


# -- participaciones: ingresos y retiradas no son ganar ni perder --------------------------


def test_un_ingreso_no_mueve_el_valor_por_participacion():
    f = snapshot_for(HOY, valoracion("15000"), [BASE], [movimiento(CashKind.DEPOSIT, "5000")])
    assert f.unit_value == D("100")
    assert f.fund_units == D("150")
    assert f.drawdown == 0 and f.state is OPTIMO


def test_una_retirada_no_mueve_el_valor_por_participacion():
    f = snapshot_for(HOY, valoracion("8000"), [BASE],
                     [movimiento(CashKind.WITHDRAWAL, "-2000")])
    assert f.unit_value == D("100")
    assert f.fund_units == D("80")
    assert f.state is OPTIMO


def test_sacar_dinero_en_alerta_no_empeora_el_drawdown():
    f = snapshot_for(HOY, valoracion("5600"), [BASE_ALERTA],
                     [movimiento(CashKind.WITHDRAWAL, "-4000")])
    assert f.unit_value == D("96")
    assert f.drawdown == D("0.04")
    assert f.state is ALERTA


def test_un_ingreso_el_dia_que_el_mercado_sube_solo_cuenta_la_subida():
    # Lo invertido sube un 2 % (10.000 → 10.200) y entran 5.000 €.
    f = snapshot_for(HOY, valoracion("15200"), [BASE], [movimiento(CashKind.DEPOSIT, "5000")])
    assert f.unit_value == D("102")
    assert f.fund_units == D("100") + D("5000") / D("102")


@pytest.mark.parametrize(
    ("tipo", "importe", "valor"),
    [
        (CashKind.DIVIDEND, "200", "102"),
        (CashKind.INTEREST, "50", "100.5"),
        (CashKind.FEE, "-100", "99"),
        (CashKind.TAX, "-300", "97"),
        (CashKind.ADJUSTMENT, "-50", "99.5"),
    ],
)
def test_dividendos_intereses_comisiones_impuestos_y_ajustes_si_cuentan(tipo, importe, valor):
    nav = D("10000") + D(importe)
    f = snapshot_for(HOY, valoracion(nav), [BASE], [movimiento(tipo, importe)])
    assert f.unit_value == D(valor)
    assert f.fund_units == D("100")


def test_solo_cuentan_los_flujos_posteriores_a_la_base():
    movimientos = [
        movimiento(CashKind.DEPOSIT, "1000", ANTEAYER),
        movimiento(CashKind.DEPOSIT, "2000", AYER),  # el día de la base: ya estaba dentro
        movimiento(CashKind.DEPOSIT, "400", HOY),
        movimiento(CashKind.WITHDRAWAL, "-100", HOY),
        movimiento(CashKind.INITIAL, "9999", HOY),  # INICIAL no es un flujo
        movimiento(CashKind.DIVIDEND, "10", HOY),
    ]
    assert external_flows(movimientos, AYER, HOY) == D("300")
    assert external_flows(movimientos, None, AYER) == D("3000")


def test_las_fotos_se_encadenan_dia_a_dia():
    manana = HOY + timedelta(days=1)
    hoy = snapshot_for(HOY, valoracion("15000"), [BASE], [movimiento(CashKind.DEPOSIT, "5000")])
    # Mañana todo lo invertido cae un 5 %: el valor por participación también.
    despues = snapshot_for(manana, valoracion("14250"), [BASE, hoy], [])
    assert despues.fund_units == D("150")
    assert despues.unit_value == D("95")
    assert despues.drawdown == D("0.05")
    assert despues.state is ALERTA


def test_sacarlo_todo_y_volver_a_meter_conserva_el_valor():
    vacia = snapshot_for(HOY, valoracion("0"), [BASE_ALERTA],
                         [movimiento(CashKind.WITHDRAWAL, "-9600")])
    assert vacia.fund_units == 0
    assert vacia.unit_value == D("96")
    manana = HOY + timedelta(days=1)
    otra_vez = snapshot_for(manana, valoracion("4800"), [BASE_ALERTA, vacia],
                            [movimiento(CashKind.DEPOSIT, "4800", manana)])
    assert otra_vez.unit_value == D("96")
    assert otra_vez.fund_units == D("50")
    assert otra_vez.state is ALERTA


# -- el valor 100 de partida ----------------------------------------------------------------


def test_la_primera_valoracion_fiable_fija_el_valor_en_100():
    # La foto del asistente se hizo a coste y no es fiable.
    asistente = foto(AYER, "12000", "120", "100", fiable=False, cobertura="0.1")
    f = snapshot_for(HOY, valoracion("13500"), [asistente], [])
    assert (f.unit_value, f.high_water_mark, f.drawdown, f.state) == (100, 100, 0, OPTIMO)
    assert f.fund_units == D("135")
    assert f.reliable


def test_mientras_no_hay_ninguna_fiable_el_valor_sigue_en_100():
    asistente = foto(AYER, "12000", "120", "100", fiable=False, cobertura="0.1")
    f = snapshot_for(HOY, valoracion("9000", cobertura="0.5"), [asistente], [])
    assert (f.unit_value, f.state, f.reliable) == (100, OPTIMO, False)


def test_con_una_fiable_ya_no_se_vuelve_a_100():
    f = snapshot_for(HOY, valoracion("9000"), [BASE], [])
    assert f.unit_value == D("90")
    assert f.state is CUIDADOS


# -- cobertura por debajo del 90 %: ni estado ni máximo ------------------------------------


@pytest.mark.parametrize(("cobertura", "fiable"), [("0.8999", False), ("0.90", True)])
def test_la_cobertura_del_90_decide_si_se_actualiza_el_estado(cobertura, fiable):
    f = snapshot_for(HOY, valoracion("8000", cobertura=cobertura), [BASE_ALERTA], [])
    assert f.reliable is fiable
    assert f.unit_value == D("80")
    if fiable:
        assert (f.state, f.drawdown, f.high_water_mark) == (BLOQUEO, D("0.2"), D("100"))
    else:
        assert (f.state, f.drawdown, f.high_water_mark) == (ALERTA, D("0.04"), D("100"))
        assert held_since(f, [BASE_ALERTA]) == AYER


def test_sin_valoracion_fiable_el_maximo_no_sube():
    f = snapshot_for(HOY, valoracion("11000", cobertura="0.5"), [BASE_ALERTA], [])
    assert f.high_water_mark == D("100")
    assert f.state is ALERTA


def test_sin_valoracion_fiable_las_participaciones_siguen_a_los_ingresos():
    f = snapshot_for(HOY, valoracion("10600", cobertura="0.5"), [BASE_ALERTA],
                     [movimiento(CashKind.DEPOSIT, "1000")])
    assert f.unit_value == D("96")
    assert f.fund_units == D("100") + D("1000") / D("96")
    assert (f.state, f.reliable) == (ALERTA, False)


def test_sin_valoracion_fiable_manda_la_fiable_de_hoy_si_la_hay():
    hoy_fiable = foto(HOY, "9100", "100", "91", maximo="100", caida="0.09", estado=CUIDADOS)
    f = snapshot_for(HOY, valoracion("9900", cobertura="0.5"), [BASE_ALERTA, hoy_fiable], [])
    assert (f.state, f.drawdown) == (CUIDADOS, D("0.09"))
    assert held_since(f, [BASE_ALERTA, hoy_fiable]) == HOY


# -- el máximo --------------------------------------------------------------------------------


def test_el_maximo_sube_con_el_valor():
    f = snapshot_for(HOY, valoracion("10500"), [BASE_ALERTA], [])
    assert f.unit_value == D("105")
    assert f.high_water_mark == D("105")
    assert f.drawdown == 0 and f.state is OPTIMO


def test_la_foto_de_hoy_se_rehace_con_la_ultima_valoracion():
    manana_temprano = foto(HOY, "10500", "100", "105")
    f = snapshot_for(HOY, valoracion("10200"), [BASE, manana_temprano], [])
    assert f.high_water_mark == D("102")  # un máximo de media mañana no queda
    assert f.drawdown == 0


# -- variación del día y serie del gráfico -------------------------------------------------


def test_variacion_desde_ayer():
    f = snapshot_for(HOY, valoracion("10100"), [BASE], [])
    cambio = day_change(f, [BASE], [])
    assert (cambio.amount_eur, cambio.fraction, cambio.since) == (D("100"), D("0.01"), AYER)


def test_la_variacion_no_cuenta_los_ingresos():
    ingreso = [movimiento(CashKind.DEPOSIT, "5000")]
    f = snapshot_for(HOY, valoracion("15150"), [BASE], ingreso)
    cambio = day_change(f, [BASE], ingreso)
    assert cambio.amount_eur == D("150")
    assert cambio.fraction == D("0.015")


def test_sin_con_que_comparar_no_hay_variacion():
    asistente = foto(AYER, "12000", "120", "100", fiable=False)
    f = snapshot_for(HOY, valoracion("12000"), [asistente], [])
    assert day_change(f, [asistente], []) is None
    no_fiable = snapshot_for(HOY, valoracion("9000", cobertura="0.5"), [BASE], [])
    assert day_change(no_fiable, [BASE], []) is None


def test_la_serie_solo_lleva_fotos_fiables():
    fotos = [
        foto(ANTEAYER, "10000", "100", "100"),
        foto(AYER, "9000", "100", "90", fiable=False),
        foto(HOY, "10200", "100", "102"),
    ]
    ahora = foto(HOY, "10300", "100", "103")
    assert unit_value_series(fotos, ahora) == [(ANTEAYER, D("100")), (HOY, D("103"))]
    assert unit_value_series(fotos) == [(ANTEAYER, D("100")), (HOY, D("102"))]


# -- auditoría ------------------------------------------------------------------------------


def cartera(efectivo, posiciones, sin_precio=()):
    """Una cartera en EUR a precio de mercado. `posiciones`: {ticker: (valor, sector)}."""
    activos = {
        t: Asset(t, f"Empresa {t}", "EUR", yahoo_symbol=f"{t}.MC", sector=sector)
        for t, (_, sector) in posiciones.items()
    }
    precios = {
        t: Price(t, HOY, D(valor), "EUR", PriceSource.MARKET, AHORA)
        for t, (valor, _) in posiciones.items()
        if t not in sin_precio
    }
    abiertas = [Position(t, D(1), D(valor)) for t, (valor, _) in posiciones.items()]
    return value_portfolio(abiertas, activos, D(efectivo), precios, {}, AHORA, AHORA)


#: 8.000 € en ocho sectores y 2.000 € de efectivo: todo justo en su límite o por debajo.
EQUILIBRADA = {
    "A": ("1000", "Banca"), "B": ("1000", "Salud"), "C": ("1000", "Tecnologia"),
    "D": ("1000", "Energia"), "E": ("1000", "Industria"), "F": ("1000", "Consumo"),
    "G": ("1000", "Telecom"), "H": ("1000", "Utilities"),
}


def por_regla(hallazgos):
    return {h.key: h for h in hallazgos}


def test_una_cartera_que_cumple_no_tiene_incumplimientos():
    assert audit(cartera("2000", EQUILIBRADA), OPTIMO, REGLAS) == ()


def test_justo_en_el_limite_no_es_incumplir():
    # A pesa exactamente el 10 %; efectivo exactamente el 15 % y el 30 % en las otras dos.
    assert audit(cartera("2000", EQUILIBRADA), OPTIMO, REGLAS) == ()
    quince = {"A": ("1000", "Banca"), "B": ("2500", "Salud"), "C": ("2500", "Tecnologia"),
              "D": ("2500", "Energia")}
    assert por_regla(audit(cartera("1500", quince), CUIDADOS, REGLAS)).keys() >= {
        ("EFECTIVO", "MINIMO")
    }
    assert ("EFECTIVO", "MINIMO") not in por_regla(audit(cartera("1500", quince), OPTIMO, REGLAS))


def test_activo_por_encima_del_tope_en_optimo():
    posiciones = {**EQUILIBRADA, "A": ("1250", "Banca"), "H": ("750", "Utilities")}
    hallazgos = por_regla(audit(cartera("2000", posiciones), OPTIMO, REGLAS))
    assert list(hallazgos) == [("ACTIVO", "A")]
    a = hallazgos[("ACTIVO", "A")]
    assert a.rule is BreachRule.ASSET and a.ticker == "A"
    assert a.title == "A pesa 12,5 % del patrimonio (límite 10 % en Óptimo)"
    assert a.correction == "Reducir A al 10 %: vender unos 250 €"
    assert a.amount_eur == D("250")


def test_en_alerta_el_tope_por_activo_es_el_5():
    posiciones = {**EQUILIBRADA, "A": ("1250", "Banca"), "H": ("750", "Utilities")}
    hallazgos = por_regla(audit(cartera("2000", posiciones), ALERTA, REGLAS))
    assert hallazgos[("ACTIVO", "A")].correction == "Reducir A al 5 %: vender unos 750 €"
    assert "(límite 5 % en Alerta)" in hallazgos[("ACTIVO", "A")].title
    assert hallazgos[("ACTIVO", "H")].amount_eur == D("250")
    # Y el efectivo tiene que llegar al 30 %: faltan 1.000 €.
    assert hallazgos[("EFECTIVO", "MINIMO")].correction == "Liberar unos 1.000 €"


def test_la_correccion_se_redondea_hacia_arriba_al_euro():
    posiciones = {**EQUILIBRADA, "A": ("1250.40", "Banca"), "H": ("750", "Utilities")}
    a = por_regla(audit(cartera("2000", posiciones), OPTIMO, REGLAS))[("ACTIVO", "A")]
    assert a.amount_eur == D("251")  # 1.250,40 − 10 % de 10.000,40 = 250,36
    assert a.correction.endswith("vender unos 251 €")


def test_el_porcentaje_no_se_confunde_con_el_limite():
    posiciones = {**EQUILIBRADA, "A": ("1004", "Banca"), "H": ("996", "Utilities")}
    a = por_regla(audit(cartera("2000", posiciones), OPTIMO, REGLAS))[("ACTIVO", "A")]
    assert a.title.startswith("A pesa 10,04 % del patrimonio")


def test_sector_por_encima_del_25():
    posiciones = {
        "T1": ("900", "Renta_Variable"), "T2": ("900", "Renta_Variable"),
        "T3": ("900", "Renta_Variable"), "B": ("900", "Banca"), "C": ("900", "Salud"),
        "D": ("900", "Energia"), "E": ("900", "Industria"), "F": ("900", "Consumo"),
        "G": ("800", "Telecom"),
    }
    hallazgos = por_regla(audit(cartera("2000", posiciones), OPTIMO, REGLAS))
    assert list(hallazgos) == [("SECTOR", "Renta_Variable")]
    s = hallazgos[("SECTOR", "Renta_Variable")]
    assert s.title == "El sector Renta Variable pesa 27,0 % del patrimonio (límite 25 %)"
    assert s.correction == "Reducir Renta Variable al 25 %: vender unos 200 €"


def test_las_posiciones_sin_sector_cuentan_juntas():
    posiciones = {
        "X1": ("900", None), "X2": ("900", None), "X3": ("900", None),
        "B": ("900", "Banca"), "C": ("900", "Salud"), "D": ("900", "Energia"),
        "E": ("900", "Industria"), "F": ("900", "Consumo"), "G": ("800", "Telecom"),
    }
    hallazgos = por_regla(audit(cartera("2000", posiciones), OPTIMO, REGLAS))
    s = hallazgos[("SECTOR", "Sin sector")]
    assert s.title == "Las posiciones sin sector pesan 27,0 % del patrimonio (límite 25 %)"
    assert "Asigna su sector" in s.correction and "X1, X2 y X3" in s.correction
    assert "unos 200 €" in s.correction


def test_efectivo_por_debajo_del_minimo_en_optimo():
    posiciones = {**EQUILIBRADA, "I": ("1000", "Otros")}
    h = por_regla(audit(cartera("1000", posiciones), OPTIMO, REGLAS))[("EFECTIVO", "MINIMO")]
    assert h.rule is BreachRule.CASH
    assert h.title == "Efectivo al 10,0 %, por debajo del mínimo del 15 %"
    assert h.correction == "Liberar unos 500 €"


def test_efectivo_por_encima_del_maximo_solo_en_optimo():
    posiciones = {k: EQUILIBRADA[k] for k in "ABCDEF"}
    posiciones["G"] = ("500", "Telecom")
    valoracion_ = cartera("3500", posiciones)  # 35 % de efectivo
    h = por_regla(audit(valoracion_, OPTIMO, REGLAS))[("EFECTIVO", "MAXIMO")]
    assert h.title == "Efectivo al 35,0 %, por encima del máximo del 30 %"
    assert h.correction == "Invertir unos 500 €"
    # En Alerta no hay techo: el 35 % cumple el mínimo del 30 %.
    assert ("EFECTIVO", "MAXIMO") not in por_regla(audit(valoracion_, ALERTA, REGLAS))
    assert ("EFECTIVO", "MINIMO") not in por_regla(audit(valoracion_, ALERTA, REGLAS))


def test_cobertura_inferior_al_100():
    posiciones = {**EQUILIBRADA}
    h = por_regla(audit(cartera("2000", posiciones, sin_precio=("C", "D")), OPTIMO, REGLAS))
    c = h[("COBERTURA", "")]
    assert c.rule is BreachRule.COVERAGE
    assert c.title == "Cobertura del 80,0 %: 2 posiciones sin precio fiable (C y D)"
    assert c.correction.startswith("Unos 2.000 € del patrimonio sin precio fiable")
    assert "90 %" in c.correction  # por debajo del 90 %: el estado no se actualiza


def test_cobertura_por_encima_del_90_sigue_siendo_un_incumplimiento():
    c = por_regla(audit(cartera("2000", EQUILIBRADA, sin_precio=("C",)), OPTIMO, REGLAS))
    assert c[("COBERTURA", "")].title.startswith("Cobertura del 90,0 %: 1 posición")
    assert "90 %" not in c[("COBERTURA", "")].correction


def test_sin_patrimonio_no_hay_nada_que_auditar():
    assert audit(valoracion("0"), OPTIMO, REGLAS) == ()


def test_antiguedad_de_un_incumplimiento():
    abierto = datetime(2026, 9, 16, 9, 0, tzinfo=MADRID)
    assert days_open(abierto, HOY) == 9
    assert days_open(AHORA, HOY) == 0
    assert is_escalated(7, REGLAS) and not is_escalated(6, REGLAS)
    assert age_text(0, False) == "abierto hoy"
    assert age_text(1, False) == "abierto desde ayer"
    assert age_text(9, True) == "abierto desde hace 9 días (escalado)"


def test_listas_de_tickers():
    assert join_names(["A"]) == "A"
    assert join_names(["A", "B"]) == "A y B"
    assert join_names(["A", "B", "C", "D", "E", "F"]) == "A, B, C y 3 más"


# -- validación de compras ------------------------------------------------------------------

#: NAV 10.000 €: 4.000 € de efectivo y 6.000 € invertidos (2.000 € en Tecnologia).
PARA_COMPRAR = {
    "AAA": ("800", "Tecnologia"), "BBB": ("1200", "Tecnologia"), "CCC": ("1500", "Banca"),
    "DDD": ("1500", "Salud"), "EEE": ("1000", "Energia"),
}


def compra(**cambios):
    datos = dict(
        ticker="NEW", units=D("10"), price=D("50"), currency="EUR", price_to_eur=D("1"),
        fee_eur=D("0"), stop=D("45"), target=D("65"), levels_currency="EUR",
        levels_to_eur=D("1"), sector="Industria",
    )
    datos.update(cambios)
    return BuyOrder(**datos)


def validar(orden, estado=OPTIMO, efectivo="4000", posiciones=None):
    return validate_buy(orden, cartera(efectivo, posiciones or PARA_COMPRAR), estado, REGLAS)


def test_una_compra_que_cumple_pasa_los_ocho_pasos():
    resultado = validar(compra())
    assert resultado.ok
    assert [r.check for r in resultado.results] == list(BUY_CHECKS)
    assert resultado.failure is None


@pytest.mark.parametrize(("estado", "texto"), [
    (CUIDADOS, "Compras prohibidas en estado Cuidados intensivos"),
    (BLOQUEO, "revisión completa"),
])
def test_paso_1_en_cuidados_intensivos_o_bloqueo_no_se_compra(estado, texto):
    resultado = validar(compra(), estado)
    assert resultado.failure.check is Check.STATE
    assert texto in resultado.failure.message
    assert "vender siempre se puede" in resultado.failure.message


def test_paso_1_en_alerta_se_compra_con_tope_del_5():
    resultado = validar(compra(), ALERTA)  # 500 € = justo el 5 %
    assert resultado.ok
    assert "tope del 5 % por activo" in resultado.result(Check.STATE).message


@pytest.mark.parametrize(("cambios", "texto"), [
    ({"units": D("0")}, "las unidades"),
    ({"price": D("-1")}, "el precio"),
    ({"units": D("0"), "price": D("0")}, "el precio y las unidades"),
])
def test_paso_2_precio_y_unidades_mayores_que_0(cambios, texto):
    resultado = validar(compra(**cambios))
    assert resultado.failure.check is Check.AMOUNTS
    assert texto in resultado.failure.message
    assert [r.check for r in resultado.results] == [Check.STATE, Check.AMOUNTS]


@pytest.mark.parametrize(("cambios", "texto"), [
    ({"stop": None}, "Falta el stop."),
    ({"target": None}, "Falta el objetivo."),
    ({"stop": D("50")}, "El stop (50,00 EUR) tiene que quedar por debajo"),
    ({"target": D("50")}, "El objetivo (50,00 EUR) tiene que quedar por encima"),
])
def test_paso_3_stop_por_debajo_y_objetivo_por_encima(cambios, texto):
    resultado = validar(compra(**cambios))
    assert resultado.failure.check is Check.LEVELS
    assert resultado.failure.message.startswith(texto)
    # Sin niveles válidos no hay ratio ni riesgo que medir; el resto sí se calcula.
    pasos = [r.check for r in resultado.results]
    assert Check.REWARD_RISK not in pasos and Check.TRADE_RISK not in pasos
    assert Check.CASH in pasos


def test_paso_3_niveles_en_eur_con_cotizacion_en_usd():
    # 100 USD a 0,85 son 85 €: un stop de 90 € queda por encima aunque 90 < 100.
    en_usd = dict(currency="USD", price=D("100"), price_to_eur=D("0.85"), units=D("5"))
    malo = validar(compra(**en_usd, stop=D("90"), target=D("110")))
    assert malo.failure.check is Check.LEVELS
    assert "(comparados en EUR)" in malo.failure.message
    bueno = validar(compra(**en_usd, stop=D("80"), target=D("110")))
    assert bueno.ok
    assert bueno.result(Check.REWARD_RISK).message.startswith("Ratio beneficio/riesgo 5,00")
    assert bueno.result(Check.TRADE_RISK).message.startswith("Riesgo hasta el stop: 25,00 €")


def test_paso_3_niveles_en_usd_con_cotizacion_en_eur():
    niveles_usd = dict(levels_currency="USD", levels_to_eur=D("0.5"))
    # Stop 80 USD = 40 € y objetivo 140 USD = 70 €, frente a 50 €.
    resultado = validar(compra(**niveles_usd, stop=D("80"), target=D("140")))
    assert resultado.ok
    assert resultado.result(Check.REWARD_RISK).message.startswith("Ratio beneficio/riesgo 2,00")


@pytest.mark.parametrize(("objetivo", "pasa", "visto"), [
    ("60", True, "2,00"),  # (60 − 50) / (50 − 45) = 2: justo el mínimo
    ("59.99", False, "1,99"),  # 1,998: se corta, no se redondea a 2,00
    ("54.95", False, "0,99"),
])
def test_paso_4_ratio_minimo(objetivo, pasa, visto):
    resultado = validar(compra(target=D(objetivo)))
    ratio = resultado.result(Check.REWARD_RISK)
    assert ratio.passed is pasa
    assert f"Ratio beneficio/riesgo {visto} (mínimo 2,0)" in ratio.message
    assert resultado.ok is pasa
    if not pasa:
        assert resultado.failure.check is Check.REWARD_RISK


def test_paso_5_peso_final_del_activo():
    resultado = validar(compra(ticker="AAA", sector="Tecnologia"))  # 800 + 500 = 13 %
    assert resultado.failure.check is Check.ASSET_WEIGHT
    assert resultado.failure.message == (
        "AAA quedaría al 13,0 % del patrimonio y el tope en estado Óptimo es del 10 %. "
        "Máximo invertible ahora: 200,00 €."
    )
    justo = validar(compra(ticker="AAA", sector="Tecnologia", units=D("4")))  # justo el 10 %
    assert justo.ok


def test_paso_5_en_alerta_con_lo_que_ya_hay_no_cabe_nada():
    resultado = validar(compra(ticker="AAA", sector="Tecnologia", units=D("1")), ALERTA)
    assert resultado.failure.check is Check.ASSET_WEIGHT
    assert "Máximo invertible ahora: 0,00 €." in resultado.failure.message


def test_paso_6_riesgo_hasta_el_stop():
    # 16 × (50 − 40) = 160 € de riesgo frente a 150 € (1,5 % de 10.000 €).
    resultado = validar(compra(units=D("16"), stop=D("40"), target=D("80")))
    assert resultado.failure.check is Check.TRADE_RISK
    assert resultado.failure.message == (
        "Riesgo hasta el stop: 160,00 €; el máximo por operación es 150,00 € (1,5 % del "
        "patrimonio)."
    )
    assert validar(compra(units=D("15"), stop=D("40"), target=D("80"))).ok  # 150 €: justo


def test_paso_7_peso_final_del_sector():
    resultado = validar(compra(ticker="FFF", sector="Tecnologia", units=D("12")))
    assert resultado.failure.check is Check.SECTOR_WEIGHT
    assert resultado.failure.message == (
        "El sector Tecnologia quedaría al 26,0 % del patrimonio (tope 25 %)."
    )
    assert validar(compra(ticker="FFF", sector="Tecnologia", units=D("10"))).ok  # 25 %


def test_paso_7_sin_sector():
    resultado = validar(compra(sector=None))
    assert resultado.ok
    assert resultado.result(Check.SECTOR_WEIGHT).message.startswith(
        "Las posiciones sin sector quedarían al 5,0 %"
    )


#: NAV 10.000 €: 1.600 € de efectivo (16 %) y 8 posiciones de 1.050 €.
POCO_EFECTIVO = {f"P{i}": ("1050", f"Sector{i}") for i in range(8)}


def test_paso_8_efectivo_minimo_del_estado():
    resultado = validar(compra(units=D("4")), efectivo="1600", posiciones=POCO_EFECTIVO)
    assert resultado.failure.check is Check.CASH
    assert resultado.failure.message == (
        "La liquidez caería al 14,0 %, por debajo del mínimo del 15 % que exige el estado "
        "Óptimo."
    )
    justo = validar(compra(units=D("2")), efectivo="1600", posiciones=POCO_EFECTIVO)
    assert justo.ok  # 1.500 € de 10.000 €: el 15 % justo


def test_paso_8_la_comision_cuenta():
    # 1.600 − 100 − 1 = 1.499 € frente al 15 % de 9.999 € (1.499,85 €).
    resultado = validar(compra(units=D("2"), fee_eur=D("1")), efectivo="1600",
                        posiciones=POCO_EFECTIVO)
    assert resultado.failure.check is Check.CASH


def test_paso_8_sin_efectivo():
    posiciones = {f"P{i}": ("990", f"Sector{i}") for i in range(10)}  # NAV 10.000 €
    resultado = validar(compra(units=D("4")), efectivo="100", posiciones=posiciones)
    assert resultado.failure.check is Check.CASH
    assert resultado.failure.message.startswith("No hay efectivo suficiente")


def test_se_muestra_la_primera_que_falla_y_se_apuntan_las_demas():
    # Falla el stop (paso 3), el peso de AAA (paso 5) y el efectivo (paso 8).
    resultado = validar(compra(ticker="AAA", sector="Tecnologia", stop=D("55"), units=D("60")))
    assert resultado.failure.check is Check.LEVELS
    assert [r.check for r in resultado.also_failing] == [
        Check.ASSET_WEIGHT, Check.SECTOR_WEIGHT, Check.CASH
    ]


def test_sin_cambio_a_eur_no_se_valida():
    with pytest.raises(ValueError):
        validar(compra(price_to_eur=D("0")))


# -- validación de ventas -------------------------------------------------------------------


def test_una_venta_solo_mira_importes_y_unidades():
    assert validate_sell("SAN", D("10"), D("5"), D("200")).ok
    todo = validate_sell("SAN", D("200"), D("5"), D("200"))
    assert todo.ok


def test_no_se_vende_mas_de_lo_que_hay():
    resultado = validate_sell("SAN", D("250.5"), D("5"), D("200"))
    assert resultado.failure.check is Check.HOLDING
    assert resultado.failure.message == (
        "No se pueden vender 250,5 unidades de SAN: solo hay 200."
    )


def test_una_venta_sin_precio_no_vale():
    resultado = validate_sell("SAN", D("10"), D("0"), D("200"))
    assert resultado.failure.check is Check.AMOUNTS
    assert len(resultado.results) == 1
