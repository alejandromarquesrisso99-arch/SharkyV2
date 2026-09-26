"""El radar en services (GUIA §5.8 y §5.9, H11): el filtro diario sobre la lista de vigilancia,
el explorador con un Claude de mentira, la caché del histórico y la compra desde una alerta.

Cartera inventada de tests/test_trades.py (hoy es el viernes 25/09/2026): SAN, IWDA y AAPL. Los
históricos de Yahoo son series inventadas (tests/fakes.py): CCJ cae de 101 a 78 y da alerta; NVO
cae solo a 88 y no llega al ratio; KRKNF no tiene histórico.
"""

from datetime import date, timedelta
from decimal import Decimal as D

import pydantic
import pytest

from fakes import (
    FakeMarket,
    FakeYahoo,
    busqueda,
    connection_error,
    extraccion,
    historico_yahoo,
    mensaje_claude,
    subida_y_caida,
    texto_citado,
)
from sharky.core.models import (
    AlertOrigin,
    AlertStatus,
    PriceSource,
    RadarAlert,
    ReportKind,
    RunStatus,
    WatchlistItem,
    WatchlistSource,
)
from sharky.core.reports import NO_AI_LABEL
from sharky.services.ai import ClaudeClient
from sharky.services.explorer import (
    ExplorationBlocked,
    ExplorerCandidate,
    ExplorerCandidates,
    RadarError,
    add_to_watchlist,
    create_exploration,
    discard_alert,
    refresh_radar,
    remove_from_watchlist,
    watch_candidate,
)
from sharky.services.market import load_histories
from sharky.services.repositories import (
    PriceHistoryRepository,
    RadarAlertRepository,
    ReportRepository,
    RunRepository,
    ThesisRepository,
    WatchlistRepository,
    load_valuation,
)
from sharky.services.settings import Settings
from test_trades import AHORA, HOY, compra, crear_cartera, registrar

CLAVE = "sk-ant-inventada-0000"


def mercado(**extra) -> FakeMarket:
    return FakeMarket(histories={
        "CCJ": historico_yahoo("CCJ", subida_y_caida(HOY), nombre="Cameco"),
        "NVO": historico_yahoo("NVO", subida_y_caida(HOY, final="88")),
        "LDO.MI": historico_yahoo("LDO.MI", subida_y_caida(HOY), divisa="EUR"),
        **extra,
    })


@pytest.fixture
def cartera(db):
    return crear_cartera(db)


def vigilar(db, *tickers):
    datos = {
        "CCJ": ("Cameco", "CCJ", "Energia"),
        "NVO": ("Novo Nordisk", "NVO", "Salud"),
        "KRKNF": ("Kraken Robotics", "KRKNF", "Defensa"),
        "LDO": ("Leonardo", "LDO.MI", "Defensa"),
    }
    with db.transaction() as conn:
        for t in tickers:
            nombre, simbolo, sector = datos[t]
            add_to_watchlist(conn, t, nombre, simbolo, sector, AHORA)


def filtro(db, ahora=AHORA, market=None, ajustes=None):
    return refresh_radar(db, market or mercado(), ajustes or Settings(), lambda: ahora)


def alertas(db):
    return RadarAlertRepository(db.connection()).list_all()


# == el paso 3: la lista de vigilancia ===========================================================


def test_el_filtro_de_la_lista_de_vigilancia(cartera):
    vigilar(cartera, "CCJ", "NVO", "KRKNF")
    hecho = filtro(cartera)
    [ccj] = hecho.new_alerts
    assert ccj.ticker == "CCJ" and ccj.origin is AlertOrigin.WATCHLIST
    assert (ccj.price, ccj.currency, ccj.stop, ccj.target) == (D("78.00"), "USD", D("71.76"),
                                                               D("101.00"))
    assert ccj.max_weight == D("0.1") and ccj.ratio == D("3.6858")
    assert (ccj.name, ccj.yahoo_symbol, ccj.sector) == ("Cameco", "CCJ", "Energia")
    descartes = {a.ticker: a for a in alertas(cartera) if a.status is AlertStatus.DISCARDED}
    assert descartes["NVO"].reason == "Ratio 1,8 · el mínimo del mandato es 2,0"
    assert descartes["KRKNF"].reason == (
        "No verificable: Yahoo no tiene cotizaciones recientes de KRKNF."
    )
    assert all(a.max_weight is None for a in descartes.values())
    # Gratis y en el Registro.
    (fila,) = RunRepository(cartera.connection()).list_recent()
    assert fila.step == "Radar" and fila.status is RunStatus.OK and fila.cost_usd == 0
    assert fila.detail == "1 alerta nueva (CCJ) · 2 descartes."


def test_lo_que_ya_esta_en_cartera_no_se_filtra(cartera):
    with cartera.transaction() as conn:  # a mano, como si se hubiera añadido antes de comprar
        WatchlistRepository(conn).add(WatchlistItem("SAN", "Santander", WatchlistSource.USER,
                                                    HOY, "SAN.MC", "EUR", "Banca"))
    market = mercado(**{"SAN.MC": historico_yahoo("SAN.MC", subida_y_caida(HOY), "EUR")})
    hecho = filtro(cartera, market=market)
    assert market.asked_history == []  # ni siquiera se descarga
    assert alertas(cartera) == [] and hecho.result.skipped[0].candidate.ticker == "SAN"


def test_la_caducidad_corre_antes_y_el_ticker_vuelve_a_alertar_hoy(cartera):
    """Con la alerta de hace 30 días sin caducar, el índice de una sola alerta activa por ticker
    impediría la nueva: caducar primero es lo que deja emitirla hoy con los niveles de hoy."""
    vigilar(cartera, "CCJ")
    with cartera.transaction() as conn:
        vieja = RadarAlertRepository(conn).add(RadarAlert(
            HOY - timedelta(days=30), "CCJ", AlertOrigin.WATCHLIST, AlertStatus.ACTIVE,
            D("70"), "USD", D("60"), D("140"), D("3"), D("0.3"), D("0.1"),
            summary="Idea antigua", yahoo_symbol="CCJ", name="Cameco",
        ))
    hecho = filtro(cartera)
    antigua = RadarAlertRepository(cartera.connection()).get(vieja)
    assert antigua.status is AlertStatus.EXPIRED
    assert antigua.reason.startswith("Caducada por edad: 30 días")
    [nueva] = hecho.new_alerts
    assert (nueva.created_on, nueva.stop, nueva.target) == (HOY, D("71.76"), D("101.00"))
    assert nueva.summary == "Idea antigua"  # la idea se hereda; los números, no
    assert "1 caducada" in hecho.run.detail


def test_una_alerta_activa_conserva_sus_niveles(cartera):
    vigilar(cartera, "CCJ")
    filtro(cartera)
    hecho = filtro(cartera, ahora=AHORA + timedelta(days=1))
    assert hecho.new_alerts == []
    [activa] = RadarAlertRepository(cartera.connection()).active()
    assert activa.created_on == HOY


def test_cada_dia_queda_un_solo_descarte_por_ticker(cartera):
    vigilar(cartera, "NVO")
    for horas in range(3):  # la rutina repite el filtro cada hora (H12)
        filtro(cartera, ahora=AHORA + timedelta(hours=horas))
    assert len(alertas(cartera)) == 1
    filtro(cartera, ahora=AHORA + timedelta(days=1))
    assert len(alertas(cartera)) == 2  # uno por día: se puede volver sobre la idea


def test_sin_conexion_no_se_llena_la_lista_de_descartes(cartera):
    vigilar(cartera, "CCJ", "NVO")
    hecho = filtro(cartera, market=FakeMarket(offline=True))
    assert alertas(cartera) == []
    assert hecho.run.status is RunStatus.SKIPPED
    assert hecho.run.detail.startswith("Sin conexión con Yahoo")


def test_sin_conexion_vale_la_cache_de_menos_de_24_horas(cartera):
    vigilar(cartera, "CCJ")
    load_histories(cartera, mercado(), ["CCJ"], AHORA)  # descargado ahora…
    luego = AHORA + timedelta(hours=5)  # … y guardado: 5 horas después es CACHE
    hecho = filtro(cartera, ahora=luego, market=FakeMarket(offline=True))
    assert [a.ticker for a in hecho.new_alerts] == ["CCJ"]
    tarde = AHORA + timedelta(days=2)
    guardado = PriceHistoryRepository(cartera.connection()).load("CCJ", tarde)
    assert guardado.source is PriceSource.STALE


def test_caduca_si_rompe_el_stop_con_el_historico_descargado(cartera):
    vigilar(cartera, "CCJ")
    with cartera.transaction() as conn:
        RadarAlertRepository(conn).add(RadarAlert(
            HOY - timedelta(days=5), "CCJ", AlertOrigin.WATCHLIST, AlertStatus.ACTIVE,
            D("85"), "USD", D("80"), D("110"), D("3"), D("0.2"), D("0.1"), yahoo_symbol="CCJ",
        ))
    hecho = filtro(cartera)
    assert hecho.result.expired[0].reason.startswith("Rompió su stop antes de que entraras")
    assert [a.ticker for a in hecho.new_alerts] == ["CCJ"]  # y vuelve con niveles de hoy


# == la lista de vigilancia y las alertas =========================================================


def test_anadir_y_quitar_de_la_lista(cartera):
    with cartera.transaction() as conn:
        item = add_to_watchlist(conn, "saab b", "Saab", "SAAB-B.ST", "Defensa europea", AHORA,
                                currency="SEK")
        assert (item.ticker, item.sector, item.currency) == ("saab-b", "Defensa_europea", "SEK")
        with pytest.raises(RadarError, match="ya está en la lista"):
            add_to_watchlist(conn, "SAAB-B", "Saab", "SAAB-B.ST", "", AHORA)
        with pytest.raises(RadarError, match="ya está en tu cartera"):
            add_to_watchlist(conn, "san", "Santander", "SAN.MC", "", AHORA)
        with pytest.raises(RadarError) as fallo:
            add_to_watchlist(conn, "", "", "", "", AHORA)
        assert len(fallo.value.errors) == 3
        assert remove_from_watchlist(conn, "saab-b")
    assert WatchlistRepository(cartera.connection()).list_all() == []


def test_descartar_una_alerta_la_aparta_hasta_que_habria_caducado(cartera):
    vigilar(cartera, "CCJ")
    [alerta] = filtro(cartera).new_alerts
    with cartera.transaction() as conn:
        descartada = discard_alert(conn, alerta.id, "  no me convence  ", AHORA)
    assert descartada.reason == "Descartada por ti el 25/09/2026: no me convence"
    hecho = filtro(cartera, ahora=AHORA + timedelta(days=1))
    assert hecho.new_alerts == []
    assert "no se vuelve a mirar hasta el 25/10/2026" in hecho.result.skipped[0].skipped
    un_mes = FakeMarket(histories={
        "CCJ": historico_yahoo("CCJ", subida_y_caida(HOY + timedelta(days=28))),
    })
    assert filtro(cartera, ahora=AHORA + timedelta(days=30), market=un_mes).new_alerts != []


def test_comprar_desde_una_alerta_la_deja_ejecutada_con_los_niveles_de_la_compra_real(cartera):
    """Criterio del H11: la alerta pasa a EJECUTADA y la tesis se abre con los niveles de la
    compra real (stop 70, objetivo 105), no con los de la alerta (71,76 y 101)."""
    vigilar(cartera, "CCJ")
    [alerta] = filtro(cartera).new_alerts
    from sharky.core.models import Asset

    nuevo = Asset("CCJ", "Cameco", "USD", yahoo_symbol="CCJ", sector="Energia")
    hecho = registrar(cartera, compra("CCJ", units="10", price="77.5", currency="USD",
                                      fx="0.85", stop="70", target="105", levels="USD",
                                      new_asset=nuevo))
    assert hecho.executed_alert == alerta.id
    ejecutada = RadarAlertRepository(cartera.connection()).get(alerta.id)
    assert ejecutada.status is AlertStatus.EXECUTED
    assert ejecutada.reason == "Comprada el 25/09/2026: 10 a 77,50 USD."
    tesis = ThesisRepository(cartera.connection()).active_for("CCJ")
    assert (tesis.stop, tesis.target, tesis.levels_currency) == (D("70"), D("105"), "USD")
    # Ya en cartera: el filtro no lo vuelve a mirar.
    assert filtro(cartera, ahora=AHORA + timedelta(hours=1)).new_alerts == []


def test_una_compra_sin_alerta_no_toca_el_radar(cartera):
    hecho = registrar(cartera, compra())
    assert hecho.executed_alert is None


# == el explorador ===============================================================================


def exploracion(*, stop="end_turn"):
    return mensaje_claude(bloques=[
        texto_citado("Voy a buscar ideas nuevas. "),
        *busqueda("uranio contratos a largo plazo", "https://ejemplo.es/ccj"),
        texto_citado("## Cameco (CCJ)\nContratos a largo plazo a precios más altos.",
                     ("https://ejemplo.es/ccj", "Cameco firma contratos")),
        texto_citado(
            "\n\n## Novo Nordisk (NVO)\nNuevos fármacos. Se podría entrar a 40 $ con stop en "
            "35 $ y objetivo 90 $.\n\n## Conclusión de la exploración\n- Mirado: energía y "
            "salud.\n- Descartado: lo que ya está caro."
        ),
    ], stop=stop, entrada=30_000, salida=4_000, busquedas=6, modelo="claude-opus-5")


def candidato(ticker, simbolo, nombre="Nombre", sector="Energía", tesis="Foso claro",
              invalida="Si cambia el ciclo"):
    return ExplorerCandidate(ticker=ticker, yahoo_symbol=simbolo, name=nombre, sector=sector,
                             thesis=tesis, invalidation=invalida)


CANDIDATOS = ExplorerCandidates(candidates=[
    candidato("CCJ", "CCJ", "Cameco",
              tesis="Contratos a largo plazo. Entrar a 40 $, stop en 35 $ y objetivo 90 $."),
    candidato("NVO", "NVO", "Novo Nordisk", "Salud"),
    candidato("SAN", "SAN.MC", "Banco Santander", "Banca"),
    candidato("KRKNF", "KRKNF", "Kraken Robotics", "Defensa"),
])


def explorar(db, clave=CLAVE, ajustes=None, market=None, **kwargs):
    valoracion = load_valuation(db.connection(), AHORA)
    return create_exploration(
        db, valoracion, ajustes or Settings(), lambda: AHORA, clave, market or mercado(),
        client_factory=lambda k: ClaudeClient(k, wait=lambda _s, _c: None), **kwargs,
    )


def test_el_candidato_del_explorador_no_tiene_donde_guardar_un_precio():
    campos = set(ExplorerCandidate.model_fields)
    assert campos == {"ticker", "yahoo_symbol", "name", "sector", "thesis", "invalidation"}
    for prohibido in ("price", "precio", "stop", "target", "objetivo", "entry", "entrada"):
        assert not any(prohibido in c for c in campos)


def test_exploracion_con_candidatos_validos(cartera, claude_falso):
    claude_falso.respuestas = [exploracion()]
    claude_falso.extracciones = [extraccion(CANDIDATOS, modelo="claude-opus-5")]
    hecho = explorar(cartera)
    informe = hecho.report
    assert informe.kind is ReportKind.EXPLORATION and informe.period == "2026-09-25"
    assert informe.used_ai and informe.model == "claude-opus-5" and informe.effort == "high"
    # Coste: A (30.000 × 5 + 4.000 × 25) / 1 M + 6 búsquedas × 10 $/1.000; B (3.000 × 5 + 400
    # × 25) / 1 M.
    assert informe.cost_usd == D("0.335")
    (fila,) = RunRepository(cartera.connection()).list_recent()
    assert fila.step == "Exploración" and fila.cost_usd == D("0.335")
    assert "4 candidatos · 1 alerta nueva (CCJ) · 2 descartes" in fila.detail
    # Las alertas, con el informe asociado y la idea del candidato.
    [ccj] = hecho.new_alerts
    assert ccj.origin is AlertOrigin.EXPLORER and ccj.report_id == informe.id
    assert (ccj.name, ccj.sector) == ("Cameco", "Energía")
    assert ccj.invalidation == "Si cambia el ciclo"
    descartes = {a.ticker: a for a in hecho.alerts if a.status is AlertStatus.DISCARDED}
    assert set(descartes) == {"NVO", "KRKNF"}
    assert "SAN" not in {a.ticker for a in alertas(cartera)}  # en cartera: fuera
    # El informe: el texto de Claude, sus fuentes y el filtro aparte.
    assert "Voy a buscar" not in informe.markdown
    assert "- [Cameco firma contratos](https://ejemplo.es/ccj)" in informe.markdown
    assert informe.conclusion.startswith("- Mirado: energía y salud.")
    assert ("| CCJ — Cameco | Energía | Alerta | 78,00 USD | 71,76 USD | 101,00 USD | 3,6 | "
            "−22,8 % |") in informe.markdown
    assert "| SAN — Banco Santander | Banca | Fuera: Ya está en tu cartera" in informe.markdown
    assert ReportRepository(cartera.connection()).get(informe.id) is not None


def test_los_precios_que_menciona_un_candidato_no_los_usa_nadie(cartera, claude_falso):
    """La tesis de CCJ dice «entrar a 40 $, stop en 35 $ y objetivo 90 $»: los niveles salen del
    filtro con el histórico real (78, 71,76 y 101)."""
    claude_falso.respuestas = [exploracion()]
    claude_falso.extracciones = [extraccion(CANDIDATOS)]
    [ccj] = explorar(cartera).new_alerts
    assert "stop en 35 $" in ccj.summary
    assert (ccj.price, ccj.stop, ccj.target) == (D("78.00"), D("71.76"), D("101.00"))
    numeros = {ccj.price, ccj.stop, ccj.target}
    assert not numeros & {D(40), D(35), D(90)}


def test_el_explorador_busca_en_la_web_con_el_contexto_de_la_cartera(cartera, claude_falso):
    vigilar(cartera, "LDO")
    claude_falso.respuestas = [exploracion(stop="pause_turn"), mensaje_claude(
        "## Conclusión de la exploración\n- Nada más.", modelo="claude-opus-5")]
    claude_falso.extracciones = [extraccion(ExplorerCandidates(candidates=[]))]
    hecho = explorar(cartera)
    primera, segunda = claude_falso.streams
    assert primera["model"] == "claude-opus-5" and primera["max_tokens"] == 32_000
    assert primera["output_config"] == {"effort": "high"}
    assert primera["tools"] == [{"type": "web_search_20250305", "name": "web_search",
                                 "max_uses": 30}]
    assert segunda["messages"][1]["role"] == "assistant"  # pause_turn reenviado tal cual
    prompt = primera["messages"][0]["content"]
    assert "- SAN — Banco Santander · Yahoo SAN.MC · Banca · peso 8,0 %" in prompt
    assert "EN VIGILANCIA: \n- LDO — Leonardo · Yahoo LDO.MI" in prompt
    assert "CON ALERTA ACTIVA: ninguna" in prompt
    # IWDA pesa un 18 %: con otra posición del 10 % pasaría del 25 % por sector.
    assert "- Renta Variable: 18,0 % de la cartera (tope 25 %)" in prompt
    assert "NO propongas precios de entrada, stops ni objetivos" in prompt
    assert "Peso máximo por activo: 10 %" in primera["system"]
    extraer = claude_falso.parses[0]
    assert extraer["output_format"] is ExplorerCandidates
    assert extraer["thinking"] == {"type": "disabled"} and extraer["max_tokens"] == 8_000
    assert "No extraigas precios, stops ni objetivos" in extraer["messages"][0]["content"]
    assert "Claude no ha propuesto ningún candidato." in hecho.report.markdown
    assert "1 reenvíos por pausa" in hecho.run.detail


@pytest.mark.parametrize("fallo", ["validacion", "red"])
def test_si_la_extraccion_falla_el_informe_se_guarda_igualmente(cartera, claude_falso, fallo):
    if fallo == "validacion":
        try:
            ExplorerCandidates.model_validate_json('{"candidates": [')
        except pydantic.ValidationError as error:
            claude_falso.extracciones = [error]
    else:
        claude_falso.extracciones = [connection_error()] * 3
    claude_falso.respuestas = [exploracion()]
    hecho = explorar(cartera)
    informe = hecho.report
    assert informe.used_ai and informe.error is None
    assert "## Cameco (CCJ)" in informe.markdown
    assert "Candidatos no disponibles:" in informe.markdown
    assert hecho.candidates_unavailable and hecho.alerts == ()
    assert alertas(cartera) == []
    assert hecho.run.status is RunStatus.OK
    assert "candidatos no disponibles" in hecho.run.detail
    assert ReportRepository(cartera.connection()).latest(ReportKind.EXPLORATION).id == informe.id


def test_si_claude_falla_la_exploracion_se_guarda_sin_ia_y_sin_alertas(cartera, claude_falso):
    claude_falso.respuestas = [connection_error()] * 3
    hecho = explorar(cartera)
    assert not hecho.report.used_ai
    assert f"**{NO_AI_LABEL}:** sin conexión con Claude." in hecho.report.markdown
    assert hecho.run.status is RunStatus.ERROR and hecho.alerts == ()
    assert claude_falso.parses == []


def test_sin_clave_o_sin_tope_el_explorador_no_se_lanza(cartera, claude_falso):
    with pytest.raises(ExplorationBlocked, match="no hay clave"):
        explorar(cartera, clave=None)
    ajustes = Settings()
    ajustes.ai.monthly_budget_usd = 1.0  # la exploración cuesta ≈ 0,80–2,50 $
    with pytest.raises(ExplorationBlocked, match="tope de gasto"):
        explorar(cartera, ajustes=ajustes)
    assert claude_falso.streams == []
    assert ReportRepository(cartera.connection()).list_all() == []


def test_vigilar_un_candidato_aunque_no_haya_dado_alerta(cartera, claude_falso):
    claude_falso.respuestas = [exploracion()]
    claude_falso.extracciones = [extraccion(CANDIDATOS)]
    hecho = explorar(cartera)
    nvo = next(a for a in hecho.alerts if a.ticker == "NVO")
    with cartera.transaction() as conn:
        item = watch_candidate(conn, nvo.id, AHORA)
    assert (item.ticker, item.name, item.yahoo_symbol, item.sector, item.currency,
            item.added_by) == ("NVO", "Novo Nordisk", "NVO", "Salud", "USD",
                               WatchlistSource.EXPLORER)


# == el histórico de Yahoo =========================================================================


def test_yahoo_da_el_historico_con_maximos_minimos_y_divisa():
    velas_yahoo = [
        (date(2026, 9, 21), 10.5, 9.5, 10.0),
        (date(2026, 9, 22), float("nan"), float("nan"), 10.2),  # sin máximo ni mínimo
        (date(2026, 9, 23), 11.0, 10.0, float("nan")),  # sin cierre: fuera
        (date(2026, 9, 24), 10.8, 10.1, 10.6),
    ]
    yahoo = FakeYahoo(series={"ACME": (velas_yahoo, "USD", "Acme Corp")})
    resultado = yahoo.market().fetch_history(["ACME", "NADA"], date(2025, 8, 21))
    serie = resultado.histories["ACME"]
    assert serie.currency == "USD" and serie.name == "Acme Corp"
    assert [(b.day.day, b.high, b.low, b.close) for b in serie.bars] == [
        (21, D("10.5"), D("9.5"), D("10")),
        (22, D("10.2"), D("10.2"), D("10.2")),
        (24, D("10.8"), D("10.1"), D("10.6")),
    ]
    assert yahoo.kwargs[0]["start"] == "2025-08-21" and yahoo.kwargs[0]["interval"] == "1d"
    assert "NADA" in resultado.failures


def test_el_historico_se_guarda_como_cache(db):
    guardado = load_histories(db, mercado(), ["CCJ", "ZZZ"], AHORA)
    assert guardado.histories["CCJ"].source is PriceSource.MARKET
    assert guardado.failures["ZZZ"] == "Yahoo no tiene cotizaciones recientes de ZZZ."
    cache = PriceHistoryRepository(db.connection()).load("CCJ", AHORA + timedelta(hours=1))
    assert cache.source is PriceSource.CACHE and len(cache.bars) == 300
    assert cache.bars[-1].close == D("78.00")
