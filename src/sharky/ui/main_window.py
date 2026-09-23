"""Ventana principal: lateral con las siete secciones y contenido a la derecha."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sharky import paths
from sharky.services.db import Database
from sharky.ui.pages import SECTIONS, Section, SettingsPage, build_page
from sharky.ui.theme import Theme, ThemeController

log = logging.getLogger(__name__)

SIDEBAR_WIDTH = 236


class MainWindow(QMainWindow):
    """La ventana de Sharky. Casi todas las secciones siguen vacías hasta su hito."""

    #: Hay que reiniciar la app (por ejemplo, tras restaurar una copia de seguridad).
    restartRequested = Signal()

    def __init__(
        self,
        theme: ThemeController,
        version: str,
        parent: QWidget | None = None,
        *,
        db: Database | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._version = version
        self._db = db
        self._buttons: dict[str, QToolButton] = {}
        self._pages: dict[str, QWidget] = {}

        self.setWindowIcon(QIcon(str(paths.icon_path())))
        self.setMinimumSize(1040, 680)
        self.resize(1280, 820)

        central = QWidget()
        caja = QHBoxLayout(central)
        caja.setContentsMargins(0, 0, 0, 0)
        caja.setSpacing(0)
        caja.addWidget(self._build_sidebar())
        caja.addWidget(self._build_content(), 1)
        self.setCentralWidget(central)

        theme.themeChanged.connect(self._sync_theme_button)
        self._sync_theme_button()
        self.show_section(SECTIONS[0].key)

    # -- construcción ------------------------------------------------------------------

    def _build_sidebar(self) -> QWidget:
        lateral = QWidget()
        lateral.setObjectName("sidebar")
        lateral.setFixedWidth(SIDEBAR_WIDTH)
        caja = QVBoxLayout(lateral)
        caja.setContentsMargins(0, 14, 0, 14)
        caja.setSpacing(2)

        caja.addWidget(self._build_brand())
        caja.addSpacing(10)

        grupo = QButtonGroup(self)
        grupo.setExclusive(True)
        for seccion in SECTIONS:
            boton = self._build_nav_button(seccion)
            grupo.addButton(boton)
            caja.addWidget(boton)
            self._buttons[seccion.key] = boton

        caja.addStretch(1)
        caja.addWidget(self._build_sidebar_footer())
        return lateral

    def _build_brand(self) -> QWidget:
        fila = QWidget()
        caja = QHBoxLayout(fila)
        caja.setContentsMargins(16, 0, 16, 0)
        caja.setSpacing(8)
        icono = QLabel()
        icono.setPixmap(QIcon(str(paths.icon_path())).pixmap(20, 20))
        caja.addWidget(icono)
        nombre = QLabel("Sharky")
        nombre.setObjectName("brand")
        caja.addWidget(nombre)
        caja.addStretch(1)
        return fila

    def _build_nav_button(self, section: Section) -> QToolButton:
        boton = QToolButton()
        boton.setObjectName("navButton")
        boton.setText(section.label)
        boton.setCheckable(True)
        boton.setAutoExclusive(True)
        boton.setCursor(Qt.CursorShape.PointingHandCursor)
        boton.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        boton.clicked.connect(lambda _checked=False, clave=section.key: self.show_section(clave))
        return boton

    def _build_sidebar_footer(self) -> QWidget:
        pie = QWidget()
        caja = QVBoxLayout(pie)
        caja.setContentsMargins(16, 10, 16, 0)
        caja.setSpacing(8)

        linea = QFrame()
        linea.setObjectName("separator")
        linea.setFrameShape(QFrame.Shape.HLine)
        caja.addWidget(linea)

        titulo = QLabel("ESTADO DEL MANDATO")
        titulo.setObjectName("sectionLabel")
        caja.addWidget(titulo)

        chip = QFrame()
        chip.setObjectName("chip")
        chip_caja = QVBoxLayout(chip)
        chip_caja.setContentsMargins(10, 6, 10, 6)
        self._mandate_label = QLabel("Sin datos todavía")
        self._mandate_label.setWordWrap(True)
        chip_caja.addWidget(self._mandate_label)
        caja.addWidget(chip)

        self._last_check_label = QLabel("Último control: —")
        self._last_check_label.setObjectName("muted")
        caja.addWidget(self._last_check_label)
        return pie

    def _build_content(self) -> QWidget:
        contenido = QWidget()
        contenido.setObjectName("content")
        caja = QVBoxLayout(contenido)
        caja.setContentsMargins(28, 20, 28, 24)
        caja.setSpacing(18)

        cabecera = QHBoxLayout()
        cabecera.setSpacing(10)
        self._title_label = QLabel()
        self._title_label.setObjectName("title")
        cabecera.addWidget(self._title_label)
        cabecera.addStretch(1)
        self._theme_button = QPushButton()
        self._theme_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_button.clicked.connect(self._theme.toggle)
        cabecera.addWidget(self._theme_button)
        caja.addLayout(cabecera)

        self._stack = QStackedWidget()
        for seccion in SECTIONS:
            pagina = build_page(seccion, self._theme, self._version, self._db)
            if isinstance(pagina, SettingsPage):
                pagina.restartRequested.connect(self.restartRequested)
            self._pages[seccion.key] = pagina
            self._stack.addWidget(pagina)
        caja.addWidget(self._stack, 1)
        return contenido

    # -- comportamiento ----------------------------------------------------------------

    def show_section(self, key: str) -> None:
        """Enseña una sección y deja marcado su botón del lateral."""
        if key not in self._pages:
            raise KeyError(f"Sección desconocida: {key}")
        seccion = next(s for s in SECTIONS if s.key == key)
        self._stack.setCurrentWidget(self._pages[key])
        self._buttons[key].setChecked(True)
        self._title_label.setText(seccion.label)
        self.setWindowTitle(f"Sharky — {seccion.label}")

    @property
    def current_section(self) -> str:
        """Clave de la sección que se está viendo."""
        return next(clave for clave, p in self._pages.items() if p is self._stack.currentWidget())

    def bring_to_front(self) -> None:
        """Trae la ventana al frente (segunda ejecución del programa o icono de la bandeja)."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def _sync_theme_button(self, *_args: object) -> None:
        oscuro = self._theme.effective is Theme.DARK
        self._theme_button.setText("Tema claro" if oscuro else "Tema oscuro")
        self._theme_button.setToolTip(
            "Cambia entre el tema claro y el oscuro. En Ajustes → Apariencia puedes elegir "
            "además el que tenga Windows."
        )
