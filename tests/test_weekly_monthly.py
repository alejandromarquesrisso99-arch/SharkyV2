"""Noticias semanales y estudio mensual (GUIA §5.7 y §7, H10) con un Claude de mentira.

- Semanal: búsqueda web con `max_uses`, `pause_turn` reenviado tal cual, las fuentes citadas en
  «Fuentes», las posiciones prioritarias y el contexto de la semana. Sin clave o sin posiciones,
  se guarda igual.
- Mensual en dos pasos: el estudio y los veredictos, válidos e inválidos. Cada veredicto válido
  se añade a su tesis como revisión (y propuesta sin aplicar) y ninguna revisión cambia un
  número de la tesis. Si la extracción falla, el estudio se guarda igual.

Cartera inventada de tests/test_trades.py (creada el 20/09/2026; hoy es el viernes 25/09): SAN
cotiza a 4 € con la tesis en 4,20 / 6 (STOP), IWDA con 80 / 120; AAPL sin tesis.
"""

from datetime import timedelta
from decimal import Decimal as D

import anthropic
import pytest

from fakes import (
    api_status_error,
    busqueda,
    connection_error,
    extraccion,
    mensaje_claude,
    texto_citado,
)
from sharky.core.levels import ProposalState, levels_of, proposal_states
from sharky.core.models import (
    Author,
    Price,
    PriceSource,
    Report,
    ReportKind,
    Run,
    RunStatus,
    ThesisEventKind,
    Trade,
    TradeKind,
)
from sharky.core.reports import NO_AI_LABEL
from sharky.core.schedule import Month
from sharky.services.ai import ClaudeClient
from sharky.services.reports import (
    ExtractedVerdict,
    MonthlyVerdicts,
    create_monthly_report,
    create_weekly_report,
    monthly_for,
    weekly_for,
)
from sharky.services.repositories import (
    PriceRepository,
    ReportRepository,
    RunRepository,
    ThesisEventRepository,
    ThesisRepository,
    TradeRepository,
    apply_proposal,
    load_valuation,
)
from sharky.services.settings import Settings
from test_trades import AHORA, HOY, INICIO, alta_tesis, crear_cartera

CLAVE = "sk-ant-inventada-0000"
SEPTIEMBRE = Month(2026, 9)

ESTUDIO = (
    "## SAN\nEl stop está roto: la tesis no se sostiene. Veredicto: CERRAR.\n\n"
    "## IWDA\nSigue en pie. Propongo subir el stop a 85 EUR. Veredicto: MANTENER.\n\n"
    "## AAPL\nSin tesis. Veredicto: REDUCIR.\n\n"
    "## Conclusión del mes\n- Liquidar SAN.\n- Subir el stop de IWDA.\n- Poner tesis a AAPL."
)


@pytest.fixture
def cartera(db):
    crear_cartera(db)
    alta_tesis(db, "SAN", stop="4.2", objetivo="6", entrada="4.5")  # 4 € ≤ 4,20 €: STOP
    alta_tesis(db, "IWDA", stop="80", objetivo="120", entrada="80")
    return db


def sin_esperas(clave):
    return ClaudeClient(clave, wait=lambda _s, _c: None)


def semanal(db, clave=CLAVE, ajustes=None, ahora=AHORA, **kwargs):
    valoracion = load_valuation(db.connection(), ahora)
    return create_weekly_report(
        db, valoracion, ajustes or Settings(), lambda: ahora, clave,
        client_factory=sin_esperas, **kwargs,
    )


def mensual(db, mes=SEPTIEMBRE, clave=CLAVE, ajustes=None, ahora=AHORA, **kwargs):
    valoracion = load_valuation(db.connection(), ahora)
    return create_monthly_report(
        db, valoracion, ajustes or Settings(), lambda: ahora, clave, mes,
        client_factory=sin_esperas, **kwargs,
    )


def guardar(db, tipo, periodo, markdown="…", conclusion="- ok", cuando=AHORA, ia=True,
            vigilar=()):
    with db.transaction() as conn:
        return ReportRepository(conn).save(Report(tipo, periodo, cuando, markdown, ia,
                                                  conclusion, tuple(vigilar)))


def veredicto(ticker, que, motivo="Porque sí.", invalida="Otra cosa.", stop=None, objetivo=None):
    return ExtractedVerdict(ticker=ticker, verdict=que, reason=motivo, invalidation=invalida,
                            proposed_stop=stop, proposed_target=objetivo)


def numeros_de_las_tesis(db):
    return {t.ticker: (levels_of(t), t.why, t.catalysts, t.risks, t.invalidation, t.status)
            for t in ThesisRepository(db.connection()).list_all()}


# == el semanal =================================================================================


def respuesta_con_busquedas():
    """Claude pausa el turno a mitad de las búsquedas y lo termina en el reenvío."""
    pausa = mensaje_claude(bloques=[
        texto_citado("Voy a buscar las noticias de cada activo. "),
        *busqueda("Banco Santander resultados", "https://ejemplo.es/san"),
        texto_citado("## SAN\nEl banco rebaja previsiones.",
                     ("https://ejemplo.es/san", "Santander rebaja previsiones")),
    ], stop="pause_turn", entrada=8_000, salida=400, busquedas=3)
    final = mensaje_claude(bloques=[
        *busqueda("iShares MSCI World", "https://ejemplo.es/iwda"),
        texto_citado("\n\n## IWDA\nNada relevante.\n\n## AAPL\nPresenta producto.",
                     ("https://ejemplo.es/aapl", "Apple presenta")),
        texto_citado("\n\n## Conclusión de la semana\n- SAN confirma la salida.\n"
                     "- AAPL sin novedades de peso."),
    ], entrada=12_000, salida=900, busquedas=2)
    return [pausa, final]


def test_semanal_con_busqueda_web_pause_turn_y_fuentes(cartera, claude_falso):
    claude_falso.respuestas = respuesta_con_busquedas()
    hecho = semanal(cartera)
    informe = hecho.report
    assert informe.kind is ReportKind.WEEKLY and informe.period == "2026-09-25"
    assert informe.used_ai and informe.effort == "medium"
    # La herramienta de la guía, con min(30, 3 × posiciones) búsquedas.
    primera, segunda = claude_falso.streams
    assert primera["tools"] == [{"type": "web_search_20250305", "name": "web_search",
                                 "max_uses": 9}]
    assert primera["max_tokens"] == 32_000 and primera["output_config"] == {"effort": "medium"}
    # El reenvío lleva la respuesta en pausa tal cual.
    assert segunda["messages"][1]["role"] == "assistant"
    assert len(segunda["messages"]) == 2
    # Todo el texto, sin el «voy a buscar» de antes de la primera sección.
    assert "Voy a buscar" not in informe.markdown
    assert informe.markdown.index("## SAN") < informe.markdown.index("## IWDA")
    assert informe.conclusion == "- SAN confirma la salida.\n- AAPL sin novedades de peso."
    # Las fuentes citadas, juntas y con su título.
    assert ("## Fuentes\n\n- [Santander rebaja previsiones](https://ejemplo.es/san)\n"
            "- [Apple presenta](https://ejemplo.es/aapl)") in informe.markdown
    # Coste: 20.000 × 2 $/M + 1.300 × 10 $/M + 5 búsquedas × 10 $/1.000.
    assert (informe.input_tokens, informe.output_tokens, informe.web_searches) == (
        20_000, 1_300, 5)
    assert informe.cost_usd == D("0.103")
    (fila,) = RunRepository(cartera.connection()).list_recent()
    assert fila.step == "Informe semanal" and fila.cost_usd == D("0.103")
    assert "5 búsquedas" in fila.detail and "2 fuentes" in fila.detail
    assert "1 reenvíos por pausa" in fila.detail
    assert "## Datos de la semana" in informe.markdown
    assert weekly_for(cartera.connection(), HOY).id == informe.id


def test_el_prompt_del_semanal(cartera, claude_falso):
    hace_una_semana = HOY - timedelta(days=8)
    with cartera.transaction() as conn:
        # AAPL estaba a 180 USD hace una semana: +11,1 %; SAN a 4,2: −4,8 %.
        PriceRepository(conn).save(Price("AAPL", hace_una_semana, D("180"), "USD",
                                         PriceSource.MARKET, AHORA - timedelta(days=8)))
        PriceRepository(conn).save(Price("SAN", hace_una_semana, D("4.2"), "EUR",
                                         PriceSource.MARKET, AHORA - timedelta(days=8)))
    guardar(cartera, ReportKind.DAILY, "2026-09-18", conclusion="- Fuera de la semana.")
    guardar(cartera, ReportKind.DAILY, "2026-09-22", conclusion="- Vigilar IWDA.",
            vigilar=("IWDA",))
    guardar(cartera, ReportKind.DAILY, "2026-09-24", conclusion="- Vigilar IWDA otra vez.",
            vigilar=("IWDA", "XYZ"))
    semanal(cartera)
    llamada = claude_falso.streams[0]
    prompt = llamada["messages"][0]["content"]
    assert prompt.startswith("Escaneo semanal de noticias del 19/09/2026 al 25/09/2026.")
    assert "Eres Sharky" in llamada["system"]
    # Los activos, en el orden de la cartera, con su símbolo, peso y variación semanal.
    assert prompt.index("- AAPL — Apple Inc.") < prompt.index("- IWDA —") < prompt.index("- SAN")
    assert "Yahoo AAPL · peso 6,8 % · en la semana +11,1 % (frente al cierre del 17/09)" in prompt
    # Las conclusiones de los diarios de la semana, con su fecha; la del 18 no.
    assert "22/09/2026:\n- Vigilar IWDA." in prompt and "Fuera de la semana" not in prompt
    # Prioritarias: el stop de SAN, el +11 % de AAPL y lo que dejaron los diarios.
    assert ("Investiga primero: SAN (stop alcanzado); AAPL (+11,1 % en la semana); "
            "IWDA (a vigilar según los controles del 22/09 y 24/09).") in prompt
    assert "$" not in prompt.replace("$ ", "")


def test_semanal_sin_clave_se_guarda_con_su_etiqueta(cartera, claude_falso):
    hecho = semanal(cartera, clave=None)
    informe = hecho.report
    assert claude_falso.streams == [] and not informe.used_ai and informe.cost_usd == 0
    assert f"{NO_AI_LABEL}:** no hay clave de Claude" in informe.markdown
    assert "## Conclusión de la semana" in informe.markdown
    assert informe.conclusion.startswith("- Stop alcanzado en SAN")
    assert "## Fuentes" not in informe.markdown
    assert hecho.run.status is RunStatus.OK


def test_semanal_sin_posiciones_no_llama_a_claude(db, claude_falso):
    from sharky.core.models import CashKind, CashMovement
    from sharky.services.repositories import CashMovementRepository

    with db.transaction() as conn:
        CashMovementRepository(conn).add(CashMovement(INICIO, CashKind.INITIAL, D("5000")))
    informe = semanal(db).report
    assert claude_falso.streams == []
    assert "no hay posiciones: no hay noticias que buscar" in informe.markdown


def test_semanal_con_el_tope_superado(cartera, claude_falso):
    with cartera.transaction() as conn:  # 9,30 + 0,80 (lo más caro del semanal) > 10 $
        RunRepository(conn).add(Run(AHORA, "manual", "Informe diario", RunStatus.OK,
                                    cost_usd=D("9.30")))
    informe = semanal(cartera).report
    assert claude_falso.streams == [] and "tope de gasto mensual" in informe.error


def test_semanal_si_claude_falla_se_guarda_igual(cartera, claude_falso):
    claude_falso.respuestas = [connection_error()] * 3
    hecho = semanal(cartera)
    assert not hecho.report.used_ai and hecho.run.status is RunStatus.ERROR
    assert "sin conexión con Claude" in hecho.report.markdown


def test_semanal_que_sigue_en_pausa_se_marca_incompleto(cartera, claude_falso):
    claude_falso.respuestas = [
        mensaje_claude(f"## SAN\nparte {n}\n", stop="pause_turn") for n in range(4)
    ]
    informe = semanal(cartera).report
    assert len(claude_falso.streams) == 4 and informe.used_ai
    assert "seguía en pausa tras los reenvíos" in informe.markdown
    assert "Claude no ha citado fuentes." in informe.markdown


# == el mensual =================================================================================


def veredictos_de_prueba():
    return MonthlyVerdicts(verdicts=[
        veredicto("SAN", "CERRAR", "El stop está roto.", "Que recupere 4,50 €."),
        veredicto("iwda", "mantener", "Sigue en pie.", "Una recesión global.", stop=85.0),
        veredicto("AAPL", "Reducir", "Pesa demasiado sin tesis.", "Nada."),
        veredicto("XYZ", "MANTENER"),  # no es de la cartera
        veredicto("SAN", "AMPLIAR"),  # repetido
    ])


def test_mensual_en_dos_pasos_con_revisiones_en_las_tesis(cartera, claude_falso):
    antes = numeros_de_las_tesis(cartera)
    claude_falso.respuestas = [mensaje_claude(ESTUDIO, entrada=30_000, salida=4_000)]
    claude_falso.extracciones = [extraccion(veredictos_de_prueba(), entrada=5_000, salida=600)]
    etapas = []
    hecho = mensual(cartera, on_stage=lambda paso, pasos: etapas.append((paso, pasos)))
    informe = hecho.report
    assert etapas == [(1, 2), (2, 2)]
    # Paso A por streaming, con esfuerzo alto; paso B con parse, sin razonamiento.
    (estudio,) = claude_falso.streams
    assert estudio["max_tokens"] == 48_000 and estudio["output_config"] == {"effort": "high"}
    assert "tools" not in estudio
    (extraer,) = claude_falso.parses
    assert extraer["output_format"] is MonthlyVerdicts and extraer["max_tokens"] == 8_000
    assert extraer["thinking"] == {"type": "disabled"}
    prompt_b = extraer["messages"][0]["content"]
    assert prompt_b.startswith("Del estudio siguiente, extrae para cada uno de estos tickers: "
                               "AAPL (sin tesis), IWDA (niveles en EUR), SAN (niveles en EUR)")
    assert "## Conclusión del mes" in prompt_b  # el estudio va entero
    # El informe: estudio, veredictos validados y datos.
    assert informe.kind is ReportKind.MONTHLY and informe.period == "2026-09"
    assert informe.conclusion.startswith("- Liquidar SAN.")
    assert "| SAN | CERRAR | — | — | revisión añadida |" in informe.markdown
    assert "| IWDA | MANTENER | 85,00 EUR | — | revisión y propuesta añadidas |" in informe.markdown
    assert "| AAPL | REDUCIR | — | — | sin tesis activa: solo aquí |" in informe.markdown
    assert "«MANTENER» para «XYZ»: no es una posición de la cartera." in informe.markdown
    assert "«AMPLIAR» para «SAN»: veredicto repetido: vale el primero." in informe.markdown
    # El coste suma los dos pasos: 35.000 × 2 $/M + 4.600 × 10 $/M.
    assert informe.cost_usd == D("0.116") and hecho.run.cost_usd == D("0.116")
    assert "veredictos: 3 de 3" in hecho.run.detail and "1 propuestas en Tesis" in hecho.run.detail
    # Las tesis: una revisión de Claude en SAN e IWDA, y la propuesta del stop en IWDA.
    tesis = {t.ticker: t for t in ThesisRepository(cartera.connection()).list_active()}
    eventos = ThesisEventRepository(cartera.connection())
    nuevos_san = [e for e in eventos.list_for(tesis["SAN"].id) if e.author is Author.CLAUDE]
    nuevos_iwda = [e for e in eventos.list_for(tesis["IWDA"].id) if e.author is Author.CLAUDE]
    assert [e.kind for e in nuevos_san] == [ThesisEventKind.REVIEW]
    assert nuevos_san[0].text.startswith("Estudio mensual de septiembre de 2026 (parcial: hasta "
                                         "el 25/09/2026): CERRAR.\nMotivo: El stop está roto.")
    assert [e.kind for e in nuevos_iwda] == [ThesisEventKind.REVIEW, ThesisEventKind.PROPOSAL]
    propuesta = nuevos_iwda[1]
    assert propuesta.new_value == '{"stop": "85.0", "divisa": "EUR"}'
    assert propuesta.old_value == '{"stop": "80"}'
    estados = proposal_states(eventos.list_for(tesis["IWDA"].id))
    assert estados[propuesta.id] is ProposalState.PENDING
    assert len(hecho.thesis_events) == 3
    # Ninguna revisión ha cambiado un número de las tesis.
    assert numeros_de_las_tesis(cartera) == antes


def test_la_propuesta_de_claude_solo_se_aplica_con_aplicar(cartera, claude_falso):
    claude_falso.respuestas = [mensaje_claude(ESTUDIO)]
    claude_falso.extracciones = [extraccion(veredictos_de_prueba())]
    mensual(cartera)
    iwda = ThesisRepository(cartera.connection()).active_for("IWDA")
    assert iwda.stop == D("80")
    propuesta = next(e for e in ThesisEventRepository(cartera.connection()).list_for(iwda.id)
                     if e.kind is ThesisEventKind.PROPOSAL)
    with cartera.transaction() as conn:
        apply_proposal(conn, propuesta.id, AHORA)
    assert ThesisRepository(cartera.connection()).active_for("IWDA").stop == D("85.0")


def test_ninguna_revision_cambia_un_numero_de_la_tesis(cartera, claude_falso):
    antes = numeros_de_las_tesis(cartera)
    eventos_antes = {
        t.id: len(ThesisEventRepository(cartera.connection()).list_for(t.id))
        for t in ThesisRepository(cartera.connection()).list_all()
    }
    claude_falso.respuestas = [mensaje_claude(ESTUDIO)]
    claude_falso.extracciones = [extraccion(MonthlyVerdicts(verdicts=[
        veredicto("SAN", "CERRAR", stop=3.5, objetivo=5.0),
        veredicto("IWDA", "AMPLIAR", stop=88.0, objetivo=150.0),
    ]))]
    mensual(cartera)
    assert numeros_de_las_tesis(cartera) == antes
    for tesis in ThesisRepository(cartera.connection()).list_all():
        nuevos = ThesisEventRepository(cartera.connection()).list_for(tesis.id)[
            eventos_antes[tesis.id]:]
        # Solo se añade: revisiones y propuestas de Claude, nunca cambios de niveles.
        assert {e.kind for e in nuevos} <= {ThesisEventKind.REVIEW, ThesisEventKind.PROPOSAL}
        assert all(e.author is Author.CLAUDE for e in nuevos)


def test_veredictos_invalidos(cartera, claude_falso):
    claude_falso.respuestas = [mensaje_claude(ESTUDIO)]
    claude_falso.extracciones = [extraccion(MonthlyVerdicts(verdicts=[
        veredicto("SAN", "VENDER"),  # no es uno de los cuatro
        veredicto("IWDA", "MANTENER", stop=130.0),  # por encima del objetivo (120): fuera
        veredicto("AAPL", "  **cerrar** ", stop=-1.0),  # vale; el stop negativo, no
    ]))]
    hecho = mensual(cartera)
    md = hecho.report.markdown
    assert "«VENDER» para «SAN»: no es MANTENER, REDUCIR, AMPLIAR ni CERRAR." in md
    assert "| SAN | no disponible | — | — | — |" in md
    assert "| IWDA | MANTENER | — | — | revisión añadida |" in md
    assert "(Cifras descartadas: el stop no quedaría por debajo del objetivo.)" in md
    assert "| AAPL | CERRAR | — | — | sin tesis activa: solo aquí |" in md
    assert "(Cifras descartadas: el stop propuesto no es mayor que 0.)" in md
    iwda = ThesisRepository(cartera.connection()).active_for("IWDA")
    assert not any(e.kind is ThesisEventKind.PROPOSAL
                   for e in ThesisEventRepository(cartera.connection()).list_for(iwda.id))


@pytest.mark.parametrize("fallo", [
    "validacion",
    "red",
    "vacia",
], ids=["respuesta_ilegible", "sin_red", "sin_datos"])
def test_si_la_extraccion_falla_el_estudio_se_guarda(cartera, claude_falso, fallo):
    import pydantic

    antes = numeros_de_las_tesis(cartera)
    eventos_antes = sum(len(ThesisEventRepository(cartera.connection()).list_for(t.id))
                        for t in ThesisRepository(cartera.connection()).list_all())
    if fallo == "validacion":
        try:
            MonthlyVerdicts.model_validate_json('{"verdicts": [')
        except pydantic.ValidationError as error:
            claude_falso.extracciones = [error]
    elif fallo == "red":
        claude_falso.extracciones = [connection_error()] * 3
    else:
        claude_falso.extracciones = [extraccion(None)]
    claude_falso.respuestas = [mensaje_claude(ESTUDIO)]
    hecho = mensual(cartera)
    informe = hecho.report
    assert informe.used_ai and informe.error is None
    assert "El stop está roto" in informe.markdown
    assert "Veredictos no disponibles:" in informe.markdown
    assert informe.conclusion.startswith("- Liquidar SAN.")
    assert hecho.run.status is RunStatus.OK and hecho.verdicts_unavailable
    assert "veredictos no disponibles" in hecho.run.detail
    if fallo == "validacion":
        assert "coste del paso B sin medir" in hecho.run.detail
    assert monthly_for(cartera.connection(), SEPTIEMBRE).id == informe.id
    assert numeros_de_las_tesis(cartera) == antes
    assert sum(len(ThesisEventRepository(cartera.connection()).list_for(t.id))
               for t in ThesisRepository(cartera.connection()).list_all()) == eventos_antes


def test_si_el_estudio_falla_no_hay_paso_b(cartera, claude_falso):
    claude_falso.respuestas = [api_status_error(anthropic.AuthenticationError, 401)]
    hecho = mensual(cartera)
    assert claude_falso.parses == []
    assert not hecho.report.used_ai and hecho.run.status is RunStatus.ERROR
    assert "Clave no válida" in hecho.report.markdown
    assert "## Veredictos por posición" not in hecho.report.markdown


def test_si_el_modelo_no_admite_quitar_el_razonamiento(cartera, claude_falso):
    claude_falso.respuestas = [mensaje_claude(ESTUDIO)]
    claude_falso.extracciones = [
        api_status_error(anthropic.BadRequestError, 400, body={
            "type": "error", "error": {"type": "invalid_request_error",
                                       "message": "thinking: disabled is not supported"}}),
        extraccion(veredictos_de_prueba()),
    ]
    hecho = mensual(cartera)
    assert [("thinking" in p, p["max_tokens"]) for p in claude_falso.parses] == [
        (True, 8_000), (False, 16_000)]
    assert "| SAN | CERRAR |" in hecho.report.markdown


def test_mensual_sin_clave(cartera, claude_falso):
    hecho = mensual(cartera, clave=None)
    assert claude_falso.streams == [] and claude_falso.parses == []
    assert not hecho.report.used_ai and "no hay clave de Claude" in hecho.report.markdown
    assert "## Conclusión del mes" in hecho.report.markdown


def test_el_contexto_del_mes(cartera, claude_falso):
    with cartera.transaction() as conn:
        TradeRepository(conn).add(Trade(HOY - timedelta(days=1), "IWDA", TradeKind.BUY, D("2"),
                                        D("90"), "EUR", D(1), D(1), D(180), forced=True,
                                        reason="Oportunidad"))
    guardar(cartera, ReportKind.DAILY, "2026-08-31", conclusion="- De agosto.")
    guardar(cartera, ReportKind.DAILY, "2026-09-21", conclusion="- SAN cerca del stop.")
    guardar(cartera, ReportKind.WEEKLY, "2026-09-20",
            markdown="**Estado:** Óptimo\n\n## SAN\nNoticia importante.\n\n"
                     "## Conclusión de la semana\n- Ojo a SAN.\n\n## Fuentes\n\n"
                     "- [Web](https://ejemplo.es)\n\n---\n\n## Datos de la semana\n\ntabla",
            conclusion="- Ojo a SAN.")
    guardar(cartera, ReportKind.MONTHLY, "2026-08", conclusion="- Agosto tranquilo.",
            cuando=AHORA - timedelta(days=24))
    mensual(cartera)
    prompt = claude_falso.streams[0]["messages"][0]["content"]
    assert prompt.startswith("Estudio mensual de septiembre de 2026 (parcial: hasta el "
                             "25/09/2026).")
    assert "- SAN (niveles en EUR): Entrada 4,50 EUR · stop 4,20 · objetivo 6,00" in prompt
    assert "Última revisión: ninguna todavía." in prompt
    assert "- Posiciones sin tesis: AAPL." in prompt
    assert "24/09 COMPRA IWDA: 2 u. a 90,00 EUR · motivo: «Oportunidad» · forzada" in prompt
    assert "APERTURA" not in prompt.split("OPERACIONES DEL MES")[1].split("CONCLUSIONES")[0]
    assert "21/09/2026:\n- SAN cerca del stop." in prompt and "De agosto" not in prompt
    # Del semanal, lo que escribió Claude; sin fuentes ni tablas.
    assert "### Semana del 14/09 al 20/09\n## SAN\nNoticia importante." in prompt
    assert "ejemplo.es" not in prompt and "Datos de la semana" not in prompt
    assert "CONCLUSIÓN DEL MES ANTERIOR: \n- Agosto tranquilo." in prompt
    assert "$" not in prompt.replace("$ ", "")


def test_la_ultima_revision_de_claude_va_en_el_contexto_siguiente(cartera, claude_falso):
    claude_falso.respuestas = [mensaje_claude(ESTUDIO), mensaje_claude(ESTUDIO)]
    claude_falso.extracciones = [extraccion(veredictos_de_prueba()),
                                 extraccion(veredictos_de_prueba())]
    mensual(cartera)
    mensual(cartera, ahora=AHORA + timedelta(hours=1))
    prompt = claude_falso.streams[1]["messages"][0]["content"]
    assert "Última revisión (25/09/2026): Estudio mensual de septiembre" in prompt
    assert "Propuesta de Claude sin aplicar (25/09): Niveles en EUR · stop 85,0" in prompt


def test_el_parcial_lo_sustituye_el_completo(cartera, claude_falso):
    parcial = mensual(cartera, clave=None).report
    uno_de_octubre = AHORA.replace(month=10, day=1)
    completo = mensual(cartera, clave=None, ahora=uno_de_octubre).report
    (unico,) = ReportRepository(cartera.connection()).list_kind(ReportKind.MONTHLY)
    assert unico.created_at == completo.created_at > parcial.created_at
    assert "Estudio parcial" in parcial.markdown and "Estudio parcial" not in unico.markdown


# == los prompts ================================================================================


@pytest.mark.parametrize("nombre, campos", [
    ("semanal", ("desde", "hasta", "activos", "conclusiones_diarias", "prioritarias")),
    ("mensual", ("mes", "valoracion", "incumplimientos", "tesis", "operaciones",
                 "conclusiones_diarias", "semanales", "conclusion_anterior")),
    ("mensual_extraccion", ("tickers", "estudio")),
])
def test_los_prompts_son_recursos_con_plantilla(nombre, campos):
    from sharky import paths
    from sharky.services.reports import prompt_template, render_prompt

    assert paths.resource_path("prompts", f"{nombre}.md").is_file()
    texto = render_prompt(nombre, dict.fromkeys(campos, "«X»"))
    assert "$" not in texto
    usados = {m.group("named") for m in prompt_template(nombre).pattern.finditer(
        prompt_template(nombre).template) if m.group("named")}
    assert usados == set(campos)
    with pytest.raises(KeyError):
        render_prompt(nombre, {})
