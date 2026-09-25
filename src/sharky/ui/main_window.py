"""Ventana principal: lateral con las siete secciones y contenido a la derecha.

Abajo del lateral, siempre visibles, el estado del mandato y la hora del último control; en el
botón del Panel, cuántas cosas requieren atención. Todo sale del Panel (ui/panel.py).

La ventana también enseña los avisos de niveles (GUIA §5.6): la ventana modal de un stop, por
encima de todo, y las notificaciones, que manda por la bandeja (`set_notifier`). Y cuando
Operar registra algo (H8), vigila otra vez los niveles y pone al día el Panel y las Tesis; si una
compra ha abierto una tesis, la enseña en Tesis para completarla.

El control diario (H9) lo lanza el `ReportRunner` que comparten el Panel y los Informes; «Leer»
lleva a Informes con ese informe abierto.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sharky import paths
from sharky.core.formatting import format_pct
from sharky.core.levels import LevelCheck
from sharky.core.mandate import STATE_LABELS, STATE_TOKENS
from sharky.services.db import Database
from sharky.services.market import FxProvider, PriceProvider, local_now
from sharky.services.settings import Settings, SettingsStore
from sharky.ui.pages import SECTIONS, Section, SettingsPage, build_page, restyle
from sharky.ui.panel import TOKEN_STYLE, PanelPage, day_text, short_when
from sharky.ui.portfolio import PortfolioPage, PriceRefresher
from sharky.ui.reports import ReportRunner, ReportsPage
from sharky.ui.theme import Theme, ThemeController
from sharky.ui.theses import LevelNotifier, StopAlertDialog, ThesesPage
from sharky.ui.trade import TradePage

log = logging.getLogger(__name__)

SIDEBAR_WIDTH = 236

#: `notificador(título, texto, crítica)`: una notificación del sistema (la de la bandeja).
Notifier = Callable[[str, str, bool], None]


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
        market: PriceProvider | None = None,
        fx: FxProvider | None = None,
        settings: Settings | None = None,
        now: Callable[[], datetime] | None = None,
        store: SettingsStore | None = None,
        runner_options: dict | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._version = version
        self._db = db
        self._market = market
        self._fx = fx
        # Un solo objeto de ajustes para todas las páginas: lo que se guarda en Ajustes vale en
        # el Panel y en los informes desde ese momento.
        self._settings = settings if settings is not None else Settings()
        self._store = store
        self._now = now
        self._buttons: dict[str, QPushButton] = {}
        self._pages: dict[str, QWidget] = {}
        #: Botones propios de cada sección, en la cabecera junto al del tema.
        self._actions: dict[str, QWidget] = {}
        #: La descarga de precios que comparten el Panel y la Cartera.
        self.refresher: PriceRefresher | None = None
        if db is not None and market is not None and fx is not None:
            self.refresher = PriceRefresher(
                db, market, fx, settings=self._settings, now=now or local_now, parent=self
            )
        #: «Ejecutar ahora» del control diario, compartido por el Panel y los Informes.
        #: `runner_options` (el cliente de Claude, la clave…) solo lo cambian los tests.
        self.reports_runner: ReportRunner | None = None
        if db is not None and self.refresher is not None:
            self.reports_runner = ReportRunner(
                db, self.refresher, self._settings, now=now or local_now, parent=self,
                **(runner_options or {}),
            )

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
        panel = self._pages.get("panel")
        if isinstance(panel, PanelPage):
            panel.summaryChanged.connect(self._sync_sidebar)
            panel.navigateRequested.connect(self.navigate)
            if panel.daily_card is not None:
                panel.daily_card.readRequested.connect(self.open_report)

        # Los avisos de niveles: después de las páginas, para que el Panel ya esté al día
        # cuando salga la ventana de un stop.
        self._notifier: Notifier | None = None
        #: Las ventanas de aviso de stop abiertas.
        self.stop_dialogs: list[StopAlertDialog] = []
        self.levels: LevelNotifier | None = None
        if db is not None and self.refresher is not None:
            self.levels = LevelNotifier(db, self.refresher, now or local_now, parent=self)
            self.levels.stopAlert.connect(self.show_stop_alert)
            self.levels.notification.connect(self._notify)
        tesis = self._pages.get("tesis")
        if isinstance(tesis, ThesesPage):
            tesis.levelsChanged.connect(self._on_levels_changed)
        operar = self._pages.get("operar")
        if isinstance(operar, TradePage):
            operar.recorded.connect(self._on_levels_changed)
            operar.thesisOpened.connect(lambda ticker: self.navigate("tesis", ticker))
        if self.reports_runner is not None:
            self.reports_runner.finished.connect(self._on_report_done)

        self._sync_sidebar()
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

    def _build_nav_button(self, section: Section) -> QPushButton:
        boton = QPushButton(section.label)
        boton.setObjectName("navButton")
        boton.setCheckable(True)
        boton.setAutoExclusive(True)
        boton.setCursor(Qt.CursorShape.PointingHandCursor)
        boton.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        boton.clicked.connect(lambda _checked=False, clave=section.key: self.show_section(clave))
        if section.key == "panel":
            # Cuántas cosas requieren atención, a la derecha del botón.
            fila = QHBoxLayout(boton)
            fila.setContentsMargins(0, 0, 12, 0)
            fila.addStretch(1)
            self.attention_badge = QLabel()
            self.attention_badge.setObjectName("badgeWarn")
            self.attention_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.attention_badge.setVisible(False)
            fila.addWidget(self.attention_badge)
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

        self._mandate_chip = QFrame()
        self._mandate_chip.setObjectName("chip")
        chip_caja = QVBoxLayout(self._mandate_chip)
        chip_caja.setContentsMargins(10, 6, 10, 6)
        self._mandate_label = QLabel("Sin datos todavía")
        self._mandate_label.setWordWrap(True)
        chip_caja.addWidget(self._mandate_label)
        caja.addWidget(self._mandate_chip)

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

        self._stack = QStackedWidget()
        for seccion in SECTIONS:
            pagina = build_page(
                seccion,
                self._theme,
                self._version,
                self._db,
                market=self._market,
                fx=self._fx,
                settings=self._settings,
                now=self._now,
                refresher=self.refresher,
                runner=self.reports_runner,
                store=self._store,
            )
            if isinstance(pagina, SettingsPage):
                pagina.restartRequested.connect(self.restartRequested)
            acciones = getattr(pagina, "header_actions", None)
            if isinstance(acciones, QWidget):
                acciones.setVisible(False)
                cabecera.addWidget(acciones)
                self._actions[seccion.key] = acciones
            self._pages[seccion.key] = pagina
            self._stack.addWidget(pagina)
        cabecera.addWidget(self._theme_button)
        caja.addLayout(cabecera)
        caja.addWidget(self._stack, 1)
        return contenido

    # -- comportamiento ----------------------------------------------------------------

    def show_section(self, key: str) -> None:
        """Enseña una sección y deja marcado su botón del lateral."""
        if key not in self._pages:
            raise KeyError(f"Sección desconocida: {key}")
        seccion = next(s for s in SECTIONS if s.key == key)
        self._stack.setCurrentWidget(self._pages[key])
        for clave, acciones in self._actions.items():
            acciones.setVisible(clave == key)
        self._buttons[key].setChecked(True)
        self._title_label.setText(seccion.label)
        self.setWindowTitle(f"Sharky — {seccion.label}")

    @property
    def current_section(self) -> str:
        """Clave de la sección que se está viendo."""
        return next(clave for clave, p in self._pages.items() if p is self._stack.currentWidget())

    def page(self, key: str) -> QWidget:
        """La página de una sección."""
        return self._pages[key]

    def navigate(self, key: str, ticker: str = "") -> None:
        """«Ver» del Panel: la sección y, en Cartera o en Tesis, la de ese ticker."""
        self.show_section(key)
        pagina = self._pages[key]
        if ticker and isinstance(pagina, PortfolioPage | ThesesPage):
            pagina.select_ticker(ticker)

    def open_report(self, report_id: int) -> None:
        """«Leer» en el Panel: Informes, con ese informe abierto."""
        self.show_section("informes")
        pagina = self._pages["informes"]
        if isinstance(pagina, ReportsPage):
            pagina.select_report(report_id)

    def _on_report_done(self, outcome: object) -> None:
        """El control diario ha terminado: los avisos de niveles que haya dejado (ya se
        guardaron antes de llamar a Claude) se enseñan si no se habían enseñado."""
        if self.levels is not None:
            niveles = getattr(outcome, "levels", None)
            if niveles is not None:
                self.levels.notify_pending(niveles.checks)

    # -- avisos de niveles -----------------------------------------------------------------

    def set_notifier(self, notifier: Notifier | None) -> None:
        """Por dónde salen las notificaciones (la bandeja). Sin él, solo quedan en el registro."""
        self._notifier = notifier

    def _notify(self, title: str, text: str, critical: bool) -> None:
        if self._notifier is None:
            log.info("Notificación sin bandeja: %s", title)
            return
        try:
            self._notifier(title, text, critical)
        except Exception:  # una notificación que falla no puede tumbar la vigilancia
            log.exception("No se ha podido enseñar la notificación «%s»", title)

    def show_stop_alert(self, checks: Sequence[LevelCheck]) -> StopAlertDialog:
        """La ventana de aviso de stop, modal y por encima de todo (aunque la ventana principal
        esté escondida en la bandeja). Si ya hay una abierta, se le añaden."""
        abiertas = [d for d in self.stop_dialogs if d.isVisible()]
        if abiertas:
            abiertas[-1].add_checks(checks)
            abiertas[-1].raise_()
            return abiertas[-1]
        dialogo = StopAlertDialog(checks)
        dialogo.openRequested.connect(self._open_from_alert)
        dialogo.destroyed.connect(lambda *_a, d=dialogo: self._forget_dialog(d))
        self.stop_dialogs.append(dialogo)
        dialogo.show()
        dialogo.raise_()
        dialogo.activateWindow()
        return dialogo

    def _forget_dialog(self, dialog: StopAlertDialog) -> None:
        if dialog in self.stop_dialogs:
            self.stop_dialogs.remove(dialog)

    def _open_from_alert(self) -> None:
        self.bring_to_front()
        self.show_section("panel")

    def _on_levels_changed(self) -> None:
        """Han cambiado los números de una tesis, una operación o el efectivo: se vigila otra
        vez con los precios guardados y el Panel (y el lateral) se ponen al día."""
        if self.levels is not None:
            try:
                self.levels.check_now()
            except Exception:
                log.exception("No se han podido vigilar los niveles tras cambiar una tesis")
        tesis = self._pages.get("tesis")
        if isinstance(tesis, ThesesPage):
            tesis.reload()  # la vigilancia puede haber dejado una propuesta en el historial
        panel = self._pages.get("panel")
        if isinstance(panel, PanelPage) and not panel.refreshing:
            panel.reload()

    def shutdown(self) -> None:
        """Al salir: que las páginas corten lo que tengan a medias en segundo plano."""
        if self.reports_runner is not None:
            self.reports_runner.shutdown()
        if self.refresher is not None:
            self.refresher.shutdown()
        for dialogo in list(self.stop_dialogs):
            dialogo.close()
        for pagina in self._pages.values():
            parar = getattr(pagina, "shutdown", None)
            if callable(parar):
                parar()

    def bring_to_front(self) -> None:
        """Trae la ventana al frente (segunda ejecución del programa o icono de la bandeja)."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def _sync_sidebar(self) -> None:
        """El estado del mandato, el último control y el recuento del Panel, en el lateral."""
        panel = self._pages.get("panel")
        datos = panel.data if isinstance(panel, PanelPage) else None
        if datos is None:
            return
        foto = datos.snapshot
        token = STATE_TOKENS[foto.state]
        self._mandate_label.setText(
            f"{STATE_LABELS[foto.state]} · drawdown {format_pct(foto.drawdown, truncate=True)}"
        )
        restyle(self._mandate_chip, f"chip{TOKEN_STYLE[token]}")
        if datos.held_since is not None:
            self._mandate_chip.setToolTip(
                "Valoración no fiable: estado y máximo de "
                f"{day_text(datos.held_since, datos.today)}."
            )
        else:
            self._mandate_chip.setToolTip("")
        ahora = (self._now or local_now)()
        cuando = short_when(datos.last_check, ahora) if datos.last_check else "—"
        self._last_check_label.setText(f"Último control: {cuando}")
        n = len(datos.items)
        self.attention_badge.setText(str(n))
        self.attention_badge.setToolTip(
            "1 cosa requiere atención" if n == 1 else f"{n} cosas requieren atención"
        )
        restyle(self.attention_badge, f"badge{TOKEN_STYLE[datos.attention_token]}")
        self.attention_badge.setVisible(n > 0)

    def _sync_theme_button(self, *_args: object) -> None:
        oscuro = self._theme.effective is Theme.DARK
        self._theme_button.setText("Tema claro" if oscuro else "Tema oscuro")
        self._theme_button.setToolTip(
            "Cambia entre el tema claro y el oscuro. En Ajustes → Apariencia puedes elegir "
            "además el que tenga Windows."
        )
