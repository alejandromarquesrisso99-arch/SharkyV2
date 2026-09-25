"""Tesis y niveles (GUIA §5.6 y §7, H7): la vigilancia pura del stop y del objetivo.

Stop, objetivo y prioridad del stop; niveles en otra divisa que la cotización (y al revés);
los tres criterios del stop propuesto y que nunca baja del vigente; NO VERIFICABLE.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from sharky.core.ledger import Position
from sharky.core.levels import (
    LevelStatus,
    ProposalState,
    StopCriterion,
    alert_title,
    changes,
    check_thesis,
    describe_event,
    evaluate_levels,
    from_json,
    history,
    initial_risk,
    levels_of,
    notification,
    position_line,
    proposal_blocker,
    proposal_states,
    proposal_text,
    propose_stop,
    stop_message,
    target_proposal_event,
    to_json,
    validate_levels,
)
from sharky.core.models import (
    Asset,
    Author,
    FxRate,
    LevelAlertKind,
    Price,
    PriceSource,
    Thesis,
    ThesisEvent,
    ThesisEventKind,
    ThesisStatus,
)
from sharky.core.valuation import value_portfolio

MADRID = timezone(timedelta(hours=2))
AHORA = datetime(2026, 9, 25, 18, 0, tzinfo=MADRID)
HOY = AHORA.date()
HACE_DOS_DIAS = timedelta(days=2)


def valorar(precio="100", divisa="EUR", *, ticker="ACME", simbolo="ACME", hace=timedelta(0),
            cambios=None, unidades="10", coste="900"):
    """Una cartera con una posición de ACME, con su precio y los cambios que se den."""
    activo = Asset(ticker, "Acme Corp", divisa, yahoo_symbol=simbolo)
    precios = {}
    if precio is not None:
        precios[ticker] = Price(ticker, HOY, D(precio), divisa, PriceSource.MARKET, AHORA - hace)
    return value_portfolio([Position(ticker, D(unidades), D(coste))], {ticker: activo},
                           D("1000"), precios, cambios or {}, AHORA, AHORA)


def cambio(divisa, valor, hace=timedelta(0)):
    return FxRate(divisa, HOY, D(valor), PriceSource.MARKET, AHORA - hace)


def tesis(stop=None, objetivo=None, entrada=None, divisa="EUR", *, ticker="ACME", id=1,
          estado=ThesisStatus.ACTIVE):
    def d(x):
        return D(x) if x is not None else None

    return Thesis(ticker, divisa, HOY, entry_price=d(entrada), stop=d(stop), target=d(objetivo),
                  status=estado, closed_on=HOY if estado is ThesisStatus.CLOSED else None, id=id)


def vigilar(t, valoracion, cambios=None, riesgo=None):
    [posicion] = valoracion.positions
    return check_thesis(t, posicion, cambios or {}, AHORA, AHORA, riesgo)


# -- stop, objetivo y prioridad ---------------------------------------------------------------


def test_precio_por_debajo_del_stop_es_stop():
    c = vigilar(tesis("100", "130"), valorar("95"))
    assert c.status is LevelStatus.STOP
    assert (c.price_eur, c.stop_eur, c.target_eur) == (D("95"), D("100"), D("130"))
    assert c.proposal is None


def test_tocar_el_stop_ya_es_stop():
    assert vigilar(tesis("100", "130"), valorar("100")).status is LevelStatus.STOP
    assert vigilar(tesis("100", "130"), valorar("100.01")).status is LevelStatus.WITHIN


def test_llegar_al_objetivo_es_objetivo():
    assert vigilar(tesis("100", "130"), valorar("130")).status is LevelStatus.TARGET
    assert vigilar(tesis("100", "130"), valorar("135")).status is LevelStatus.TARGET
    assert vigilar(tesis("100", "130"), valorar("129.99")).status is LevelStatus.WITHIN


def test_entre_niveles_no_hay_aviso():
    c = vigilar(tesis("100", "130"), valorar("110"))
    assert c.status is LevelStatus.WITHIN
    assert c.alert(HOY) is None and c.proposal is None


def test_el_stop_manda_sobre_el_objetivo():
    # Tras subir el stop por encima del objetivo, un precio entre los dos cumple las dos
    # condiciones: manda el stop.
    c = vigilar(tesis("120", "110"), valorar("115"))
    assert c.status is LevelStatus.STOP
    assert c.proposal is None
    assert c.alert(HOY).kind is LevelAlertKind.STOP


def test_solo_stop_o_solo_objetivo():
    assert vigilar(tesis(stop="100"), valorar("99")).status is LevelStatus.STOP
    assert vigilar(tesis(stop="100"), valorar("500")).status is LevelStatus.WITHIN
    assert vigilar(tesis(objetivo="130"), valorar("131")).status is LevelStatus.TARGET


def test_sin_stop_ni_objetivo_no_hay_nada_que_vigilar():
    c = vigilar(tesis(entrada="100"), valorar("100", hace=HACE_DOS_DIAS))
    assert c.status is LevelStatus.NO_LEVELS
    assert c.alert(HOY) is None


# -- divisas: todo en EUR -----------------------------------------------------------------------


def test_niveles_en_usd_con_cotizacion_en_eur():
    valoracion = valorar("100")  # 100 € por acción
    t = tesis("110", "150", divisa="USD")
    # Con el dólar a 0,90 €, el stop son 99 €: el precio sigue por encima.
    c = vigilar(t, valoracion, {"USD": cambio("USD", "0.90")})
    assert c.status is LevelStatus.WITHIN
    assert c.stop_eur == D("99.00")
    # Con el dólar a 0,95 €, el stop son 104,50 €: salta.
    c = vigilar(t, valoracion, {"USD": cambio("USD", "0.95")})
    assert c.status is LevelStatus.STOP
    assert c.stop_eur == D("104.50")
    assert alert_title(c) == "Stop alcanzado: ACME a 100,00 € (stop 110,00 USD (104,50 €))"


def test_objetivo_en_usd_con_cotizacion_en_eur_y_su_propuesta_en_usd():
    c = vigilar(tesis("80", "120", entrada="100", divisa="USD"), valorar("100"),
                {"USD": cambio("USD", "0.80")}, riesgo=D("20"))
    # 100 € son 125 USD: pasa del objetivo de 120 USD (96 €).
    assert c.status is LevelStatus.TARGET
    assert c.price_in_levels == D("125")
    assert c.proposal.stop == D("105.00")  # 125 − 20, en USD
    assert c.proposal.criterion is StopCriterion.TRAILING


def test_niveles_en_eur_con_cotizacion_en_usd():
    cambios = {"USD": cambio("USD", "0.9")}
    valoracion = valorar("200", "USD", cambios=cambios)  # 180 € por acción
    assert vigilar(tesis("185", "300"), valoracion, cambios).status is LevelStatus.STOP
    c = vigilar(tesis("175", "300"), valoracion, cambios)
    assert c.status is LevelStatus.WITHIN
    assert c.price_eur == D("180.0")


def test_niveles_en_la_misma_divisa_extranjera():
    cambios = {"USD": cambio("USD", "0.85")}
    valoracion = valorar("215.4", "USD", cambios=cambios)
    assert vigilar(tesis("215.4", "300", divisa="USD"), valoracion, cambios).status is (
        LevelStatus.STOP
    )
    c = vigilar(tesis("200", "215.4", divisa="USD"), valoracion, cambios)
    assert c.status is LevelStatus.TARGET
    assert c.price_in_levels == D("215.4")  # sin pasar por EUR: el mismo número


def test_peniques_de_londres():
    cambios = {"GBP": cambio("GBP", "1.2")}
    valoracion = valorar("250", "GBp", cambios=cambios)  # 250 peniques = 3,00 €
    assert vigilar(tesis("260", "400", divisa="GBp"), valoracion, cambios).status is (
        LevelStatus.STOP
    )
    # Niveles en libras: 2,40 GBP = 2,88 € (sigue por encima); 2,60 GBP = 3,12 € (salta).
    assert vigilar(tesis("2.4", "4", divisa="GBP"), valoracion, cambios).status is (
        LevelStatus.WITHIN
    )
    assert vigilar(tesis("2.6", "4", divisa="GBP"), valoracion, cambios).status is (
        LevelStatus.STOP
    )


# -- NO VERIFICABLE -----------------------------------------------------------------------------


def test_precio_antiguo_no_es_verificable():
    c = vigilar(tesis("100", "130"), valorar("50", hace=HACE_DOS_DIAS))
    assert c.status is LevelStatus.UNVERIFIABLE  # nunca «a salvo», aunque esté muy abajo
    assert "24 horas" in c.note
    assert c.price_eur is None and c.alert(HOY) is None


def test_sin_simbolo_no_es_verificable():
    c = vigilar(tesis("100", "130"), valorar("95", simbolo=None))
    assert c.status is LevelStatus.UNVERIFIABLE
    assert "símbolo" in c.note


def test_sin_cambio_del_precio_no_es_verificable():
    c = vigilar(tesis("100", "130", divisa="USD"), valorar("95", "USD"))  # sin cambio USD
    assert c.status is LevelStatus.UNVERIFIABLE


def test_sin_cambio_de_la_divisa_de_los_niveles_no_es_verificable():
    c = vigilar(tesis("110", "150", divisa="USD"), valorar("100"))
    assert c.status is LevelStatus.UNVERIFIABLE
    assert c.note == "Sin tipo de cambio USD→EUR para los niveles en USD."


def test_cambio_antiguo_de_los_niveles_no_es_verificable():
    c = vigilar(tesis("110", "150", divisa="USD"), valorar("100"),
                {"USD": cambio("USD", "0.95", HACE_DOS_DIAS)})
    assert c.status is LevelStatus.UNVERIFIABLE
    assert "más de 24 horas" in c.note


def test_precio_de_cache_si_es_verificable():
    c = vigilar(tesis("100", "130"), valorar("95", hace=timedelta(hours=5)))
    assert c.status is LevelStatus.STOP


# -- el stop propuesto ---------------------------------------------------------------------------


def test_propuesta_break_even():
    # Entrada 100, stop 90, riesgo 10; a 105, el trailing daría 95: gana la entrada.
    p = propose_stop(D("105"), D("100"), D("90"), D("10"))
    assert (p.stop, p.criterion, p.raises) == (D("100"), StopCriterion.BREAK_EVEN, True)


def test_propuesta_trailing_mantiene_el_riesgo_inicial():
    p = propose_stop(D("121"), D("100"), D("90"), D("10"))
    assert (p.stop, p.criterion) == (D("111"), StopCriterion.TRAILING)


def test_en_un_empate_gana_el_break_even():
    p = propose_stop(D("110"), D("100"), D("90"), D("10"))
    assert (p.stop, p.criterion) == (D("100"), StopCriterion.BREAK_EVEN)


def test_propuesta_sin_entrada_es_precio_menos_8():
    p = propose_stop(D("200"), None, D("150"), None)
    assert (p.stop, p.criterion) == (D("184.00"), StopCriterion.MINUS_8)


def test_propuesta_sin_stop_es_precio_menos_8():
    p = propose_stop(D("200"), D("100"), None, None)
    assert (p.stop, p.criterion, p.current, p.raises) == (
        D("184.00"), StopCriterion.MINUS_8, None, True
    )


def test_la_propuesta_nunca_baja_del_stop_vigente():
    # Con el stop ya en 115, ni el break-even (100) ni el trailing (111) lo suben.
    p = propose_stop(D("121"), D("100"), D("115"), D("10"))
    assert (p.stop, p.criterion, p.raises) == (D("115"), StopCriterion.CURRENT_STOP, False)
    # Tampoco el −8 %: 184 queda por debajo del stop de 190.
    p = propose_stop(D("200"), None, D("190"), None)
    assert (p.stop, p.criterion, p.raises) == (D("190"), StopCriterion.CURRENT_STOP, False)


def test_un_candidato_por_encima_del_precio_no_vale():
    # Una entrada por encima del precio no puede ser el stop: saltaría en el acto.
    p = propose_stop(D("125"), D("130"), D("90"), D("10"))
    assert (p.stop, p.criterion) == (D("115"), StopCriterion.TRAILING)


def test_sin_riesgo_inicial_solo_queda_el_break_even():
    p = propose_stop(D("125"), D("100"), D("90"), None)
    assert (p.stop, p.criterion) == (D("100"), StopCriterion.BREAK_EVEN)


def test_los_stops_calculados_se_redondean_hacia_abajo():
    assert propose_stop(D("121.559"), D("100"), D("90"), D("10")).stop == D("111.55")
    assert propose_stop(D("0.5"), None, None, None).stop == D("0.4600")


def test_el_objetivo_lleva_su_propuesta():
    c = vigilar(tesis("90", "120", entrada="100"), valorar("121"), riesgo=D("10"))
    assert c.status is LevelStatus.TARGET
    assert (c.proposal.stop, c.proposal.criterion) == (D("111"), StopCriterion.TRAILING)
    aviso = c.alert(HOY)
    assert aviso.kind is LevelAlertKind.TARGET
    assert (aviso.price_eur, aviso.level_eur) == (D("121"), D("120"))
    assert (aviso.proposed_stop, aviso.criterion) == (D("111"), "TRAILING")
    assert proposal_text(c) == (
        "No obliga a vender. Sharky propone subir el stop a 111,00 € (mantiene el riesgo "
        "inicial)."
    )


# -- el riesgo inicial sale del historial ----------------------------------------------------


def evento(tipo, nuevo=None, viejo=None, *, id, autor=Author.USER, ref=None, texto=""):
    return ThesisEvent(1, AHORA, tipo, autor, texto,
                       old_value=to_json(viejo) if viejo is not None else None,
                       new_value=to_json(nuevo) if nuevo is not None else None,
                       ref_event_id=ref, id=id)


def creada(**valores):
    base = {"entrada": None, "stop": None, "objetivo": None, "conviccion": None, "divisa": "EUR"}
    return evento(ThesisEventKind.CREATED, {**base, **valores}, id=1)


def test_el_riesgo_inicial_es_el_de_la_creacion_aunque_se_suba_el_stop():
    eventos = [
        creada(entrada="100", stop="90", objetivo="120"),
        evento(ThesisEventKind.LEVELS_CHANGED, {"stop": "111"}, {"stop": "90"}, id=2),
    ]
    assert initial_risk(eventos, "EUR") == D("10")
    # Con el stop vigente (111) daría 125 − (100 − 111) = 136, por encima del precio.
    c = vigilar(tesis("111", "120", entrada="100"), valorar("125"),
                riesgo=initial_risk(eventos, "EUR"))
    assert c.proposal.stop == D("115")


def test_el_riesgo_inicial_llega_cuando_la_tesis_tiene_entrada_y_stop():
    eventos = [
        creada(entrada="100"),
        evento(ThesisEventKind.LEVELS_CHANGED, {"stop": "80"}, {"stop": None}, id=2),
        evento(ThesisEventKind.LEVELS_CHANGED, {"stop": "95"}, {"stop": "80"}, id=3),
    ]
    assert initial_risk(eventos, "EUR") == D("20")


def test_cambiar_la_divisa_de_los_niveles_reinicia_el_riesgo_inicial():
    eventos = [
        creada(entrada="100", stop="90"),
        evento(ThesisEventKind.LEVELS_CHANGED,
               {"divisa": "USD", "entrada": "110", "stop": "95"},
               {"divisa": "EUR", "entrada": "100", "stop": "90"}, id=2),
    ]
    assert initial_risk(eventos, "USD") == D("15")


def test_sin_riesgo_inicial_valido():
    assert initial_risk([], "EUR") is None
    assert initial_risk([creada(entrada="100", stop="100")], "EUR") is None
    assert initial_risk([creada(entrada="100")], "EUR") is None


# -- varias tesis a la vez ---------------------------------------------------------------------


def test_solo_se_vigilan_las_tesis_activas_con_posicion_abierta():
    valoracion = valorar("95")
    tesis_cerrada = tesis("100", "130", ticker="ACME", id=1, estado=ThesisStatus.CLOSED)
    sin_posicion = tesis("100", "130", ticker="OTRA", id=2)
    assert evaluate_levels([tesis_cerrada, sin_posicion], valoracion, {}) == ()
    activa = tesis("100", "130", ticker="ACME", id=3)
    [c] = evaluate_levels([tesis_cerrada, sin_posicion, activa], valoracion, {})
    assert c.thesis.id == 3 and c.status is LevelStatus.STOP


def test_la_vigilancia_usa_el_historial_de_cada_tesis():
    t = tesis("111", "120", entrada="100", id=7)
    historial = {7: [creada(entrada="100", stop="90", objetivo="120")]}
    [c] = evaluate_levels([t], valorar("125"), {}, historial)
    assert (c.proposal.stop, c.proposal.criterion) == (D("115"), StopCriterion.TRAILING)


# -- validación -----------------------------------------------------------------------------------


def test_validar_los_numeros_de_una_tesis():
    assert validate_levels(D("100"), D("90"), D("120"), 8) == []
    assert validate_levels(None, None, None, None) == []
    assert validate_levels(D("0"), D("-1"), D("120"), 11) == [
        "La entrada tiene que ser mayor que 0.",
        "El stop tiene que ser mayor que 0.",
        "La convicción va de 1 a 10.",
    ]
    assert validate_levels(None, D("120"), D("120"), None) == [
        "El stop (120,00) tiene que quedar por debajo del objetivo (120,00)."
    ]
    assert validate_levels(None, D("130"), D("120"), None, check_order=False) == []


def test_un_stop_por_encima_del_precio_no_es_un_error():
    # Así se prueba el aviso: el stop por encima del precio actual salta, no se rechaza.
    assert validate_levels(D("100"), D("500"), D("600"), None) == []


# -- propuestas e historial ----------------------------------------------------------------------


def propuesta(id, autor=Author.SYSTEM, stop="111"):
    return evento(ThesisEventKind.PROPOSAL, {"stop": stop, "divisa": "EUR"}, {"stop": "90"},
                  id=id, autor=autor, texto="Objetivo alcanzado.")


def test_estado_de_las_propuestas():
    eventos = [
        creada(entrada="100", stop="90"),
        propuesta(2),
        evento(ThesisEventKind.LEVELS_CHANGED, {"stop": "111"}, {"stop": "90"}, id=3, ref=2),
        propuesta(4, stop="115"),
        evento(ThesisEventKind.REVIEW, id=5, ref=4),
        propuesta(6, stop="118"),
        propuesta(7, stop="120"),
        propuesta(8, autor=Author.CLAUDE, stop="100"),
    ]
    assert proposal_states(eventos) == {
        2: ProposalState.APPLIED,
        4: ProposalState.DISCARDED,
        6: ProposalState.SUPERSEDED,
        7: ProposalState.PENDING,
        8: ProposalState.PENDING,  # la de Claude no sustituye a la de Sharky
    }


def test_cuando_no_se_puede_aplicar_una_propuesta():
    p = propuesta(2, stop="111")
    assert proposal_blocker(p, tesis("90", "120", entrada="100")) == ""
    assert proposal_blocker(p, tesis("115", "130")) == (
        "Tu stop ya es igual o más alto que el propuesto."
    )
    assert "ahora van en USD" in proposal_blocker(p, tesis("90", "120", divisa="USD"))
    assert proposal_blocker(p, tesis("90", "120", estado=ThesisStatus.CLOSED)) == (
        "La tesis está cerrada."
    )


def test_la_propuesta_del_objetivo_va_al_historial():
    c = vigilar(tesis("90", "120", entrada="100"), valorar("121"), riesgo=D("10"))
    e = target_proposal_event(c, 1, AHORA)
    assert (e.kind, e.author) == (ThesisEventKind.PROPOSAL, Author.SYSTEM)
    assert from_json(e.new_value) == {"stop": "111.00", "divisa": "EUR"}
    assert from_json(e.old_value) == {"stop": "90"}
    assert e.text == "Objetivo alcanzado a 121,00 €. Criterio: mantiene el riesgo inicial."
    # Si no sube el stop, no hay nada que proponer.
    c = vigilar(tesis("115", "120", entrada="100"), valorar("121"), riesgo=D("10"))
    assert target_proposal_event(c, 1, AHORA) is None


def test_cambios_solo_con_lo_que_cambia():
    antes = levels_of(tesis("610", "950", entrada="628"))
    despues = levels_of(tesis("640.00", "950", entrada="628"))
    assert changes(antes, despues) == ({"stop": "610"}, {"stop": "640.00"})
    assert changes(antes, levels_of(tesis("610.0", "950.00", entrada="628"))) == ({}, {})


def test_el_historial_se_lee_del_mas_reciente_al_mas_antiguo():
    eventos = [
        evento(ThesisEventKind.CREATED,
               {"entrada": "628", "stop": "610", "objetivo": "950", "conviccion": 8,
                "divisa": "EUR"}, id=1, texto="Tesis creada con el alta rápida"),
        evento(ThesisEventKind.LEVELS_CHANGED, {"stop": "640"}, {"stop": "610"}, id=2,
               texto="el mínimo de octubre aguantó dos veces"),
        propuesta(3, stop="690"),
        evento(ThesisEventKind.REVIEW, id=4, ref=3),
        evento(ThesisEventKind.REVIEW, {"riesgos": "Nuevo"}, {"riesgos": ""}, id=5),
    ]
    lineas = [(h.title, h.detail) for h in history(eventos)]
    assert lineas == [
        ("Textos editados por ti: riesgos", ""),
        ("Propuesta descartada por ti", ""),
        ("Propuesta de Sharky: stop 690,00 EUR", "Objetivo alcanzado."),
        ("Stop cambiado por ti: 610,00 → 640,00",
         "Motivo: el mínimo de octubre aguantó dos veces"),
        ("Tesis creada con el alta rápida",
         "Entrada 628,00 EUR · stop 610,00 · objetivo 950,00 · convicción 8"),
    ]
    assert history(eventos)[2].state is ProposalState.DISCARDED


def test_titulos_de_otros_cambios():
    varios = evento(ThesisEventKind.LEVELS_CHANGED, {"stop": "95", "objetivo": "140"},
                    {"stop": "90", "objetivo": "130"}, id=2)
    assert describe_event(varios).title == (
        "Niveles cambiados por ti: stop 90,00 → 95,00 · objetivo 130,00 → 140,00"
    )
    conviccion = evento(ThesisEventKind.LEVELS_CHANGED, {"conviccion": 9}, {"conviccion": 8},
                        id=3)
    assert describe_event(conviccion).title == "Convicción cambiada por ti: 8 → 9"
    aplicada = evento(ThesisEventKind.LEVELS_CHANGED, {"stop": "111"}, {"stop": "90"}, id=4,
                      ref=2)
    assert describe_event(aplicada).title == "Propuesta aplicada por ti: stop 90,00 → 111,00"


# -- textos de los avisos -------------------------------------------------------------------------


def test_textos_del_aviso_de_stop():
    valoracion = valorar("5.12", unidades="200", coste="900")
    c = vigilar(tesis("5.20", "7"), valoracion)
    assert alert_title(c) == "Stop alcanzado: ACME a 5,12 € (stop 5,20 €)"
    assert stop_message(c) == (
        "ACME cotiza a 5,12 € y su tesis fija el stop en 5,20 €. Salida obligatoria del "
        "mandato: liquida en tu bróker y registra la venta."
    )
    assert position_line(c) == "Valor de la posición: 1.024,00 € · PnL +124,00 €"
    assert notification(c)[0] == "Stop-loss alcanzado en ACME"


def test_textos_del_aviso_de_objetivo():
    c = vigilar(tesis("640", "950", entrada="628"), valorar("955"), riesgo=D("18"))
    assert alert_title(c) == "Objetivo alcanzado: ACME a 955,00 € (objetivo 950,00 €)"
    titulo, texto = notification(c)
    assert titulo == "Objetivo alcanzado en ACME"
    assert texto == (
        "No obliga a vender. Sharky propone subir el stop a 937,00 € (mantiene el riesgo "
        "inicial)."
    )


@pytest.mark.parametrize("stop", ["960", "990"])
def test_con_el_stop_por_encima_del_objetivo_y_del_precio_manda_el_stop(stop):
    c = vigilar(tesis(stop, "950", entrada="628", id=1), valorar("955"))
    assert c.status is LevelStatus.STOP
