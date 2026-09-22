"""Tema claro y tema oscuro (GUIA §5.10).

Este es el único sitio del programa donde se escribe un color: los dos temas salen del mismo
juego de tokens y de ahí se generan la paleta de Qt y la hoja de estilo. Ningún otro widget
escribe un color a mano; si necesita uno, lo pide aquí.

Los dos temas mantienen un contraste de 4,5:1 como mínimo para el texto normal (hay un test
que lo comprueba) y los estados llevan siempre su etiqueta, nunca solo el color.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from string import Template

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)


class Theme(StrEnum):
    """Elección de tema. SYSTEM sigue al que tenga Windows (la opción por defecto)."""

    LIGHT = "claro"
    DARK = "oscuro"
    SYSTEM = "sistema"


#: Etiquetas para la interfaz.
THEME_LABELS: dict[Theme, str] = {
    Theme.LIGHT: "Claro",
    Theme.DARK: "Oscuro",
    Theme.SYSTEM: "El que tenga Windows",
}

#: Los tokens que tiene que definir cualquier tema.
TOKEN_KEYS: tuple[str, ...] = (
    "background",        # fondo de la ventana
    "surface",           # tarjetas y campos
    "surface_alt",       # lateral y zonas secundarias
    "surface_selected",  # sección activa y selección
    "border",            # líneas y separadores
    "text",              # texto normal
    "text_muted",        # texto secundario
    "accent",            # acento (enlaces, botón principal, gráfico)
    "accent_text",       # texto sobre el acento
    "ok",                # estado: cumple
    "warn",              # estado: alerta
    "danger",            # estado: exige actuar
)

LIGHT_TOKENS: dict[str, str] = {
    "background": "#F4F3F0",
    "surface": "#FFFFFF",
    "surface_alt": "#EAE8E3",
    "surface_selected": "#E4EDF4",
    "border": "#D8D5CF",
    "text": "#1B1A17",
    "text_muted": "#57544D",
    "accent": "#1D4E6E",
    "accent_text": "#FFFFFF",
    "ok": "#15703E",
    "warn": "#8A5A00",
    "danger": "#B3261E",
}

DARK_TOKENS: dict[str, str] = {
    "background": "#14171A",
    "surface": "#1E2226",
    "surface_alt": "#272C31",
    "surface_selected": "#24333F",
    "border": "#343A40",
    "text": "#E7E9EB",
    "text_muted": "#A7ADB4",
    "accent": "#79B4DC",
    "accent_text": "#0E1519",
    "ok": "#5BC98D",
    "warn": "#E0A93C",
    "danger": "#F0786B",
}

TOKENS: dict[Theme, dict[str, str]] = {
    Theme.LIGHT: LIGHT_TOKENS,
    Theme.DARK: DARK_TOKENS,
}

_STYLESHEET = Template(
    """
    QWidget { color: $text; }
    QMainWindow, QDialog, QWidget#content { background-color: $background; }
    QWidget#sidebar { background-color: $surface_alt; border-right: 1px solid $border; }

    QLabel#brand { font-size: 16px; font-weight: 600; }
    QLabel#title { font-size: 24px; font-weight: 600; }
    QLabel#cardTitle { font-size: 16px; font-weight: 600; }
    QLabel#muted { color: $text_muted; }
    QLabel#sectionLabel { color: $text_muted; font-size: 11px; font-weight: 600; }

    QFrame#card { background-color: $surface; border: 1px solid $border; border-radius: 10px; }
    QFrame#chip { background-color: $surface; border: 1px solid $border; border-radius: 6px; }
    QFrame#separator { background-color: $border; border: none; max-height: 1px; }

    QToolButton#navButton {
        background-color: transparent; border: none;
        border-left: 3px solid transparent; padding: 9px 12px 9px 13px; text-align: left;
    }
    QToolButton#navButton:hover { background-color: $surface; }
    QToolButton#navButton:checked {
        background-color: $surface_selected; border-left: 3px solid $accent; font-weight: 600;
    }

    QPushButton {
        background-color: $surface; color: $text; border: 1px solid $border;
        border-radius: 6px; padding: 6px 14px;
    }
    QPushButton:hover { background-color: $surface_alt; }
    QPushButton:pressed, QPushButton:checked { background-color: $surface_selected; }
    QPushButton#primary {
        background-color: $accent; color: $accent_text; border: 1px solid $accent;
    }
    QPushButton:disabled { color: $text_muted; }

    QScrollArea { background-color: transparent; border: none; }
    QScrollArea > QWidget > QWidget { background-color: transparent; }
    QToolTip {
        background-color: $surface; color: $text; border: 1px solid $border; padding: 4px;
    }
    """
)


def tokens(theme: Theme) -> dict[str, str]:
    """Tokens de un tema concreto. SYSTEM se resuelve antes."""
    return dict(TOKENS[resolve(theme)])


def color(theme: Theme, token: str) -> QColor:
    """Un color del tema, para lo que no se pueda pintar con la hoja de estilo."""
    return QColor(tokens(theme)[token])


def system_theme() -> Theme:
    """El tema que tiene Windows ahora mismo. Si no se sabe, el claro."""
    try:
        esquema = QGuiApplication.styleHints().colorScheme()
    except (AttributeError, RuntimeError):  # pragma: no cover - Qt sin styleHints
        return Theme.LIGHT
    return Theme.DARK if esquema == Qt.ColorScheme.Dark else Theme.LIGHT


def resolve(theme: Theme) -> Theme:
    """Convierte SYSTEM en claro u oscuro; los otros se devuelven tal cual."""
    return system_theme() if theme is Theme.SYSTEM else theme


def build_palette(t: Mapping[str, str]) -> QPalette:
    """Paleta de Qt a partir de los tokens."""

    def c(clave: str) -> QColor:
        return QColor(t[clave])

    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, c("background"))
    p.setColor(QPalette.ColorRole.WindowText, c("text"))
    p.setColor(QPalette.ColorRole.Base, c("surface"))
    p.setColor(QPalette.ColorRole.AlternateBase, c("surface_alt"))
    p.setColor(QPalette.ColorRole.Text, c("text"))
    p.setColor(QPalette.ColorRole.PlaceholderText, c("text_muted"))
    p.setColor(QPalette.ColorRole.Button, c("surface"))
    p.setColor(QPalette.ColorRole.ButtonText, c("text"))
    p.setColor(QPalette.ColorRole.BrightText, c("danger"))
    p.setColor(QPalette.ColorRole.Highlight, c("accent"))
    p.setColor(QPalette.ColorRole.HighlightedText, c("accent_text"))
    p.setColor(QPalette.ColorRole.ToolTipBase, c("surface"))
    p.setColor(QPalette.ColorRole.ToolTipText, c("text"))
    p.setColor(QPalette.ColorRole.Link, c("accent"))
    p.setColor(QPalette.ColorRole.LinkVisited, c("accent"))
    p.setColor(QPalette.ColorRole.Mid, c("border"))
    p.setColor(QPalette.ColorRole.Dark, c("border"))
    for rol in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        p.setColor(QPalette.ColorGroup.Disabled, rol, c("text_muted"))
    return p


def build_stylesheet(t: Mapping[str, str]) -> str:
    """Hoja de estilo a partir de los tokens."""
    return _STYLESHEET.substitute(t)


def apply_theme(app: QApplication, theme: Theme) -> Theme:
    """Aplica el tema a la aplicación y devuelve el tema efectivo (nunca SYSTEM)."""
    efectivo = resolve(theme)
    t = TOKENS[efectivo]
    app.setStyle("Fusion")
    app.setPalette(build_palette(t))
    app.setStyleSheet(build_stylesheet(t))
    return efectivo


class ThemeController(QObject):
    """Guarda el tema elegido, lo aplica y avisa a quien pinte por su cuenta.

    El cambio se aplica al momento, sin reiniciar. Con SYSTEM, sigue a Windows en caliente.
    """

    #: Se emite con el tema efectivo (claro u oscuro).
    themeChanged = Signal(str)

    def __init__(self, app: QApplication, theme: Theme = Theme.SYSTEM) -> None:
        super().__init__(app)
        self._app = app
        self._choice = Theme(theme)
        self._effective = apply_theme(app, self._choice)
        try:
            app.styleHints().colorSchemeChanged.connect(self._on_system_change)
        except (AttributeError, RuntimeError):  # pragma: no cover - Qt sin la señal
            log.debug("Esta versión de Qt no avisa de los cambios de tema de Windows")

    @property
    def choice(self) -> Theme:
        """Lo que ha elegido el usuario (puede ser SYSTEM)."""
        return self._choice

    @property
    def effective(self) -> Theme:
        """El tema que se está viendo: claro u oscuro."""
        return self._effective

    def set_theme(self, theme: Theme) -> Theme:
        """Cambia el tema elegido y lo aplica."""
        self._choice = Theme(theme)
        return self._apply()

    def toggle(self) -> Theme:
        """Botón de la cabecera: salta al contrario del que se está viendo."""
        return self.set_theme(Theme.LIGHT if self._effective is Theme.DARK else Theme.DARK)

    def _apply(self) -> Theme:
        anterior = self._effective
        self._effective = apply_theme(self._app, self._choice)
        if self._effective != anterior:
            log.info("Tema aplicado: %s", self._effective)
        self.themeChanged.emit(str(self._effective))
        return self._effective

    def _on_system_change(self, *_args: object) -> None:
        if self._choice is Theme.SYSTEM:
            self._apply()
