"""Ajustes → Claude (GUIA §5.7 y §5.10, punto 7; H9).

- **Clave**: guardarla en el Administrador de credenciales de Windows, probarla (`models.list`,
  no gasta tokens) y borrarla. Nunca se enseña ni se registra.
- **Modelo y esfuerzo de cada acción**, con la lista de modelos que da `models.list`, y el coste
  aproximado de cada una.
- **Precios** por millón de tokens de entrada y de salida, y de la búsqueda web.
- **Tope de gasto mensual** y lo gastado en el mes (del Registro de ejecuciones).

Los cambios se guardan en `settings.json` con «Guardar cambios» y valen desde ese momento: el
Panel y los informes leen los mismos ajustes. Probar la clave y pedir la lista de modelos van a
la red: en un hilo de trabajo.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from pydantic import ValidationError
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sharky.core.formatting import format_units, month_name, parse_decimal
from sharky.core.reports import ACTION_LABELS, AIAction, BudgetCheck
from sharky.services import secrets
from sharky.services.ai import EFFORTS, ClaudeClient, KeyCheck, KeyStatus, check_api_key
from sharky.services.db import Database
from sharky.services.market import local_now
from sharky.services.reports import action_estimate, spent_this_month
from sharky.services.secrets import SecretsError
from sharky.services.settings import ActionAI, AISettings, ModelPrice, Settings, SettingsStore
from sharky.ui.pages import muted, set_state
from sharky.ui.workers import Worker, start

log = logging.getLogger(__name__)

ACTIONS: tuple[AIAction, ...] = (
    AIAction.DAILY, AIAction.WEEKLY, AIAction.MONTHLY, AIAction.EXPLORER,
)


def _number(value: float) -> str:
    """Un número de los ajustes, en formato español y sin ceros de relleno: 2.0 → «2»."""
    return format_units(Decimal(repr(value)))


def _field(text: str = "", width: int = 90) -> QLineEdit:
    campo = QLineEdit(text)
    campo.setAlignment(Qt.AlignmentFlag.AlignRight)
    campo.setMaximumWidth(width)
    return campo


def _label(text: str) -> QLabel:
    etiqueta = QLabel(text)
    etiqueta.setObjectName("fieldLabel")
    return etiqueta


def list_models_job(key: str) -> list[str]:
    """Los modelos que puede usar la clave. Corre en un hilo de trabajo."""
    with ClaudeClient(key, timeout=30.0) as cliente:
        return cliente.list_models()


class ClaudeCard(QFrame):
    """La tarjeta «Claude» de Ajustes."""

    #: Han cambiado los ajustes de Claude o la clave (el Panel vuelve a poner sus precios).
    changed = Signal()

    def __init__(
        self,
        settings: Settings,
        store: SettingsStore | None = None,
        db: Database | None = None,
        *,
        now: Callable[[], datetime] = local_now,
        key_checker: Callable[[str], KeyCheck] = check_api_key,
        models_lister: Callable[[str], list[str]] = list_models_job,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._settings = settings
        self._store = store
        self._db = db
        self._now = now
        self._key_checker = key_checker
        self._models_lister = models_lister
        self._worker: Worker | None = None
        self.model_boxes: dict[AIAction, QComboBox] = {}
        self.effort_boxes: dict[AIAction, QComboBox] = {}
        self.estimate_labels: dict[AIAction, QLabel] = {}
        self.price_edits: dict[str, tuple[QLineEdit, QLineEdit]] = {}

        caja = QVBoxLayout(self)
        caja.setContentsMargins(20, 18, 20, 18)
        caja.setSpacing(10)
        titulo = QLabel("Claude")
        titulo.setObjectName("cardTitle")
        caja.addWidget(titulo)
        caja.addWidget(muted(
            "Sin clave, o si Claude falla, todo lo demás funciona y cada informe sale con la "
            "etiqueta «Sin análisis de IA». Ninguna llamada a Claude empieza sin enseñar antes "
            "lo que va a costar."
        ))

        caja.addWidget(_label("Clave"))
        self.key_status_label = muted("")
        caja.addWidget(self.key_status_label)
        fila = QHBoxLayout()
        fila.setSpacing(8)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("sk-ant-…  (se guarda en el Administrador de "
                                         "credenciales de Windows)")
        fila.addWidget(self.key_edit, 1)
        self.test_button = QPushButton("Probar clave")
        self.test_button.setToolTip("Pregunta a Claude qué modelos puede usar la clave. No "
                                    "gasta tokens.")
        self.test_button.clicked.connect(self.test_key)
        fila.addWidget(self.test_button)
        self.save_key_button = QPushButton("Guardar clave")
        self.save_key_button.clicked.connect(self.save_key)
        fila.addWidget(self.save_key_button)
        self.delete_key_button = QPushButton("Borrar clave")
        self.delete_key_button.setObjectName("danger")
        self.delete_key_button.clicked.connect(self.delete_key)
        fila.addWidget(self.delete_key_button)
        caja.addLayout(fila)
        self.key_message = muted("")
        self.key_message.setVisible(False)
        caja.addWidget(self.key_message)

        caja.addSpacing(4)
        cabecera = QHBoxLayout()
        cabecera.addWidget(_label("Modelo y esfuerzo de cada acción"))
        cabecera.addStretch(1)
        self.models_button = QPushButton("Actualizar lista de modelos")
        self.models_button.setToolTip("Pide a Claude la lista de modelos (models.list). No "
                                      "gasta tokens.")
        self.models_button.clicked.connect(self.load_models)
        cabecera.addWidget(self.models_button)
        caja.addLayout(cabecera)
        rejilla = QGridLayout()
        rejilla.setHorizontalSpacing(12)
        rejilla.setVerticalSpacing(6)
        for columna, texto in enumerate(("Acción", "Modelo", "Esfuerzo", "Coste aproximado")):
            rejilla.addWidget(muted(texto), 0, columna)
        for n, accion in enumerate(ACTIONS, 1):
            rejilla.addWidget(QLabel(ACTION_LABELS[accion]), n, 0)
            modelos = QComboBox()
            modelos.setEditable(True)
            modelos.setMinimumWidth(200)
            # Al elegir un modelo o terminar de escribirlo (no a cada tecla).
            modelos.activated.connect(self._sync_price_rows)
            modelos.lineEdit().editingFinished.connect(self._sync_price_rows)
            rejilla.addWidget(modelos, n, 1)
            esfuerzos = QComboBox()
            esfuerzos.addItems(list(EFFORTS))
            rejilla.addWidget(esfuerzos, n, 2)
            estimacion = muted("")
            estimacion.setWordWrap(False)
            rejilla.addWidget(estimacion, n, 3)
            self.model_boxes[accion] = modelos
            self.effort_boxes[accion] = esfuerzos
            self.estimate_labels[accion] = estimacion
        rejilla.setColumnStretch(4, 1)
        caja.addLayout(rejilla)
        self.models_message = muted("")
        self.models_message.setVisible(False)
        caja.addWidget(self.models_message)

        caja.addSpacing(4)
        caja.addWidget(_label("Precios en USD por millón de tokens"))
        self._prices_grid = QGridLayout()
        self._prices_grid.setHorizontalSpacing(12)
        self._prices_grid.setVerticalSpacing(6)
        for columna, texto in enumerate(("Modelo", "Entrada", "Salida")):
            self._prices_grid.addWidget(muted(texto), 0, columna)
        self._prices_grid.setColumnStretch(3, 1)
        caja.addLayout(self._prices_grid)
        busqueda = QHBoxLayout()
        busqueda.addWidget(QLabel("Búsqueda web, USD por cada 1.000 búsquedas"))
        self.search_edit = _field()
        busqueda.addWidget(self.search_edit)
        busqueda.addStretch(1)
        caja.addLayout(busqueda)

        caja.addSpacing(4)
        caja.addWidget(_label("Tope de gasto mensual"))
        tope = QHBoxLayout()
        tope.addWidget(QLabel("USD al mes"))
        self.budget_edit = _field()
        tope.addWidget(self.budget_edit)
        self.spent_label = muted("")
        self.spent_label.setWordWrap(False)
        tope.addWidget(self.spent_label)
        tope.addStretch(1)
        caja.addLayout(tope)
        caja.addWidget(muted(
            "Si una acción fuera a superar el tope, no se lanza: se avisa, y el informe sale con "
            "su parte calculada y la etiqueta «Sin análisis de IA»."
        ))

        botones = QHBoxLayout()
        botones.addStretch(1)
        self.restore_button = QPushButton("Restaurar valores por defecto")
        self.restore_button.clicked.connect(self.restore_defaults)
        botones.addWidget(self.restore_button)
        self.save_button = QPushButton("Guardar cambios")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        botones.addWidget(self.save_button)
        caja.addLayout(botones)
        self.save_message = muted("")
        self.save_message.setVisible(False)
        caja.addWidget(self.save_message)

        self.load(settings.ai)
        self.refresh()

    # -- rellenar ----------------------------------------------------------------------

    def load(self, ai: AISettings) -> None:
        """Pone en el formulario estos ajustes (sin guardarlos)."""
        for accion in ACTIONS:
            elegido: ActionAI = getattr(ai, accion.value)
            caja = self.model_boxes[accion]
            caja.blockSignals(True)
            self._fill_models(caja, sorted(ai.prices), elegido.model)
            caja.blockSignals(False)
            self.effort_boxes[accion].setCurrentText(elegido.effort)
        for modelo in list(self.price_edits):
            self._remove_price_row(modelo)
        for modelo, precio in ai.prices.items():
            self._add_price_row(modelo, _number(precio.input_per_mtok),
                                _number(precio.output_per_mtok))
        self.search_edit.setText(_number(ai.web_search_per_1000_usd))
        self.budget_edit.setText(_number(ai.monthly_budget_usd))
        self._sync_price_rows()

    @staticmethod
    def _fill_models(caja: QComboBox, modelos: list[str], elegido: str) -> None:
        caja.clear()
        todos = list(dict.fromkeys([*modelos, elegido]))
        caja.addItems(todos)
        caja.setCurrentText(elegido)

    def _add_price_row(self, model: str, entrada: str = "", salida: str = "") -> None:
        fila = len(self.price_edits) + 1
        nombre = QLabel(model)
        nombre.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        campo_entrada, campo_salida = _field(entrada), _field(salida)
        self._prices_grid.addWidget(nombre, fila, 0)
        self._prices_grid.addWidget(campo_entrada, fila, 1)
        self._prices_grid.addWidget(campo_salida, fila, 2)
        self.price_edits[model] = (campo_entrada, campo_salida)

    def _remove_price_row(self, model: str) -> None:
        campos = self.price_edits.pop(model)
        for i in reversed(range(self._prices_grid.count())):
            widget = self._prices_grid.itemAt(i).widget()
            if widget is None:
                continue
            if widget in campos or (isinstance(widget, QLabel) and widget.text() == model):
                self._prices_grid.removeWidget(widget)
                widget.deleteLater()

    def _sync_price_rows(self, *_args: object) -> None:
        """Un modelo elegido que no tiene precio gana su fila, para ponérselo."""
        for caja in self.model_boxes.values():
            modelo = caja.currentText().strip()
            if modelo and modelo not in self.price_edits:
                self._add_price_row(modelo)

    def refresh(self) -> None:
        """Lo que cambia sin tocar el formulario: la clave, el gasto del mes y las
        estimaciones."""
        hay = secrets.has_api_key()
        self.key_status_label.setText(
            "Hay una clave guardada en el Administrador de credenciales de Windows."
            if hay else
            "No hay clave: los informes salen sin análisis de IA."
        )
        self.delete_key_button.setEnabled(hay)
        if self._db is None:
            self.spent_label.setText("")
            return
        conn = self._db.connection()
        ahora = self._now()
        gastado = spent_this_month(conn, ahora)
        tope = Decimal(repr(self._settings.ai.monthly_budget_usd))
        for accion in ACTIONS:
            self.estimate_labels[accion].setText(action_estimate(conn, accion).text)
        comprobacion = BudgetCheck(gastado, tope, action_estimate(conn, AIAction.DAILY))
        self.spent_label.setText(
            f"Gasto de {month_name(ahora.month)}: {comprobacion.spent_text}"
        )

    # -- la clave ----------------------------------------------------------------------

    def _key_state(self, text: str, state: str) -> None:
        set_state(self.key_message, text, state)

    def save_key(self) -> None:
        clave = self.key_edit.text().strip()
        if not clave:
            self._key_state("Escribe la clave antes de guardarla.", "warnText")
            return
        try:
            secrets.save_api_key(clave)
        except SecretsError as error:
            self._key_state(str(error), "dangerText")
            return
        self.key_edit.clear()
        self._key_state("Clave guardada.", "okText")
        self.refresh()
        self.changed.emit()

    def test_key(self) -> None:
        clave = self.key_edit.text().strip() or secrets.load_api_key()
        if not clave:
            self._key_state("No hay clave que probar: escríbela primero.", "warnText")
            return
        self._busy(True)
        self._key_state("Probando la clave…", "muted")
        trabajo = Worker(self._key_checker, clave)
        trabajo.signals.finished.connect(self._on_key_checked)
        trabajo.signals.failed.connect(self._on_worker_failed)
        self._worker = start(trabajo)

    def _on_key_checked(self, resultado: KeyCheck) -> None:
        self._busy(False)
        estilo = {
            KeyStatus.VALID: "okText",
            KeyStatus.INVALID: "dangerText",
        }.get(resultado.status, "warnText")
        self._key_state(resultado.message, estilo)

    def delete_key(self) -> None:
        if not self.confirm_delete_key():
            return
        try:
            borrada = secrets.delete_api_key()
        except SecretsError as error:
            self._key_state(str(error), "dangerText")
            return
        self._key_state("Clave borrada." if borrada else "No había ninguna clave.", "muted")
        self.refresh()
        self.changed.emit()

    # -- modelos -----------------------------------------------------------------------

    def load_models(self) -> None:
        clave = secrets.load_api_key()
        if not clave:
            set_state(self.models_message, "Guarda antes una clave: la lista de modelos se "
                      "pide a Claude.", "warnText")
            return
        self._busy(True)
        set_state(self.models_message, "Pidiendo la lista de modelos…", "muted")
        trabajo = Worker(self._models_lister, clave)
        trabajo.signals.finished.connect(self._on_models)
        trabajo.signals.failed.connect(self._on_models_failed)
        self._worker = start(trabajo)

    def _on_models(self, modelos: list[str]) -> None:
        self._busy(False)
        for caja in self.model_boxes.values():
            elegido = caja.currentText()
            caja.blockSignals(True)
            self._fill_models(caja, sorted(set(modelos)), elegido)
            caja.blockSignals(False)
        set_state(self.models_message, f"{len(modelos)} modelos disponibles con tu clave.",
                  "muted")

    def _on_models_failed(self, message: str) -> None:
        self._busy(False)
        set_state(self.models_message, f"No se ha podido pedir la lista: {message}", "warnText")

    def _on_worker_failed(self, message: str) -> None:
        self._busy(False)
        self._key_state(f"No se ha podido probar: {message}", "warnText")

    def _busy(self, busy: bool) -> None:
        if not busy:
            self._worker = None
        self.test_button.setEnabled(not busy)
        self.models_button.setEnabled(not busy)

    # -- guardar -----------------------------------------------------------------------

    def form_settings(self) -> tuple[AISettings | None, list[str]]:
        """Los ajustes del formulario, o los errores (todos a la vez)."""
        errores: list[str] = []

        def numero(campo: QLineEdit, que: str) -> Decimal | None:
            try:
                valor = parse_decimal(campo.text())
            except ValueError:
                errores.append(f"{que}: escribe un número.")
                return None
            if valor < 0:
                errores.append(f"{que}: no puede ser negativo.")
                return None
            return valor

        acciones: dict[str, ActionAI] = {}
        for accion in ACTIONS:
            modelo = self.model_boxes[accion].currentText().strip()
            if not modelo:
                errores.append(f"{ACTION_LABELS[accion]}: elige un modelo.")
                continue
            acciones[accion.value] = ActionAI(
                model=modelo, effort=self.effort_boxes[accion].currentText()
            )
        precios: dict[str, ModelPrice] = {}
        elegidos = {a.model for a in acciones.values()}
        self._sync_price_rows()
        for modelo, (entrada, salida) in self.price_edits.items():
            if not entrada.text().strip() and not salida.text().strip():
                if modelo in elegidos:
                    errores.append(f"Pon el precio de {modelo}: lo usa una acción.")
                continue
            e = numero(entrada, f"Precio de entrada de {modelo}")
            s = numero(salida, f"Precio de salida de {modelo}")
            if e is not None and s is not None:
                precios[modelo] = ModelPrice(input_per_mtok=float(e), output_per_mtok=float(s))
        busqueda = numero(self.search_edit, "Búsqueda web")
        tope = numero(self.budget_edit, "Tope de gasto mensual")
        if errores or busqueda is None or tope is None:
            return None, errores
        try:
            return AISettings(
                **acciones,
                prices=precios,
                web_search_per_1000_usd=float(busqueda),
                monthly_budget_usd=float(tope),
            ), []
        except ValidationError as error:  # pragma: no cover - lo de arriba ya lo filtra
            return None, [str(error)]

    def save(self) -> bool:
        nuevo, errores = self.form_settings()
        if nuevo is None:
            set_state(self.save_message, "\n".join(errores), "dangerText")
            return False
        self._settings.ai = nuevo
        if self._store is not None:
            try:
                self._store.save(self._settings)
            except OSError as error:
                log.exception("No se han podido guardar los ajustes de Claude")
                set_state(self.save_message, f"No se han podido guardar: {error}", "dangerText")
                return False
        log.info("Ajustes de Claude guardados")
        set_state(self.save_message, "Guardado. Vale desde ya.", "okText")
        self.refresh()
        self.changed.emit()
        return True

    def restore_defaults(self) -> None:
        self.load(AISettings())
        set_state(self.save_message, "Valores por defecto puestos: pulsa «Guardar cambios» "
                  "para aplicarlos.", "warnText")

    # -- diálogos (los tests los sustituyen) ---------------------------------------------

    def confirm_delete_key(self) -> bool:
        respuesta = QMessageBox.question(
            self,
            "Borrar clave",
            "¿Borrar la clave de Claude del Administrador de credenciales?\n\nLos informes "
            "saldrán sin análisis de IA hasta que pongas otra.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return respuesta == QMessageBox.StandardButton.Yes

    def showEvent(self, event: object) -> None:
        super().showEvent(event)
        self.refresh()
