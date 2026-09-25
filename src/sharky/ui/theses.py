"""Tesis (GUIA §5.6 y §5.10, punto 4; §7, H7) y los avisos de stop y objetivo.

- A la izquierda, las tesis: activas primero y cerradas después, cada una con su estado de hoy
  (stop alcanzado, objetivo alcanzado, niveles sin verificar, propuesta pendiente…), y «Alta
  rápida» para poner stop y objetivo a todas las posiciones sin tesis desde una tabla.
- A la derecha, el editor: los números (solo los cambia el usuario, con un motivo opcional),
  los textos y el historial, que solo crece, con «Aplicar» y «Descartar» en las propuestas.

Los avisos (`LevelNotifier`): después de cada valoración que guarda avisos nuevos, un STOP
abre una ventana por encima de todo y manda una notificación; un OBJETIVO, solo la
notificación. Cada aviso se enseña una vez al día; el Panel lo sigue marcando mientras dure.

Aquí no se calcula nada: la vigilancia sale de `core.levels` y lo guardado, de los
repositorios.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sharky.core.formatting import format_price, format_signed_amount, parse_decimal
from sharky.core.levels import (
    MAX_CONVICTION,
    MIN_CONVICTION,
    STATUS_LABELS,
    STOP_ACTION,
    HistoryEntry,
    LevelCheck,
    LevelStatus,
    ProposalState,
    alert_title,
    changes,
    format_eur_price,
    format_level_eur,
    history,
    levels_of,
    notification,
    position_line,
    proposal_blocker,
    proposal_states,
    proposal_text,
    stop_message,
)
from sharky.core.models import Asset, LevelAlertKind, Thesis, ThesisEvent, ThesisStatus
from sharky.core.valuation import BASE_CURRENCY, PositionValue, Valuation
from sharky.services.db import Database
from sharky.services.market import load_valuation, local_now
from sharky.services.repositories import (
    AssetRepository,
    LevelAlertRepository,
    RecordedLevels,
    ThesisEdit,
    ThesisError,
    ThesisEventRepository,
    ThesisRepository,
    apply_proposal,
    create_theses,
    discard_proposal,
    load_level_checks,
    record_levels,
    update_thesis,
)
from sharky.ui.pages import card, muted, restyle, set_state, state_label
from sharky.ui.portfolio import PriceRefresher, RefreshOutcome
from sharky.ui.theme import ThemeController

log = logging.getLogger(__name__)

LIST_WIDTH = 270
TEXT_HEIGHT = 64
NO_CONVICTION = "—"

EDITOR_NOTE = (
    "Los números solo los cambias tú. Claude y Sharky proponen; cada cambio queda en el "
    "historial con el valor anterior."
)

STATE_TEXT: dict[ProposalState, str] = {
    ProposalState.PENDING: "Propuesta no aplicada",
    ProposalState.APPLIED: "Aplicada",
    ProposalState.DISCARDED: "Descartada",
    ProposalState.SUPERSEDED: "Sustituida por una propuesta más reciente",
}


def _text(value: Decimal | None) -> str:
    return format_price(value) if value is not None else ""


def _clear(layout: QVBoxLayout) -> None:
    """Vacía una caja. Los widgets se esconden ya: su `deleteLater` llega después y, mientras
    tanto, no pueden quedar pintados debajo de los nuevos."""
    while layout.count():
        elemento = layout.takeAt(0)
        widget = elemento.widget()
        if widget is not None:
            widget.hide()
            widget.deleteLater()


def _parse(text: str, name: str, errors: list[str]) -> Decimal | None:
    """Un número de un campo (coma o punto); vacío es None. Si no vale, apunta el error."""
    limpio = text.strip()
    if not limpio:
        return None
    try:
        return parse_decimal(limpio)
    except ValueError:
        errors.append(f"{name}: «{limpio}» no es un número.")
        return None


def status_line(thesis: Thesis, check: LevelCheck | None, pending: bool) -> tuple[str, str]:
    """La línea de estado de una tesis en la lista, con su estilo (color del tema)."""
    if thesis.status is ThesisStatus.CLOSED:
        cuando = f"{thesis.closed_on:%d/%m/%Y}" if thesis.closed_on else ""
        return f"cerrada el {cuando}".strip(), "muted"
    if check is None:
        return "sin posición abierta", "muted"
    if check.status is LevelStatus.STOP:
        return "stop alcanzado", "dangerText"
    if check.status is LevelStatus.TARGET:
        return "objetivo alcanzado", "warnText"
    if check.status is LevelStatus.UNVERIFIABLE:
        return "niveles sin verificar", "warnText"
    if pending:
        return "propuesta pendiente", "warnText"
    if check.status is LevelStatus.NO_LEVELS:
        return "sin stop ni objetivo", "warnText"
    if thesis.conviction is not None:
        return f"convicción {thesis.conviction}", "muted"
    return "entre niveles", "muted"


def check_text(thesis: Thesis, check: LevelCheck | None) -> tuple[str, str]:
    """Lo que dice hoy la vigilancia de la tesis que se está editando, con su estilo."""
    if thesis.status is ThesisStatus.CLOSED:
        partes = [f"Cerrada el {thesis.closed_on:%d/%m/%Y}" if thesis.closed_on else "Cerrada"]
        if thesis.realized_pnl_eur is not None:
            partes.append(f"PnL realizado {format_signed_amount(thesis.realized_pnl_eur)} €")
        if thesis.close_reason:
            partes.append(thesis.close_reason)
        return " · ".join(partes), "muted"
    if check is None:
        return "Sin posición abierta: no hay nada que vigilar.", "muted"
    if check.status is LevelStatus.STOP:
        return f"{alert_title(check)}. {STOP_ACTION}", "dangerText"
    if check.status is LevelStatus.TARGET:
        return f"{alert_title(check)}. {proposal_text(check)}", "warnText"
    if check.status is LevelStatus.UNVERIFIABLE:
        return f"No verificable: {check.note}", "warnText"
    if check.status is LevelStatus.NO_LEVELS:
        return "Sin stop ni objetivo: no se vigila nada. Ponle al menos un stop.", "warnText"
    partes = [f"{STATUS_LABELS[check.status]}: cotiza a {format_eur_price(check.price_eur)}"]
    if thesis.stop is not None:
        partes.append(f"stop {format_level_eur(thesis.stop, check.currency, check.stop_eur)}")
    if thesis.target is not None:
        partes.append(
            f"objetivo {format_level_eur(thesis.target, check.currency, check.target_eur)}"
        )
    return " · ".join(partes), "okText"


# -- la lista ---------------------------------------------------------------------------------


class ThesisItem(QPushButton):
    """Una tesis en la lista: ticker, nombre, activa o cerrada y su estado de hoy."""

    def __init__(self, thesis: Thesis, name: str, status: tuple[str, str],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.thesis = thesis
        self.setObjectName("thesisItem")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        rejilla = QGridLayout(self)
        rejilla.setContentsMargins(14, 10, 14, 10)
        rejilla.setHorizontalSpacing(8)
        rejilla.setVerticalSpacing(4)
        ticker = QLabel(thesis.ticker)
        ticker.setObjectName("itemTitle")
        rejilla.addWidget(ticker, 0, 0)
        # Una sola línea: un nombre largo se corta (entero en la ayuda) en vez de crecer.
        nombre = muted(name)
        nombre.setWordWrap(False)
        nombre.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        nombre.setToolTip(name)
        rejilla.addWidget(nombre, 0, 1)
        activa = thesis.status is ThesisStatus.ACTIVE
        self.state_label = QLabel("Activa" if activa else "Cerrada")
        self.state_label.setObjectName("muted")
        rejilla.addWidget(self.state_label, 0, 2, Qt.AlignmentFlag.AlignTop)
        texto, estilo = status
        self.status_label = state_label(texto, estilo)
        self.status_label.setWordWrap(False)
        rejilla.addWidget(self.status_label, 1, 0, 1, 3)
        rejilla.setColumnStretch(1, 1)
        self.setMinimumHeight(rejilla.sizeHint().height() + 6)
        self.setAccessibleName(f"{thesis.ticker}: {texto}")


# -- el historial -----------------------------------------------------------------------------


class HistoryRow(QWidget):
    """Una línea del historial. Las propuestas pendientes llevan «Aplicar» y «Descartar»."""

    def __init__(self, entry: HistoryEntry, today_year: int, blocker: str, locked: bool,
                 on_apply: Callable[[int], None], on_discard: Callable[[int], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entry = entry
        self.blocker = blocker
        self.apply_button: QPushButton | None = None
        self.discard_button: QPushButton | None = None
        self.note_label: QLabel | None = None
        fila = QHBoxLayout(self)
        fila.setContentsMargins(0, 10, 0, 10)
        fila.setSpacing(16)
        momento = entry.event.created_at
        formato = "%d/%m" if momento.year == today_year else "%d/%m/%Y"
        fecha = muted(f"{momento:{formato}}")
        fecha.setWordWrap(False)
        fecha.setToolTip(f"{momento:%d/%m/%Y %H:%M}")
        fecha.setMinimumWidth(48)
        fila.addWidget(fecha, 0, Qt.AlignmentFlag.AlignTop)
        textos = QVBoxLayout()
        textos.setSpacing(4)
        self.title_label = QLabel(entry.title)
        self.title_label.setObjectName("itemTitle")
        self.title_label.setWordWrap(True)
        textos.addWidget(self.title_label)
        self.detail_label = muted(entry.detail)
        self.detail_label.setVisible(bool(entry.detail))
        textos.addWidget(self.detail_label)
        if entry.state is ProposalState.PENDING and entry.event.id is not None:
            botones = QHBoxLayout()
            botones.setSpacing(10)
            id_evento = entry.event.id
            self.apply_button = QPushButton("Aplicar")
            self.apply_button.setObjectName("primary")
            self.apply_button.clicked.connect(lambda _c=False: on_apply(id_evento))
            botones.addWidget(self.apply_button)
            self.discard_button = QPushButton("Descartar")
            self.discard_button.clicked.connect(lambda _c=False: on_discard(id_evento))
            botones.addWidget(self.discard_button)
            self.note_label = muted("")
            botones.addWidget(self.note_label, 1)
            textos.addLayout(botones)
            self.set_locked(locked)
        elif entry.state is not None:
            textos.addWidget(muted(STATE_TEXT[entry.state]))
        fila.addLayout(textos, 1)

    def set_locked(self, locked: bool) -> None:
        """Con cambios sin guardar en el editor, las propuestas esperan."""
        if self.apply_button is None or self.discard_button is None or self.note_label is None:
            return
        if locked:
            nota = "Guarda o descarta antes tus cambios del editor."
        else:
            nota = self.blocker or STATE_TEXT[ProposalState.PENDING]
        self.note_label.setText(nota)
        self.apply_button.setEnabled(not locked and not self.blocker)
        self.discard_button.setEnabled(not locked)


# -- la página --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Loaded:
    """Lo que se puso en el editor al abrir una tesis (para saber si hay cambios)."""

    thesis_id: int
    fields: tuple[str, ...]


class ThesesPage(QWidget):
    """La sección Tesis."""

    #: Han cambiado los números de alguna tesis: toca vigilar los niveles otra vez.
    levelsChanged = Signal()

    def __init__(
        self,
        db: Database,
        theme: ThemeController,
        refresher: PriceRefresher,
        *,
        now: Callable[[], datetime] = local_now,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._theme = theme
        self._refresher = refresher
        self._now = now
        self.valuation: Valuation | None = None
        self.theses: list[Thesis] = []
        self.checks: dict[int, LevelCheck] = {}
        self.events: dict[int, list[ThesisEvent]] = {}
        self.assets: dict[str, Asset] = {}
        self.items: list[ThesisItem] = []
        self.history_rows: list[HistoryRow] = []
        self.selected_id: int | None = None
        self._loaded: _Loaded | None = None
        self._filling = False

        caja = QHBoxLayout(self)
        caja.setContentsMargins(0, 0, 0, 0)
        caja.setSpacing(16)
        caja.addWidget(self._build_list())
        caja.addWidget(self._build_detail(), 1)

        refresher.succeeded.connect(self._on_refreshed)
        self.reload()

    # -- construcción ------------------------------------------------------------------

    def _build_list(self) -> QWidget:
        desplazable = QScrollArea()
        desplazable.setWidgetResizable(True)
        desplazable.setFrameShape(QFrame.Shape.NoFrame)
        desplazable.setFixedWidth(LIST_WIDTH)
        desplazable.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        interior = QWidget()
        desplazable.setWidget(interior)
        caja = QVBoxLayout(interior)
        caja.setContentsMargins(0, 0, 4, 0)
        caja.setSpacing(10)
        self._list_box = QVBoxLayout()
        self._list_box.setSpacing(10)
        caja.addLayout(self._list_box)
        self.quick_add_button = QPushButton("Alta rápida")
        self.quick_add_button.setToolTip(
            "Pon stop y objetivo a todas las posiciones sin tesis desde una tabla."
        )
        self.quick_add_button.clicked.connect(self.open_quick_add)
        caja.addWidget(self.quick_add_button)
        caja.addStretch(1)
        return desplazable

    def _build_detail(self) -> QWidget:
        desplazable = QScrollArea()
        desplazable.setWidgetResizable(True)
        desplazable.setFrameShape(QFrame.Shape.NoFrame)
        interior = QWidget()
        desplazable.setWidget(interior)
        exterior = QVBoxLayout(interior)
        exterior.setContentsMargins(0, 0, 0, 0)
        exterior.setSpacing(0)

        self.empty_card, caja_vacio = card("Todavía no hay ninguna tesis")
        caja_vacio.addWidget(muted(
            "Una tesis es tu opinión sobre una posición: por qué la tienes, su stop y su "
            "objetivo. Sin tesis no se vigila ningún stop. Con «Alta rápida» pones stop y "
            "objetivo a todas tus posiciones de una vez."
        ))
        exterior.addWidget(self.empty_card)

        self.editor_card = QFrame()
        self.editor_card.setObjectName("card")
        marco = self.editor_card
        caja = QVBoxLayout(marco)
        caja.setContentsMargins(24, 20, 24, 20)
        caja.setSpacing(10)
        exterior.addWidget(marco)
        exterior.addStretch(1)

        cabecera = QHBoxLayout()
        cabecera.setSpacing(12)
        self.title_label = QLabel()
        self.title_label.setObjectName("cardTitle")
        cabecera.addWidget(self.title_label)
        self.status_chip = QFrame()
        self.status_chip.setObjectName("chipOk")
        chip = QHBoxLayout(self.status_chip)
        chip.setContentsMargins(8, 2, 8, 2)
        self.status_chip_label = QLabel()
        chip.addWidget(self.status_chip_label)
        cabecera.addWidget(self.status_chip)
        cabecera.addStretch(1)
        cabecera.addWidget(muted("Convicción"))
        self.conviction_combo = QComboBox()
        self.conviction_combo.addItem(NO_CONVICTION, None)
        for n in range(MIN_CONVICTION, MAX_CONVICTION + 1):
            self.conviction_combo.addItem(f"{n} / {MAX_CONVICTION}", n)
        self.conviction_combo.currentIndexChanged.connect(self._on_edited)
        cabecera.addWidget(self.conviction_combo)
        caja.addLayout(cabecera)

        rejilla = QGridLayout()
        rejilla.setHorizontalSpacing(14)
        rejilla.setVerticalSpacing(4)
        self.entry_edit = QLineEdit()
        self.stop_edit = QLineEdit()
        self.target_edit = QLineEdit()
        self.currency_combo = QComboBox()
        campos = (
            ("Precio de entrada", self.entry_edit),
            ("Stop-loss", self.stop_edit),
            ("Objetivo", self.target_edit),
            ("Divisa de los niveles", self.currency_combo),
        )
        for columna, (etiqueta, campo) in enumerate(campos):
            texto = QLabel(etiqueta)
            texto.setObjectName("fieldLabel")
            rejilla.addWidget(texto, 0, columna)
            rejilla.addWidget(campo, 1, columna)
            rejilla.setColumnStretch(columna, 1)
        for campo in (self.entry_edit, self.stop_edit, self.target_edit):
            campo.setPlaceholderText("—")
            campo.textChanged.connect(self._on_edited)
        self.currency_combo.currentIndexChanged.connect(self._on_edited)
        caja.addLayout(rejilla)
        caja.addWidget(muted(EDITOR_NOTE))
        self.check_label = state_label()
        caja.addWidget(self.check_label)

        self.text_edits: dict[str, QPlainTextEdit] = {}
        for clave, etiqueta in (
            ("why", "Por qué la tengo"),
            ("catalysts", "Catalizadores"),
            ("risks", "Riesgos"),
            ("invalidation", "Qué la invalidaría"),
        ):
            texto = QLabel(etiqueta)
            texto.setObjectName("fieldLabel")
            caja.addWidget(texto)
            campo = QPlainTextEdit()
            campo.setTabChangesFocus(True)
            campo.setFixedHeight(TEXT_HEIGHT)
            campo.textChanged.connect(self._on_edited)
            caja.addWidget(campo)
            self.text_edits[clave] = campo

        self.error_label = state_label(state="dangerText")
        self.error_label.setVisible(False)
        caja.addWidget(self.error_label)
        botones = QHBoxLayout()
        botones.addStretch(1)
        self.revert_button = QPushButton("Descartar cambios")
        self.revert_button.clicked.connect(self.revert)
        botones.addWidget(self.revert_button)
        self.save_button = QPushButton("Guardar")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        botones.addWidget(self.save_button)
        caja.addLayout(botones)

        titulo = QLabel("Historial")
        titulo.setObjectName("cardTitle")
        caja.addSpacing(6)
        caja.addWidget(titulo)
        self._history_box = QVBoxLayout()
        self._history_box.setSpacing(0)
        caja.addLayout(self._history_box)
        return desplazable

    # -- datos -------------------------------------------------------------------------

    def reload(self) -> None:
        """Vuelve a leer las tesis y a vigilarlas con lo guardado (sin descargar nada).

        Si hay cambios sin guardar en el editor, se respetan.
        """
        conn = self._db.connection()
        ahora = self._now()
        self.valuation = load_valuation(conn, ahora, self._refresher.market_at)
        self.checks = {
            c.thesis.id: c for c in load_level_checks(conn, self.valuation)
            if c.thesis.id is not None
        }
        todas = ThesisRepository(conn).list_all()
        activas = sorted((t for t in todas if t.status is ThesisStatus.ACTIVE),
                         key=lambda t: t.ticker)
        cerradas = sorted((t for t in todas if t.status is ThesisStatus.CLOSED),
                          key=lambda t: (t.closed_on or t.opened_on, t.id or 0), reverse=True)
        self.theses = activas + cerradas
        eventos = ThesisEventRepository(conn)
        self.events = {t.id: eventos.list_for(t.id) for t in self.theses if t.id is not None}
        self.assets = {a.ticker: a for a in AssetRepository(conn).list_all()}
        ids = [t.id for t in self.theses]
        if self.selected_id not in ids:
            self.selected_id = ids[0] if ids else None
        self._show_list()
        self._show_quick_add()
        self._show_selected()

    def thesis(self, thesis_id: int | None) -> Thesis | None:
        return next((t for t in self.theses if t.id == thesis_id), None)

    @property
    def selected(self) -> Thesis | None:
        return self.thesis(self.selected_id)

    def without_thesis(self) -> list[PositionValue]:
        """Las posiciones abiertas sin tesis activa."""
        if self.valuation is None:
            return []
        con_tesis = {t.ticker for t in self.theses if t.status is ThesisStatus.ACTIVE}
        return [p for p in self.valuation.positions if p.ticker not in con_tesis]

    def _pending(self, thesis: Thesis) -> bool:
        if thesis.id is None:
            return False
        estados = proposal_states(self.events.get(thesis.id, []))
        return any(e is ProposalState.PENDING for e in estados.values())

    def _show_list(self) -> None:
        _clear(self._list_box)
        self.items = []
        for tesis in self.theses:
            activo = self.assets.get(tesis.ticker)
            nombre = activo.name if activo is not None else ""
            linea = status_line(tesis, self.checks.get(tesis.id), self._pending(tesis))
            item = ThesisItem(tesis, nombre, linea)
            item.setChecked(tesis.id == self.selected_id)
            item.clicked.connect(lambda _c=False, i=tesis.id: self.select(i))
            self._list_box.addWidget(item)
            self.items.append(item)

    def _show_quick_add(self) -> None:
        n = len(self.without_thesis())
        if n == 0:
            texto = "Alta rápida:\ntodas tienen tesis"
        else:
            texto = f"Alta rápida:\n{n} {'posición' if n == 1 else 'posiciones'} sin tesis"
        self.quick_add_button.setText(texto)
        self.quick_add_button.setEnabled(n > 0)

    def select(self, thesis_id: int | None) -> None:
        """Elige una tesis de la lista (si hay cambios sin guardar, se pierden)."""
        if thesis_id != self.selected_id:
            self._loaded = None
        self.selected_id = thesis_id
        for item in self.items:
            item.setChecked(item.thesis.id == thesis_id)
        self._show_selected()

    def select_ticker(self, ticker: str) -> bool:
        """La tesis activa de ese ticker (o la última cerrada)."""
        tesis = next((t for t in self.theses if t.ticker == ticker), None)
        if tesis is None:
            return False
        self.select(tesis.id)
        return True

    # -- el editor -----------------------------------------------------------------------

    def _field_texts(self) -> tuple[str, ...]:
        return (
            self.entry_edit.text().strip(),
            self.stop_edit.text().strip(),
            self.target_edit.text().strip(),
            str(self.currency_combo.currentData()),
            str(self.conviction_combo.currentData()),
            *(e.toPlainText().strip() for e in self.text_edits.values()),
        )

    @property
    def dirty(self) -> bool:
        return self._loaded is not None and self._loaded.fields != self._field_texts()

    def _currencies(self, thesis: Thesis) -> list[str]:
        divisas = [thesis.levels_currency]
        activo = self.assets.get(thesis.ticker)
        for divisa in (BASE_CURRENCY, activo.currency if activo is not None else None):
            if divisa and divisa not in divisas:
                divisas.append(divisa)
        return divisas

    def _fill(self, thesis: Thesis) -> None:
        self._filling = True
        try:
            self.entry_edit.setText(_text(thesis.entry_price))
            self.stop_edit.setText(_text(thesis.stop))
            self.target_edit.setText(_text(thesis.target))
            self.currency_combo.clear()
            for divisa in self._currencies(thesis):
                self.currency_combo.addItem(divisa, divisa)
            self.currency_combo.setCurrentIndex(0)
            self.conviction_combo.setCurrentIndex(
                max(0, self.conviction_combo.findData(thesis.conviction))
                if thesis.conviction is not None else 0
            )
            for clave, campo in self.text_edits.items():
                campo.setPlainText(getattr(thesis, clave))
        finally:
            self._filling = False
        self._loaded = _Loaded(thesis.id or 0, self._field_texts())
        set_state(self.error_label, "", "dangerText")

    def _show_selected(self) -> None:
        tesis = self.selected
        self.empty_card.setVisible(tesis is None)
        self.editor_card.setVisible(tesis is not None)
        if tesis is None:
            self._loaded = None
            return
        if self._loaded is None or self._loaded.thesis_id != tesis.id or not self.dirty:
            self._fill(tesis)
        activo = self.assets.get(tesis.ticker)
        nombre = f"{tesis.ticker} · {activo.name}" if activo is not None else tesis.ticker
        self.title_label.setText(nombre)
        activa = tesis.status is ThesisStatus.ACTIVE
        self.status_chip_label.setText("Activa" if activa else "Cerrada")
        restyle(self.status_chip, "chipOk" if activa else "chip")
        for campo in (self.entry_edit, self.stop_edit, self.target_edit):
            campo.setReadOnly(not activa)
        for campo in self.text_edits.values():
            campo.setReadOnly(not activa)
        self.currency_combo.setEnabled(activa)
        self.conviction_combo.setEnabled(activa)
        self.save_button.setVisible(activa)
        self.revert_button.setVisible(activa)
        texto, estilo = check_text(tesis, self.checks.get(tesis.id))
        set_state(self.check_label, texto, estilo)
        self._show_history(tesis)
        self._update_buttons()

    def _show_history(self, thesis: Thesis) -> None:
        _clear(self._history_box)
        self.history_rows = []
        eventos = self.events.get(thesis.id, []) if thesis.id is not None else []
        anio = self._now().year
        for numero, entrada in enumerate(history(eventos)):
            if numero:
                linea = QFrame()
                linea.setObjectName("separator")
                linea.setFrameShape(QFrame.Shape.HLine)
                self._history_box.addWidget(linea)
            bloqueo = (
                proposal_blocker(entrada.event, thesis)
                if entrada.state is ProposalState.PENDING
                else ""
            )
            fila = HistoryRow(entrada, anio, bloqueo, self.dirty, self.apply, self.discard)
            self._history_box.addWidget(fila)
            self.history_rows.append(fila)

    def _on_edited(self, *_args: object) -> None:
        if self._filling:
            return
        self._update_buttons()

    def _update_buttons(self) -> None:
        sucio = self.dirty
        self.save_button.setEnabled(sucio)
        self.revert_button.setEnabled(sucio)
        for fila in self.history_rows:
            fila.set_locked(sucio)

    def revert(self) -> None:
        tesis = self.selected
        if tesis is not None:
            self._fill(tesis)
            self._show_selected()

    def _edit_from_fields(self, errors: list[str]) -> ThesisEdit:
        return ThesisEdit(
            entry_price=_parse(self.entry_edit.text(), "Precio de entrada", errors),
            stop=_parse(self.stop_edit.text(), "Stop-loss", errors),
            target=_parse(self.target_edit.text(), "Objetivo", errors),
            conviction=self.conviction_combo.currentData(),
            levels_currency=str(self.currency_combo.currentData()),
            why=self.text_edits["why"].toPlainText(),
            catalysts=self.text_edits["catalysts"].toPlainText(),
            risks=self.text_edits["risks"].toPlainText(),
            invalidation=self.text_edits["invalidation"].toPlainText(),
        )

    def save(self) -> bool:
        """«Guardar»: si cambian números, pide un motivo (opcional) y lo deja en el historial."""
        tesis = self.selected
        if tesis is None or tesis.id is None or tesis.status is not ThesisStatus.ACTIVE:
            return False
        errores: list[str] = []
        edicion = self._edit_from_fields(errores)
        if errores:
            set_state(self.error_label, "\n".join(errores), "dangerText")
            return False
        propuesta = replace(
            tesis,
            entry_price=edicion.entry_price,
            stop=edicion.stop,
            target=edicion.target,
            conviction=edicion.conviction,
            levels_currency=edicion.levels_currency,
        )
        numeros = bool(changes(levels_of(tesis), levels_of(propuesta))[1])
        motivo = ""
        if numeros:
            pedido = self.ask_reason(tesis)
            if pedido is None:
                return False
            motivo = pedido
        try:
            with self._db.transaction() as conn:
                update_thesis(conn, tesis.id, edicion, self._now(), motivo)
        except ThesisError as error:
            set_state(self.error_label, str(error), "dangerText")
            return False
        self._loaded = None
        self.reload()
        if numeros:
            self.levelsChanged.emit()
        return True

    def ask_reason(self, thesis: Thesis) -> str | None:
        """El motivo del cambio de números (opcional). None si se cancela (los tests lo
        sustituyen)."""
        texto, aceptado = QInputDialog.getText(
            self,
            "Motivo del cambio",
            f"¿Por qué cambias los números de {thesis.ticker}? Queda en el historial "
            "(opcional).",
        )
        return texto if aceptado else None

    # -- propuestas ----------------------------------------------------------------------

    def apply(self, event_id: int) -> bool:
        """«Aplicar» una propuesta: el usuario acepta sus números."""
        try:
            with self._db.transaction() as conn:
                apply_proposal(conn, event_id, self._now())
        except ThesisError as error:
            set_state(self.error_label, str(error), "dangerText")
            return False
        self._loaded = None
        self.reload()
        self.levelsChanged.emit()
        return True

    def discard(self, event_id: int) -> bool:
        """«Descartar» una propuesta."""
        try:
            with self._db.transaction() as conn:
                discard_proposal(conn, event_id, self._now())
        except ThesisError as error:
            set_state(self.error_label, str(error), "dangerText")
            return False
        self.reload()
        return True

    # -- alta rápida ---------------------------------------------------------------------

    def open_quick_add(self) -> None:
        posiciones = self.without_thesis()
        if not posiciones:
            return
        dialogo = QuickAddDialog(
            self._db, posiciones, self._now, self,
            currencies={t: a.currency for t, a in self.assets.items()},
        )
        try:
            if self.run_dialog(dialogo):
                creadas = dialogo.created
                self.reload()
                if creadas:
                    self.select(creadas[0])
                self.levelsChanged.emit()
        finally:
            dialogo.deleteLater()

    def run_dialog(self, dialog: QDialog) -> bool:
        """Enseña el diálogo y dice si se ha guardado (los tests lo sustituyen)."""
        return dialog.exec() == QDialog.DialogCode.Accepted

    # -- visibilidad ---------------------------------------------------------------------

    def _on_refreshed(self, _outcome: RefreshOutcome) -> None:
        self.reload()

    def showEvent(self, event: object) -> None:
        super().showEvent(event)
        if not self._refresher.running:
            self.reload()


# -- «Alta rápida» ----------------------------------------------------------------------------


@dataclass
class QuickRow:
    """Una fila del alta rápida: la posición y sus campos."""

    position: PositionValue
    entry_edit: QLineEdit
    currency_combo: QComboBox
    stop_edit: QLineEdit
    target_edit: QLineEdit
    default_entry: str


class QuickAddDialog(QDialog):
    """«Alta rápida»: stop y objetivo para todas las posiciones sin tesis, desde una tabla.

    Entrada = coste medio y niveles en EUR por defecto (GUIA §5.10). Las filas sin stop ni
    objetivo se saltan. Todo se guarda en una transacción, y si algo no vale no se guarda nada
    y salen todos los errores a la vez.
    """

    COLUMNS = ("Ticker", "Nombre", "Precio", "Entrada", "Divisa", "Stop", "Objetivo")

    def __init__(
        self,
        db: Database,
        positions: Sequence[PositionValue],
        now: Callable[[], datetime],
        parent: QWidget | None = None,
        *,
        currencies: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._now = now
        #: La divisa de cotización de cada ticker (la del activo), para ofrecerla en los niveles.
        self._currencies = dict(currencies or {})
        self.created: list[int] = []
        self.rows: list[QuickRow] = []
        self.setWindowTitle("Alta rápida de tesis")
        self.setMinimumWidth(820)
        caja = QVBoxLayout(self)
        caja.setContentsMargins(20, 18, 20, 18)
        caja.setSpacing(10)
        titulo = QLabel("Alta rápida: stop y objetivo para las posiciones sin tesis")
        titulo.setObjectName("cardTitle")
        caja.addWidget(titulo)
        caja.addWidget(muted(
            "La entrada es el coste medio en EUR. Si cambias la divisa de los niveles, escribe "
            "la entrada en esa divisa (o déjala vacía). Las filas sin stop ni objetivo se "
            "saltan. Después completas cada tesis en su editor."
        ))

        self.table = QTableWidget(len(positions), len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        cabecera = self.table.horizontalHeader()
        cabecera.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        cabecera.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for fila, posicion in enumerate(positions):
            self._add_row(fila, posicion)
        self.table.verticalHeader().setDefaultSectionSize(40)
        caja.addWidget(self.table, 1)

        self.error_label = state_label(state="dangerText")
        self.error_label.setVisible(False)
        caja.addWidget(self.error_label)
        botones = QHBoxLayout()
        botones.addStretch(1)
        cancelar = QPushButton("Cancelar")
        cancelar.clicked.connect(self.reject)
        botones.addWidget(cancelar)
        self.save_button = QPushButton("Crear tesis")
        self.save_button.setObjectName("primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        botones.addWidget(self.save_button)
        caja.addLayout(botones)

    def _add_row(self, fila: int, p: PositionValue) -> None:
        def celda(texto: str, alinear_derecha: bool = False) -> QTableWidgetItem:
            item = QTableWidgetItem(texto)
            if alinear_derecha:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return item

        self.table.setItem(fila, 0, celda(p.ticker))
        self.table.setItem(fila, 1, celda(p.name))
        precio = f"{format_price(p.price.price)} {p.price.currency}" if p.price else "—"
        item_precio = celda(precio, True)
        if not p.reliable:
            item_precio.setToolTip(p.note or "Precio no fiable.")
        self.table.setItem(fila, 2, item_precio)
        por_defecto = format_price(p.avg_cost_eur)
        entrada = QLineEdit(por_defecto)
        divisa = QComboBox()
        divisa.addItem(BASE_CURRENCY, BASE_CURRENCY)
        cotiza = p.price.currency if p.price is not None else self._currencies.get(p.ticker)
        if cotiza and cotiza != BASE_CURRENCY:
            divisa.addItem(cotiza, cotiza)
        stop = QLineEdit()
        objetivo = QLineEdit()
        for campo in (entrada, stop, objetivo):
            campo.setAlignment(Qt.AlignmentFlag.AlignRight)
            campo.setPlaceholderText("—")
        self.table.setCellWidget(fila, 3, entrada)
        self.table.setCellWidget(fila, 4, divisa)
        self.table.setCellWidget(fila, 5, stop)
        self.table.setCellWidget(fila, 6, objetivo)
        datos = QuickRow(p, entrada, divisa, stop, objetivo, por_defecto)
        divisa.currentIndexChanged.connect(lambda _i, d=datos: self._on_currency(d))
        self.rows.append(datos)

    @staticmethod
    def _on_currency(row: QuickRow) -> None:
        """El coste medio está en EUR: en otra divisa no vale como entrada."""
        en_euros = row.currency_combo.currentData() == BASE_CURRENCY
        texto = row.entry_edit.text().strip()
        if not en_euros and texto == row.default_entry:
            row.entry_edit.clear()
        elif en_euros and not texto:
            row.entry_edit.setText(row.default_entry)

    def row(self, ticker: str) -> QuickRow:
        return next(r for r in self.rows if r.position.ticker == ticker)

    def save(self) -> bool:
        hoy = self._now().date()
        errores: list[str] = []
        nuevas: list[Thesis] = []
        for r in self.rows:
            if not r.stop_edit.text().strip() and not r.target_edit.text().strip():
                continue
            propios: list[str] = []
            entrada = _parse(r.entry_edit.text(), "la entrada", propios)
            stop = _parse(r.stop_edit.text(), "el stop", propios)
            objetivo = _parse(r.target_edit.text(), "el objetivo", propios)
            errores.extend(f"{r.position.ticker}: {e}" for e in propios)
            nuevas.append(Thesis(r.position.ticker, str(r.currency_combo.currentData()), hoy,
                                 entry_price=entrada, stop=stop, target=objetivo))
        if not nuevas and not errores:
            errores.append("Pon el stop o el objetivo de al menos una posición.")
        if not errores:
            try:
                with self._db.transaction() as conn:
                    self.created = create_theses(conn, nuevas, self._now())
            except ThesisError as error:
                errores = error.errors
        if errores:
            set_state(self.error_label, "\n".join(errores), "dangerText")
            return False
        self.accept()
        return True


# -- los avisos -------------------------------------------------------------------------------


class StopAlertDialog(QDialog):
    """El aviso de stop (GUIA §5.6): una ventana modal por encima de todo."""

    #: «Abrir Sharky».
    openRequested = Signal()

    def __init__(self, checks: Sequence[LevelCheck], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.checks: list[LevelCheck] = []
        self.setWindowTitle("Sharky")
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # Cerrar el aviso nunca cierra Sharky, aunque la ventana principal esté escondida.
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setMinimumWidth(520)
        caja = QVBoxLayout(self)
        caja.setContentsMargins(24, 20, 24, 20)
        caja.setSpacing(12)
        titulo = QHBoxLayout()
        titulo.setSpacing(10)
        punto = QLabel()
        punto.setObjectName("dotDanger")
        punto.setAccessibleName("Exige actuar")
        titulo.addWidget(punto, 0, Qt.AlignmentFlag.AlignVCenter)
        self.title_label = QLabel("Stop-loss alcanzado")
        self.title_label.setObjectName("alertTitle")
        titulo.addWidget(self.title_label)
        titulo.addStretch(1)
        caja.addLayout(titulo)
        self._messages = QVBoxLayout()
        self._messages.setSpacing(12)
        caja.addLayout(self._messages)
        botones = QHBoxLayout()
        botones.addStretch(1)
        self.close_button = QPushButton("Cerrar")
        self.close_button.clicked.connect(self.reject)
        botones.addWidget(self.close_button)
        self.open_button = QPushButton("Abrir Sharky")
        self.open_button.setObjectName("primary")
        self.open_button.setDefault(True)
        self.open_button.clicked.connect(self._on_open)
        botones.addWidget(self.open_button)
        caja.addLayout(botones)
        self.add_checks(checks)

    def add_checks(self, checks: Sequence[LevelCheck]) -> None:
        """Añade stops a una ventana que sigue abierta."""
        for c in checks:
            if any(x.ticker == c.ticker for x in self.checks):
                continue
            self.checks.append(c)
            bloque = QVBoxLayout()
            bloque.setSpacing(4)
            mensaje = QLabel(stop_message(c))
            mensaje.setWordWrap(True)
            bloque.addWidget(mensaje)
            bloque.addWidget(muted(position_line(c)))
            self._messages.addLayout(bloque)
        n = len(self.checks)
        self.title_label.setText("Stop-loss alcanzado" if n <= 1 else f"{n} stop-loss alcanzados")

    def _on_open(self) -> None:
        self.openRequested.emit()
        self.accept()


class LevelNotifier(QObject):
    """Enseña los avisos de niveles guardados y todavía sin enseñar (GUIA §5.6).

    STOP → `stopAlert` (la ventana modal) y una notificación crítica; OBJETIVO → solo la
    notificación. Cada aviso se marca como enseñado en cuanto se enseña: el mismo día no se
    repite, ni actualizando precios ni reabriendo la app.
    """

    #: Los stops que hay que avisar con la ventana modal (tupla de LevelCheck).
    stopAlert = Signal(object)
    #: Una notificación: título, texto y si es crítica.
    notification = Signal(str, str, bool)

    def __init__(
        self,
        db: Database,
        refresher: PriceRefresher,
        now: Callable[[], datetime] = local_now,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._refresher = refresher
        self._now = now
        refresher.succeeded.connect(self._on_refreshed)

    def _on_refreshed(self, outcome: RefreshOutcome) -> None:
        self.notify_pending(outcome.levels.checks)

    def check_now(self) -> RecordedLevels:
        """Vigila con los precios guardados (sin descargar) y avisa de lo nuevo. Tras cambiar
        los números de una tesis: la base de datos, sin red."""
        conn = self._db.connection()
        ahora = self._now()
        valoracion = load_valuation(conn, ahora, self._refresher.market_at)
        with self._db.transaction() as tx:
            registro = record_levels(tx, valoracion, ahora)
        self.notify_pending(registro.checks)
        return registro

    def notify_pending(self, checks: Sequence[LevelCheck]) -> None:
        hoy = self._now().date()
        with self._db.transaction() as conn:
            avisos = LevelAlertRepository(conn)
            pendientes = avisos.pending(hoy)
            if not pendientes:
                return
            avisos.mark_notified(a.id for a in pendientes if a.id is not None)
        por_ticker = {c.ticker: c for c in checks}
        stops: list[LevelCheck] = []
        objetivos: list[LevelCheck] = []
        for aviso in pendientes:
            c = por_ticker.get(aviso.ticker)
            if c is None:
                log.warning("Aviso de %s sin su vigilancia: no se puede enseñar", aviso.ticker)
                continue
            if aviso.kind is LevelAlertKind.STOP and c.status is LevelStatus.STOP:
                stops.append(c)
            elif aviso.kind is LevelAlertKind.TARGET and c.status is LevelStatus.TARGET:
                objetivos.append(c)
        if stops:
            log.info("Aviso de stop: %s", ", ".join(c.ticker for c in stops))
            self.stopAlert.emit(tuple(stops))
        for c in stops:
            self.notification.emit(*notification(c), True)
        for c in objetivos:
            log.info("Aviso de objetivo: %s", c.ticker)
            self.notification.emit(*notification(c), False)
