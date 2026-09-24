"""Asistente de primer arranque (GUIA §5.2): clave de Claude, cartera y resumen.

Sale en lugar de la ventana principal mientras no haya cartera. Nada se escribe hasta pulsar
«Crear cartera»; entonces, en un hilo de trabajo y por este orden:

1. la cartera, en una sola transacción: activos, operaciones APERTURA, efectivo INICIAL y
   primera foto del NAV (si falla, no queda nada escrito);
2. los ajustes: bróker e inicio con Windows (la casilla solo se guarda; se aplica en H12);
3. la clave de Claude, en el Administrador de credenciales.

Cancelar no escribe nada.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from sharky import paths
from sharky.core.csv_import import (
    MAX_BYTES,
    TEMPLATE_CSV,
    TEMPLATE_FILENAME,
    CsvImport,
    CsvPosition,
    Opening,
    OpeningError,
    build_opening,
    parse_positions_csv,
)
from sharky.core.formatting import (
    format_amount,
    format_eur,
    format_price,
    format_units,
    parse_decimal,
)
from sharky.services import secrets
from sharky.services.ai import KeyCheck, KeyStatus, check_api_key
from sharky.services.db import Database
from sharky.services.repositories import create_portfolio
from sharky.services.settings import DEFAULT_BROKER, Settings, SettingsStore
from sharky.ui.pages import muted
from sharky.ui.pages import set_state as _set_state
from sharky.ui.pages import state_label as _state_label
from sharky.ui.workers import Worker, start

log = logging.getLogger(__name__)

STEPS: tuple[str, ...] = ("Clave de Claude", "Tu cartera", "Resumen")
BROKER_MAX_LENGTH = 80


# -- lo que se guarda al confirmar ------------------------------------------------------


@dataclass(frozen=True)
class SetupRequest:
    """Lo que el asistente guarda al pulsar «Crear cartera»."""

    opening: Opening
    api_key: str | None  # None: no se toca la clave (no hay, o se conserva la guardada)
    start_with_windows: bool
    broker: str


@dataclass(frozen=True)
class SetupResult:
    """La cartera ya está creada. `warnings`: lo de después que no se ha podido guardar."""

    warnings: tuple[str, ...] = ()


def apply_setup(
    db: Database,
    store: SettingsStore,
    settings: Settings,
    request: SetupRequest,
    save_key: Callable[[str], None] = secrets.save_api_key,
) -> SetupResult:
    """Guarda lo del asistente. Corre en un hilo de trabajo.

    Si lanza, no se ha escrito nada: los ajustes nuevos se validan antes y la cartera entra en
    una sola transacción. Lo que va después (ajustes y clave) ya no deshace la cartera; si
    falla, se devuelve como aviso.
    """
    nuevos = settings.model_copy(deep=True)
    nuevos.automation.start_with_windows = request.start_with_windows
    nuevos.portfolio.broker = request.broker

    with db.transaction() as conn:
        create_portfolio(conn, request.opening)
    log.info(
        "Cartera creada: %d posiciones, efectivo inicial %s, patrimonio a coste %s",
        len(request.opening.trades),
        format_eur(request.opening.initial_cash.amount_eur),
        format_eur(request.opening.snapshot.nav_eur),
    )

    avisos: list[str] = []
    try:
        store.save(nuevos)
    except OSError:
        log.exception("No se han podido guardar los ajustes del asistente")
        avisos.append(
            "No se han podido guardar los ajustes (bróker e inicio con Windows): se usarán los "
            "valores por defecto."
        )
    if request.api_key:
        try:
            save_key(request.api_key)
        except (secrets.SecretsError, ValueError) as error:
            log.error("No se ha podido guardar la clave de Claude (%s)", type(error).__name__)
            avisos.append(
                "No se ha podido guardar la clave de Claude en el Administrador de "
                "credenciales. Sharky funcionará sin IA hasta que la vuelvas a poner."
            )
    return SetupResult(tuple(avisos))


# -- piezas comunes ---------------------------------------------------------------------


def _documents_dir() -> str:
    return QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)


class StepBar(QWidget):
    """«1 Clave de Claude —— 2 Tu cartera —— 3 Resumen», con el paso actual resaltado."""

    def __init__(self, current: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        caja = QHBoxLayout(self)
        caja.setContentsMargins(0, 0, 0, 8)
        caja.setSpacing(10)
        for indice, nombre in enumerate(STEPS):
            if indice:
                linea = QFrame()
                linea.setObjectName("separator")
                linea.setFrameShape(QFrame.Shape.HLine)
                linea.setFixedWidth(44)
                caja.addWidget(linea, 0, Qt.AlignmentFlag.AlignVCenter)
            numero = QLabel(str(indice + 1))
            numero.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if indice < current:
                numero.setObjectName("stepNumberDone")
            elif indice == current:
                numero.setObjectName("stepNumberCurrent")
            else:
                numero.setObjectName("stepNumber")
            caja.addWidget(numero)
            etiqueta = QLabel(nombre)
            etiqueta.setObjectName("stepLabelCurrent" if indice == current else "muted")
            caja.addWidget(etiqueta)
        caja.addStretch(1)


class _Page(QWizardPage):
    """Página con la barra de pasos, su título y el contenido desplazable."""

    def __init__(self, step: int, title: str, intro: str) -> None:
        super().__init__()
        self.step = step
        exterior = QVBoxLayout(self)
        exterior.setContentsMargins(0, 0, 0, 0)
        desplazable = QScrollArea()
        desplazable.setWidgetResizable(True)
        desplazable.setFrameShape(QFrame.Shape.NoFrame)
        exterior.addWidget(desplazable)
        interior = QWidget()
        desplazable.setWidget(interior)
        self.body = QVBoxLayout(interior)
        self.body.setContentsMargins(8, 8, 8, 8)
        self.body.setSpacing(12)
        self.body.addWidget(StepBar(step))
        titulo = QLabel(title)
        titulo.setObjectName("title")
        self.body.addWidget(titulo)
        self.body.addWidget(muted(intro))

    def _finish_layout(self) -> None:
        self.body.addStretch(1)
        self.body.addWidget(muted(f"Paso {self.step + 1} de {len(STEPS)}"))


# -- paso 1: clave ----------------------------------------------------------------------


class KeyPage(_Page):
    """La clave de Claude: campo oculto y «Probar clave». Se puede dejar en blanco."""

    def __init__(self, checker: Callable[[str], KeyCheck], has_saved_key: bool) -> None:
        super().__init__(
            0,
            "Clave de Claude",
            "Sharky vigila tu cartera, calcula todos los números y te avisa cuando algo exige "
            "actuar. No opera por ti ni es asesoramiento financiero: las decisiones son tuyas.",
        )
        self._checker = checker
        self.has_saved_key = has_saved_key
        self._check: KeyCheck | None = None
        self._checked_key = ""
        self._checking: str | None = None
        self._worker: Worker | None = None

        self.body.addWidget(
            muted(
                "Para los informes, Sharky usa Claude con tu propia clave de la API de "
                "Anthropic. Se guarda en el Administrador de credenciales de Windows, nunca en "
                "un fichero. Probarla no gasta nada."
            )
        )
        fila = QHBoxLayout()
        fila.setSpacing(10)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText(
            "Ya hay una clave guardada: déjalo en blanco para conservarla"
            if has_saved_key
            else "sk-ant-…"
        )
        self.key_edit.textChanged.connect(self._on_text_changed)
        fila.addWidget(self.key_edit, 1)
        self.test_button = QPushButton("Probar clave")
        self.test_button.clicked.connect(self.start_check)
        fila.addWidget(self.test_button)
        self.body.addLayout(fila)

        self.result_label = _state_label()
        self.result_label.setVisible(False)
        self.body.addWidget(self.result_label)
        self.body.addWidget(
            muted(
                "Puedes dejarla en blanco: Sharky funciona sin IA y lo indica en cada informe."
            )
        )
        self._finish_layout()
        self._sync()

    # -- estado ------------------------------------------------------------------------

    def api_key(self) -> str:
        return self.key_edit.text().strip()

    @property
    def keeps_saved_key(self) -> bool:
        """El campo está en blanco y ya había una clave: se conserva."""
        return not self.api_key() and self.has_saved_key

    def key_check(self) -> KeyCheck | None:
        """El resultado de «Probar clave» para la clave que hay escrita ahora, si se probó."""
        if self._check is None or self._checked_key != self.api_key():
            return None
        return self._check

    @property
    def checking(self) -> bool:
        return self._worker is not None

    def isComplete(self) -> bool:  # noqa: N802 - nombre de Qt
        if self.checking:
            return False
        comprobacion = self.key_check()
        return comprobacion is None or comprobacion.usable

    # -- probar ------------------------------------------------------------------------

    def start_check(self) -> None:
        clave = self.api_key()
        if not clave or self.checking:
            return
        self._checking = clave
        _set_state(self.result_label, "Probando la clave…", "muted")
        trabajo = Worker(self._checker, clave)
        trabajo.signals.finished.connect(self._on_checked)
        trabajo.signals.failed.connect(self._on_check_failed)
        self._worker = start(trabajo)  # se guarda: sin referencia, las señales se perderían
        self._sync()

    def _on_checked(self, resultado: KeyCheck) -> None:
        clave, self._checking, self._worker = self._checking, None, None
        if clave != self.api_key():
            _set_state(self.result_label, "", "muted")  # la clave cambió mientras se probaba
        else:
            self._check, self._checked_key = resultado, clave
            estado = {KeyStatus.VALID: "okText", KeyStatus.INVALID: "dangerText"}.get(
                resultado.status, "warnText"
            )
            _set_state(self.result_label, resultado.message, estado)
        self._sync()

    def _on_check_failed(self, _mensaje: str) -> None:
        self._on_checked(
            KeyCheck(
                KeyStatus.UNVERIFIED,
                "No se ha podido comprobar la clave. Se guardará sin comprobar.",
            )
        )

    def _on_text_changed(self, _texto: str) -> None:
        if not self.checking:
            _set_state(self.result_label, "", "muted")
        self._sync()

    def _sync(self) -> None:
        self.test_button.setEnabled(bool(self.api_key()) and not self.checking)
        self.test_button.setText("Probando…" if self.checking else "Probar clave")
        self.completeChanged.emit()


# -- paso 2: cartera --------------------------------------------------------------------


class PortfolioPage(_Page):
    """El CSV con vista previa (coste medio editable), el efectivo y el bróker."""

    HEADERS: tuple[str, ...] = (
        "ticker",
        "nombre",
        "unidades",
        "coste medio (€)",
        "divisa",
        "símbolo",
    )
    COST_COLUMN = 3
    SYMBOL_COLUMN = 5
    NUMERIC_COLUMNS = (2, 3)
    MAX_TABLE_HEIGHT = 340

    def __init__(self) -> None:
        super().__init__(
            1,
            "Tu cartera",
            "Adjunta un CSV con tus posiciones. Si no lo tienes, guarda la plantilla, rellénala "
            "en Excel y vuelve aquí. Nada se escribe hasta que confirmes.",
        )
        self._import: CsvImport | None = None
        self._file_name = ""
        self._positions: list[CsvPosition] = []

        botones = QHBoxLayout()
        botones.setSpacing(10)
        self.attach_button = QPushButton("Adjuntar CSV…")
        self.attach_button.setObjectName("primary")
        self.attach_button.clicked.connect(self.attach_csv)
        botones.addWidget(self.attach_button)
        self.template_button = QPushButton("Guardar plantilla CSV…")
        self.template_button.clicked.connect(self.save_template)
        botones.addWidget(self.template_button)
        self.clear_button = QPushButton("Quitar CSV")
        self.clear_button.setToolTip("Empezar sin posiciones, solo con efectivo")
        self.clear_button.clicked.connect(self.clear_csv)
        self.clear_button.setVisible(False)
        botones.addWidget(self.clear_button)
        botones.addStretch(1)
        self.body.addLayout(botones)
        self.file_status = _state_label()
        self.file_status.setVisible(False)
        self.body.addWidget(self.file_status)

        self.body.addWidget(self._build_preview())
        self.body.addLayout(self._build_cash_and_broker())
        self.nav_hint = _state_label()
        self.nav_hint.setVisible(False)
        self.body.addWidget(self.nav_hint)
        self._finish_layout()
        self._changed()

    def _build_preview(self) -> QFrame:
        self.preview = QFrame()
        self.preview.setObjectName("card")
        caja = QVBoxLayout(self.preview)
        caja.setContentsMargins(16, 14, 16, 14)
        caja.setSpacing(10)
        self.preview_title = QLabel()
        self.preview_title.setObjectName("cardTitle")
        caja.addWidget(self.preview_title)
        self.preview_hint = muted(
            "¿El coste medio no es el correcto? Haz doble clic en su celda y corrígelo: con él "
            "se calcula el rendimiento de cada posición."
        )
        caja.addWidget(self.preview_hint)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(list(self.HEADERS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        cabecera = self.table.horizontalHeader()
        cabecera.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for columna in range(len(self.HEADERS)):
            cabecera.setSectionResizeMode(columna, QHeaderView.ResizeMode.ResizeToContents)
            if columna in self.NUMERIC_COLUMNS:
                self.table.horizontalHeaderItem(columna).setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
        cabecera.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._on_item_changed)
        caja.addWidget(self.table)

        self.cost_status = _state_label()
        self.cost_status.setVisible(False)
        caja.addWidget(self.cost_status)

        self.errors_box, self.errors_title, self.errors_label = self._issue_box("dangerBox")
        caja.addWidget(self.errors_box)
        self.warnings_box, self.warnings_title, self.warnings_label = self._issue_box("warnBox")
        caja.addWidget(self.warnings_box)
        self.preview.setVisible(False)
        return self.preview

    @staticmethod
    def _issue_box(name: str) -> tuple[QFrame, QLabel, QLabel]:
        marco = QFrame()
        marco.setObjectName(name)
        caja = QVBoxLayout(marco)
        caja.setContentsMargins(14, 10, 14, 10)
        caja.setSpacing(6)
        titulo = _state_label(state="dangerText" if name == "dangerBox" else "warnText")
        caja.addWidget(titulo)
        texto = QLabel()
        texto.setWordWrap(True)
        texto.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        caja.addWidget(texto)
        marco.setVisible(False)
        return marco, titulo, texto

    def _build_cash_and_broker(self) -> QGridLayout:
        rejilla = QGridLayout()
        rejilla.setHorizontalSpacing(16)
        rejilla.setVerticalSpacing(6)
        efectivo = QLabel("Efectivo en la cuenta (€)")
        efectivo.setObjectName("cardTitle")
        rejilla.addWidget(efectivo, 0, 0)
        broker = QLabel("Bróker")
        broker.setObjectName("cardTitle")
        rejilla.addWidget(broker, 0, 1)

        self.cash_edit = QLineEdit()
        self.cash_edit.setPlaceholderText("0,00")
        self.cash_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.cash_edit.textChanged.connect(self._changed)
        self.cash_edit.editingFinished.connect(self._on_cash_finished)
        rejilla.addWidget(self.cash_edit, 1, 0)
        self.broker_edit = QLineEdit(DEFAULT_BROKER)
        self.broker_edit.setMaxLength(BROKER_MAX_LENGTH)
        self.broker_edit.textChanged.connect(self._changed)
        rejilla.addWidget(self.broker_edit, 1, 1)

        self.cash_error = _state_label(state="dangerText")
        self.cash_error.setVisible(False)
        rejilla.addWidget(self.cash_error, 2, 0)
        self.broker_error = _state_label(state="dangerText")
        self.broker_error.setVisible(False)
        rejilla.addWidget(self.broker_error, 2, 1)
        rejilla.setColumnStretch(0, 1)
        rejilla.setColumnStretch(1, 1)
        rejilla.setColumnStretch(2, 1)
        return rejilla

    # -- lo que hay escrito --------------------------------------------------------------

    @property
    def csv_import(self) -> CsvImport | None:
        return self._import

    @property
    def file_name(self) -> str:
        return self._file_name

    def positions(self) -> list[CsvPosition]:
        """Las posiciones válidas del CSV, con el coste medio que haya corregido el usuario."""
        return list(self._positions)

    def cash(self) -> Decimal | None:
        """El efectivo escrito (en blanco = 0), o None si no es un importe válido."""
        texto = self.cash_edit.text().strip()
        if not texto:
            return Decimal("0")
        try:
            valor = parse_decimal(texto)
        except ValueError:
            return None
        return valor if valor >= 0 else None

    def broker(self) -> str:
        return self.broker_edit.text().strip()

    def isComplete(self) -> bool:  # noqa: N802 - nombre de Qt
        efectivo = self.cash()
        if self._import is not None and not self._import.ok:
            return False
        if efectivo is None or not self.broker():
            return False
        invertido = sum((p.cost_eur for p in self._positions), Decimal("0"))
        return efectivo + invertido > 0

    # -- CSV -----------------------------------------------------------------------------

    def attach_csv(self) -> None:
        ruta = self.choose_csv_file()
        if ruta is not None:
            self.load_csv(ruta)

    def load_csv(self, path: Path) -> None:
        """Lee el fichero (como mucho MAX_BYTES + 1, así uno enorme no congela nada)."""
        try:
            with path.open("rb") as fichero:
                datos = fichero.read(MAX_BYTES + 1)
        except OSError as error:
            log.warning("No se ha podido leer el CSV %s: %s", path, error)
            self.show_error(f"No se ha podido leer {path.name}: {error.strerror or error}")
            return
        self.load_csv_bytes(datos, path.name)

    def load_csv_bytes(self, data: bytes, name: str) -> None:
        lectura = parse_positions_csv(data)
        log.info(
            "CSV %s leído: %d posiciones, %d errores y %d avisos (%s, separador %s%s)",
            name,
            len(lectura.positions),
            len(lectura.errors),
            len(lectura.warnings),
            lectura.encoding,
            lectura.delimiter_name,
            ", con cabecera" if lectura.has_header else ", sin cabecera",
        )
        self._import = lectura
        self._file_name = name
        self._positions = list(lectura.positions)
        _set_state(self.file_status, "", "muted")
        self._render()
        self._changed()

    def clear_csv(self) -> None:
        self._import = None
        self._file_name = ""
        self._positions = []
        self._render()
        self._changed()

    def save_template(self) -> None:
        ruta = self.choose_template_destination()
        if ruta is None:
            return
        try:
            # Con BOM y fin de línea de Windows: Excel la abre bien, con sus tildes.
            ruta.write_text(TEMPLATE_CSV, encoding="utf-8-sig", newline="\r\n")
        except OSError as error:
            log.warning("No se ha podido guardar la plantilla en %s: %s", ruta, error)
            self.show_error(f"No se ha podido guardar la plantilla: {error.strerror or error}")
            return
        log.info("Plantilla CSV guardada en %s", ruta)
        _set_state(
            self.file_status,
            f"Plantilla guardada en {ruta}. Rellénala con tus posiciones y adjúntala con "
            "«Adjuntar CSV…».",
            "muted",
        )

    def _render(self) -> None:
        lectura = self._import
        self.clear_button.setVisible(lectura is not None)
        self.preview.setVisible(lectura is not None)
        _set_state(self.cost_status, "", "dangerText")
        if lectura is None:
            self.table.setRowCount(0)
            return

        n = len(lectura.positions)
        if lectura.data_rows == 0 and lectura.ok:
            titulo = f"{self._file_name} — sin posiciones: empezarás solo con efectivo"
        else:
            leidas = "posición leída" if n == 1 else "posiciones leídas"
            titulo = f"{self._file_name} — {n} {leidas}"
        self.preview_title.setText(titulo)
        self.preview_hint.setVisible(n > 0)

        self.table.blockSignals(True)
        self.table.setRowCount(0)  # fuera las filas (y los avisos «sin símbolo») de otro CSV
        self.table.setRowCount(n)
        for fila, p in enumerate(lectura.positions):
            valores = (p.ticker, p.name, format_units(p.units), format_price(p.avg_cost_eur),
                       p.currency, p.yahoo_symbol or "")
            for columna, valor in enumerate(valores):
                celda = QTableWidgetItem(valor)
                marcas = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if columna == self.COST_COLUMN:
                    marcas |= Qt.ItemFlag.ItemIsEditable
                    celda.setToolTip("Doble clic para corregir el coste medio")
                celda.setFlags(marcas)
                if columna in self.NUMERIC_COLUMNS:
                    celda.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(fila, columna, celda)
            if p.yahoo_symbol is None:
                aviso = _state_label("sin símbolo", "warnText")
                self.table.setCellWidget(fila, self.SYMBOL_COLUMN, aviso)
        self.table.blockSignals(False)
        self.table.setVisible(n > 0)
        alto = self.table.horizontalHeader().height() + 4
        alto += sum(self.table.rowHeight(f) for f in range(n))
        self.table.setFixedHeight(min(self.MAX_TABLE_HEIGHT, alto))

        errores, avisos = lectura.errors, lectura.warnings
        lineas_con_error = len({e.line for e in errores})
        self.errors_box.setVisible(bool(errores))
        self.errors_title.setText(
            f"Errores ({len(errores)}"
            + (f" en {lineas_con_error} líneas" if lineas_con_error > 1 else "")
            + "): corrige el CSV y vuelve a adjuntarlo."
        )
        self.errors_label.setText("\n".join(f"• {e}" for e in errores))
        self.warnings_box.setVisible(bool(avisos))
        self.warnings_title.setText(f"Avisos ({len(avisos)}): no impiden crear la cartera.")
        self.warnings_label.setText("\n".join(f"• {a}" for a in avisos))

    # -- coste medio editable ---------------------------------------------------------

    def set_avg_cost_text(self, row: int, text: str) -> None:
        """Lo mismo que escribir en la celda del coste medio (lo usan los tests)."""
        self.table.item(row, self.COST_COLUMN).setText(text)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != self.COST_COLUMN:
            return
        fila = item.row()
        posicion = self._positions[fila]
        try:
            nueva = posicion.with_avg_cost(parse_decimal(item.text()))
        except ValueError:
            self._set_cost_cell(fila, posicion.avg_cost_eur)
            _set_state(
                self.cost_status,
                f"El coste medio de {posicion.ticker} tiene que ser un número mayor que 0 "
                "(con coma o punto decimal). Se mantiene el anterior.",
                "dangerText",
            )
            return
        self._positions[fila] = nueva
        self._set_cost_cell(fila, nueva.avg_cost_eur)
        _set_state(self.cost_status, "", "dangerText")
        if nueva.avg_cost_eur != posicion.avg_cost_eur:
            log.info("Coste medio de %s corregido en el asistente", posicion.ticker)
        self._changed()

    def _set_cost_cell(self, row: int, value: Decimal) -> None:
        self.table.blockSignals(True)
        self.table.item(row, self.COST_COLUMN).setText(format_price(value))
        self.table.blockSignals(False)

    # -- efectivo y bróker ------------------------------------------------------------

    def _on_cash_finished(self) -> None:
        """Al salir del campo, el importe se reescribe en formato español: así se ve cómo se
        ha entendido («2.900» es 2,90; «2.900,00» es 2.900,00)."""
        efectivo = self.cash()
        if efectivo is not None and self.cash_edit.text().strip():
            self.cash_edit.setText(format_amount(efectivo))

    def _changed(self, *_args: object) -> None:
        texto = self.cash_edit.text().strip()
        efectivo = self.cash()
        if efectivo is None:
            try:
                negativo = parse_decimal(texto) < 0
            except ValueError:
                negativo = False
            _set_state(
                self.cash_error,
                "El efectivo no puede ser negativo."
                if negativo
                else "Escribe el efectivo en euros, por ejemplo 2.900,00.",
                "dangerText",
            )
        else:
            _set_state(self.cash_error, "", "dangerText")
        _set_state(
            self.broker_error,
            "" if self.broker() else "Escribe el nombre de tu bróker.",
            "dangerText",
        )
        invertido = sum((p.cost_eur for p in self._positions), Decimal("0"))
        vacia = efectivo is not None and efectivo + invertido <= 0
        csv_ok = self._import is None or self._import.ok
        _set_state(
            self.nav_hint,
            "Para crear la cartera hace falta al menos una posición o algo de efectivo."
            if vacia and csv_ok
            else "",
            "warnText",
        )
        self.completeChanged.emit()

    # -- diálogos (los tests los sustituyen) -----------------------------------------------

    def choose_csv_file(self) -> Path | None:
        ruta, _filtro = QFileDialog.getOpenFileName(
            self,
            "Adjuntar el CSV de tus posiciones",
            _documents_dir(),
            "CSV (*.csv *.txt);;Todos los ficheros (*)",
        )
        return Path(ruta) if ruta else None

    def choose_template_destination(self) -> Path | None:
        ruta, _filtro = QFileDialog.getSaveFileName(
            self,
            "Guardar la plantilla CSV",
            str(Path(_documents_dir()) / TEMPLATE_FILENAME),
            "CSV (*.csv)",
        )
        return Path(ruta) if ruta else None

    def show_error(self, message: str) -> None:
        QMessageBox.warning(self, "Sharky", message)


# -- paso 3: resumen --------------------------------------------------------------------


class SummaryPage(_Page):
    """Resumen, casilla de inicio con Windows y «Crear cartera»."""

    def __init__(self) -> None:
        super().__init__(
            2,
            "Resumen",
            "Revisa los datos. Al pulsar «Crear cartera» se guardan todos a la vez; hasta "
            "entonces no se ha escrito nada.",
        )
        self._ready = False
        marco = QFrame()
        marco.setObjectName("card")
        rejilla = QGridLayout(marco)
        rejilla.setContentsMargins(18, 14, 18, 14)
        rejilla.setHorizontalSpacing(20)
        rejilla.setVerticalSpacing(10)
        self.values: dict[str, QLabel] = {}
        for fila, (clave, texto) in enumerate(
            (
                ("key", "Clave de Claude"),
                ("broker", "Bróker"),
                ("positions", "Posiciones"),
                ("cash", "Efectivo"),
                ("nav", "Patrimonio inicial"),
            )
        ):
            etiqueta = QLabel(texto)
            etiqueta.setObjectName("muted")
            rejilla.addWidget(etiqueta, fila, 0, Qt.AlignmentFlag.AlignTop)
            valor = QLabel()
            valor.setWordWrap(True)
            rejilla.addWidget(valor, fila, 1)
            self.values[clave] = valor
        rejilla.setColumnStretch(1, 1)
        self.body.addWidget(marco)

        self.body.addWidget(
            muted(
                "El patrimonio se calcula a coste hasta que Sharky descargue precios; a partir "
                "de ahí, a precio de mercado y en euros."
            )
        )
        self.symbol_warning = _state_label(state="warnText")
        self.symbol_warning.setVisible(False)
        self.body.addWidget(self.symbol_warning)

        self.autostart = QCheckBox("Iniciar Sharky al iniciar sesión en Windows")
        self.autostart.setChecked(True)
        self.body.addWidget(self.autostart)
        self.body.addWidget(muted("Se guarda ahora y se aplica a partir del hito H12."))
        self.body.addWidget(
            muted(
                "Sharky nunca envía órdenes a tu bróker: aquí registras lo que ya has hecho "
                "allí."
            )
        )
        self.status_label = _state_label()
        self.status_label.setVisible(False)
        self.body.addWidget(self.status_label)
        self._finish_layout()

    def initializePage(self) -> None:  # noqa: N802 - nombre de Qt
        asistente = self.wizard()
        if isinstance(asistente, SetupWizard):
            asistente.fill_summary()

    def set_ready(self, ready: bool) -> None:
        self._ready = ready
        self.completeChanged.emit()

    def set_status(self, text: str, state: str = "muted") -> None:
        _set_state(self.status_label, text, state)

    def isComplete(self) -> bool:  # noqa: N802 - nombre de Qt
        return self._ready


# -- el asistente -----------------------------------------------------------------------


class SetupWizard(QWizard):
    """El asistente de primer arranque. `exec()` devuelve Accepted si se creó la cartera."""

    def __init__(
        self,
        db: Database,
        store: SettingsStore,
        settings: Settings,
        *,
        key_checker: Callable[[str], KeyCheck] = check_api_key,
        save_key: Callable[[str], None] = secrets.save_api_key,
        has_saved_key: bool | None = None,
        today: Callable[[], date] = date.today,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._store = store
        self._settings = settings
        self._save_key = save_key
        self._today = today
        self._busy = False
        self._committed = False
        self._worker: Worker | None = None

        if has_saved_key is None:
            has_saved_key = secrets.has_api_key()
        self.key_page = KeyPage(key_checker, has_saved_key)
        self.portfolio_page = PortfolioPage()
        self.summary_page = SummaryPage()
        for pagina in (self.key_page, self.portfolio_page, self.summary_page):
            self.addPage(pagina)

        self.setWindowTitle("Sharky — configuración inicial")
        self.setWindowIcon(QIcon(str(paths.icon_path())))
        self.setWizardStyle(QWizard.WizardStyle.ClassicStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setButtonText(QWizard.WizardButton.NextButton, "Siguiente")
        self.setButtonText(QWizard.WizardButton.BackButton, "Atrás")
        self.setButtonText(QWizard.WizardButton.FinishButton, "Crear cartera")
        self.setButtonText(QWizard.WizardButton.CancelButton, "Cancelar")
        for boton in (QWizard.WizardButton.NextButton, QWizard.WizardButton.FinishButton):
            principal = self.button(boton)
            principal.setObjectName("primary")
            # QWizard ya les ha aplicado el estilo al montar su disposición: se repite.
            principal.style().unpolish(principal)
            principal.style().polish(principal)
        self.setMinimumSize(760, 600)
        self.resize(900, 760)

    # -- resumen -----------------------------------------------------------------------

    def key_summary(self) -> str:
        pagina = self.key_page
        if pagina.api_key():
            comprobacion = pagina.key_check()
            if comprobacion is not None and comprobacion.status is KeyStatus.VALID:
                return "Comprobada. Se guardará en el Administrador de credenciales de Windows."
            if comprobacion is None:
                return "Se guardará sin comprobar (no has pulsado «Probar clave»)."
            return "Se guardará sin comprobar: " + comprobacion.message
        if pagina.keeps_saved_key:
            return "Se conserva la que ya estaba guardada en este equipo."
        return "Sin clave: Sharky funcionará sin IA y lo indicará en cada informe."

    def build_request(self) -> SetupRequest:
        """Lo que se va a guardar, con lo que hay escrito ahora. Lanza OpeningError."""
        cartera = self.portfolio_page
        efectivo = cartera.cash()
        if efectivo is None:
            raise OpeningError("El efectivo no es un importe válido.")
        apertura = build_opening(cartera.positions(), efectivo, self._today(), cartera.broker())
        return SetupRequest(
            opening=apertura,
            api_key=self.key_page.api_key() or None,
            start_with_windows=self.summary_page.autostart.isChecked(),
            broker=cartera.broker(),
        )

    def fill_summary(self) -> None:
        resumen = self.summary_page
        valores = resumen.values
        try:
            peticion = self.build_request()
        except OpeningError as error:
            resumen.set_status(str(error), "dangerText")
            resumen.set_ready(False)
            return
        apertura = peticion.opening
        valores["key"].setText(self.key_summary())
        valores["broker"].setText(peticion.broker)
        n = len(apertura.trades)
        if n:
            valores["positions"].setText(
                f"{n} {'posición' if n == 1 else 'posiciones'} · coste "
                f"{format_eur(apertura.invested_eur)}"
            )
        else:
            valores["positions"].setText("Ninguna: empiezas solo con efectivo.")
        valores["cash"].setText(format_eur(apertura.initial_cash.amount_eur))
        valores["nav"].setText(format_eur(apertura.snapshot.nav_eur))
        sin_simbolo = [p.ticker for p in self.portfolio_page.positions() if not p.yahoo_symbol]
        _set_state(
            resumen.symbol_warning,
            (
                f"Sin símbolo de cotización: {', '.join(sin_simbolo)}. Se valorará a coste hasta "
                "que se lo asignes."
            )
            if sin_simbolo
            else "",
            "warnText",
        )
        resumen.set_status("", "muted")
        resumen.set_ready(True)

    # -- confirmar o cancelar --------------------------------------------------------------

    def done(self, result: int) -> None:
        """«Crear cartera» guarda en segundo plano y cierra al terminar; cancelar cierra sin
        escribir nada. Mientras se guarda no se puede cerrar."""
        if self._busy:
            return
        if result == QDialog.DialogCode.Accepted and not self._committed:
            if self.validateCurrentPage():
                self._start_commit()
            return
        if result != QDialog.DialogCode.Accepted:
            log.info("Asistente de primer arranque cancelado: no se ha escrito nada")
        super().done(result)

    def _start_commit(self) -> None:
        try:
            peticion = self.build_request()
        except OpeningError as error:
            self.summary_page.set_status(str(error), "dangerText")
            return
        self._set_busy(True)
        trabajo = Worker(apply_setup, self._db, self._store, self._settings, peticion,
                         self._save_key)
        trabajo.signals.finished.connect(self._on_committed)
        trabajo.signals.failed.connect(self._on_commit_failed)
        self._worker = start(trabajo)

    def _on_committed(self, result: SetupResult) -> None:
        self._worker = None
        self._set_busy(False)
        self._committed = True
        if result.warnings:
            self.show_warning("La cartera se ha creado, pero:\n\n" + "\n\n".join(result.warnings))
        self.done(QDialog.DialogCode.Accepted)

    def _on_commit_failed(self, message: str) -> None:
        self._worker = None
        self._set_busy(False)
        self.summary_page.set_status("No se ha creado la cartera.", "dangerText")
        self.show_error(f"No se ha podido crear la cartera y no se ha guardado nada.\n\n{message}")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for boton in (
            QWizard.WizardButton.BackButton,
            QWizard.WizardButton.FinishButton,
            QWizard.WizardButton.CancelButton,
        ):
            self.button(boton).setEnabled(not busy)
        if busy:
            self.summary_page.set_status("Creando la cartera…", "muted")

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def committed(self) -> bool:
        return self._committed

    def bring_to_front(self) -> None:
        """Otra ejecución de Sharky pide la ventana: la del asistente sale al frente."""
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    # -- diálogos (los tests los sustituyen) -----------------------------------------------

    def show_error(self, message: str) -> None:
        QMessageBox.warning(self, "Sharky", message)

    def show_warning(self, message: str) -> None:
        QMessageBox.information(self, "Sharky", message)
