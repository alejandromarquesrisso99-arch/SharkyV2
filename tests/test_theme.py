"""Tema claro y tema oscuro: tokens, contraste y que nadie pinte por su cuenta."""

import re
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QPalette

import sharky
from sharky.ui import theme
from sharky.ui.theme import Theme, ThemeController, apply_theme

CONTRASTE_MINIMO = 4.5
FONDOS = ("background", "surface", "surface_alt", "surface_selected")
TEXTOS = ("text", "text_muted", "ok", "warn", "danger", "accent")

CARPETA_UI = Path(sharky.__file__).resolve().parent / "ui"
COLOR_A_MANO = re.compile(r"""["']#[0-9A-Fa-f]{3,8}["']|QColor\(|rgba?\(""")


def luminancia(hexadecimal: str) -> float:
    color = QColor(hexadecimal)
    canales = []
    for c in (color.redF(), color.greenF(), color.blueF()):
        canales.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * canales[0] + 0.7152 * canales[1] + 0.0722 * canales[2]


def contraste(uno: str, otro: str) -> float:
    a, b = luminancia(uno), luminancia(otro)
    claro, oscuro = max(a, b), min(a, b)
    return (claro + 0.05) / (oscuro + 0.05)


@pytest.mark.parametrize("tema", [Theme.LIGHT, Theme.DARK])
def test_los_dos_temas_definen_los_mismos_tokens(tema):
    assert set(theme.TOKENS[tema]) == set(theme.TOKEN_KEYS)


@pytest.mark.parametrize("tema", [Theme.LIGHT, Theme.DARK])
def test_todos_los_tokens_son_colores_validos(tema):
    for nombre, valor in theme.TOKENS[tema].items():
        assert QColor(valor).isValid(), f"{tema}/{nombre} no es un color"


@pytest.mark.parametrize("tema", [Theme.LIGHT, Theme.DARK])
def test_el_contraste_llega_a_4_5(tema):
    tokens = theme.TOKENS[tema]
    for fondo in FONDOS:
        for texto in TEXTOS:
            razon = contraste(tokens[texto], tokens[fondo])
            assert razon >= CONTRASTE_MINIMO, (
                f"{tema}: {texto} sobre {fondo} se queda en {razon:.2f}:1"
            )


@pytest.mark.parametrize("tema", [Theme.LIGHT, Theme.DARK])
def test_el_texto_sobre_el_acento_tambien_contrasta(tema):
    tokens = theme.TOKENS[tema]
    assert contraste(tokens["accent_text"], tokens["accent"]) >= CONTRASTE_MINIMO


def test_ningun_widget_escribe_un_color_a_mano():
    """Toda la paleta vive en ui/theme.py (GUIA §5.10)."""
    culpables = []
    for fichero in CARPETA_UI.glob("*.py"):
        if fichero.name == "theme.py":
            continue
        for numero, linea in enumerate(fichero.read_text(encoding="utf-8").splitlines(), 1):
            if COLOR_A_MANO.search(linea):
                culpables.append(f"{fichero.name}:{numero}: {linea.strip()}")
    assert not culpables, "colores fuera de theme.py:\n" + "\n".join(culpables)


def test_el_tema_del_sistema_se_resuelve_a_claro_u_oscuro(qapp):
    assert theme.resolve(Theme.SYSTEM) in (Theme.LIGHT, Theme.DARK)
    assert theme.resolve(Theme.DARK) is Theme.DARK


def test_aplicar_un_tema_cambia_la_paleta_y_la_hoja_de_estilo(qapp):
    apply_theme(qapp, Theme.LIGHT)
    claro = qapp.palette().color(QPalette.ColorRole.Window).name()
    apply_theme(qapp, Theme.DARK)
    oscuro = qapp.palette().color(QPalette.ColorRole.Window).name()
    assert claro != oscuro
    assert claro.lower() == theme.LIGHT_TOKENS["background"].lower()
    assert oscuro.lower() == theme.DARK_TOKENS["background"].lower()
    assert theme.DARK_TOKENS["accent"] in qapp.styleSheet()

    # Con hoja de estilo, Qt envuelve el estilo en un proxy sin nombre: se mira sin ella.
    qapp.setStyleSheet("")
    assert qapp.style().name().lower() == "fusion"
    apply_theme(qapp, Theme.LIGHT)


def test_el_controlador_cambia_de_tema_y_avisa(qapp):
    controlador = ThemeController(qapp, Theme.LIGHT)
    avisos = []
    controlador.themeChanged.connect(avisos.append)

    assert controlador.effective is Theme.LIGHT
    assert controlador.toggle() is Theme.DARK
    assert controlador.choice is Theme.DARK
    assert avisos == [str(Theme.DARK)]

    controlador.set_theme(Theme.SYSTEM)
    assert controlador.choice is Theme.SYSTEM
    assert controlador.effective in (Theme.LIGHT, Theme.DARK)
    apply_theme(qapp, Theme.LIGHT)


def test_la_hoja_de_estilo_usa_todos_los_tokens_del_tema(qapp):
    hoja = theme.build_stylesheet(theme.DARK_TOKENS)
    assert "$" not in hoja  # no ha quedado ningún hueco sin sustituir
    assert hoja.strip()


@pytest.mark.parametrize("tema", [Theme.LIGHT, Theme.DARK])
@pytest.mark.parametrize("estado", ["ok", "warn", "danger"])
def test_las_etiquetas_de_estado_contrastan_con_su_fondo(tema, estado):
    """«Mercado», «Coste»…: el texto de color sobre su fondo teñido sigue siendo legible."""
    texto, fondo, _borde = theme.chip_tokens(tema, estado)
    assert contraste(texto, fondo) >= CONTRASTE_MINIMO


def test_mezclar_colores():
    assert theme.mix("#000000", "#FFFFFF", 0.5) == "#808080"
    assert theme.mix("#123456", "#FFFFFF", 0) == "#123456"


def test_retener_el_tema_deja_el_cambio_para_despues(qapp, monkeypatch):
    """Mientras el asistente está abierto, un cambio de tema de Windows espera (Qt 6.11
    revienta si la hoja de estilo cambia con un QWizard vivo). Al salir se aplica el último,
    una sola vez."""
    controlador = ThemeController(qapp, Theme.SYSTEM)
    avisos = []
    controlador.themeChanged.connect(avisos.append)
    hoja_antes = qapp.styleSheet()
    efectivo_antes = controlador.effective
    otro = Theme.DARK if efectivo_antes is Theme.LIGHT else Theme.LIGHT

    with controlador.held():
        assert controlador.on_hold
        monkeypatch.setattr(theme, "system_theme", lambda: otro)
        controlador._on_system_change()
        monkeypatch.setattr(theme, "system_theme", lambda: efectivo_antes)
        controlador._on_system_change()
        monkeypatch.setattr(theme, "system_theme", lambda: otro)
        controlador._on_system_change()
        assert qapp.styleSheet() == hoja_antes  # nada aplicado todavía
        assert controlador.effective is efectivo_antes
        assert avisos == []
        with controlador.held():  # se puede anidar
            pass
        assert avisos == []

    assert not controlador.on_hold
    assert controlador.effective is otro
    assert avisos == [str(otro)]  # una sola vez, con el último
    with controlador.held():
        pass
    assert avisos == [str(otro)]  # sin cambios pendientes, salir no aplica nada
    apply_theme(qapp, Theme.LIGHT)
