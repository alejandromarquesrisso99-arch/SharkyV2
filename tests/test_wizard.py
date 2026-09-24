"""El asistente de primer arranque con pytest-qt: sin red, sin clave real y con datos inventados."""

from datetime import date
from decimal import Decimal as D

import pytest
from PySide6.QtWidgets import QDialog, QWizard

from sharky.core.csv_import import TEMPLATE_CSV, parse_positions_csv
from sharky.core.ledger import build_ledger
from sharky.core.models import CashKind
from sharky.services import secrets
from sharky.services.ai import KeyCheck, KeyStatus
from sharky.services.repositories import (
    CashMovementRepository,
    NavSnapshotRepository,
    TradeRepository,
    has_portfolio,
)
from sharky.services.settings import Settings, SettingsStore
from sharky.ui.wizard import SetupWizard

HOY = date(2026, 9, 24)
CLAVE = "sk-ant-inventada-5678"
CSV_CON_ERRORES = (
    b"ticker;nombre;isin;unidades;coste_medio_eur;divisa\n"
    b"SAN;Banco;;200;4,5;EUR\n"
    b"MAL TICKER;Uno;;10;5;EUR\n"
    b"SAN;Repetido;;1;1;EUR\n"
)


class Comprobador:
    """«Probar clave» de mentira: devuelve lo que se le diga y apunta lo que recibe."""

    def __init__(self, status: KeyStatus = KeyStatus.VALID, message: str = "Clave válida.") -> None:
        self.resultado = KeyCheck(status, message)
        self.claves: list[str] = []

    def __call__(self, clave: str) -> KeyCheck:
        self.claves.append(clave)
        return self.resultado


@pytest.fixture
def comprobador():
    return Comprobador()


@pytest.fixture
def crear_asistente(qtbot, db, comprobador):
    def crear(**opciones) -> SetupWizard:
        parametros = dict(key_checker=comprobador, has_saved_key=False, today=lambda: HOY)
        parametros.update(opciones)
        asistente = SetupWizard(db, SettingsStore(), Settings(), **parametros)
        qtbot.addWidget(asistente)
        asistente.errores = []
        asistente.show_error = asistente.errores.append
        asistente.portfolio_page.show_error = asistente.errores.append
        asistente.show()
        return asistente

    return crear


@pytest.fixture
def asistente(crear_asistente):
    return crear_asistente()


def boton(asistente, cual: QWizard.WizardButton):
    return asistente.button(cual)


def siguiente(asistente) -> None:
    boton(asistente, QWizard.WizardButton.NextButton).click()


def probar_clave(asistente, qtbot, clave: str = CLAVE) -> None:
    pagina = asistente.key_page
    pagina.key_edit.setText(clave)
    pagina.test_button.click()
    qtbot.waitUntil(lambda: not pagina.checking, timeout=5000)


def adjuntar(asistente, tmp_path, contenido: bytes = TEMPLATE_CSV.encode("utf-8-sig")):
    ruta = tmp_path / "posiciones.csv"
    ruta.write_bytes(contenido)
    asistente.portfolio_page.choose_csv_file = lambda: ruta
    asistente.portfolio_page.attach_button.click()
    return ruta


def crear_cartera(asistente, qtbot) -> None:
    assert asistente.currentPage() is asistente.summary_page
    with qtbot.waitSignal(asistente.finished, timeout=5000):
        boton(asistente, QWizard.WizardButton.FinishButton).click()


# -- el camino completo -----------------------------------------------------------------


def test_csv_valido_y_confirmar_crea_la_cartera(asistente, qtbot, db, tmp_path, keyring_falso):
    assert asistente.currentPage() is asistente.key_page
    probar_clave(asistente, qtbot)
    assert asistente.key_page.result_label.text() == "Clave válida."
    siguiente(asistente)

    cartera = asistente.portfolio_page
    assert asistente.currentPage() is cartera
    adjuntar(asistente, tmp_path)
    assert cartera.preview.isVisibleTo(asistente)
    assert "3 posiciones leídas" in cartera.preview_title.text()
    assert cartera.table.rowCount() == 3
    cartera.cash_edit.setText("2.900,00")
    assert cartera.broker() == "Trade Republic"  # por defecto
    # Nada se ha escrito todavía.
    assert not has_portfolio(db.connection())
    siguiente(asistente)

    resumen = asistente.summary_page
    assert resumen.values["positions"].text() == "3 posiciones · coste 2.800,00 €"
    assert resumen.values["cash"].text() == "2.900,00 €"
    assert resumen.values["nav"].text() == "5.700,00 €"
    assert resumen.values["key"].text().startswith("Comprobada")
    assert resumen.autostart.isChecked()  # marcada por defecto
    assert not has_portfolio(db.connection())

    crear_cartera(asistente, qtbot)
    assert asistente.result() == QDialog.DialogCode.Accepted
    assert asistente.errores == []

    conn = db.connection()
    assert has_portfolio(conn)
    operaciones = TradeRepository(conn).list_all()
    assert {o.trade_date for o in operaciones} == {HOY}
    assert CashMovementRepository(conn).balance() == D("2900.00")
    assert NavSnapshotRepository(conn).get(HOY).unit_value == D("100")
    ajustes = SettingsStore().load()
    assert ajustes.portfolio.broker == "Trade Republic"
    assert ajustes.automation.start_with_windows is True
    assert secrets.load_api_key() == CLAVE


def test_cancelar_no_escribe_nada(asistente, qtbot, db, tmp_path, keyring_falso):
    probar_clave(asistente, qtbot)
    siguiente(asistente)
    adjuntar(asistente, tmp_path)
    asistente.portfolio_page.cash_edit.setText("1000")
    siguiente(asistente)
    asistente.summary_page.autostart.setChecked(False)

    boton(asistente, QWizard.WizardButton.CancelButton).click()
    assert asistente.result() == QDialog.DialogCode.Rejected
    assert not asistente.isVisible()
    assert not has_portfolio(db.connection())
    assert NavSnapshotRepository(db.connection()).list_all() == []
    assert not SettingsStore().path.exists()
    assert keyring_falso.almacen == {}


def test_se_puede_empezar_solo_con_efectivo(asistente, qtbot, db):
    siguiente(asistente)  # sin clave: se omite
    asistente.portfolio_page.cash_edit.setText("1500")
    siguiente(asistente)
    assert asistente.summary_page.values["positions"].text().startswith("Ninguna")
    assert asistente.summary_page.values["key"].text().startswith("Sin clave")
    crear_cartera(asistente, qtbot)

    conn = db.connection()
    assert has_portfolio(conn)
    assert TradeRepository(conn).list_all() == []
    (inicial,) = CashMovementRepository(conn).list_all()
    assert (inicial.kind, inicial.amount_eur) == (CashKind.INITIAL, D("1500"))
    assert NavSnapshotRepository(conn).get(HOY).reliable is True
    assert secrets.load_api_key() is None


def test_la_casilla_de_inicio_con_windows_se_guarda_desmarcada(asistente, qtbot):
    siguiente(asistente)
    asistente.portfolio_page.cash_edit.setText("10")
    asistente.portfolio_page.broker_edit.setText("  Mi bróker  ")
    siguiente(asistente)
    asistente.summary_page.autostart.setChecked(False)
    crear_cartera(asistente, qtbot)
    ajustes = SettingsStore().load()
    assert ajustes.automation.start_with_windows is False
    assert ajustes.portfolio.broker == "Mi bróker"


# -- el CSV en la página 2 --------------------------------------------------------------


def test_un_csv_con_errores_los_enseña_todos_y_no_deja_seguir(asistente, tmp_path):
    siguiente(asistente)
    adjuntar(asistente, tmp_path, CSV_CON_ERRORES)
    cartera = asistente.portfolio_page
    asistente.portfolio_page.cash_edit.setText("100")
    assert not cartera.isComplete()
    assert not boton(asistente, QWizard.WizardButton.NextButton).isEnabled()
    assert cartera.errors_box.isVisibleTo(asistente)
    texto = cartera.errors_label.text()
    assert "Línea 3 (MAL TICKER)" in texto and "Línea 4 (SAN)" in texto
    assert "Errores (2 en 2 líneas)" in cartera.errors_title.text()

    siguiente(asistente)
    assert asistente.currentPage() is cartera  # sigue aquí

    # Quitar el CSV deja empezar solo con efectivo.
    cartera.clear_button.click()
    assert cartera.isComplete()
    assert not cartera.preview.isVisibleTo(asistente)


def test_sin_simbolo_es_un_aviso_que_no_bloquea(asistente, tmp_path):
    siguiente(asistente)
    adjuntar(
        asistente,
        tmp_path,
        b"ticker;nombre;isin;unidades;coste_medio_eur;divisa\nVUSA;Vanguard;IE00B3XXRP09;12;95;EUR\n",
    )
    cartera = asistente.portfolio_page
    assert cartera.isComplete()
    assert cartera.warnings_box.isVisibleTo(asistente)
    assert "Línea 2 (VUSA): sin símbolo" in cartera.warnings_label.text()
    celda = cartera.table.cellWidget(0, cartera.SYMBOL_COLUMN)
    assert celda.text() == "sin símbolo"
    siguiente(asistente)
    assert "VUSA" in asistente.summary_page.symbol_warning.text()


def test_el_coste_medio_se_corrige_en_la_vista_previa(asistente, qtbot, db, tmp_path):
    siguiente(asistente)
    adjuntar(asistente, tmp_path)
    cartera = asistente.portfolio_page
    fila_aapl = next(f for f in range(3) if cartera.table.item(f, 0).text() == "AAPL")
    cartera.set_avg_cost_text(fila_aapl, "162,3")
    assert cartera.table.item(fila_aapl, cartera.COST_COLUMN).text() == "162,30"
    assert {p.ticker: p.avg_cost_eur for p in cartera.positions()}["AAPL"] == D("162.3")

    # Un valor que no vale se rechaza y se mantiene el anterior.
    cartera.set_avg_cost_text(fila_aapl, "gratis")
    assert cartera.table.item(fila_aapl, cartera.COST_COLUMN).text() == "162,30"
    assert "AAPL" in cartera.cost_status.text()
    cartera.set_avg_cost_text(fila_aapl, "0")
    assert {p.ticker: p.avg_cost_eur for p in cartera.positions()}["AAPL"] == D("162.3")

    siguiente(asistente)
    assert asistente.summary_page.values["positions"].text() == "3 posiciones · coste 2.923,00 €"
    crear_cartera(asistente, qtbot)
    libro = build_ledger(TradeRepository(db.connection()).list_all())
    assert libro.position("AAPL").avg_cost_eur == D("162.3")


def test_guardar_la_plantilla(asistente, tmp_path):
    siguiente(asistente)
    destino = tmp_path / "carpeta con espacios" / "plantilla_sharky.csv"
    destino.parent.mkdir()
    asistente.portfolio_page.choose_template_destination = lambda: destino
    asistente.portfolio_page.template_button.click()
    datos = destino.read_bytes()
    assert datos.startswith(b"\xef\xbb\xbf")  # UTF-8 con BOM, para Excel
    assert b"\r\n" in datos
    assert datos.decode("utf-8-sig").replace("\r\n", "\n") == TEMPLATE_CSV
    lectura = parse_positions_csv(datos)
    assert lectura.ok and len(lectura.positions) == 3
    assert "Plantilla guardada" in asistente.portfolio_page.file_status.text()


def test_un_fichero_que_no_se_puede_leer_se_explica(asistente, tmp_path):
    siguiente(asistente)
    asistente.portfolio_page.choose_csv_file = lambda: tmp_path / "no-existe.csv"
    asistente.portfolio_page.attach_button.click()
    assert asistente.errores and "no-existe.csv" in asistente.errores[0]
    assert asistente.portfolio_page.csv_import is None


# -- efectivo y bróker ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("escrito", "reescrito"),
    [("2900", "2.900,00"), ("2900.5", "2.900,50"), ("2.900,00", "2.900,00"), ("2.900", "2,90")],
)
def test_el_efectivo_se_reescribe_en_formato_espanol(asistente, escrito, reescrito):
    siguiente(asistente)
    campo = asistente.portfolio_page.cash_edit
    campo.setText(escrito)
    campo.editingFinished.emit()
    assert campo.text() == reescrito


@pytest.mark.parametrize("escrito", ["mucho", "-5"])
def test_efectivo_no_valido_no_deja_seguir(asistente, escrito):
    siguiente(asistente)
    cartera = asistente.portfolio_page
    cartera.cash_edit.setText(escrito)
    assert not cartera.isComplete()
    assert cartera.cash_error.text()


def test_sin_posiciones_ni_efectivo_no_deja_seguir(asistente):
    siguiente(asistente)
    cartera = asistente.portfolio_page
    assert not cartera.isComplete()  # sin CSV y con 0 €
    assert "al menos una posición o algo de efectivo" in cartera.nav_hint.text()
    cartera.cash_edit.setText("0,01")
    assert cartera.isComplete()


def test_el_broker_es_obligatorio(asistente):
    siguiente(asistente)
    cartera = asistente.portfolio_page
    cartera.cash_edit.setText("10")
    cartera.broker_edit.setText("   ")
    assert not cartera.isComplete()
    assert cartera.broker_error.text()


# -- la clave en la página 1 ------------------------------------------------------------


def test_probar_clave_usa_el_comprobador_y_no_la_red(asistente, qtbot, comprobador, claude_falso):
    probar_clave(asistente, qtbot, f"  {CLAVE} ")
    assert comprobador.claves == [CLAVE]
    assert claude_falso.creados == []


def test_una_clave_no_valida_no_deja_seguir_hasta_cambiarla(crear_asistente, qtbot):
    asistente = crear_asistente(
        key_checker=Comprobador(KeyStatus.INVALID, "Clave no válida: Claude la ha rechazado.")
    )
    pagina = asistente.key_page
    probar_clave(asistente, qtbot)
    assert pagina.result_label.objectName() == "dangerText"
    assert not pagina.isComplete()
    assert not boton(asistente, QWizard.WizardButton.NextButton).isEnabled()
    pagina.key_edit.setText(CLAVE + "x")  # otra clave, sin probar: se puede seguir
    assert pagina.isComplete()
    pagina.key_edit.setText("")  # sin clave: también
    assert pagina.isComplete()


def test_sin_conexion_se_guarda_sin_comprobar(crear_asistente, qtbot, keyring_falso):
    sin_red = Comprobador(KeyStatus.OFFLINE, "Sin conexión: la clave se guardará sin comprobar.")
    asistente = crear_asistente(key_checker=sin_red)
    probar_clave(asistente, qtbot)
    assert asistente.key_page.isComplete()
    assert asistente.key_page.result_label.objectName() == "warnText"
    siguiente(asistente)
    asistente.portfolio_page.cash_edit.setText("100")
    siguiente(asistente)
    assert "sin comprobar" in asistente.summary_page.values["key"].text()
    crear_cartera(asistente, qtbot)
    assert secrets.load_api_key() == CLAVE


def test_una_clave_sin_probar_tambien_se_guarda(asistente, qtbot, keyring_falso):
    asistente.key_page.key_edit.setText(CLAVE)
    siguiente(asistente)
    asistente.portfolio_page.cash_edit.setText("100")
    siguiente(asistente)
    assert "no has pulsado «Probar clave»" in asistente.summary_page.values["key"].text()
    crear_cartera(asistente, qtbot)
    assert secrets.load_api_key() == CLAVE


def test_si_ya_habia_clave_en_blanco_se_conserva(crear_asistente, qtbot, keyring_falso):
    keyring_falso.set_password(secrets.SERVICE, secrets.API_KEY_USER, "la-de-antes")
    asistente = crear_asistente(has_saved_key=None)  # la mira en el Administrador
    assert asistente.key_page.has_saved_key
    siguiente(asistente)
    asistente.portfolio_page.cash_edit.setText("100")
    siguiente(asistente)
    assert asistente.summary_page.values["key"].text().startswith("Se conserva")
    crear_cartera(asistente, qtbot)
    assert secrets.load_api_key() == "la-de-antes"


# -- si falla al guardar ----------------------------------------------------------------


def test_si_falla_al_crear_la_cartera_no_hay_nada_y_se_puede_reintentar(
    asistente, qtbot, db, monkeypatch
):
    def revienta(*_args, **_kwargs):
        raise RuntimeError("la base de datos está bloqueada")

    monkeypatch.setattr("sharky.ui.wizard.apply_setup", revienta)
    siguiente(asistente)
    asistente.portfolio_page.cash_edit.setText("100")
    siguiente(asistente)
    boton(asistente, QWizard.WizardButton.FinishButton).click()
    qtbot.waitUntil(lambda: bool(asistente.errores), timeout=5000)
    assert "no se ha guardado nada" in asistente.errores[0]
    assert "bloqueada" in asistente.errores[0]
    assert asistente.isVisible() and not asistente.busy
    assert not has_portfolio(db.connection())
    assert boton(asistente, QWizard.WizardButton.FinishButton).isEnabled()


def test_mientras_guarda_no_se_puede_cancelar(asistente):
    asistente._set_busy(True)
    asistente.reject()
    assert asistente.isVisible()
    assert not boton(asistente, QWizard.WizardButton.CancelButton).isEnabled()
    asistente._set_busy(False)
    asistente.reject()
    assert not asistente.isVisible()
