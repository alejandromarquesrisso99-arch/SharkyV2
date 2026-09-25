"""Tema claro y tema oscuro (GUIA §5.10).

Este es el único sitio del programa donde se escribe un color: los dos temas salen del mismo
juego de tokens y de ahí se generan la paleta de Qt y la hoja de estilo. Ningún otro widget
escribe un color a mano; si necesita uno, lo pide aquí.

Los dos temas mantienen un contraste de 4,5:1 como mínimo para el texto normal (hay un test
que lo comprueba) y los estados llevan siempre su etiqueta, nunca solo el color.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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

    QPushButton#navButton {
        background-color: transparent; border: none; border-radius: 0px;
        border-left: 3px solid transparent; padding: 9px 12px 9px 13px; text-align: left;
    }
    QPushButton#navButton:hover { background-color: $surface; }
    QPushButton#navButton:checked {
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
    QPushButton#primary:disabled {
        background-color: $surface; color: $text_muted; border: 1px solid $border;
    }
    QPushButton#danger { color: $danger; border: 1px solid $danger; font-weight: 600; }
    QPushButton#danger:disabled { color: $text_muted; border: 1px solid $border; }

    QLabel#okText { color: $ok; font-weight: 600; }
    QLabel#warnText { color: $warn; font-weight: 600; }
    QLabel#dangerText { color: $danger; font-weight: 600; }
    QFrame#warnBox { background-color: $surface; border: 1px solid $warn; border-radius: 8px; }
    QFrame#dangerBox {
        background-color: $surface; border: 1px solid $danger; border-radius: 8px;
    }
    QFrame#okBox { background-color: $ok_fill; border: 1px solid $ok_line; border-radius: 8px; }
    QFrame#rejectBox {
        background-color: $danger_fill; border: 1px solid $danger_line; border-radius: 8px;
    }

    QLabel#stepNumber {
        background-color: $surface_alt; color: $text_muted; border-radius: 14px;
        font-weight: 600; min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px;
    }
    QLabel#stepNumberCurrent {
        background-color: $accent; color: $accent_text; border-radius: 14px;
        font-weight: 600; min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px;
    }
    QLabel#stepNumberDone {
        background-color: $ok; color: $surface; border-radius: 14px;
        font-weight: 600; min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px;
    }
    QLabel#stepLabelCurrent { font-weight: 600; }

    QLineEdit {
        background-color: $surface; color: $text; border: 1px solid $border;
        border-radius: 6px; padding: 6px 8px;
    }
    QLineEdit:focus { border: 1px solid $accent; }
    QDateEdit {
        background-color: $surface; color: $text; border: 1px solid $border;
        border-radius: 6px; padding: 5px 8px;
    }
    QTableView {
        background-color: $surface; color: $text; border: none; gridline-color: $border;
        selection-background-color: $surface_selected; selection-color: $text;
    }
    QProgressBar {
        background-color: $surface_alt; color: $text; border: 1px solid $border;
        border-radius: 4px; text-align: center; max-height: 14px;
    }
    QProgressBar::chunk { background-color: $accent; border-radius: 3px; }
    QComboBox {
        background-color: $surface; color: $text; border: 1px solid $border;
        border-radius: 6px; padding: 5px 8px;
    }
    QComboBox QAbstractItemView {
        background-color: $surface; color: $text;
        selection-background-color: $surface_selected; selection-color: $text;
    }
    QHeaderView::section {
        background-color: $surface; color: $text_muted; border: none;
        border-bottom: 1px solid $border; padding: 6px 18px 6px 8px; font-weight: 600;
    }

    QLabel#bigNumber { font-size: 26px; font-weight: 700; }
    QLabel#stateOk { color: $ok; font-size: 28px; font-weight: 700; }
    QLabel#stateWarn { color: $warn; font-size: 28px; font-weight: 700; }
    QLabel#stateDanger { color: $danger; font-size: 28px; font-weight: 700; }
    QLabel#itemTitle { font-weight: 600; }

    QFrame#chipOk { background-color: $ok_fill; border: 1px solid $ok_line; border-radius: 6px; }
    QFrame#chipOk QLabel { color: $ok; font-weight: 600; }
    QFrame#chipWarn {
        background-color: $warn_fill; border: 1px solid $warn_line; border-radius: 6px;
    }
    QFrame#chipWarn QLabel { color: $warn; font-weight: 600; }
    QFrame#chipDanger {
        background-color: $danger_fill; border: 1px solid $danger_line; border-radius: 6px;
    }
    QFrame#chipDanger QLabel { color: $danger; font-weight: 600; }
    QLabel#chipNeutral {
        background-color: $surface_alt; color: $text_muted; border: 1px solid $border;
        border-radius: 9px; padding: 1px 8px; font-weight: 600;
    }

    QLabel#badgeDanger, QLabel#badgeWarn {
        color: $surface; border-radius: 9px; padding: 0px 6px; font-weight: 700;
        min-height: 18px; max-height: 18px;
    }
    QLabel#badgeDanger { background-color: $danger; }
    QLabel#badgeWarn { background-color: $warn; }

    QLabel#dotDanger, QLabel#dotWarn, QLabel#dotOk {
        border-radius: 5px; min-width: 10px; max-width: 10px; min-height: 10px; max-height: 10px;
    }
    QLabel#dotDanger { background-color: $danger; }
    QLabel#dotWarn { background-color: $warn; }
    QLabel#dotOk { background-color: $ok; }

    QPushButton#link {
        background-color: transparent; color: $accent; border: none; padding: 2px 6px;
        font-weight: 600;
    }
    QPushButton#link:hover { background-color: $surface_selected; }

    QPushButton#thesisItem, QPushButton#reportItem {
        background-color: $surface; border: 1px solid $border; border-radius: 8px;
        padding: 0px; text-align: left;
    }
    QPushButton#thesisItem:hover, QPushButton#reportItem:hover { background-color: $surface_alt; }
    QPushButton#thesisItem:checked, QPushButton#reportItem:checked {
        background-color: $surface_selected; border: 1px solid $accent;
    }
    QPushButton#filterChip {
        background-color: $surface; border: 1px solid $border; border-radius: 12px;
        padding: 3px 14px; min-height: 18px; font-weight: 600;
    }
    QPushButton#filterChip:checked {
        background-color: $surface_selected; border: 1px solid $accent;
    }
    QFrame#chipMuted {
        background-color: $surface_alt; border: 1px solid $border; border-radius: 6px;
    }
    QFrame#chipMuted QLabel { color: $text_muted; font-weight: 600; }
    QTextBrowser { background-color: $surface; color: $text; border: none; }
    QLabel#fieldLabel { color: $text_muted; font-weight: 600; }
    QLabel#alertTitle { color: $danger; font-size: 20px; font-weight: 700; }
    QPlainTextEdit {
        background-color: $surface; color: $text; border: 1px solid $border;
        border-radius: 6px; padding: 4px 6px;
    }
    QPlainTextEdit:focus { border: 1px solid $accent; }
    QLineEdit:read-only, QPlainTextEdit[readOnly="true"] { background-color: $surface_alt; }

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


def mix(base: str, other: str, amount: float) -> str:
    """El color `base` con un `amount` (0 a 1) de `other` por encima, en «#RRGGBB»."""
    uno, otro = QColor(base), QColor(other)
    canales = (
        round(a + (b - a) * amount)
        for a, b in (
            (uno.red(), otro.red()),
            (uno.green(), otro.green()),
            (uno.blue(), otro.blue()),
        )
    )
    return "#" + "".join(f"{c:02X}" for c in canales)


#: Cuánto del color de estado lleva el fondo y el borde de una etiqueta de estado.
CHIP_FILL = 0.12
CHIP_BORDER = 0.35


def chip_tokens(theme: Theme, token: str) -> tuple[str, str, str]:
    """Texto, fondo y borde de una etiqueta de estado (Mercado, Coste…) con el color `token`
    (ok, warn o danger) sobre una tarjeta."""
    t = tokens(theme)
    return (
        t[token],
        mix(t["surface"], t[token], CHIP_FILL),
        mix(t["surface"], t[token], CHIP_BORDER),
    )


def chip_colors(theme: Theme, token: str) -> tuple[QColor, QColor, QColor]:
    """Lo mismo que `chip_tokens`, como colores de Qt para pintar."""
    texto, fondo, borde = chip_tokens(theme, token)
    return QColor(texto), QColor(fondo), QColor(borde)


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
    """Hoja de estilo a partir de los tokens (y de los fondos teñidos que salen de ellos)."""
    derivados = {}
    for estado in ("ok", "warn", "danger"):
        derivados[f"{estado}_fill"] = mix(t["surface"], t[estado], CHIP_FILL)
        derivados[f"{estado}_line"] = mix(t["surface"], t[estado], CHIP_BORDER)
    return _STYLESHEET.substitute({**t, **derivados})


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

    El cambio se aplica al momento, sin reiniciar. Con SYSTEM, sigue a Windows en caliente,
    salvo dentro de `held()`, que lo deja para después.
    """

    #: Se emite con el tema efectivo (claro u oscuro).
    themeChanged = Signal(str)

    def __init__(self, app: QApplication, theme: Theme = Theme.SYSTEM) -> None:
        super().__init__(app)
        self._app = app
        self._choice = Theme(theme)
        self._effective = apply_theme(app, self._choice)
        self._holds = 0
        self._pending = False
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

    @property
    def on_hold(self) -> bool:
        """Hay un `held()` en curso: los cambios esperan."""
        return self._holds > 0

    @contextmanager
    def held(self) -> Iterator[None]:
        """Retiene los cambios de tema mientras dura y, al salir, aplica el último una vez.

        Qt 6.11 revienta (violación de acceso) si la hoja de estilo de la aplicación cambia
        con un QWizard vivo y el estilo Fusion. El asistente de primer arranque se abre dentro
        de un `held()` y se destruye antes de salir de él: si Windows cambia de tema mientras
        tanto, el cambio espera a que el asistente ya no exista.
        """
        self._holds += 1
        try:
            yield
        finally:
            self._holds -= 1
            if not self._holds and self._pending:
                self._pending = False
                log.info("Se aplica el cambio de tema que esperaba")
                self._apply()

    def set_theme(self, theme: Theme) -> Theme:
        """Cambia el tema elegido y lo aplica."""
        self._choice = Theme(theme)
        return self._apply()

    def toggle(self) -> Theme:
        """Botón de la cabecera: salta al contrario del que se está viendo."""
        return self.set_theme(Theme.LIGHT if self._effective is Theme.DARK else Theme.DARK)

    def _apply(self) -> Theme:
        if self._holds:
            self._pending = True
            log.info("Cambio de tema retenido hasta que se cierre el asistente")
            return self._effective
        anterior = self._effective
        self._effective = apply_theme(self._app, self._choice)
        if self._effective != anterior:
            log.info("Tema aplicado: %s", self._effective)
        self.themeChanged.emit(str(self._effective))
        return self._effective

    def _on_system_change(self, *_args: object) -> None:
        if self._choice is Theme.SYSTEM:
            self._apply()
