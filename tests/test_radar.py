"""El filtro del radar y la caducidad de sus alertas (GUIA §5.8, H11): lógica pura, con series
de precios inventadas.

El test que más importa es el primero: con umbrales mal puestos (como en la versión anterior)
el radar no dispara nunca. Aquí una acción normal, con un ATR del 2,6 %, que ha caído un 23 %
desde su máximo, tiene que dar alerta.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from fakes import subida_y_caida, velas
from sharky.core.models import AlertOrigin, AlertStatus, MandateState, PriceSource, RadarAlert
from sharky.core.radar import (
    ALREADY_ACTIVE,
    IN_PORTFOLIO,
    Candidate,
    DailyBar,
    ExpiryKind,
    FilterFailure,
    Holdings,
    PriceHistory,
    RadarRules,
    RawCandidate,
    alert_history,
    apply_filter,
    average_true_range,
    check_candidates,
    discarded_until,
    expire_alerts,
    has_twelve_months,
    high_52w,
    idea_for,
    low_8w,
    radar_pass,
    rejected_candidates,
    true_range,
)
from sharky.services.settings import Settings

d = Decimal
HOY = date(2026, 9, 25)  # viernes
AHORA = datetime(2026, 9, 25, 22, 0, tzinfo=UTC)


def reglas(**cambios) -> RadarRules:
    """Los umbrales por defecto de Ajustes (10–40 %, 8–15 %, ratio 2,0) en Óptimo (10 %)."""
    ajustes = Settings()
    base = ajustes.radar.rules(ajustes.mandate.rules(), MandateState.OPTIMAL)
    return RadarRules(**{**base.__dict__, **cambios})


def historia(barras, *, fuente=PriceSource.MARKET, divisa="USD", simbolo="ACME",
             descargado=AHORA) -> PriceHistory:
    return PriceHistory(simbolo, divisa, tuple(barras), descargado, fuente)


def plana(precio="100", *, dias=300, rango="0.5", especiales=None, final=None):
    """Una serie plana en `precio` (el último cierre, `final` si se da)."""
    cierres = [d(precio)] * dias
    if final is not None:
        cierres[-1] = d(final)
    return velas(cierres, hasta=HOY, rango=rango, especiales=especiales)


# -- el filtro puede disparar ----------------------------------------------------------------


def test_una_accion_normal_que_ha_caido_un_23_por_ciento_da_alerta():
    """El caso realista: sube de 80 a 100 y cae a 78, con un rango diario de ±1 (ATR 2,56 %).

    Máximo de 52 semanas 101; mínimo de 8 semanas 77; ATR 2. El stop «el mayor de» (77 frente a
    78 − 3 = 75) queda a un 1,3 %: más cerca del 8 %, así que se baja al 8 % (71,76). Ratio
    (101 − 78) / (78 − 71,76) = 3,69 ≥ 2,0.
    """
    resultado = apply_filter(historia(subida_y_caida(HOY)), HOY, reglas())
    assert resultado.passed, resultado.reason
    assert (resultado.price, resultado.high, resultado.low) == (d("78.00"), d("101.00"),
                                                                 d("77.00"))
    assert resultado.atr == d(2)
    assert resultado.widened
    assert resultado.stop == d("71.76")
    assert resultado.target == d("101.00")
    assert resultado.ratio.quantize(d("0.01")) == d("3.69")
    assert resultado.drop.quantize(d("0.0001")) == d("0.2277")
    assert resultado.max_weight == d("0.1")
    assert resultado.reason is None and resultado.failure is None


def tramos(*puntos: tuple[int, str]) -> list[Decimal]:
    """Cierres que van en línea recta de un punto al siguiente: [(días, hasta), …] desde el
    primer valor."""
    (_, inicio), *resto = puntos
    cierres = [d(inicio)]
    for dias, destino in resto:
        a = cierres[-1]
        cierres += [(a + (d(destino) - a) * i / dias).quantize(d("0.01"))
                    for i in range(1, dias + 1)]
    return cierres


def test_una_accion_volatil_que_ya_rebota_dispara_con_el_stop_de_su_estructura():
    """Sube a 100, cae hasta 70 y rebota a 78 en tres semanas, con un rango de ±3 (ATR 6): el
    stop es el mayor de 67 (mínimo de 8 semanas) y 78 − 9 = 69, a un 11,5 %, dentro de la
    banda: no se toca. Ratio (103 − 78) / (78 − 69) = 2,78."""
    cierres = tramos((0, "80"), (150, "100"), (134, "70"), (15, "78"))
    resultado = apply_filter(historia(velas(cierres, hasta=HOY, rango="3")), HOY, reglas())
    assert resultado.passed, resultado.reason
    assert resultado.atr == d(6)
    assert not resultado.widened
    assert resultado.stop == d("69.00")
    assert resultado.target == d("103.00")
    assert resultado.ratio.quantize(d("0.01")) == d("2.78")


# -- un caso por cada motivo de descarte -------------------------------------------------------


def test_falla_por_ratio():
    """Cae solo un 12,9 % (de 101 a 88): con el stop al 8 % (80,96), el ratio es 1,85."""
    resultado = apply_filter(historia(subida_y_caida(HOY, final="88")), HOY, reglas())
    assert not resultado.passed
    assert resultado.failure is FilterFailure.RATIO
    assert resultado.reason == "Ratio 1,8 · el mínimo del mandato es 2,0"
    assert resultado.stop == d("80.96") and resultado.target == d("101.00")


@pytest.mark.parametrize(("final", "texto"), [
    ("96", "Caída desde máximos del 4,9 % · el filtro pide entre el 10 % y el 40 %"),
    ("55", "Caída desde máximos del 45,6 % · el filtro pide entre el 10 % y el 40 %"),
])
def test_falla_por_caida_fuera_del_10_al_40(final, texto):
    resultado = apply_filter(historia(subida_y_caida(HOY, final=final)), HOY, reglas())
    assert not resultado.passed
    assert resultado.failure is FilterFailure.DROP
    assert resultado.reason == texto
    assert resultado.stop is None and resultado.target is None


def test_falla_por_stop_mas_lejos_del_15():
    """Una acción muy volátil (rango ±4,5, ATR 9) que ha rebotado un 20 % desde su mínimo: el
    stop «el mayor de» 55,5 y 72 − 13,5 = 58,5 queda a un 18,75 %."""
    cierres = tramos((0, "80"), (150, "100"), (119, "60"), (5, "60"), (25, "72"))
    resultado = apply_filter(historia(velas(cierres, hasta=HOY, rango="4.5")), HOY, reglas())
    assert not resultado.passed
    assert resultado.drop.quantize(d("0.001")) == d("0.311")  # la caída sí está en rango
    assert resultado.failure is FilterFailure.STOP
    assert resultado.raw_stop == d("58.5")
    assert resultado.reason == (
        "El stop calculado queda a un 18,8 % · el filtro lo acota al 8–15 %"
    )
    assert resultado.stop is None


def test_sin_12_meses_de_historico_no_es_verificable():
    ocho_meses = subida_y_caida(HOY, dias=170, dia_pico=60)
    resultado = apply_filter(historia(ocho_meses), HOY, reglas())
    assert not resultado.passed
    assert resultado.failure is FilterFailure.UNVERIFIABLE
    assert resultado.reason.startswith("No verificable: sin 12 meses de histórico fiable")


def test_una_cotizacion_no_fiable_no_es_verificable():
    serie = subida_y_caida(HOY)
    antigua = historia(serie, fuente=PriceSource.STALE, descargado=AHORA - timedelta(days=3))
    resultado = apply_filter(antigua, HOY, reglas())
    assert resultado.failure is FilterFailure.UNVERIFIABLE
    assert "no se ha podido descargar otro" in resultado.reason
    # La caché de menos de 24 h sí es fiable.
    assert apply_filter(historia(serie, fuente=PriceSource.CACHE), HOY, reglas()).passed


def test_sin_cotizacion_reciente_o_sin_historico_no_es_verificable():
    suspendida = subida_y_caida(HOY - timedelta(days=10))
    resultado = apply_filter(historia(suspendida), HOY, reglas())
    assert resultado.failure is FilterFailure.UNVERIFIABLE
    assert "sin cotización reciente" in resultado.reason
    sin_nada = apply_filter(None, HOY, reglas(), "Yahoo no tiene cotizaciones de ZZZ.")
    assert sin_nada.reason == "No verificable: Yahoo no tiene cotizaciones de ZZZ."


# -- las fronteras -----------------------------------------------------------------------------


def test_caida_del_10_y_del_40_entran_y_un_poco_mas_no():
    maximo = {150: (d(100), d("99.5"))}
    diez = apply_filter(historia(plana("90", especiales=maximo)), HOY, reglas())
    assert diez.failure is not FilterFailure.DROP  # entra por la caída (falla por el ratio)
    cuarenta = apply_filter(historia(plana("60", especiales=maximo)), HOY, reglas())
    assert cuarenta.passed
    fuera = apply_filter(historia(plana("59.99", especiales=maximo)), HOY, reglas())
    assert fuera.failure is FilterFailure.DROP
    assert fuera.reason.startswith("Caída desde máximos del 40,1 %")


def test_con_una_caida_del_10_al_13_8_ningun_stop_da_el_ratio():
    """Los umbrales, entre sí: con el stop más cerca posible (8 %), el ratio de 2,0 pide una
    caída del 13,8 % o más. Una caída del 13,5 % no puede pasar."""
    maximo = {150: (d(100), d("99.5"))}
    resultado = apply_filter(historia(plana("86.5", especiales=maximo)), HOY, reglas())
    assert resultado.failure is FilterFailure.RATIO
    assert resultado.stop == d("79.58")  # ya al 8 %: no hay stop más cercano


def test_ratio_de_2_exacto_pasa_y_un_poco_menos_no():
    """Precio 100, stop al 8 % (92): el objetivo 116 da un ratio de 2,0 exacto."""
    pasa = apply_filter(historia(plana(especiales={150: (d(116), d("99.5"))})), HOY, reglas())
    assert pasa.passed and pasa.ratio == d(2)
    no = apply_filter(historia(plana(especiales={150: (d("115.99"), d("99.5"))})), HOY,
                      reglas())
    assert no.failure is FilterFailure.RATIO
    assert no.reason == "Ratio 1,9 · el mínimo del mandato es 2,0"


def test_stop_al_8_y_al_15_exactos_no_se_tocan():
    # Rango ±3 (ATR 6): 100 − 9 = 91; mínimo de 8 semanas 92 → stop 92, a un 8 % justo.
    ocho = apply_filter(historia(plana(rango="3", especiales={
        150: (d(130), d(97)), -20: (d(103), d(92)),
    })), HOY, reglas())
    assert ocho.passed and not ocho.widened and ocho.stop == d("92.00")
    # Rango ±5 (ATR ≥ 10): 100 − 15 ≤ 85; mínimo de 8 semanas 85 → stop a un 15 % justo.
    quince = apply_filter(historia(plana(rango="5", especiales={
        150: (d(131), d(95)), -35: (d(105), d(85)),
    })), HOY, reglas())
    assert quince.passed and quince.stop == d("85.00")
    fuera = apply_filter(historia(plana(rango="5", especiales={
        150: (d(131), d(95)), -35: (d(105), d("84.99")),
    })), HOY, reglas())
    assert fuera.failure is FilterFailure.STOP
    assert "queda a un 15,1 %" in fuera.reason


def test_el_peso_maximo_sugerido_es_el_tope_del_estado():
    ajustes = Settings()
    alerta = ajustes.radar.rules(ajustes.mandate.rules(), MandateState.ALERT)
    resultado = apply_filter(historia(subida_y_caida(HOY)), HOY, alerta)
    assert resultado.passed and resultado.max_weight == d("0.05")


# -- las piezas del cálculo ---------------------------------------------------------------------


def test_rango_verdadero_y_atr_de_wilder_a_mano():
    barras = [
        DailyBar(date(2026, 1, 1), d(10), d(8), d(9)),
        DailyBar(date(2026, 1, 2), d(11), d(9), d(10)),  # TR = 2
        DailyBar(date(2026, 1, 5), d(12), d(9), d(11)),  # TR = 3
        DailyBar(date(2026, 1, 6), d(10), d(7), d(8)),  # TR = max(3, 1, 4) = 4 (hueco)
    ]
    assert true_range(barras[0], None) == d(2)
    assert true_range(barras[3], d(11)) == d(4)
    # ATR(2): (2 + 3) / 2 = 2,5; después (2,5 × 1 + 4) / 2 = 3,25.
    assert average_true_range(barras, period=2) == d("3.25")
    with pytest.raises(ValueError):
        average_true_range(barras, period=14)


def test_ventanas_de_52_y_8_semanas():
    serie = plana(especiales={
        0: (d(200), d(99)),  # hace más de 52 semanas: no cuenta
        -45: (d(120), d(99)),  # dentro de 52 semanas
        -45 + 1: (d(101), d(50)),  # hace más de 8 semanas: no cuenta para el mínimo
        -30: (d(101), d(90)),  # dentro de 8 semanas
    })
    assert high_52w(serie) == d(120)
    assert low_8w(serie) == d(90)
    assert has_twelve_months(serie)
    assert not has_twelve_months(serie[-200:])


# -- la caducidad ------------------------------------------------------------------------------


def alerta(creada=date(2026, 9, 10), *, ticker="ACME", stop="90", objetivo="130",
           divisa="USD", estado=AlertStatus.ACTIVE, ident=1, peso="0.1", **extra) -> RadarAlert:
    return RadarAlert(
        created_on=creada, ticker=ticker, origin=AlertOrigin.WATCHLIST, status=estado,
        price=d(100), currency=divisa, stop=d(stop), target=d(objetivo), ratio=d(3),
        drawdown_from_high=d("0.2"), max_weight=d(peso) if peso else None,
        yahoo_symbol=ticker, id=ident, **extra,
    )


def test_caduca_por_edad_a_los_30_dias():
    a = alerta(date(2026, 8, 26))
    assert expire_alerts([a], date(2026, 9, 24), 30, {}) == []
    [caducada] = expire_alerts([a], HOY, 30, {})
    assert caducada.kind is ExpiryKind.AGE
    assert caducada.reason == "Caducada por edad: 30 días desde el 26/08/2026 sin entrar."


def test_caduca_si_un_cierre_rompe_el_stop_antes_de_entrar():
    cierres = [d(100)] * 290 + [d(95), d("89.50"), d(96), d(97), d(98)] + [d(99)] * 5
    serie = historia(velas(cierres, hasta=HOY, rango="0.5"))
    [caducada] = expire_alerts([alerta()], HOY, 30, {"ACME": serie})
    assert caducada.kind is ExpiryKind.STOP
    assert caducada.reason.startswith("Rompió su stop antes de que entraras: cierre de 89,50 USD")
    assert "(stop 90,00 USD)" in caducada.reason


def test_caduca_si_alcanza_el_objetivo_sin_ti():
    cierres = [d(100)] * 295 + [d(120), d(131), d(125), d(126), d(127)]
    serie = historia(velas(cierres, hasta=HOY, rango="0.5"))
    [caducada] = expire_alerts([alerta()], HOY, 30, {"ACME": serie})
    assert caducada.kind is ExpiryKind.TARGET
    assert caducada.reason.startswith("Alcanzó el objetivo sin ti: cierre de 131,00 USD")


def test_los_cierres_de_antes_de_la_alerta_no_cuentan():
    # El cierre de 80 fue antes de emitir la alerta: no la caduca.
    cierres = [d(100)] * 280 + [d(80)] + [d(100)] * 19
    serie = historia(velas(cierres, hasta=HOY, rango="0.5"))
    assert expire_alerts([alerta(date(2026, 9, 15))], HOY, 30, {"ACME": serie}) == []


def test_sin_cotizacion_fiable_o_en_otra_divisa_no_caduca_por_precio():
    cierres = [d(100)] * 299 + [d(50)]
    rota = velas(cierres, hasta=HOY, rango="0.5")
    antigua = historia(rota, fuente=PriceSource.STALE)
    en_euros = historia(rota, divisa="EUR")
    assert expire_alerts([alerta()], HOY, 30, {"ACME": antigua}) == []
    assert expire_alerts([alerta()], HOY, 30, {"ACME": en_euros}) == []
    assert len(expire_alerts([alerta()], HOY, 30, {"ACME": historia(rota)})) == 1


def test_la_caducidad_corre_antes_del_filtro_y_el_ticker_vuelve_a_alertar_hoy():
    """ACME tiene una alerta de hace 30 días: caduca hoy y, en la misma pasada, el filtro la
    vuelve a emitir con los niveles de hoy."""
    vieja = alerta(date(2026, 8, 26), stop="60", objetivo="140")
    serie = historia(subida_y_caida(HOY))
    pasada = radar_pass(
        [Candidate("ACME", "Acme", AlertOrigin.WATCHLIST, "ACME")], [vieja], Holdings(),
        {"ACME": serie}, {}, HOY, reglas(),
    )
    assert [e.alert.id for e in pasada.expired] == [1]
    [nueva] = pasada.new_alerts
    assert (nueva.created_on, nueva.stop, nueva.target) == (HOY, d("71.76"), d("101.00"))
    # Sin caducar (de hace 10 días), la alerta conserva sus niveles y el filtro no la mira.
    reciente = alerta(date(2026, 9, 15), stop="60", objetivo="140")
    otra = radar_pass(
        [Candidate("ACME", "Acme", AlertOrigin.WATCHLIST, "ACME")], [reciente], Holdings(),
        {"ACME": serie}, {}, HOY, reglas(),
    )
    assert otra.expired == () and otra.new_alerts == []
    assert otra.verdicts[0].skipped == ALREADY_ACTIVE


# -- la pasada del radar -----------------------------------------------------------------------


def test_lo_que_ya_esta_en_cartera_queda_fuera_por_ticker_o_por_simbolo():
    serie = historia(subida_y_caida(HOY), simbolo="SAN.MC")
    cartera = Holdings.of([("SAN", "SAN.MC")])
    pasada = radar_pass(
        [Candidate("SAN", "Santander", AlertOrigin.WATCHLIST, "SAN.MC"),
         Candidate("BSAN", "Santander", AlertOrigin.EXPLORER, "SAN.MC")],
        [], cartera, {"SAN.MC": serie}, {}, HOY, reglas(),
    )
    assert [v.skipped for v in pasada.verdicts] == [IN_PORTFOLIO, IN_PORTFOLIO]
    assert pasada.new_alerts == [] and pasada.rejections == []


def test_un_descarte_del_filtro_se_guarda_con_su_motivo_y_sin_peso():
    pasada = radar_pass(
        [Candidate("ACME", "Acme", AlertOrigin.EXPLORER, "ACME", summary="Una idea")],
        [], Holdings(), {"ACME": historia(subida_y_caida(HOY, final="88"))}, {}, HOY, reglas(),
    )
    [descarte] = pasada.rejections
    assert descarte.status is AlertStatus.DISCARDED
    assert descarte.max_weight is None
    assert descarte.reason == "Ratio 1,8 · el mínimo del mandato es 2,0"
    assert descarte.summary == "Una idea" and descarte.stop == d("80.96")


def test_la_vigilancia_hereda_la_idea_del_explorador():
    antigua = alerta(date(2026, 7, 1), estado=AlertStatus.DISCARDED, peso=None,
                     summary="Contratos a largo plazo", invalidation="Si cae el uranio")
    pasada = radar_pass(
        [Candidate("ACME", "Acme", AlertOrigin.WATCHLIST, "ACME")], [antigua], Holdings(),
        {"ACME": historia(subida_y_caida(HOY))}, {}, HOY, reglas(),
    )
    [nueva] = pasada.new_alerts
    assert nueva.origin is AlertOrigin.WATCHLIST
    assert (nueva.summary, nueva.invalidation) == ("Contratos a largo plazo", "Si cae el uranio")
    assert idea_for("acme", [antigua]) == ("Contratos a largo plazo", "Si cae el uranio")


def test_una_alerta_descartada_por_ti_no_vuelve_hasta_que_habria_caducado():
    tuya = alerta(date(2026, 9, 10), estado=AlertStatus.DISCARDED)
    assert discarded_until("ACME", [tuya], 30) == date(2026, 10, 10)
    pasada = radar_pass(
        [Candidate("ACME", "Acme", AlertOrigin.WATCHLIST, "ACME")], [tuya], Holdings(),
        {"ACME": historia(subida_y_caida(HOY))}, {}, HOY, reglas(),
    )
    assert pasada.new_alerts == []
    assert "no se vuelve a mirar hasta el 10/10/2026" in pasada.verdicts[0].skipped


def test_candidatos_rechazados_e_historial():
    filas = [
        alerta(date(2026, 9, 1), ticker="AAA", estado=AlertStatus.DISCARDED, peso=None,
               ident=1, reason="Ratio 1,2"),
        alerta(date(2026, 9, 2), ticker="AAA", ident=2),  # después dio alerta
        alerta(date(2026, 9, 3), ticker="BBB", estado=AlertStatus.DISCARDED, peso=None,
               ident=3, reason="Caída"),
        alerta(date(2026, 9, 4), ticker="CCC", estado=AlertStatus.EXPIRED, ident=4),
        alerta(date(2026, 9, 5), ticker="DDD", estado=AlertStatus.DISCARDED, ident=5),
        alerta(date(2026, 9, 6), ticker="EEE", estado=AlertStatus.EXECUTED, ident=6),
    ]
    assert [a.ticker for a in rejected_candidates(filas)] == ["BBB"]
    assert [a.ticker for a in alert_history(filas)] == ["EEE", "DDD", "CCC"]


# -- los candidatos del explorador -------------------------------------------------------------


def crudo(ticker, simbolo="X", **extra):
    datos = {"name": "Nombre", "sector": "Energía nuclear", "thesis": "Foso",
             "invalidation": "Nada", **extra}
    return RawCandidate(ticker, simbolo, **datos)


def test_candidatos_limpios_como_mucho_8_sin_repetir():
    crudos = [crudo(f"T{i}") for i in range(10)]
    crudos[1] = crudo("t0")  # repetido (sin distinguir mayúsculas)
    crudos[2] = crudo("MAL ?")  # no vale
    crudos[3] = crudo("SAAB B", "SAAB-B.ST")
    candidatos, fuera = check_candidates(crudos)
    assert [c.ticker for c in candidatos] == ["T0", "SAAB-B", "T4", "T5", "T6", "T7"]
    assert fuera[0].ticker == "MAL ?" and fuera[0].reason == "ticker no válido"
    assert candidatos[1].yahoo_symbol == "SAAB-B.ST"
    assert candidatos[0].sector == "Energía_nuclear"
    assert all(c.origin is AlertOrigin.EXPLORER for c in candidatos)
