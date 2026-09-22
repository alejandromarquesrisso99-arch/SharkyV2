"""Las siete secciones de la ventana (GUIA §5.10).

En H1 están vacías a propósito: cada una dice qué vivirá en ella y en qué hito llega. La
única que ya hace algo es Ajustes, con Apariencia (el tema) y Acerca de (la versión).
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from sharky import paths
from sharky.ui.theme import THEME_LABELS, Theme, ThemeController


@dataclass(frozen=True)
class Section:
    """Una sección del lateral."""

    key: str
    label: str
    summary: str
    milestone: str


SECTIONS: tuple[Section, ...] = (
    Section(
        "panel",
        "Panel",
        "Estado del mandato, patrimonio, efectivo, lo que requiere atención, el gráfico del "
        "valor por participación y las tarjetas de los tres informes.",
        "H6",
    ),
    Section(
        "cartera",
        "Cartera",
        "Tabla de posiciones con precio, peso, PnL y procedencia del dato, y la exposición "
        "por sector frente a su tope.",
        "H5",
    ),
    Section(
        "operar",
        "Operar",
        "Registro de compras, ventas y movimientos de efectivo, con la validación del "
        "mandato antes de guardar nada.",
        "H8",
    ),
    Section(
        "tesis",
        "Tesis",
        "Tesis activas y cerradas, editor, historial que solo crece y propuestas pendientes "
        "de aplicar.",
        "H7",
    ),
    Section(
        "radar",
        "Radar",
        "Alertas de oportunidad con sus niveles, candidatos descartados con su motivo y la "
        "lista de vigilancia.",
        "H11",
    ),
    Section(
        "informes",
        "Informes",
        "Informes diarios, semanales, mensuales y de exploración, con su lector y su coste.",
        "H9",
    ),
    Section(
        "ajustes",
        "Ajustes",
        "Claude, mandato, apariencia, radar, automatización, datos y registro de ejecuciones.",
        "H13",
    ),
)


def card(title: str) -> tuple[QFrame, QVBoxLayout]:
    """Una tarjeta con su título. Devuelve la tarjeta y la caja donde meter el contenido."""
    marco = QFrame()
    marco.setObjectName("card")
    caja = QVBoxLayout(marco)
    caja.setContentsMargins(20, 18, 20, 18)
    caja.setSpacing(10)
    etiqueta = QLabel(title)
    etiqueta.setObjectName("cardTitle")
    caja.addWidget(etiqueta)
    return marco, caja


def muted(text: str) -> QLabel:
    """Texto secundario, siempre con el color del tema."""
    etiqueta = QLabel(text)
    etiqueta.setObjectName("muted")
    etiqueta.setWordWrap(True)
    return etiqueta


class PlaceholderPage(QWidget):
    """Sección todavía vacía: cuenta qué tendrá y en qué hito."""

    def __init__(self, section: Section, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.section = section
        caja = QVBoxLayout(self)
        caja.setContentsMargins(0, 0, 0, 0)
        caja.setSpacing(16)

        marco, contenido = card("Todavía no hay nada aquí")
        contenido.addWidget(muted(section.summary))
        contenido.addWidget(muted(f"Esta pantalla se construye en el hito {section.milestone}."))
        caja.addWidget(marco)
        caja.addStretch(1)


class SettingsPage(QWidget):
    """Ajustes: en H1 solo Apariencia y Acerca de; el resto llega en H13."""

    def __init__(
        self,
        section: Section,
        theme: ThemeController,
        version: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.section = section
        self._theme = theme
        caja = QVBoxLayout(self)
        caja.setContentsMargins(0, 0, 0, 0)
        caja.setSpacing(16)

        caja.addWidget(self._appearance_card())
        caja.addWidget(self._about_card(version))

        marco, contenido = card("El resto de Ajustes")
        contenido.addWidget(muted(section.summary))
        contenido.addWidget(muted("Se completa en el hito H13."))
        caja.addWidget(marco)
        caja.addStretch(1)

        theme.themeChanged.connect(self._sync_buttons)

    def _appearance_card(self) -> QFrame:
        marco, contenido = card("Apariencia")
        contenido.addWidget(muted("El cambio se aplica al momento, sin reiniciar."))
        self._buttons: dict[Theme, QRadioButton] = {}
        grupo = QButtonGroup(self)
        for tema in (Theme.LIGHT, Theme.DARK, Theme.SYSTEM):
            texto = THEME_LABELS[tema]
            if tema is Theme.SYSTEM:
                texto += "  ·  recomendado"
            boton = QRadioButton(texto)
            boton.setChecked(self._theme.choice is tema)
            boton.toggled.connect(lambda marcado, t=tema: self._on_choice(marcado, t))
            grupo.addButton(boton)
            contenido.addWidget(boton)
            self._buttons[tema] = boton
        return marco

    def _about_card(self, version: str) -> QFrame:
        marco, contenido = card("Acerca de")
        titulo = QLabel(f"Sharky, versión {version}")
        titulo.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        contenido.addWidget(titulo)
        contenido.addWidget(muted("Vigilancia de una cartera real de inversión en euros."))
        contenido.addWidget(muted("Sharky nunca envía órdenes a un bróker."))
        ruta = muted(f"Carpeta de datos: {paths.data_dir()}")
        ruta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        contenido.addWidget(ruta)
        return marco

    def _on_choice(self, marcado: bool, tema: Theme) -> None:
        if marcado and self._theme.choice is not tema:
            self._theme.set_theme(tema)

    def _sync_buttons(self, *_args: object) -> None:
        """Si el tema cambia desde la cabecera, el botón marcado sigue siendo el correcto."""
        for tema, boton in self._buttons.items():
            boton.blockSignals(True)
            boton.setChecked(self._theme.choice is tema)
            boton.blockSignals(False)


def build_page(section: Section, theme: ThemeController, version: str) -> QWidget:
    """La página de una sección."""
    if section.key == "ajustes":
        return SettingsPage(section, theme, version)
    return PlaceholderPage(section)
