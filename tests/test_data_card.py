"""Ajustes → Datos: copia en segundo plano y restauración con confirmación y reinicio."""

from datetime import date, datetime
from decimal import Decimal as D

import pytest
from PySide6.QtWidgets import QDialog

from sharky.core.models import CashKind, CashMovement
from sharky.services.backup import create_backup, list_backups
from sharky.services.repositories import CashMovementRepository
from sharky.ui.main_window import MainWindow
from sharky.ui.pages import DataCard, SettingsPage
from sharky.ui.theme import Theme, ThemeController


def poner_efectivo(db, importe):
    with db.transaction() as conn:
        conn.execute("DELETE FROM cash_movements")
        CashMovementRepository(conn).add(
            CashMovement(date(2026, 9, 1), CashKind.INITIAL, D(importe))
        )


@pytest.fixture
def tarjeta(qtbot, db):
    t = DataCard(db)
    qtbot.addWidget(t)
    return t


def test_sin_copias_lo_dice(tarjeta):
    assert "Todavía no hay ninguna copia" in tarjeta._last_label.text()
    assert "14 últimas" in tarjeta._last_label.text()


def test_copia_ahora_en_segundo_plano(tarjeta, qtbot):
    tarjeta.backup_button.click()
    assert not tarjeta.backup_button.isEnabled()  # ocupada mientras trabaja
    qtbot.waitUntil(lambda: tarjeta.backup_button.isEnabled(), timeout=5000)
    assert len(list_backups()) == 1
    assert "Copia guardada" in tarjeta.status_label.text()
    assert "Última copia" in tarjeta._last_label.text()


def test_restaurar_pide_confirmacion_y_si_no_no_toca_nada(tarjeta, db, monkeypatch):
    poner_efectivo(db, "1000")
    copia = create_backup(db, datetime(2026, 9, 23, 20, 0, 0))
    poner_efectivo(db, "5")
    preguntas = []
    monkeypatch.setattr(tarjeta, "choose_backup_file", lambda: copia)
    monkeypatch.setattr(tarjeta, "confirm_restore", lambda ruta: preguntas.append(ruta) or False)
    tarjeta.restore_button.click()
    assert preguntas == [copia]
    assert CashMovementRepository(db.connection()).balance() == D("5")
    assert tarjeta.restore_button.isEnabled()


def test_cancelar_la_eleccion_de_fichero_no_hace_nada(tarjeta, monkeypatch):
    monkeypatch.setattr(tarjeta, "choose_backup_file", lambda: None)
    monkeypatch.setattr(tarjeta, "confirm_restore", lambda ruta: pytest.fail("no debe preguntar"))
    tarjeta.restore_button.click()


def test_restaurar_confirmado_trae_los_datos_y_pide_reiniciar(tarjeta, db, qtbot, monkeypatch):
    poner_efectivo(db, "1000")
    copia = create_backup(db, datetime(2026, 9, 23, 20, 0, 0))
    poner_efectivo(db, "5")
    avisos = []
    monkeypatch.setattr(tarjeta, "choose_backup_file", lambda: copia)
    monkeypatch.setattr(tarjeta, "confirm_restore", lambda ruta: True)
    monkeypatch.setattr(tarjeta, "notify_restart", avisos.append)
    with qtbot.waitSignal(tarjeta.restartRequested, timeout=5000):
        tarjeta.restore_button.click()
    assert CashMovementRepository(db.connection()).balance() == D("1000")
    assert avisos and avisos[0].restored_from == copia
    assert not tarjeta.restore_button.isEnabled()  # ya no se toca nada: se va a reiniciar


def test_una_copia_que_no_vale_se_explica_y_no_reinicia(tarjeta, qtbot, monkeypatch, tmp_path):
    rota = tmp_path / "rota.db"
    rota.write_bytes(b"no soy una base de datos" * 50)
    errores = []
    monkeypatch.setattr(tarjeta, "choose_backup_file", lambda: rota)
    monkeypatch.setattr(tarjeta, "confirm_restore", lambda ruta: True)
    monkeypatch.setattr(tarjeta, "show_error", errores.append)
    reinicios = []
    tarjeta.restartRequested.connect(lambda: reinicios.append(1))
    tarjeta.restore_button.click()
    qtbot.waitUntil(lambda: bool(errores), timeout=5000)
    assert "no es una copia de Sharky" in errores[0]
    assert tarjeta.restore_button.isEnabled()
    assert reinicios == []


def test_la_peticion_de_reinicio_llega_a_la_ventana(qtbot, qapp, db):
    ventana = MainWindow(ThemeController(qapp, Theme.LIGHT), "9.9.9", db=db)
    qtbot.addWidget(ventana)
    pagina = ventana._pages["ajustes"]
    assert isinstance(pagina, SettingsPage)
    assert isinstance(pagina.data_card, DataCard)
    with qtbot.waitSignal(ventana.restartRequested, timeout=1000):
        pagina.data_card.restartRequested.emit()


def test_sin_base_de_datos_no_hay_tarjeta_de_datos(qtbot, qapp):
    ventana = MainWindow(ThemeController(qapp, Theme.LIGHT), "9.9.9")
    qtbot.addWidget(ventana)
    assert ventana._pages["ajustes"].data_card is None


# -- borrar la cartera ------------------------------------------------------------------


def test_borrar_la_cartera_pide_confirmacion_y_si_no_no_toca_nada(tarjeta, db, monkeypatch):
    poner_efectivo(db, "1000")
    monkeypatch.setattr(tarjeta, "confirm_wipe", lambda: False)
    tarjeta.wipe_button.click()
    assert CashMovementRepository(db.connection()).balance() == D("1000")
    assert list_backups() == []
    assert tarjeta.wipe_button.isEnabled()


def test_borrar_la_cartera_confirmado_la_vacia_y_pide_reiniciar(tarjeta, db, qtbot, monkeypatch):
    from sharky.services.repositories import has_portfolio

    poner_efectivo(db, "1000")
    avisos = []
    monkeypatch.setattr(tarjeta, "confirm_wipe", lambda: True)
    monkeypatch.setattr(tarjeta, "notify_wiped", avisos.append)
    with qtbot.waitSignal(tarjeta.restartRequested, timeout=5000):
        tarjeta.wipe_button.click()
    assert not has_portfolio(db.connection())  # al reiniciar, sale el asistente
    (copia,) = list_backups()
    assert avisos == [copia]
    assert copia.name.endswith("-antes-de-borrar.db")
    assert not tarjeta.wipe_button.isEnabled()  # ya no se toca nada: se va a reiniciar


def test_el_aviso_explica_que_se_borra_que_se_conserva_y_cuanto_dura_la_copia(qtbot):
    from sharky.ui.pages import WipeConfirmDialog

    dialogo = WipeConfirmDialog()
    qtbot.addWidget(dialogo)
    texto = dialogo.explanation.text()
    for palabra in ("posiciones", "tesis", "informes", "radar"):
        assert palabra in texto
    assert "Se conservan los ajustes, la clave de Claude y las copias" in texto
    assert "14 copias más recientes" in texto
    assert "dos semanas" in texto
    assert "asistente" in texto


def test_solo_se_puede_borrar_tras_escribir_borrar(qtbot):
    from sharky.ui.pages import WipeConfirmDialog

    dialogo = WipeConfirmDialog()
    qtbot.addWidget(dialogo)
    assert not dialogo.wipe_button.isEnabled()
    assert dialogo.cancel_button.isDefault()  # Intro no borra nada
    for texto in ("borrar", "BORRA", "SÍ", "BORRAR YA"):
        dialogo.confirm_edit.setText(texto)
        assert not dialogo.wipe_button.isEnabled(), texto
    dialogo.confirm_edit.setText(" BORRAR ")
    assert dialogo.wipe_button.isEnabled()
    dialogo.wipe_button.click()
    assert dialogo.result() == QDialog.DialogCode.Accepted
