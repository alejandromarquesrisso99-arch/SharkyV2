"""La ventana principal: las siete secciones y el cambio de tema en caliente."""

import pytest
from PySide6.QtWidgets import QLabel, QPushButton, QRadioButton

from sharky.ui.main_window import MainWindow
from sharky.ui.pages import SECTIONS, PlaceholderPage, SettingsPage
from sharky.ui.theme import Theme, ThemeController

VERSION_DE_PRUEBA = "9.9.9"


@pytest.fixture
def controlador(qapp):
    return ThemeController(qapp, Theme.LIGHT)


@pytest.fixture
def ventana(qtbot, controlador):
    v = MainWindow(controlador, VERSION_DE_PRUEBA)
    qtbot.addWidget(v)
    return v


def _todo_el_texto(widget) -> str:
    return " ".join(e.text() for e in widget.findChildren(QLabel))


def boton_de_tema(ventana) -> QPushButton:
    botones = [b for b in ventana.findChildren(QPushButton) if b.text().startswith("Tema ")]
    assert len(botones) == 1
    return botones[0]


def test_hay_siete_secciones_y_en_el_orden_de_la_guia():
    claves = [s.key for s in SECTIONS]
    assert claves == ["panel", "cartera", "operar", "tesis", "radar", "informes", "ajustes"]


def test_la_ventana_abre_en_el_panel(ventana):
    assert ventana.current_section == "panel"
    assert ventana.windowTitle() == "Sharky — Panel"


def test_se_puede_ir_a_cada_seccion(ventana):
    for seccion in SECTIONS:
        ventana.show_section(seccion.key)
        assert ventana.current_section == seccion.key
        assert ventana.windowTitle() == f"Sharky — {seccion.label}"


def test_el_lateral_tiene_un_boton_por_seccion(ventana, qtbot):
    for seccion in SECTIONS:
        boton = ventana._buttons[seccion.key]
        assert boton.text() == seccion.label
    ventana._buttons["radar"].click()
    assert ventana.current_section == "radar"
    assert ventana._buttons["radar"].isChecked()
    assert not ventana._buttons["panel"].isChecked()


def test_una_seccion_que_no_existe_da_error(ventana):
    with pytest.raises(KeyError):
        ventana.show_section("bolsa")


def test_las_seis_primeras_secciones_estan_vacias_y_lo_dicen(ventana):
    for seccion in SECTIONS[:-1]:
        pagina = ventana._pages[seccion.key]
        assert isinstance(pagina, PlaceholderPage)
        texto = _todo_el_texto(pagina)
        assert "Todavía no hay nada aquí" in texto
        assert seccion.milestone in texto


def test_ajustes_ensena_la_version_y_la_carpeta_de_datos(ventana, carpeta_de_datos):
    pagina = ventana._pages["ajustes"]
    assert isinstance(pagina, SettingsPage)
    texto = _todo_el_texto(pagina)
    assert VERSION_DE_PRUEBA in texto
    assert str(carpeta_de_datos) in texto


def test_el_boton_de_la_cabecera_cambia_el_tema(ventana, controlador, qtbot):
    boton = boton_de_tema(ventana)
    assert controlador.effective is Theme.LIGHT
    assert boton.text() == "Tema oscuro"

    boton.click()
    assert controlador.effective is Theme.DARK
    assert boton.text() == "Tema claro"

    boton.click()
    assert controlador.effective is Theme.LIGHT


def test_apariencia_elige_el_tema_y_el_boton_se_entera(ventana, controlador):
    pagina = ventana._pages["ajustes"]
    radios = {b.text().split("  ·")[0]: b for b in pagina.findChildren(QRadioButton)}
    assert set(radios) == {"Claro", "Oscuro", "El que tenga Windows"}

    radios["Oscuro"].click()
    assert controlador.choice is Theme.DARK
    assert boton_de_tema(ventana).text() == "Tema claro"

    radios["El que tenga Windows"].click()
    assert controlador.choice is Theme.SYSTEM
    assert controlador.effective in (Theme.LIGHT, Theme.DARK)


def test_el_boton_de_la_cabecera_deja_marcado_el_radio_correcto(ventana, controlador):
    pagina = ventana._pages["ajustes"]
    radios = {b.text().split("  ·")[0]: b for b in pagina.findChildren(QRadioButton)}
    boton_de_tema(ventana).click()  # claro -> oscuro
    assert radios["Oscuro"].isChecked()
    assert not radios["Claro"].isChecked()


def test_traer_al_frente_no_revienta(ventana):
    ventana.bring_to_front()
    assert ventana.isVisible()
