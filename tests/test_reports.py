"""El control diario (GUIA §5.7 y §7, H9) con un Claude de mentira: éxito, reintento con menos
esfuerzo, error de red, clave no válida, sin clave y tope superado. En todos los casos el informe
se guarda, y los avisos de niveles ya estaban guardados antes de llamar a Claude.

Cartera inventada de tests/test_trades.py; SAN cotiza a 4 € y su tesis tiene el stop en 4,20 €.
"""

from datetime import datetime, timedelta
from decimal import Decimal as D

import anthropic
import pytest

from fakes import RESPUESTA_POR_DEFECTO, api_status_error, connection_error, mensaje_claude
from sharky import paths
from sharky.core.models import (
    LevelAlertKind,
    Price,
    PriceSource,
    Report,
    ReportKind,
    Run,
    RunStatus,
    Trade,
    TradeKind,
)
from sharky.core.reports import NO_AI_LABEL, AIAction
from sharky.services.ai import AIError, AIErrorKind, ClaudeClient
from sharky.services.db import Database
from sharky.services.reports import (
    action_estimate,
    create_daily_report,
    daily_for,
    render_prompt,
    spent_this_month,
    system_prompt,
)
from sharky.services.repositories import (
    LevelAlertRepository,
    NavSnapshotRepository,
    PriceRepository,
    ReportRepository,
    RunRepository,
    TradeRepository,
    load_valuation,
)
from sharky.services.settings import MandateSettings, Settings
from test_trades import AHORA, HOY, INICIO, alta_tesis, crear_cartera

CLAVE = "sk-ant-inventada-0000"


@pytest.fixture
def cartera(db):
    crear_cartera(db)
    alta_tesis(db, "SAN", stop="4.2", objetivo="6", entrada="4.5")  # 4 € ≤ 4,20 €: STOP
    alta_tesis(db, "IWDA", stop="80", objetivo="120", entrada="80")
    return db


def sin_esperas(clave):
    return ClaudeClient(clave, wait=lambda _s, _c: None)


def diario(db, clave=CLAVE, ajustes=None, ahora=AHORA, **kwargs):
    valoracion = load_valuation(db.connection(), ahora)
    return create_daily_report(
        db, valoracion, ajustes or Settings(), lambda: ahora, clave,
        client_factory=sin_esperas, **kwargs,
    )


def avisos_guardados(db):
    """Los avisos de niveles de hoy, leídos con otra conexión: solo se ven si ya están
    confirmados en la base de datos."""
    otra = Database(db.path)
    try:
        return [(a.ticker, a.kind) for a in LevelAlertRepository(otra.connection())
                .list_for_date(HOY)]
    finally:
        otra.close_all()


@pytest.fixture
def vigilar_antes(claude_falso, cartera):
    """Apunta, en cada llamada a Claude, qué avisos había ya guardados."""
    vistos = []
    claude_falso.al_llamar = lambda _kwargs: vistos.append(avisos_guardados(cartera))
    return vistos


def gastar(db, dolares, cuando=AHORA, paso="Informe diario", estado=RunStatus.OK):
    with db.transaction() as conn:
        RunRepository(conn).add(Run(cuando, "manual", paso, estado, cost_usd=D(dolares)))


# -- los seis casos del criterio de aceptación ----------------------------------------------------


def test_exito(cartera, claude_falso, vigilar_antes):
    hecho = diario(cartera)
    informe = hecho.report
    assert informe.used_ai and informe.error is None
    assert informe.kind is ReportKind.DAILY and informe.period == "2026-09-25"
    assert informe.model == "claude-sonnet-5" and informe.effort == "low"
    assert (informe.input_tokens, informe.output_tokens) == (2_000, 600)
    # 2.000 × 2 $/M + 600 × 10 $/M
    assert informe.cost_usd == D("0.01")
    assert RESPUESTA_POR_DEFECTO.strip() in informe.markdown
    assert "## Datos del día" in informe.markdown and "**Estado:**" in informe.markdown
    assert informe.conclusion == "- Vigilar el stop de SAN."
    assert informe.watch_positions[0] == "SAN"
    # Guardado, con su fila en el Registro y el coste real.
    guardado = ReportRepository(cartera.connection()).latest(ReportKind.DAILY)
    assert guardado.id == informe.id and guardado.cost_usd == D("0.01")
    (fila,) = RunRepository(cartera.connection()).list_recent()
    assert fila.step == "Informe diario" and fila.status is RunStatus.OK
    assert fila.cost_usd == D("0.01") and fila.triggered_by == "manual"
    assert "2.000 tokens de entrada · 600 de salida" in fila.detail
    # Los avisos ya estaban guardados cuando se llamó a Claude.
    assert vigilar_antes == [[("SAN", LevelAlertKind.STOP)]]


def test_solo_razonamiento_y_max_tokens_reintenta_con_menos_esfuerzo(
    cartera, claude_falso, vigilar_antes
):
    claude_falso.respuestas = [
        mensaje_claude("", stop="max_tokens", razonamiento=True, entrada=2_000, salida=16_000),
        mensaje_claude(),
    ]
    ajustes = Settings()
    ajustes.ai.daily.effort = "medium"
    informe = diario(cartera, ajustes=ajustes).report
    assert [s["output_config"]["effort"] for s in claude_falso.streams] == ["medium", "low"]
    assert informe.used_ai and informe.effort == "low"
    # Se cobran los dos intentos: 4.000 × 2 $/M + 16.600 × 10 $/M.
    assert (informe.input_tokens, informe.output_tokens) == (4_000, 16_600)
    assert informe.cost_usd == D("0.174")
    assert vigilar_antes == [[("SAN", LevelAlertKind.STOP)]] * 2


def test_con_el_esfuerzo_por_defecto_el_reintento_va_sin_razonamiento(cartera, claude_falso):
    claude_falso.respuestas = [
        mensaje_claude("", stop="max_tokens", razonamiento=True),
        mensaje_claude(),
    ]
    informe = diario(cartera).report
    assert claude_falso.streams[1]["thinking"] == {"type": "disabled"}
    assert informe.used_ai and informe.effort == "low sin razonamiento"


def test_error_de_red(cartera, claude_falso, vigilar_antes):
    claude_falso.respuestas = [connection_error()] * 3
    hecho = diario(cartera)
    informe = hecho.report
    assert not informe.used_ai and hecho.ai_error is AIErrorKind.NETWORK
    assert f"{NO_AI_LABEL}:** sin conexión con Claude." in informe.markdown
    assert informe.error == "sin conexión con Claude."
    # La conclusión la escribe Sharky con lo medido: el stop de SAN, lo primero.
    assert informe.conclusion.startswith("- Stop alcanzado en SAN")
    assert "## Conclusión del día" in informe.markdown
    assert hecho.run.status is RunStatus.ERROR and hecho.run.cost_usd == 0
    assert ReportRepository(cartera.connection()).get(informe.id) is not None
    assert len(vigilar_antes) == 3 and all(v == [("SAN", LevelAlertKind.STOP)]
                                           for v in vigilar_antes)


def test_clave_no_valida(cartera, claude_falso, vigilar_antes):
    claude_falso.respuestas = [api_status_error(anthropic.AuthenticationError, 401)]
    hecho = diario(cartera)
    assert not hecho.report.used_ai and hecho.ai_error is AIErrorKind.INVALID_KEY
    assert "Clave no válida" in hecho.report.markdown
    assert len(claude_falso.streams) == 1  # sin reintentos
    assert vigilar_antes == [[("SAN", LevelAlertKind.STOP)]]
    assert daily_for(cartera.connection(), HOY) is not None


def test_sin_clave(cartera, claude_falso):
    hecho = diario(cartera, clave=None)
    informe = hecho.report
    assert claude_falso.creados == [] and claude_falso.streams == []
    assert not informe.used_ai and informe.model is None and informe.cost_usd == 0
    assert "no hay clave de Claude" in informe.markdown
    assert hecho.run.status is RunStatus.OK
    assert avisos_guardados(cartera) == [("SAN", LevelAlertKind.STOP)]
    assert daily_for(cartera.connection(), HOY).id == informe.id


def test_tope_superado(cartera, claude_falso):
    gastar(cartera, "9.97")  # 9,97 $ + lo más caro del diario (0,05 $) > 10 $
    gastar(cartera, "5", cuando=AHORA - timedelta(days=40))  # otro mes: no cuenta
    hecho = diario(cartera)
    informe = hecho.report
    assert claude_falso.streams == []
    assert not informe.used_ai and "se superaría el tope de gasto mensual" in informe.markdown
    assert "9,97 $ de 10,00 $" in informe.error
    assert avisos_guardados(cartera) == [("SAN", LevelAlertKind.STOP)]
    assert daily_for(cartera.connection(), HOY) is not None


def test_justo_en_el_tope_si_se_lanza(cartera, claude_falso):
    gastar(cartera, "9.95")  # 9,95 + 0,05 = 10: cabe
    assert diario(cartera).report.used_ai


# -- más detalles -----------------------------------------------------------------------------


def test_cancelar_al_cerrar_no_guarda_nada_pero_los_avisos_si(cartera, claude_falso):
    import threading

    cancelar = threading.Event()
    claude_falso.durante_stream = lambda n: cancelar.set()
    with pytest.raises(AIError) as fallo:
        diario(cartera, cancel=cancelar)
    assert fallo.value.kind is AIErrorKind.CANCELLED
    assert ReportRepository(cartera.connection()).list_all() == []
    assert RunRepository(cartera.connection()).list_recent() == []
    assert avisos_guardados(cartera) == [("SAN", LevelAlertKind.STOP)]


def test_la_foto_del_dia_se_guarda_antes_de_claude(cartera, claude_falso):
    fotos = []
    claude_falso.al_llamar = lambda _k: fotos.append(
        NavSnapshotRepository(Database(cartera.path).connection()).get(HOY)
    )
    diario(cartera)
    assert fotos[0] is not None and fotos[0].snapshot_date == HOY


def test_ejecutar_otra_vez_sustituye_el_informe_pero_no_su_coste(cartera, claude_falso):
    primero = diario(cartera).report
    segundo = diario(cartera, ahora=AHORA + timedelta(hours=1)).report
    (informe,) = ReportRepository(cartera.connection()).list_all()
    assert informe.created_at == segundo.created_at > primero.created_at
    # El gasto del mes sale del Registro: los dos cuentan.
    assert spent_this_month(cartera.connection(), AHORA) == D("0.02")


def test_la_estimacion_es_la_media_de_las_tres_ultimas(cartera):
    conn = cartera.connection()
    assert action_estimate(conn, AIAction.DAILY).text == "≈ 0,02–0,05 $"
    for coste, minutos in (("0.10", 1), ("0.03", 2), ("0.04", 3), ("0.05", 4)):
        gastar(cartera, coste, cuando=AHORA + timedelta(minutes=minutos))
    gastar(cartera, "0", cuando=AHORA + timedelta(minutes=5))  # sin IA: no cuenta
    gastar(cartera, "0.90", cuando=AHORA + timedelta(minutes=6), estado=RunStatus.ERROR)
    estimacion = action_estimate(conn, AIAction.DAILY)
    assert estimacion.measured and estimacion.low == D("0.04")
    assert estimacion.text == "≈ 0,04 $"


def test_si_claude_no_trae_la_conclusion_la_pone_sharky(cartera, claude_falso):
    claude_falso.respuestas = [mensaje_claude("Todo tranquilo salvo SAN.")]
    informe = diario(cartera).report
    assert informe.used_ai
    assert informe.conclusion.startswith("- Stop alcanzado en SAN")
    assert "Escrita por Sharky" in informe.markdown


def test_sin_precio_del_modelo_no_se_llama(cartera, claude_falso):
    ajustes = Settings()
    ajustes.ai.daily.model = "claude-nuevo-9"
    informe = diario(cartera, ajustes=ajustes).report
    assert claude_falso.streams == []
    assert "falta el precio de claude-nuevo-9" in informe.error


def test_el_prompt_lleva_el_mandato_de_los_ajustes_y_el_contexto_de_hoy(cartera, claude_falso):
    ajustes = Settings()
    ajustes.mandate = MandateSettings(max_sector_weight_pct=22.5)
    diario(cartera, ajustes=ajustes)
    (llamada,) = claude_falso.streams
    assert "Peso máximo por sector: 22,5 %." in llamada["system"]
    assert llamada["system"].startswith("Eres Sharky, analista")
    prompt = llamada["messages"][0]["content"]
    assert prompt.startswith("Control diario del 25/09/2026.")
    assert "Stop alcanzado: SAN a 4,00 € (stop 4,20 €)" in prompt
    assert "AAPL (Apple Inc., Tecnologia)" in prompt
    assert "OPERACIONES FORZADAS FUERA DEL MANDATO: ninguna." in prompt
    assert "$" not in prompt.replace("$ ", "")  # no queda ningún $campo sin rellenar


def test_variacion_desde_el_ultimo_control_y_movimientos(cartera, claude_falso):
    anteayer = HOY - timedelta(days=2)
    with cartera.transaction() as conn:
        PriceRepository(conn).save(Price("SAN", anteayer, D("4.4"), "EUR", PriceSource.MARKET,
                                         AHORA - timedelta(days=2)))
        PriceRepository(conn).save(Price("IWDA", anteayer, D("89.5"), "EUR",
                                         PriceSource.MARKET, AHORA - timedelta(days=2)))
        ReportRepository(conn).save(Report(ReportKind.DAILY, anteayer.isoformat(),
                                           AHORA - timedelta(days=2), "viejo", False))
    informe = diario(cartera).report
    prompt = claude_falso.streams[0]["messages"][0]["content"]
    # SAN: 4 frente a 4,40 → −9,1 %; IWDA: 90 frente a 89,50 → +0,6 % (no llega al 3 %).
    assert "MOVIMIENTOS DE ±3 % O MÁS DESDE EL ÚLTIMO CONTROL: \n- SAN −9,1 %" in prompt
    assert "IWDA +0,6 %" not in prompt.split("NIVELES")[0].split("MOVIMIENTOS")[1]
    assert "desde el último control +0,6 % (frente al cierre del 23/09)" in prompt
    # SAN por su stop (primero) e IWDA por su propio incumplimiento (pesa un 18 %).
    assert informe.watch_positions == ("SAN", "IWDA")


def test_las_operaciones_forzadas_desde_el_ultimo_control(cartera, claude_falso):
    with cartera.transaction() as conn:
        TradeRepository(conn).add(Trade(HOY, "IWDA", TradeKind.BUY, D("5"), D("90"), "EUR",
                                        D(1), D(1), D(450), D(80), D(120), "EUR", forced=True,
                                        reason="Oportunidad"))
        TradeRepository(conn).add(Trade(INICIO, "SAN", TradeKind.BUY, D("1"), D("4"), "EUR",
                                        D(1), D(0), D(4), forced=True, reason="Antigua"))
        ReportRepository(conn).save(Report(ReportKind.DAILY, (HOY - timedelta(days=1))
                                           .isoformat(), AHORA - timedelta(days=1), "", False))
    diario(cartera)
    prompt = claude_falso.streams[0]["messages"][0]["content"]
    assert "25/09 COMPRA IWDA: 5 u. a 90,00 EUR · motivo: «Oportunidad»" in prompt
    assert "Antigua" not in prompt


def test_los_prompts_son_recursos_con_plantilla():
    assert paths.resource_path("prompts", "sistema.md").is_file()
    assert paths.resource_path("prompts", "diario.md").is_file()
    sistema = system_prompt(MandateSettings().rules())
    assert "Mandato vigente:\n- Peso máximo por activo: 10 % en Óptimo; 5 % en los demás" in sistema
    with pytest.raises(KeyError):
        render_prompt("diario", {"fecha": "25/09/2026"})


def test_el_reloj_manda_en_el_mes_del_gasto(cartera):
    gastar(cartera, "1.5", cuando=datetime(2026, 9, 1, 0, 0, tzinfo=AHORA.tzinfo))
    gastar(cartera, "2", cuando=datetime(2026, 8, 31, 23, 59, tzinfo=AHORA.tzinfo))
    assert spent_this_month(cartera.connection(), AHORA) == D("1.5")
