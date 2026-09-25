"""Tesis guardadas y avisos de niveles (GUIA §5.6 y §7, H7): alta rápida, historial con el valor
anterior y el nuevo, propuestas que se aplican o se descartan y un aviso por ticker, tipo y día.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from fakes import FakeMarket
from sharky.core.levels import LevelStatus, ProposalState, from_json, proposal_states
from sharky.core.models import (
    Asset,
    Author,
    CashKind,
    CashMovement,
    LevelAlertKind,
    Price,
    PriceSource,
    Thesis,
    ThesisEventKind,
    ThesisStatus,
    Trade,
    TradeKind,
)
from sharky.services.market import FxQuote, Quote, load_valuation, refresh_market
from sharky.services.repositories import (
    AssetRepository,
    CashMovementRepository,
    LevelAlertRepository,
    PriceRepository,
    ThesisEdit,
    ThesisError,
    ThesisEventRepository,
    ThesisRepository,
    TradeRepository,
    apply_proposal,
    create_theses,
    discard_proposal,
    record_levels,
    update_thesis,
)
from sharky.services.settings import MandateSettings
from sharky.ui.portfolio import refresh_and_value

MADRID = timezone(timedelta(hours=2))
AHORA = datetime(2026, 9, 25, 18, 0, tzinfo=MADRID)
HOY = AHORA.date()
MANANA = AHORA + timedelta(days=1)


@pytest.fixture
def cartera(db):
    """ACME (EUR) con 10 acciones a 100 € y ZETA (USD) con 5 a 90 €; 5.000 € de efectivo."""
    with db.transaction() as conn:
        for ticker, divisa, unidades, coste in (("ACME", "EUR", "10", "100"),
                                                 ("ZETA", "USD", "5", "90")):
            AssetRepository(conn).add(Asset(ticker, f"{ticker} Corp", divisa,
                                            yahoo_symbol=f"{ticker}.X"))
            TradeRepository(conn).add(Trade(HOY - timedelta(days=30), ticker, TradeKind.OPENING,
                                            D(unidades), D(coste), "EUR", D(1), D(0),
                                            D(unidades) * D(coste)))
        CashMovementRepository(conn).add(CashMovement(HOY - timedelta(days=30),
                                                      CashKind.INITIAL, D("5000")))
    return db


def precio(db, ticker, valor, cuando=AHORA, divisa="EUR"):
    with db.transaction() as conn:
        PriceRepository(conn).save(Price(ticker, cuando.date(), D(valor), divisa,
                                         PriceSource.MARKET, cuando))


def alta(db, *theses, cuando=AHORA):
    with db.transaction() as conn:
        return create_theses(conn, list(theses), cuando)


def tesis(ticker="ACME", stop=None, objetivo=None, entrada=None, divisa="EUR", conviccion=None):
    def d(x):
        return D(x) if x is not None else None

    return Thesis(ticker, divisa, HOY, entry_price=d(entrada), stop=d(stop), target=d(objetivo),
                  conviction=conviccion)


def vigilar(db, cuando=AHORA):
    valoracion = load_valuation(db.connection(), cuando, cuando)
    with db.transaction() as conn:
        return record_levels(conn, valoracion, cuando)


def eventos(db, id_tesis):
    return ThesisEventRepository(db.connection()).list_for(id_tesis)


def edicion(t, **cambios):
    datos = {
        "entry_price": t.entry_price, "stop": t.stop, "target": t.target,
        "conviction": t.conviction, "levels_currency": t.levels_currency, "why": t.why,
        "catalysts": t.catalysts, "risks": t.risks, "invalidation": t.invalidation,
    }
    datos.update(cambios)
    return ThesisEdit(**datos)


# -- alta rápida --------------------------------------------------------------------------------


def test_alta_rapida_crea_las_tesis_con_su_evento_de_creacion(cartera):
    ids = alta(cartera, tesis("ACME", "90", "130", "100"), tesis("ZETA", stop="80"))
    guardadas = ThesisRepository(cartera.connection()).list_active()
    assert [t.ticker for t in guardadas] == ["ACME", "ZETA"]
    acme = ThesisRepository(cartera.connection()).get(ids[0])
    assert (acme.entry_price, acme.stop, acme.target, acme.status) == (
        D("100"), D("90"), D("130"), ThesisStatus.ACTIVE
    )
    [creacion] = eventos(cartera, ids[0])
    assert (creacion.kind, creacion.author) == (ThesisEventKind.CREATED, Author.USER)
    assert creacion.text == "Tesis creada con el alta rápida"
    assert from_json(creacion.new_value) == {
        "entrada": "100", "stop": "90", "objetivo": "130", "conviccion": None, "divisa": "EUR"
    }


def test_alta_rapida_entra_entera_o_no_entra_y_da_todos_los_errores(cartera):
    alta(cartera, tesis("ZETA", stop="80"))
    with pytest.raises(ThesisError) as error:
        alta(cartera, tesis("ACME", "130", "120"), tesis("ZETA", stop="70"),
             tesis("NADA", stop="1"), tesis("ACME", stop="-1", divisa="EURO"))
    assert error.value.errors == [
        "ACME: El stop (130,00) tiene que quedar por debajo del objetivo (120,00).",
        "ZETA: ya tiene una tesis activa.",
        "NADA: no existe ese activo.",
        "ACME: El stop tiene que ser mayor que 0.",
        "ACME: «EURO» no es una divisa.",
        "ACME: ya tiene una tesis activa.",
    ]
    assert [t.ticker for t in ThesisRepository(cartera.connection()).list_active()] == ["ZETA"]


# -- editar: cada cambio queda en el historial -----------------------------------------------


def test_cambiar_numeros_deja_el_valor_anterior_el_nuevo_y_el_motivo(cartera):
    [id_tesis] = alta(cartera, tesis("ACME", "610", "950", "628", conviccion=8))
    t = ThesisRepository(cartera.connection()).get(id_tesis)
    with cartera.transaction() as conn:
        update_thesis(conn, id_tesis, edicion(t, stop=D("640")), AHORA,
                      "el mínimo de octubre aguantó dos veces")
    cambio = eventos(cartera, id_tesis)[-1]
    assert (cambio.kind, cambio.author) == (ThesisEventKind.LEVELS_CHANGED, Author.USER)
    assert from_json(cambio.old_value) == {"stop": "610"}
    assert from_json(cambio.new_value) == {"stop": "640"}
    assert cambio.text == "el mínimo de octubre aguantó dos veces"
    assert ThesisRepository(cartera.connection()).get(id_tesis).stop == D("640")


def test_cambiar_textos_deja_una_revision_y_sin_cambios_no_se_escribe_nada(cartera):
    [id_tesis] = alta(cartera, tesis("ACME", "90"))
    t = ThesisRepository(cartera.connection()).get(id_tesis)
    with cartera.transaction() as conn:
        assert update_thesis(conn, id_tesis, edicion(t, stop=D("90.00")), AHORA) == ()
        update_thesis(conn, id_tesis, edicion(t, why="  Foso de verdad  "), AHORA)
    revision = eventos(cartera, id_tesis)[-1]
    assert (revision.kind, revision.author) == (ThesisEventKind.REVIEW, Author.USER)
    assert from_json(revision.old_value) == {"por_que": ""}
    assert from_json(revision.new_value) == {"por_que": "Foso de verdad"}
    assert len(eventos(cartera, id_tesis)) == 2


def test_los_numeros_se_validan_al_editar(cartera):
    [id_tesis] = alta(cartera, tesis("ACME", "90", "130"))
    t = ThesisRepository(cartera.connection()).get(id_tesis)
    with pytest.raises(ThesisError, match="por debajo del objetivo"), cartera.transaction() as conn:
        update_thesis(conn, id_tesis, edicion(t, stop=D("140")), AHORA)
    with pytest.raises(ThesisError, match="de 1 a 10"), cartera.transaction() as conn:
        update_thesis(conn, id_tesis, edicion(t, conviction=0), AHORA)
    assert len(eventos(cartera, id_tesis)) == 1


def test_una_tesis_cerrada_no_se_edita(cartera):
    with cartera.transaction() as conn:
        id_tesis = ThesisRepository(conn).add(
            Thesis("ACME", "EUR", HOY, stop=D("90"), status=ThesisStatus.CLOSED, closed_on=HOY)
        )
    t = ThesisRepository(cartera.connection()).get(id_tesis)
    with pytest.raises(ThesisError, match="cerrada"), cartera.transaction() as conn:
        update_thesis(conn, id_tesis, edicion(t, stop=D("95")), AHORA)


# -- avisos: uno por ticker, tipo y día ----------------------------------------------------


def test_un_stop_se_avisa_una_vez_al_dia(cartera):
    alta(cartera, tesis("ACME", "100", "130"))
    precio(cartera, "ACME", "95")
    primero = vigilar(cartera)
    assert [(a.ticker, a.kind) for a in primero.new_alerts] == [("ACME", LevelAlertKind.STOP)]
    assert (primero.new_alerts[0].price_eur, primero.new_alerts[0].level_eur) == (D("95"), D("100"))
    assert vigilar(cartera).new_alerts == ()  # el mismo día, nada nuevo
    repo = LevelAlertRepository(cartera.connection())
    assert [a.ticker for a in repo.pending(HOY)] == ["ACME"]
    with cartera.transaction() as conn:
        LevelAlertRepository(conn).mark_notified([primero.new_alerts[0].id])
    assert repo.pending(HOY) == []
    # Al día siguiente, con el precio aún por debajo, hay un aviso nuevo.
    precio(cartera, "ACME", "94", cuando=MANANA)
    siguiente = vigilar(cartera, MANANA)
    assert [a.alert_date for a in siguiente.new_alerts] == [MANANA.date()]


def test_sin_precio_fiable_no_se_guarda_aviso(cartera):
    alta(cartera, tesis("ACME", "100", "130"))
    precio(cartera, "ACME", "95", cuando=AHORA - timedelta(days=2))
    registro = vigilar(cartera)
    assert [c.status for c in registro.checks] == [LevelStatus.UNVERIFIABLE]
    assert registro.new_alerts == ()


def test_el_objetivo_guarda_su_propuesta_en_el_historial(cartera):
    [id_tesis] = alta(cartera, tesis("ACME", "90", "120", "100"))
    precio(cartera, "ACME", "121")
    registro = vigilar(cartera)
    [aviso] = registro.new_alerts
    assert (aviso.kind, aviso.proposed_stop, aviso.criterion) == (
        LevelAlertKind.TARGET, D("111.00"), "TRAILING"
    )
    [id_propuesta] = registro.proposals
    propuesta = ThesisEventRepository(cartera.connection()).get(id_propuesta)
    assert (propuesta.kind, propuesta.author) == (ThesisEventKind.PROPOSAL, Author.SYSTEM)
    assert from_json(propuesta.new_value) == {"stop": "111.00", "divisa": "EUR"}
    # Mañana, al mismo precio, se avisa otra vez pero no se repite la misma propuesta.
    precio(cartera, "ACME", "121", cuando=MANANA)
    registro = vigilar(cartera, MANANA)
    assert len(registro.new_alerts) == 1 and registro.proposals == ()
    # Si sigue subiendo, la propuesta nueva sustituye a la anterior.
    pasado = MANANA + timedelta(days=1)
    precio(cartera, "ACME", "125", cuando=pasado)
    [id_nueva] = vigilar(cartera, pasado).proposals
    estados = proposal_states(eventos(cartera, id_tesis))
    assert estados == {id_propuesta: ProposalState.SUPERSEDED, id_nueva: ProposalState.PENDING}


# -- aplicar y descartar ---------------------------------------------------------------------


def objetivo_alcanzado(db, a="121"):
    [id_tesis] = alta(db, tesis("ACME", "90", "120", "100"))
    precio(db, "ACME", a)
    [id_propuesta] = vigilar(db).proposals
    return id_tesis, id_propuesta


def test_aplicar_la_propuesta_sube_el_stop_y_lo_deja_en_el_historial(cartera):
    id_tesis, id_propuesta = objetivo_alcanzado(cartera)
    with cartera.transaction() as conn:
        nueva = apply_proposal(conn, id_propuesta, AHORA)
    assert nueva.stop == D("111.00")
    assert ThesisRepository(cartera.connection()).get(id_tesis).stop == D("111.00")
    aplicada = eventos(cartera, id_tesis)[-1]
    assert (aplicada.kind, aplicada.author, aplicada.ref_event_id) == (
        ThesisEventKind.LEVELS_CHANGED, Author.USER, id_propuesta
    )
    assert from_json(aplicada.old_value) == {"stop": "90"}
    assert proposal_states(eventos(cartera, id_tesis))[id_propuesta] is ProposalState.APPLIED
    with pytest.raises(ThesisError, match="ya se aplicó"), cartera.transaction() as conn:
        apply_proposal(conn, id_propuesta, AHORA)


def test_descartar_la_propuesta_no_toca_los_numeros(cartera):
    id_tesis, id_propuesta = objetivo_alcanzado(cartera)
    with cartera.transaction() as conn:
        discard_proposal(conn, id_propuesta, AHORA)
    assert ThesisRepository(cartera.connection()).get(id_tesis).stop == D("90")
    descarte = eventos(cartera, id_tesis)[-1]
    assert (descarte.kind, descarte.author, descarte.ref_event_id) == (
        ThesisEventKind.REVIEW, Author.USER, id_propuesta
    )
    with pytest.raises(ThesisError, match="ya se descartó"), cartera.transaction() as conn:
        apply_proposal(conn, id_propuesta, AHORA)


def test_una_propuesta_no_puede_bajar_el_stop(cartera):
    id_tesis, id_propuesta = objetivo_alcanzado(cartera)
    t = ThesisRepository(cartera.connection()).get(id_tesis)
    with cartera.transaction() as conn:
        update_thesis(conn, id_tesis, edicion(t, stop=D("115")), AHORA)
    with pytest.raises(ThesisError, match="igual o más alto"), cartera.transaction() as conn:
        apply_proposal(conn, id_propuesta, AHORA)
    assert ThesisRepository(cartera.connection()).get(id_tesis).stop == D("115")


def test_aplicar_puede_dejar_el_stop_por_encima_del_objetivo(cartera):
    # A 150 con el objetivo en 120, el stop propuesto (140) pasa del objetivo: vale.
    id_tesis, id_propuesta = objetivo_alcanzado(cartera, a="150")
    with cartera.transaction() as conn:
        apply_proposal(conn, id_propuesta, AHORA)
    t = ThesisRepository(cartera.connection()).get(id_tesis)
    assert (t.stop, t.target) == (D("140.00"), D("120"))


# -- con la actualización de precios ----------------------------------------------------------


def test_niveles_en_usd_de_una_accion_en_eur_piden_el_cambio_del_dolar(cartera):
    alta(cartera, tesis("ACME", "110", "150", divisa="USD"))
    mercado = FakeMarket(
        {"ACME.X": Quote("ACME.X", D("100"), "EUR", HOY)},
        {"USD": FxQuote("USD", D("0.95"), HOY)},
    )
    refresh_market(cartera, mercado, mercado, AHORA)
    assert mercado.asked_fx == [["USD"]]
    [c] = vigilar(cartera).checks
    assert c.status is LevelStatus.STOP  # 110 USD = 104,50 €, por encima de 100 €


def test_actualizar_precios_guarda_los_avisos_con_la_foto_del_dia(cartera):
    alta(cartera, tesis("ACME", "100", "130"))
    mercado = FakeMarket(
        {"ACME.X": Quote("ACME.X", D("95"), "EUR", HOY),
         "ZETA.X": Quote("ZETA.X", D("20"), "USD", HOY)},
        {"USD": FxQuote("USD", D("0.9"), HOY)},
    )
    resultado = refresh_and_value(cartera, mercado, mercado, lambda: AHORA,
                                  MandateSettings().rules())
    assert [a.kind for a in resultado.levels.new_alerts] == [LevelAlertKind.STOP]
    assert [a.ticker for a in LevelAlertRepository(cartera.connection()).list_for_date(HOY)] == [
        "ACME"
    ]
