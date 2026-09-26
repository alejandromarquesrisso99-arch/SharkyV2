"""Pantalla Informes, tarjeta «Control diario» del Panel y Ajustes → Claude (GUIA §5.7, §5.10 y
§7, H9): el precio aproximado antes de lanzar, el control en segundo plano, el coste real
después, el lector con su coste y la marca «Sin IA», exportar a .md, la clave, el modelo y el
esfuerzo de cada acción, los precios y el tope de gasto.

Cartera inventada de tests/test_trades.py, con Claude y Yahoo de mentira.
"""

from datetime import timedelta
from decimal import Decimal as D

import pytest
from PySide6.QtCore import QCoreApplication, QEvent

from fakes import FakeMarket, connection_error, extraccion, mensaje_claude, texto_citado
from sharky.core.models import Report, ReportKind, Run, RunStatus
from sharky.core.reports import AIAction
from sharky.core.schedule import Month
from sharky.services import secrets
from sharky.services.ai import ClaudeClient, KeyCheck, KeyStatus
from sharky.services.market import FxQuote, Quote
from sharky.services.repositories import ReportRepository, RunRepository
from sharky.services.settings import Settings, SettingsStore
from sharky.ui.main_window import MainWindow
from sharky.ui.reports import (
    EXTRACTION_STAGE,
    DailyCard,
    ReportsPage,
    report_meta,
    report_title,
)
from sharky.ui.settings import ClaudeCard
from sharky.ui.theme import Theme, ThemeController
from test_trades import AHORA, HOY, alta_tesis, crear_cartera

CLAVE = "sk-ant-inventada-0000"
COTIZACIONES = {
    "SAN.MC": Quote("SAN.MC", D("4"), "EUR", HOY),
    "IWDA.AS": Quote("IWDA.AS", D("90"), "EUR", HOY),
    "AAPL": Quote("AAPL", D("200"), "USD", HOY),
}
CAMBIOS = {"USD": FxQuote("USD", D("0.85"), HOY)}


@pytest.fixture
def cartera(db):
    crear_cartera(db)
    alta_tesis(db, "SAN", stop="4.2", objetivo="6", entrada="4.5")
    return db


@pytest.fixture
def tema(qapp):
    controlador = ThemeController(qapp, Theme.LIGHT)
    yield controlador
    controlador.set_theme(Theme.LIGHT)


def cerrar(ventana):
    """Cierra y borra en el acto (si no, las ventanas se acumulan entre tests)."""
    ventana.shutdown()
    ventana.close()
    ventana.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def ajustes():
    return Settings()


@pytest.fixture
def ventana(qtbot, cartera, tema, ajustes):
    mercado = FakeMarket(COTIZACIONES, CAMBIOS)
    v = MainWindow(
        tema, "9.9.9", db=cartera, market=mercado, fx=mercado, settings=ajustes,
        now=lambda: AHORA, store=SettingsStore(),
        runner_options={"client_factory": lambda k: ClaudeClient(k, wait=lambda _s, _c: None)},
    )
    v.set_notifier(lambda *_a: None)
    yield v
    cerrar(v)


@pytest.fixture
def con_clave(keyring_falso):
    secrets.save_api_key(CLAVE)


def tarjeta(ventana) -> DailyCard:
    card = ventana.page("panel").daily_card
    assert card is not None
    return card


def informes(ventana) -> ReportsPage:
    return ventana.page("informes")


def ejecutar(qtbot, ventana, respuesta=True):
    """«Ejecutar ahora» con el diálogo contestado; espera a que termine. Devuelve el plan que
    enseñó el diálogo."""
    card = tarjeta(ventana)
    vistos = []

    def confirmar(plan):
        vistos.append(plan)
        return respuesta

    card.confirm_run = confirmar
    if not respuesta:
        card.run_button.click()
        return vistos[0]
    with qtbot.waitSignal(ventana.reports_runner.stopped, timeout=10_000):
        card.run_button.click()
    return vistos[0]


def guardar_informe(db, tipo=ReportKind.DAILY, periodo="2026-09-20", ia=True, coste="0.03",
                    cuando=AHORA - timedelta(days=5)):
    with db.transaction() as conn:
        return ReportRepository(conn).save(Report(
            tipo, periodo, cuando, f"Informe {tipo.value} del {periodo}.\n\n## Conclusión\n- Ok.",
            ia, "- Todo en orden.", model="claude-sonnet-5" if ia else None,
            effort="low" if ia else None, input_tokens=1_000 if ia else 0,
            output_tokens=300 if ia else 0, cost_usd=D(coste) if ia else D(0),
            error=None if ia else "no hay clave de Claude.",
        ))


# -- la tarjeta del Panel ---------------------------------------------------------------------


def test_el_boton_ensena_el_precio_antes_de_lanzarse(con_clave, ventana):
    card = tarjeta(ventana)
    assert card.run_button.text() == "Ejecutar ahora · ≈ 0,02–0,05 $"
    assert card.chip_label.text() == "Pendiente"
    assert "Gasto de septiembre: 0,00 $ de 10,00 $." in card.run_button.toolTip()
    assert not card.read_button.isEnabled()


def test_sin_clave_el_boton_dice_gratis(ventana):
    tarjeta(ventana).refresh()
    assert tarjeta(ventana).run_button.text() == "Ejecutar ahora · gratis, sin IA"


def test_ejecutar_ahora_hace_el_diario_en_segundo_plano(qtbot, con_clave, ventana, cartera,
                                                        claude_falso):
    plan = ejecutar(qtbot, ventana)
    # El diálogo enseñaba el modelo, el esfuerzo, el precio y el gasto del mes.
    assert plan.uses_ai and plan.model == "claude-sonnet-5" and plan.effort == "low"
    assert plan.budget.estimate.text == "≈ 0,02–0,05 $" and plan.existing is None
    assert len(claude_falso.streams) == 1
    card = tarjeta(ventana)
    # Después, el coste real.
    assert card.message_label.text() == (
        "Control diario guardado. Coste real: 0,01 $ (2.000 tokens de entrada · 600 de salida)."
    )
    assert card.chip_label.text() == "Hecho hoy"
    assert "claude-sonnet-5 · esfuerzo low · 0,01 $" in card.when_label.text()
    assert card.conclusion_label.text() == "• Vigilar el stop de SAN."
    assert card.read_button.isEnabled() and card.run_button.isEnabled()
    # Y se lee en Informes, con su coste real.
    pagina = informes(ventana)
    assert pagina.current is not None and pagina.current.id == card.report.id
    assert pagina.meta_label.text() == "claude-sonnet-5 · esfuerzo low · 0,01 $"
    assert (RunRepository(cartera.connection()).list_recent()[0].cost_usd == D("0.01"))


def test_primero_se_actualizan_los_precios(qtbot, con_clave, ventana):
    ejecutar(qtbot, ventana)
    assert ventana.refresher.market_at is not None
    assert ventana.refresher.prices.asked  # se ha descargado antes de llamar a Claude


def test_la_segunda_vez_avisa_de_que_sustituye(qtbot, con_clave, ventana, cartera):
    ejecutar(qtbot, ventana)
    plan = ejecutar(qtbot, ventana, respuesta=False)
    assert plan.existing is not None
    assert len(ReportRepository(cartera.connection()).list_all()) == 1


def test_cancelar_el_dialogo_no_lanza_nada(qtbot, con_clave, ventana, claude_falso, cartera):
    ejecutar(qtbot, ventana, respuesta=False)
    assert not ventana.reports_runner.running
    assert claude_falso.streams == []
    assert ReportRepository(cartera.connection()).list_all() == []


def test_si_claude_falla_se_guarda_sin_ia_y_se_dice(qtbot, con_clave, ventana, claude_falso):
    claude_falso.respuestas = [connection_error()] * 3
    ejecutar(qtbot, ventana)
    card = tarjeta(ventana)
    assert card.message_label.text() == "Guardado sin análisis de IA: sin conexión con Claude."
    assert card.chip_label.text() == "Sin IA"
    assert informes(ventana).items[0].report.used_ai is False


def test_tope_superado_ofrece_generar_sin_ia(qtbot, con_clave, ventana, cartera, claude_falso):
    with cartera.transaction() as conn:
        RunRepository(conn).add(Run(AHORA, "manual", "Informe diario", RunStatus.OK,
                                    cost_usd=D("9.99")))
    tarjeta(ventana).refresh()
    assert tarjeta(ventana).run_button.text() == "Ejecutar ahora · gratis: tope de gasto"
    plan = ejecutar(qtbot, ventana)
    assert not plan.uses_ai and "tope de gasto mensual" in plan.no_ai_reason
    assert claude_falso.streams == []
    assert "tope de gasto mensual" in tarjeta(ventana).message_label.text()


def test_leer_lleva_al_informe(qtbot, con_clave, ventana):
    ejecutar(qtbot, ventana)
    card = tarjeta(ventana)
    otro = guardar_informe(ventana._db)
    informes(ventana).reload(select_id=otro)
    card.read_button.click()
    assert ventana.current_section == "informes"
    assert informes(ventana).current.id == card.report.id


def test_el_aviso_de_stop_sale_aunque_claude_tarde(qtbot, con_clave, ventana, claude_falso):
    # Cuando Claude recibe la llamada, la ventana del stop ya está abierta.
    abiertas = []
    claude_falso.al_llamar = lambda _k: abiertas.append(len(ventana.stop_dialogs))
    ejecutar(qtbot, ventana)
    assert abiertas == [1]


# -- la pantalla Informes ----------------------------------------------------------------------


def test_la_lista_filtra_y_el_lector_ensena_coste_y_sin_ia(ventana, cartera):
    guardar_informe(cartera, periodo="2026-09-20", cuando=AHORA - timedelta(days=5))
    guardar_informe(cartera, periodo="2026-09-21", ia=False, cuando=AHORA - timedelta(days=4))
    guardar_informe(cartera, ReportKind.WEEKLY, periodo="2026-W38", coste="0.52",
                    cuando=AHORA - timedelta(days=3))
    guardar_informe(cartera, ReportKind.MONTHLY, periodo="2026-08", coste="0.38",
                    cuando=AHORA - timedelta(days=2))
    pagina = informes(ventana)
    pagina.reload()  # con la ventana visible, al entrar en la sección (showEvent)
    titulos = [i.report.kind for i in pagina.items]
    assert titulos == [ReportKind.MONTHLY, ReportKind.WEEKLY, ReportKind.DAILY, ReportKind.DAILY]
    # El más reciente, abierto; el mensual lleva su mes.
    assert pagina.heading_label.text() == "Estudio mensual — agosto 2026 · 23/09/2026"
    assert pagina.meta_label.text() == "claude-sonnet-5 · esfuerzo low · 0,38 $"
    pagina.filter_buttons[ReportKind.DAILY].click()
    assert [i.report.period for i in pagina.items] == ["2026-09-21", "2026-09-20"]
    sin_ia = pagina.items[0]
    assert sin_ia.cost_label.objectName() == "chipWarn"
    sin_ia.click()
    assert pagina.meta_label.text() == "Sin análisis de IA · gratis"
    assert "Informe DIARIO del 2026-09-21" in pagina.browser.toPlainText()
    pagina.items[1].click()
    assert pagina.meta_label.text() == "claude-sonnet-5 · esfuerzo low · 0,03 $"
    assert "1.000 tokens de entrada · 300 de salida" in pagina.meta_label.toolTip()
    pagina.filter_buttons[ReportKind.EXPLORATION].click()
    assert pagina.items == [] and pagina.empty_list_label.text() == (
        "No hay informes de este tipo."
    )


def test_sin_informes_lo_dice(ventana):
    pagina = informes(ventana)
    assert pagina.items == [] and not pagina.export_button.isEnabled()
    assert "Todavía no hay informes" in pagina.browser.toPlainText()


def test_exportar_a_md(ventana, cartera, tmp_path):
    guardar_informe(cartera)
    pagina = informes(ventana)
    pagina.reload()
    destino = tmp_path / "Mis informes" / "diario.md"
    destino.parent.mkdir()
    pedidos = []
    pagina.choose_export_path = lambda nombre: pedidos.append(nombre) or destino
    pagina.export_button.click()
    assert pedidos == ["sharky-diario-2026-09-20.md"]
    texto = destino.read_text(encoding="utf-8")
    assert texto.startswith("# Control diario · 20/09/2026\n\n*claude-sonnet-5 · esfuerzo low "
                            "· 0,03 $*\n\nInforme DIARIO")
    assert "Exportado" in pagina.status_label.text()


def test_la_meta_de_un_informe():
    informe = Report(ReportKind.DAILY, "2026-09-25", AHORA, "", True, model="claude-sonnet-5",
                     effort="low sin razonamiento", cost_usd=D("0.031"))
    assert report_meta(informe) == "claude-sonnet-5 · esfuerzo low sin razonamiento · 0,03 $"


def test_se_pinta_en_los_dos_temas(qtbot, con_clave, ventana, tema):
    ejecutar(qtbot, ventana)
    ventana.resize(1400, 900)
    for seccion in ("panel", "informes", "ajustes"):
        ventana.show_section(seccion)
        for eleccion in (Theme.LIGHT, Theme.DARK):
            tema.set_theme(eleccion)
            assert not ventana.grab().isNull()


# -- Ajustes → Claude ---------------------------------------------------------------------------


def claude(ventana) -> ClaudeCard:
    return ventana.page("ajustes").claude_card


def test_guardar_probar_y_borrar_la_clave(qtbot, ventana, keyring_falso):
    card = claude(ventana)
    assert "No hay clave" in card.key_status_label.text()
    assert not card.delete_key_button.isEnabled()
    card.key_edit.setText(f"  {CLAVE} ")
    card.save_key_button.click()
    assert secrets.load_api_key() == CLAVE
    assert card.key_edit.text() == "" and card.key_message.text() == "Clave guardada."
    assert "Hay una clave guardada" in card.key_status_label.text()

    probadas = []

    def probar(clave):
        probadas.append(clave)
        return KeyCheck(KeyStatus.VALID, "Clave válida.")

    card._key_checker = probar
    with qtbot.waitSignal(card.test_button.clicked, timeout=1000):
        card.test_button.click()
    qtbot.waitUntil(lambda: card.key_message.text() == "Clave válida.", timeout=5000)
    assert probadas == [CLAVE]  # sin nada escrito, se prueba la guardada

    card.confirm_delete_key = lambda: True
    card.delete_key_button.click()
    assert secrets.load_api_key() is None
    assert card.key_message.text() == "Clave borrada."


def test_la_clave_nunca_se_ve(ventana):
    from PySide6.QtWidgets import QLineEdit

    assert claude(ventana).key_edit.echoMode() == QLineEdit.EchoMode.Password


def test_modelo_esfuerzo_precios_y_tope_se_guardan(ventana, ajustes, carpeta_de_datos):
    card = claude(ventana)
    card.effort_boxes[AIAction.DAILY].setCurrentText("medium")
    card.budget_edit.setText("12,5")
    entrada, salida = card.price_edits["claude-sonnet-5"]
    entrada.setText("2,5")
    card.search_edit.setText("9")
    card.save_button.click()
    assert card.save_message.text() == "Guardado. Vale desde ya."
    assert ajustes.ai.daily.effort == "medium"
    assert ajustes.ai.monthly_budget_usd == 12.5
    assert ajustes.ai.prices["claude-sonnet-5"].input_per_mtok == 2.5
    assert ajustes.ai.web_search_per_1000_usd == 9.0
    guardados = SettingsStore().load()
    assert guardados.ai.daily.effort == "medium" and guardados.ai.monthly_budget_usd == 12.5
    # El Panel ya lo usa: el diálogo diría esfuerzo medium.
    assert ventana.reports_runner.plan_daily().effort == "medium"


def test_un_modelo_nuevo_pide_su_precio(ventana, ajustes):
    card = claude(ventana)
    caja = card.model_boxes[AIAction.DAILY]
    caja.setCurrentText("claude-opus-5-5")
    card.save_button.click()
    assert "Pon el precio de claude-opus-5-5" in card.save_message.text()
    assert ajustes.ai.daily.model == "claude-sonnet-5"  # no se ha guardado nada
    entrada, salida = card.price_edits["claude-opus-5-5"]
    entrada.setText("4")
    salida.setText("20")
    card.save_button.click()
    assert ajustes.ai.daily.model == "claude-opus-5-5"
    assert ajustes.ai.prices["claude-opus-5-5"].output_per_mtok == 20.0


def test_numeros_que_no_valen(ventana, ajustes):
    card = claude(ventana)
    card.budget_edit.setText("mucho")
    card.search_edit.setText("-1")
    card.save_button.click()
    assert "Tope de gasto mensual: escribe un número." in card.save_message.text()
    assert "Búsqueda web: no puede ser negativo." in card.save_message.text()
    assert ajustes.ai.monthly_budget_usd == 10.0


def test_restaurar_pone_los_de_por_defecto_sin_guardar(ventana, ajustes):
    card = claude(ventana)
    card.budget_edit.setText("3")
    card.save_button.click()
    card.restore_button.click()
    assert card.budget_edit.text() == "10"
    assert ajustes.ai.monthly_budget_usd == 3.0
    card.save_button.click()
    assert ajustes.ai.monthly_budget_usd == 10.0


def test_la_lista_de_modelos_sale_de_claude(qtbot, con_clave, ventana):
    card = claude(ventana)
    card._models_lister = lambda _clave: ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"]
    card.models_button.click()
    qtbot.waitUntil(lambda: "3 modelos" in card.models_message.text(), timeout=5000)
    caja = card.model_boxes[AIAction.DAILY]
    assert [caja.itemText(i) for i in range(caja.count())] == [
        "claude-haiku-4-5", "claude-opus-5", "claude-sonnet-5"
    ]
    assert caja.currentText() == "claude-sonnet-5"


def test_gasto_del_mes_y_estimaciones(ventana, cartera):
    with cartera.transaction() as conn:
        RunRepository(conn).add(Run(AHORA, "manual", "Informe diario", RunStatus.OK,
                                    cost_usd=D("1.234")))
    card = claude(ventana)
    card.refresh()
    assert card.spent_label.text() == "Gasto de septiembre: 1,23 $ de 10,00 $"
    assert card.estimate_labels[AIAction.DAILY].text() == "≈ 0,02–0,05 $"


# -- H10: las tarjetas del semanal y del mensual ------------------------------------------------
# La cartera se creó el domingo 20/09; hoy es el viernes 25/09.


def ejecutar_tarjeta(qtbot, ventana, card, respuesta=True):
    vistos = []

    def confirmar(plan):
        vistos.append(plan)
        return respuesta

    card.confirm_run = confirmar
    with qtbot.waitSignal(ventana.reports_runner.stopped, timeout=10_000):
        card.run_button.click()
    return vistos[0]


def test_las_tres_tarjetas_con_su_precio_y_lo_que_toca(con_clave, ventana):
    panel = ventana.page("panel")
    assert [type(t).__name__ for t in panel.report_cards] == ["DailyCard", "WeeklyCard",
                                                               "MonthlyCard"]
    semanal, mensual = panel.weekly_card, panel.monthly_card
    assert semanal.run_button.text() == "Ejecutar ahora · ≈ 0,30–0,80 $"
    assert mensual.run_button.text() == "Ejecutar ahora · ≈ 0,20–0,60 $"
    # El semanal toca el domingo; el mensual, el 1 de noviembre (septiembre está a medias).
    assert semanal.chip_label.text() == "Próximo: domingo 27/09"
    assert "los domingos, o a los 7 días del último" in semanal.toolTip()
    assert mensual.chip_label.text() == "Próximo: domingo 01/11"
    assert semanal.when_label.text() == "Todavía no hay ningún semanal."
    assert not semanal.read_button.isEnabled()


def test_ejecutar_el_semanal(qtbot, con_clave, ventana, claude_falso):
    claude_falso.respuestas = [mensaje_claude(bloques=[
        texto_citado("## SAN\nRebaja previsiones.", ("https://ejemplo.es/san", "SAN")),
        texto_citado("\n\n## Conclusión de la semana\n- SAN a la baja."),
    ], busquedas=4)]
    card = ventana.page("panel").weekly_card
    etapas = []
    ventana.reports_runner.stageChanged.connect(etapas.append)
    plan = ejecutar_tarjeta(qtbot, ventana, card)
    assert plan.action is AIAction.WEEKLY and plan.effort == "medium" and plan.existing is None
    assert "Claude está buscando noticias en la web…" in etapas
    assert claude_falso.streams[0]["tools"][0]["type"] == "web_search_20250305"
    assert card.chip_label.text() == "Hecho hoy"
    assert card.conclusion_label.text() == "• SAN a la baja."
    assert card.message_label.text().startswith("Semanal guardado. Coste real: 0,05 $ (")
    assert "4 búsquedas" in card.message_label.text()
    # Las otras tarjetas no se dan por enteradas.
    assert not ventana.page("panel").daily_card.message_label.isVisible()
    pagina = informes(ventana)
    assert pagina.current.kind is ReportKind.WEEKLY
    assert pagina.heading_label.text() == "Noticias semanales — del 19/09 al 25/09 · 25/09/2026"
    assert "[SAN](https://ejemplo.es/san)" in pagina.current.markdown


def test_mientras_uno_corre_los_demas_esperan(qtbot, con_clave, ventana):
    panel = ventana.page("panel")
    panel.weekly_card.confirm_run = lambda plan: True
    with qtbot.waitSignal(ventana.reports_runner.stopped, timeout=10_000):
        panel.weekly_card.run_button.click()
        assert not panel.daily_card.run_button.isEnabled()
        assert not panel.monthly_card.run_button.isEnabled()
        assert not panel.daily_card.progress.isVisible()
    assert panel.daily_card.run_button.isEnabled()


def test_ejecutar_el_mensual_deja_las_propuestas_en_tesis(qtbot, con_clave, ventana, cartera,
                                                          claude_falso):
    from sharky.services.reports import ExtractedVerdict, MonthlyVerdicts

    claude_falso.respuestas = [mensaje_claude(
        "## SAN\nEl stop está roto.\n\n## Conclusión del mes\n- Salir de SAN."
    )]
    claude_falso.extracciones = [extraccion(MonthlyVerdicts(verdicts=[ExtractedVerdict(
        ticker="SAN", verdict="MANTENER", reason="Aguanta.", invalidation="Nada.",
        proposed_stop=3.8, proposed_target=None,
    )]))]
    card = ventana.page("panel").monthly_card
    etapas = []
    ventana.reports_runner.stageChanged.connect(etapas.append)
    plan = ejecutar_tarjeta(qtbot, ventana, card)
    # Solo el mes en curso (la cartera es de septiembre): parcial.
    assert [o.month for o in plan.month_options] == [Month(2026, 9)]
    assert plan.study == Month(2026, 9) and plan.month_options[0].partial
    assert EXTRACTION_STAGE in etapas
    assert card.message_label.text().endswith("Añadidos a Tesis: 2 revisiones y propuestas.")
    assert card.when_label.text().startswith("Último: septiembre 2026 (parcial), hoy")
    # En Tesis, la propuesta de Claude, con «Aplicar».
    tesis = ventana.page("tesis")
    assert tesis.select_ticker("SAN")
    fila = next(f for f in tesis.history_rows if f.apply_button is not None)
    assert fila.title_label.text() == "Propuesta de Claude: stop 3,80 EUR"
    assert fila.apply_button.isEnabled()
    revision = next(f for f in tesis.history_rows if f.title_label.text() == "Revisión de Claude")
    assert "MANTENER" in revision.detail_label.text()
    informe = informes(ventana).current
    assert informe.kind is ReportKind.MONTHLY
    assert report_title(informe) == "Estudio mensual — septiembre 2026 (parcial)"


def test_el_dialogo_del_mensual_deja_elegir_el_mes(qtbot, ventana, cartera):
    from sharky.ui.reports import MonthlyRunDialog, MonthOption, RunPlan

    Mes = Month

    plan = ventana.reports_runner.plan(AIAction.MONTHLY)
    existente = Report(ReportKind.MONTHLY, "2026-08", AHORA - timedelta(days=24), "…", True)
    plan = RunPlan(plan.action, plan.model, plan.effort, True, True, plan.budget, None,
                   plan.month, (MonthOption(Mes(2026, 8), False, existente),
                                MonthOption(Mes(2026, 9), True, None)), Mes(2026, 8))
    dialogo = MonthlyRunDialog(plan)
    qtbot.addWidget(dialogo)
    assert dialogo.chosen == Mes(2026, 8)
    assert "Ya hay un estudio de ese mes (hecho el 01/09/2026): se sustituirá." in (
        dialogo.info_label.text())
    assert dialogo.accept_button.text() == "Ejecutar (≈ 0,20–0,60 $)"
    dialogo.choose(Mes(2026, 9))
    assert dialogo.chosen == Mes(2026, 9) and "se sustituirá" not in dialogo.info_label.text()
    assert [b.text() for b in dialogo.month_buttons.values()] == [
        "Mes anterior: agosto de 2026", "Mes en curso (parcial): septiembre de 2026, hasta hoy",
    ]


def test_titulos_de_semanales_y_mensuales():
    semanal = Report(ReportKind.WEEKLY, "2026-09-27", AHORA, "", True)
    assert report_title(semanal) == "Noticias semanales — del 21/09 al 27/09"
    completo = Report(ReportKind.MONTHLY, "2026-08", AHORA, "", True)
    assert report_title(completo) == "Estudio mensual — agosto 2026"
    raro = Report(ReportKind.WEEKLY, "2026-W38", AHORA, "", True)
    assert report_title(raro) == "Noticias semanales"


def test_el_mes_que_se_propone(ventana, cartera):
    runner = ventana.reports_runner
    runner._now = lambda: AHORA.replace(month=10, day=3)  # reloj falso: sábado 03/10
    # Septiembre (el primer mes, incompleto) no sale solo, pero a mano se propone él.
    plan = runner.plan(AIAction.MONTHLY)
    assert [o.month for o in plan.month_options] == [Month(2026, 9), Month(2026, 10)]
    assert plan.study == Month(2026, 9) and plan.existing is None
    # Con su estudio ya hecho, se propone el mes en curso.
    guardar_informe(cartera, ReportKind.MONTHLY, periodo="2026-09",
                    cuando=AHORA.replace(month=10, day=1))
    plan = runner.plan(AIAction.MONTHLY)
    assert plan.study == Month(2026, 10) and plan.existing is None
    assert plan.option(Month(2026, 9)).existing is not None


def test_la_tarjeta_del_mensual_dice_lo_que_toca(ventana, cartera):
    card = ventana.page("panel").monthly_card
    noviembre = AHORA.replace(month=11, day=2)  # reloj falso: lunes 02/11
    ventana.reports_runner._now = card._now = lambda: noviembre
    card.refresh()
    assert card.chip_label.text() == "Toca: octubre" and card.chip.objectName() == "chipWarn"
    guardar_informe(cartera, ReportKind.MONTHLY, periodo="2026-10", coste="0.41",
                    cuando=noviembre.replace(day=1))
    card.refresh()
    assert card.chip_label.text() == "Hecho: octubre" and card.chip.objectName() == "chipOk"
    assert card.when_label.text().startswith("octubre 2026, 01/11/2026")
    assert card.conclusion_label.text() == "• Todo en orden."
