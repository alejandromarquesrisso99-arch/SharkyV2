"""Las siete secciones de la ventana (GUIA §5.10).

Las que todavía no tienen su hito están vacías a propósito: cada una dice qué vivirá en ella y
en qué hito llega. Ya funcionan Cartera (H5, en ui/portfolio.py) y Ajustes, con Apariencia (el
tema), Datos (copia de seguridad y restauración, H3) y Acerca de (la versión).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sharky import paths
from sharky.services.backup import (
    KEEP_BACKUPS,
    RestoreResult,
    backup_time,
    create_backup,
    list_backups,
    restore_backup,
    wipe_portfolio,
)
from sharky.services.db import Database
from sharky.services.market import FxProvider, PriceProvider
from sharky.services.settings import Settings
from sharky.ui.theme import THEME_LABELS, Theme, ThemeController
from sharky.ui.workers import Worker, start

log = logging.getLogger(__name__)


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


def state_label(text: str = "", state: str = "muted") -> QLabel:
    """Una línea de estado. `state`: muted, okText, warnText o dangerText (color del tema)."""
    etiqueta = QLabel(text)
    etiqueta.setObjectName(state)
    etiqueta.setWordWrap(True)
    return etiqueta


def set_state(label: QLabel, text: str, state: str) -> None:
    """Cambia el texto y el estado de una línea; sin texto, se oculta."""
    label.setText(text)
    if label.objectName() != state:
        label.setObjectName(state)
        label.style().unpolish(label)
        label.style().polish(label)
    label.setVisible(bool(text))


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


#: Lo que hay que escribir para confirmar «Borrar cartera».
WIPE_CONFIRMATION = "BORRAR"


class WipeConfirmDialog(QDialog):
    """Confirmación de «Borrar cartera»: qué se borra, qué se conserva, cuánto dura la copia
    previa, y el botón solo se activa tras escribir BORRAR."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Borrar cartera")
        self.setMinimumWidth(520)
        caja = QVBoxLayout(self)
        caja.setContentsMargins(20, 18, 20, 18)
        caja.setSpacing(10)

        titulo = QLabel("¿Borrar toda la cartera?")
        titulo.setObjectName("cardTitle")
        caja.addWidget(titulo)

        aviso = QFrame()
        aviso.setObjectName("dangerBox")
        texto_aviso = QVBoxLayout(aviso)
        texto_aviso.setContentsMargins(14, 10, 14, 10)
        self.explanation = QLabel(
            "Se borra todo lo que hay en Sharky: posiciones, operaciones, efectivo, historial "
            "del patrimonio, tesis, informes, avisos y radar.\n\n"
            "Se conservan los ajustes, la clave de Claude y las copias de seguridad.\n\n"
            "Antes se guarda una copia de lo que hay, y con «Restaurar copia…» puedes "
            f"recuperarla, pero solo durante un tiempo: Sharky guarda las {KEEP_BACKUPS} copias "
            "más recientes y borra las anteriores, así que con una copia al día dura unas dos "
            "semanas.\n\n"
            "Después, Sharky se reinicia y abre el asistente para crear la cartera de nuevo."
        )
        self.explanation.setWordWrap(True)
        texto_aviso.addWidget(self.explanation)
        caja.addWidget(aviso)

        caja.addWidget(QLabel(f"Para confirmar, escribe {WIPE_CONFIRMATION}:"))
        self.confirm_edit = QLineEdit()
        self.confirm_edit.setPlaceholderText(WIPE_CONFIRMATION)
        self.confirm_edit.textChanged.connect(self._on_text)
        caja.addWidget(self.confirm_edit)

        botones = QHBoxLayout()
        botones.addStretch(1)
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        botones.addWidget(self.cancel_button)
        self.wipe_button = QPushButton("Borrar cartera")
        self.wipe_button.setObjectName("danger")
        self.wipe_button.setEnabled(False)
        self.wipe_button.clicked.connect(self.accept)
        botones.addWidget(self.wipe_button)
        caja.addLayout(botones)

    def _on_text(self, texto: str) -> None:
        self.wipe_button.setEnabled(texto.strip() == WIPE_CONFIRMATION)


class DataCard(QFrame):
    """Ajustes → Datos: copia de seguridad, restauración (H3) y borrar la cartera. El resto
    llega en H13.

    Las tres acciones tocan el disco, así que corren en segundo plano. Restaurar y borrar piden
    confirmación y, al terminar, piden reiniciar la app con `restartRequested`.
    """

    restartRequested = Signal()

    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._db = db
        self._worker: Worker | None = None

        caja = QVBoxLayout(self)
        caja.setContentsMargins(20, 18, 20, 18)
        caja.setSpacing(10)
        titulo = QLabel("Datos")
        titulo.setObjectName("cardTitle")
        caja.addWidget(titulo)

        ruta = muted(f"Carpeta de datos: {paths.data_dir()}")
        ruta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        caja.addWidget(ruta)
        self._last_label = muted("")
        caja.addWidget(self._last_label)

        fila = QHBoxLayout()
        fila.setSpacing(10)
        self.backup_button = QPushButton("Copia de seguridad ahora")
        self.backup_button.clicked.connect(self.start_backup)
        fila.addWidget(self.backup_button)
        self.restore_button = QPushButton("Restaurar copia…")
        self.restore_button.clicked.connect(self.start_restore)
        fila.addWidget(self.restore_button)
        fila.addStretch(1)
        self.wipe_button = QPushButton("Borrar cartera…")
        self.wipe_button.setObjectName("danger")
        self.wipe_button.setToolTip(
            "Borra todo y vuelve a abrir el asistente de primer arranque. Antes guarda una copia."
        )
        self.wipe_button.clicked.connect(self.start_wipe)
        fila.addWidget(self.wipe_button)
        caja.addLayout(fila)

        self.status_label = muted("")
        caja.addWidget(self.status_label)
        self.refresh()

    # -- estado ------------------------------------------------------------------------

    def refresh(self) -> None:
        """Pone al día la línea de la última copia."""
        copias = list_backups()
        cuando = backup_time(copias[-1]) if copias else None
        if cuando is None:
            texto = "Todavía no hay ninguna copia de seguridad."
        else:
            texto = f"Última copia: {cuando:%d/%m/%Y %H:%M}."
        self._last_label.setText(f"{texto} Se guardan las {KEEP_BACKUPS} últimas.")

    def _set_busy(self, busy: bool, text: str) -> None:
        self.backup_button.setEnabled(not busy)
        self.restore_button.setEnabled(not busy)
        self.wipe_button.setEnabled(not busy)
        self.status_label.setText(text)

    def _run(self, worker: Worker, on_done: Callable[[Any], None]) -> None:
        worker.signals.finished.connect(on_done)
        worker.signals.failed.connect(self._on_failed)
        self._worker = start(worker)  # se guarda: sin referencia, las señales se perderían

    # -- copia -------------------------------------------------------------------------

    def start_backup(self) -> None:
        self._set_busy(True, "Haciendo la copia de seguridad…")
        self._run(Worker(create_backup, self._db, datetime.now()), self._on_backup_done)

    def _on_backup_done(self, ruta: Path) -> None:
        self._set_busy(False, f"Copia guardada: {ruta.name}")
        self.refresh()

    # -- restauración ------------------------------------------------------------------

    def start_restore(self) -> None:
        ruta = self.choose_backup_file()
        if ruta is None or not self.confirm_restore(ruta):
            return
        self._set_busy(True, "Restaurando la copia…")
        self._run(Worker(restore_backup, self._db, ruta, datetime.now()), self._on_restore_done)

    def _on_restore_done(self, resultado: RestoreResult) -> None:
        self._set_busy(True, "Copia restaurada. Sharky se reinicia…")
        log.info("Restauración terminada: se pide reiniciar Sharky")
        self.notify_restart(resultado)
        self.restartRequested.emit()

    # -- borrar la cartera -------------------------------------------------------------

    def start_wipe(self) -> None:
        if not self.confirm_wipe():
            return
        self._set_busy(True, "Borrando la cartera…")
        self._run(Worker(wipe_portfolio, self._db, datetime.now()), self._on_wipe_done)

    def _on_wipe_done(self, seguridad: Path) -> None:
        self._set_busy(True, "Cartera borrada. Sharky se reinicia…")
        log.info("Cartera borrada: se pide reiniciar Sharky para abrir el asistente")
        self.notify_wiped(seguridad)
        self.restartRequested.emit()

    def _on_failed(self, mensaje: str) -> None:
        self._set_busy(False, "")
        self.refresh()
        self.show_error(mensaje)

    # -- diálogos (los tests los sustituyen) ---------------------------------------------

    def choose_backup_file(self) -> Path | None:
        ruta, _filtro = QFileDialog.getOpenFileName(
            self,
            "Elige la copia que quieres restaurar",
            str(paths.backups_dir()),
            "Copias de Sharky (*.db)",
        )
        return Path(ruta) if ruta else None

    def confirm_restore(self, path: Path) -> bool:
        cuando = backup_time(path)
        fecha = f" del {cuando:%d/%m/%Y a las %H:%M}" if cuando else ""
        caja = QMessageBox(self)
        caja.setIcon(QMessageBox.Icon.Warning)
        caja.setWindowTitle("Restaurar copia de seguridad")
        caja.setText(f"¿Sustituir todos los datos por los de la copia{fecha}?")
        caja.setInformativeText(
            f"Fichero: {path.name}\n\n"
            "Antes se guarda una copia de lo que hay ahora, por si te arrepientes. Al "
            "terminar, Sharky se reiniciará. La clave de Claude no cambia."
        )
        restaurar = caja.addButton("Restaurar", QMessageBox.ButtonRole.AcceptRole)
        cancelar = caja.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        caja.setDefaultButton(cancelar)
        caja.exec()
        return caja.clickedButton() is restaurar

    def notify_restart(self, result: RestoreResult) -> None:
        QMessageBox.information(
            self,
            "Copia restaurada",
            f"Se han restaurado los datos de {result.restored_from.name}.\n\n"
            f"Lo que había antes está en {result.safety_copy.name}.\n"
            "Sharky se reinicia ahora.",
        )

    def confirm_wipe(self) -> bool:
        dialogo = WipeConfirmDialog(self)
        try:
            return dialogo.exec() == QDialog.DialogCode.Accepted
        finally:
            dialogo.deleteLater()

    def notify_wiped(self, safety_copy: Path) -> None:
        QMessageBox.information(
            self,
            "Cartera borrada",
            f"Lo que había está en {safety_copy.name}, en la carpeta de copias.\n"
            "Sharky se reinicia ahora y abre el asistente para crear la cartera.",
        )

    def show_error(self, message: str) -> None:
        QMessageBox.warning(self, "Sharky", message)


class SettingsPage(QWidget):
    """Ajustes: Apariencia, Datos y Acerca de; el resto llega en H13."""

    #: Tras restaurar una copia, la app tiene que reiniciarse.
    restartRequested = Signal()

    def __init__(
        self,
        section: Section,
        theme: ThemeController,
        version: str,
        parent: QWidget | None = None,
        *,
        db: Database | None = None,
    ) -> None:
        super().__init__(parent)
        self.section = section
        self._theme = theme
        exterior = QVBoxLayout(self)
        exterior.setContentsMargins(0, 0, 0, 0)
        desplazable = QScrollArea()
        desplazable.setWidgetResizable(True)
        desplazable.setFrameShape(QFrame.Shape.NoFrame)
        exterior.addWidget(desplazable)
        interior = QWidget()
        desplazable.setWidget(interior)
        caja = QVBoxLayout(interior)
        caja.setContentsMargins(0, 0, 0, 0)
        caja.setSpacing(16)

        caja.addWidget(self._appearance_card())
        self.data_card: DataCard | None = None
        if db is not None:
            self.data_card = DataCard(db)
            self.data_card.restartRequested.connect(self.restartRequested)
            caja.addWidget(self.data_card)
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


def build_page(
    section: Section,
    theme: ThemeController,
    version: str,
    db: Database | None = None,
    *,
    market: PriceProvider | None = None,
    fx: FxProvider | None = None,
    settings: Settings | None = None,
    now: Callable[[], datetime] | None = None,
) -> QWidget:
    """La página de una sección. Cartera necesita la base de datos y el mercado."""
    if section.key == "ajustes":
        return SettingsPage(section, theme, version, db=db)
    if section.key == "cartera" and db is not None and market is not None and fx is not None:
        from sharky.ui.portfolio import PortfolioPage  # portfolio usa las piezas de aquí

        extra = {"now": now} if now is not None else {}
        return PortfolioPage(db, theme, market, fx, settings=settings, **extra)
    return PlaceholderPage(section)
