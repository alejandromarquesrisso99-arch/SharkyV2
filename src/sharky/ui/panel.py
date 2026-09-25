"""Panel (GUIA §5.10, punto 1, y §7, H6).

- Estado del mandato con su drawdown y una barra con marcas en 3, 8 y 20 %.
- Patrimonio (NAV) con su variación del día, coste, PnL y cobertura.
- Efectivo frente a la banda del mandato.
- «Requiere atención»: stops y objetivos alcanzados y niveles que no se pueden verificar (H7),
  incumplimientos con su antigüedad, datos no fiables y posiciones sin tesis. Los avisos de
  niveles se calculan con cada valoración: siguen aquí mientras dure la condición, aunque la
  ventana de aviso y la notificación solo salgan una vez al día.
- Gráfico del valor por participación (pyqtgraph).
- Tarjetas de los informes y del radar: «Próximamente» hasta H9, H10 y H11.

Todo lo que se enseña sale de core (valoración, foto del NAV y auditoría); aquí no se calcula
ni un euro. El Panel no escribe en la base de datos: la foto del día y los incumplimientos se
guardan al actualizar precios (`PriceRefresher`), que es la misma descarga que la de Cartera.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sharky.core.formatting import (
    format_eur,
    format_limit_pct,
    format_number,
    format_pct,
    format_signed_amount,
)
from sharky.core.levels import LevelCheck, LevelStatus, alert_title, proposal_text
from sharky.core.mandate import (
    ALERT_DRAWDOWN,
    CASH_ABOVE,
    CASH_BELOW,
    INTENSIVE_CARE_DRAWDOWN,
    LOCKDOWN_DRAWDOWN,
    STATE_LABELS,
    STATE_TOKENS,
    BreachRule,
    DayChange,
    Finding,
    MandateRules,
    age_text,
    audit,
    day_change,
    days_open,
    held_since,
    is_escalated,
    join_names,
    snapshot_for,
    state_limits_text,
    unit_value_series,
)
from sharky.core.models import Breach, MandateState, NavSnapshot, ThesisStatus
from sharky.core.valuation import Valuation
from sharky.services.db import Database
from sharky.services.market import load_valuation, local_now
from sharky.services.repositories import (
    BreachRepository,
    CashMovementRepository,
    NavSnapshotRepository,
    ThesisRepository,
    load_level_checks,
)
from sharky.services.settings import Settings
from sharky.ui import theme as theme_module
from sharky.ui.pages import card, muted, restyle, set_state, state_label
from sharky.ui.portfolio import PriceRefresher, RefreshOutcome, RefreshProgress
from sharky.ui.theme import ThemeController

log = logging.getLogger(__name__)

#: Sufijo del nombre de objeto (para la hoja de estilo) de cada color de estado.
TOKEN_STYLE = {"ok": "Ok", "warn": "Warn", "danger": "Danger"}

CHART_HEIGHT = 260


def day_text(day: date, today: date) -> str:
    """«hoy», «ayer» o «23/09/2026»."""
    if day == today:
        return "hoy"
    if day == today - timedelta(days=1):
        return "ayer"
    return f"{day:%d/%m/%Y}"


def short_when(moment: datetime, now: datetime) -> str:
    """«hoy, 09:14», «ayer, 18:00» o «23/09/2026, 18:00»."""
    return f"{day_text(moment.date(), now.date())}, {moment:%H:%M}"


# -- los datos del Panel --------------------------------------------------------------------


@dataclass(frozen=True)
class AttentionItem:
    """Una línea de «Requiere atención»."""

    token: str  # "danger" o "warn"
    title: str
    detail: str
    section: str  # adónde lleva «Ver»
    ticker: str = ""


@dataclass(frozen=True)
class PanelData:
    """Todo lo que enseña el Panel, calculado con lo guardado y la valoración de ahora."""

    valuation: Valuation
    snapshot: NavSnapshot  # la foto de hoy tal como queda con esta valoración
    held_since: date | None  # valoración no fiable: día de la foto de la que salen estado y máximo
    change: DayChange | None
    findings: tuple[Finding, ...]
    items: tuple[AttentionItem, ...]
    series: list[tuple[date, Decimal]]
    last_check: datetime | None
    rules: MandateRules
    today: date
    levels: tuple[LevelCheck, ...] = ()

    @property
    def state(self) -> MandateState:
        return self.snapshot.state

    @property
    def attention_token(self) -> str:
        """El color del recuento: rojo si algo exige actuar; si no, ámbar; sin nada, verde."""
        if any(i.token == "danger" for i in self.items):
            return "danger"
        return "warn" if self.items else "ok"

    def finding(self, rule: BreachRule, subject: str = "") -> Finding | None:
        return next((f for f in self.findings if f.rule is rule and f.subject == subject), None)


def level_items(levels: Sequence[LevelCheck]) -> list[AttentionItem]:
    """Los avisos de niveles (GUIA §5.6): cada stop en rojo, cada objetivo en ámbar con su
    propuesta, y las tesis que no se pueden verificar, juntas."""
    items: list[AttentionItem] = []
    for c in levels:
        if c.status is LevelStatus.STOP:
            items.append(AttentionItem(
                "danger", alert_title(c),
                "El mandato exige liquidar. Ejecuta en tu bróker y regístralo.", "tesis", c.ticker,
            ))
    for c in levels:
        if c.status is LevelStatus.TARGET:
            items.append(AttentionItem("warn", alert_title(c), proposal_text(c), "tesis", c.ticker))
    sin_verificar = [c.ticker for c in levels if c.status is LevelStatus.UNVERIFIABLE]
    if sin_verificar:
        items.append(AttentionItem(
            "warn",
            f"Niveles sin verificar: {join_names(sin_verificar)}",
            "Sin precio fiable no se sabe si han tocado su stop o su objetivo: actualiza los "
            "precios.",
            "tesis",
            sin_verificar[0] if len(sin_verificar) == 1 else "",
        ))
    return items


def attention_items(
    valuation: Valuation,
    findings: Sequence[Finding],
    open_breaches: dict[tuple[str, str], Breach],
    with_thesis: set[str],
    rules: MandateRules,
    today: date,
    levels: Sequence[LevelCheck] = (),
) -> tuple[AttentionItem, ...]:
    """«Requiere atención»: los avisos de niveles, los incumplimientos (rojo si están
    escalados, o si la valoración no es fiable) y las posiciones sin tesis. Lo rojo va primero,
    y dentro de lo rojo, los stops."""
    items: list[AttentionItem] = level_items(levels)
    for h in findings:
        guardado = open_breaches.get(h.key)
        dias = days_open(guardado.opened_at, today) if guardado is not None else 0
        escalado = is_escalated(dias, rules)
        rojo = escalado or (h.rule is BreachRule.COVERAGE and not valuation.reliable)
        items.append(
            AttentionItem(
                "danger" if rojo else "warn",
                h.title,
                f"{h.correction} · {age_text(dias, escalado)}",
                "cartera",
                h.ticker,
            )
        )
    sin_tesis = [p.ticker for p in valuation.positions if p.ticker not in with_thesis]
    if sin_tesis:
        n = len(sin_tesis)
        cuantas = "1 posición sin tesis" if n == 1 else f"{n} posiciones sin tesis"
        items.append(
            AttentionItem(
                "warn",
                f"{cuantas}: {join_names(sin_tesis)}",
                "Sin tesis no se vigila ningún stop",
                "tesis",
            )
        )
    return tuple(sorted(items, key=lambda i: i.token != "danger"))


def panel_data(
    conn: sqlite3.Connection,
    valuation: Valuation,
    rules: MandateRules,
    now: datetime,
    market_at: datetime | None = None,
) -> PanelData:
    """Los datos del Panel con lo guardado y esta valoración. Solo lee."""
    hoy = now.date()
    fotos = NavSnapshotRepository(conn).list_all()
    movimientos = CashMovementRepository(conn).list_all()
    foto = snapshot_for(hoy, valuation, fotos, movimientos)
    hallazgos = audit(valuation, foto.state, rules)
    abiertos = {(b.rule, b.subject): b for b in BreachRepository(conn).list_open()}
    con_tesis = {
        t.ticker for t in ThesisRepository(conn).list_all() if t.status is ThesisStatus.ACTIVE
    }
    niveles = load_level_checks(conn, valuation)
    precios = [p.price.fetched_at for p in valuation.positions if p.price is not None]
    return PanelData(
        valuation=valuation,
        snapshot=foto,
        held_since=held_since(foto, fotos),
        change=day_change(foto, fotos, movimientos),
        findings=hallazgos,
        items=attention_items(valuation, hallazgos, abiertos, con_tesis, rules, hoy, niveles),
        series=unit_value_series(fotos, foto),
        last_check=market_at or max(precios, default=None),
        rules=rules,
        today=hoy,
        levels=niveles,
    )


# -- piezas ---------------------------------------------------------------------------------


class LevelBar(QWidget):
    """Una barra con marcas: el drawdown frente a 3, 8 y 20 %, o el efectivo frente a su
    banda. Los colores salen del tema en cada repintado."""

    BAR = 8.0
    TOP = 5.0

    def __init__(self, theme: ThemeController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self.fraction = 0.0
        self.token = "accent"
        self.marks: list[tuple[float, str]] = []
        self.setMinimumWidth(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(18)

    def set_values(self, fraction: float, token: str, marks: Sequence[tuple[float, str]]) -> None:
        """`fraction` y la posición de cada marca van de 0 a 1 sobre el ancho de la barra."""
        self.fraction = max(0.0, min(1.0, fraction))
        self.token = token
        self.marks = [(max(0.0, min(1.0, x)), texto) for x, texto in marks]
        self.setFixedHeight(34 if any(texto for _, texto in self.marks) else 18)
        self.update()

    def paintEvent(self, _event: object) -> None:
        tema = self._theme.effective
        pintor = QPainter(self)
        pintor.setRenderHint(QPainter.RenderHint.Antialiasing)
        ancho = float(self.width())
        arriba, alto = self.TOP, self.BAR
        pintor.setPen(Qt.PenStyle.NoPen)
        pintor.setBrush(theme_module.color(tema, "surface_alt"))
        pintor.drawRoundedRect(QRectF(0, arriba, ancho, alto), alto / 2, alto / 2)
        if self.fraction > 0:
            pintor.setBrush(theme_module.color(tema, self.token))
            relleno = max(alto, ancho * self.fraction)
            pintor.drawRoundedRect(QRectF(0, arriba, relleno, alto), alto / 2, alto / 2)
        fuente = QFont(self.font())
        fuente.setPointSizeF(max(7.0, fuente.pointSizeF() * 0.85))
        medidas = QFontMetrics(fuente)
        pintor.setFont(fuente)
        for x_rel, texto in self.marks:
            x = min(ancho - 1, max(1.0, ancho * x_rel))
            pintor.setPen(QPen(theme_module.color(tema, "text_muted"), 1.5))
            pintor.drawLine(QPointF(x, arriba - 3), QPointF(x, arriba + alto + 3))
            if texto:
                w = medidas.horizontalAdvance(texto)
                izquierda = min(ancho - w, max(0.0, x - w / 2))
                pintor.drawText(
                    QRectF(izquierda, arriba + alto + 5, w + 1, medidas.height()),
                    Qt.AlignmentFlag.AlignLeft,
                    texto,
                )
        pintor.end()


class DayAxis(pg.AxisItem):
    """Eje de fechas (días como ordinales) con etiquetas «dd/mm»."""

    def tickValues(self, minVal: float, maxVal: float, size: float) -> list:
        niveles = super().tickValues(minVal, maxVal, size)
        return [
            (max(espaciado, 1.0), [v for v in valores if float(v).is_integer()])
            for espaciado, valores in niveles
        ]

    def tickStrings(self, values: list, scale: float, spacing: float) -> list[str]:
        textos = []
        for v in values:
            try:
                dia = date.fromordinal(int(round(v)))
            except (ValueError, OverflowError):
                textos.append("")
                continue
            textos.append(f"{dia:%d/%m}")
        return textos


class ValueAxis(pg.AxisItem):
    """Eje de valores con coma decimal."""

    def tickStrings(self, values: list, scale: float, spacing: float) -> list[str]:
        decimales = max(0, -math.floor(math.log10(spacing))) if spacing > 0 else 0
        return [format_number(Decimal(str(float(v))), decimales) for v in values]


class UnitValueChart(QWidget):
    """El valor por participación, día a día, con los colores del tema."""

    def __init__(self, theme: ThemeController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self.points: list[tuple[date, Decimal]] = []
        caja = QVBoxLayout(self)
        caja.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget(
            axisItems={"bottom": DayAxis("bottom"), "right": ValueAxis("right")}
        )
        self.plot.setAntialiasing(True)
        self.plot.setFixedHeight(CHART_HEIGHT)
        self.plot.setMouseEnabled(x=False, y=False)
        item = self.plot.getPlotItem()
        item.hideAxis("left")
        item.showAxis("right")
        item.setMenuEnabled(False)
        item.hideButtons()
        item.showGrid(x=False, y=True, alpha=0.6)
        item.getAxis("right").setStyle(maxTickLevel=0)  # pocas líneas: solo las principales
        self._curve = item.plot([], [])
        self._last = pg.ScatterPlotItem(size=9)
        item.addItem(self._last)
        caja.addWidget(self.plot)
        theme.themeChanged.connect(self.apply_theme)
        self.apply_theme()

    def set_series(self, series: Sequence[tuple[date, Decimal]]) -> None:
        self.points = list(series)
        xs = [float(d.toordinal()) for d, _ in self.points]
        ys = [float(v) for _, v in self.points]
        self._curve.setData(xs, ys)
        self._last.setData(xs[-1:], ys[-1:])
        vista = self.plot.getPlotItem()
        if len(xs) == 1:
            vista.setXRange(xs[0] - 3, xs[0] + 3, padding=0)
            vista.setYRange(ys[0] - 2, ys[0] + 2, padding=0)
        elif xs:
            vista.enableAutoRange()

    def apply_theme(self, *_args: object) -> None:
        tema = self._theme.effective

        def c(token: str) -> QColor:
            return theme_module.color(tema, token)

        self.plot.setBackground(c("surface"))
        for eje in ("bottom", "right"):
            eje_item = self.plot.getPlotItem().getAxis(eje)
            eje_item.setPen(pg.mkPen(c("border")))
            eje_item.setTextPen(pg.mkPen(c("text_muted")))
        self._curve.setPen(pg.mkPen(c("accent"), width=2))
        self._last.setBrush(pg.mkBrush(c("accent")))
        self._last.setPen(pg.mkPen(c("surface"), width=1.5))


def coming_soon(title: str, text: str) -> QFrame:
    """Una tarjeta que todavía no hace nada: su título, «Próximamente» y qué tendrá."""
    marco = QFrame()
    marco.setObjectName("card")
    caja = QVBoxLayout(marco)
    caja.setContentsMargins(20, 16, 20, 16)
    caja.setSpacing(8)
    fila = QHBoxLayout()
    fila.setSpacing(10)
    titulo = QLabel(title)
    titulo.setObjectName("cardTitle")
    fila.addWidget(titulo)
    chip = QLabel("Próximamente")
    chip.setObjectName("chipNeutral")
    fila.addWidget(chip)
    fila.addStretch(1)
    caja.addLayout(fila)
    caja.addWidget(muted(text))
    return marco


def _big(text: str = "", name: str = "bigNumber") -> QLabel:
    etiqueta = QLabel(text)
    etiqueta.setObjectName(name)
    return etiqueta


# -- la página --------------------------------------------------------------------------


class PanelPage(QWidget):
    """La sección Panel."""

    #: «Ver» en «Requiere atención»: la sección y, si lo hay, el ticker.
    navigateRequested = Signal(str, str)
    #: Han cambiado el estado o lo que requiere atención (para el lateral).
    summaryChanged = Signal()
    #: Ha terminado una actualización de precios (bien o mal).
    refreshFinished = Signal()

    def __init__(
        self,
        db: Database,
        theme: ThemeController,
        refresher: PriceRefresher,
        *,
        settings: Settings | None = None,
        now: Callable[[], datetime] = local_now,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._theme = theme
        self._refresher = refresher
        self._settings = settings or Settings()
        self._now = now
        self.data: PanelData | None = None
        #: (línea, botón «Ver») de «Requiere atención», en el orden en que se ven.
        self.attention_rows: list[tuple[AttentionItem, QPushButton]] = []

        self.header_actions = self._build_actions()

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

        self.progress_box = RefreshProgress(refresher)
        caja.addWidget(self.progress_box)
        self.messages_box = QFrame()
        self.messages_box.setObjectName("warnBox")
        mensajes = QVBoxLayout(self.messages_box)
        mensajes.setContentsMargins(14, 10, 14, 10)
        self.messages_label = QLabel()
        self.messages_label.setWordWrap(True)
        self.messages_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        mensajes.addWidget(self.messages_label)
        self.messages_box.setVisible(False)
        caja.addWidget(self.messages_box)

        arriba = QHBoxLayout()
        arriba.setSpacing(16)
        arriba.addWidget(self._build_mandate_card(), 1)
        arriba.addWidget(self._build_nav_card(), 1)
        arriba.addWidget(self._build_cash_card(), 1)
        caja.addLayout(arriba)

        abajo = QHBoxLayout()
        abajo.setSpacing(16)
        izquierda = QVBoxLayout()
        izquierda.setSpacing(16)
        izquierda.addWidget(self._build_attention_card())
        izquierda.addWidget(self._build_chart_card())
        izquierda.addStretch(1)
        abajo.addLayout(izquierda, 3)
        derecha = QVBoxLayout()
        derecha.setSpacing(16)
        for titulo, texto in (
            ("Control diario", "El control del día con Claude: su conclusión, «Leer» y «Ejecutar "
             "ahora» con su precio aproximado. Llega en el hito H9."),
            ("Noticias semanales", "El escaneo de noticias de cada posición, con sus fuentes. "
             "Llega en el hito H10."),
            ("Estudio mensual", "El estudio del mes con un veredicto por posición. Llega en el "
             "hito H10."),
            ("Oportunidades en radar", "Las alertas activas del radar y el acceso a la pantalla "
             "Radar. Llega en el hito H11."),
        ):
            derecha.addWidget(coming_soon(titulo, texto))
        derecha.addStretch(1)
        abajo.addLayout(derecha, 2)
        caja.addLayout(abajo)
        caja.addStretch(1)

        refresher.started.connect(self._on_refresh_started)
        refresher.succeeded.connect(self._on_refresh_done)
        refresher.failed.connect(self._on_refresh_failed)
        refresher.stopped.connect(self._on_refresh_stopped)
        theme.themeChanged.connect(self._on_theme_changed)
        self.reload()

    # -- construcción ------------------------------------------------------------------

    def _build_actions(self) -> QWidget:
        acciones = QWidget()
        fila = QHBoxLayout(acciones)
        fila.setContentsMargins(0, 0, 0, 0)
        self.refresh_button = QPushButton("Actualizar precios (gratis)")
        self.refresh_button.setToolTip(
            "Descarga el último cierre de cada posición y los tipos de cambio, guarda la foto "
            "del día y revisa el mandato. No llama a Claude."
        )
        self.refresh_button.clicked.connect(self.start_refresh)
        fila.addWidget(self.refresh_button)
        return acciones

    def _build_mandate_card(self) -> QFrame:
        marco, caja = card("Estado del mandato")
        fila = QHBoxLayout()
        fila.setSpacing(12)
        self.state_label = _big(name="stateOk")
        fila.addWidget(self.state_label, 0, Qt.AlignmentFlag.AlignBottom)
        self.drawdown_label = muted("")
        self.drawdown_label.setWordWrap(False)
        fila.addWidget(self.drawdown_label, 0, Qt.AlignmentFlag.AlignBottom)
        fila.addStretch(1)
        caja.addLayout(fila)
        self.drawdown_bar = LevelBar(self._theme)
        caja.addWidget(self.drawdown_bar)
        self.limits_label = muted("")
        caja.addWidget(self.limits_label)
        self.unit_label = muted("")
        caja.addWidget(self.unit_label)
        self.held_label = state_label(state="warnText")
        self.held_label.setVisible(False)
        caja.addWidget(self.held_label)
        caja.addStretch(1)
        return marco

    def _build_nav_card(self) -> QFrame:
        marco, caja = card("Patrimonio")
        self.nav_label = _big()
        caja.addWidget(self.nav_label)
        self.change_label = state_label()
        caja.addWidget(self.change_label)
        self.pnl_label = muted("")
        caja.addWidget(self.pnl_label)
        self.coverage_label = muted("")
        caja.addWidget(self.coverage_label)
        caja.addStretch(1)
        return marco

    def _build_cash_card(self) -> QFrame:
        marco, caja = card("Efectivo")
        self.cash_label = _big()
        caja.addWidget(self.cash_label)
        self.cash_weight_label = state_label()
        caja.addWidget(self.cash_weight_label)
        self.cash_bar = LevelBar(self._theme)
        caja.addWidget(self.cash_bar)
        self.band_label = muted("")
        caja.addWidget(self.band_label)
        self.cash_status_label = state_label()
        caja.addWidget(self.cash_status_label)
        caja.addStretch(1)
        return marco

    def _build_attention_card(self) -> QFrame:
        marco, caja = card("Requiere atención")
        caja.setSpacing(6)
        self.attention_subtitle = muted("")
        caja.addWidget(self.attention_subtitle)
        self._attention_box = QVBoxLayout()
        self._attention_box.setSpacing(0)
        caja.addLayout(self._attention_box)
        return marco

    def _build_chart_card(self) -> QFrame:
        marco, caja = card("Valor por participación")
        caja.addWidget(
            muted("Empieza en 100. Ingresos y retiradas no cuentan como ganancia ni como caída.")
        )
        self.chart = UnitValueChart(self._theme)
        caja.addWidget(self.chart)
        self.chart_note = muted("")
        caja.addWidget(self.chart_note)
        return marco

    # -- datos -------------------------------------------------------------------------

    @property
    def refreshing(self) -> bool:
        return self._refresher.running

    def reload(self) -> None:
        """Vuelve a calcular el Panel con lo guardado (sin descargar nada)."""
        conn = self._db.connection()
        ahora = self._now()
        valoracion = load_valuation(conn, ahora, self._refresher.market_at)
        self._show(valoracion, ahora)

    def _show(self, valuation: Valuation, now: datetime) -> None:
        datos = panel_data(
            self._db.connection(), valuation, self._refresher.rules(), now,
            self._refresher.market_at,
        )
        self.data = datos
        self._show_mandate(datos)
        self._show_nav(datos)
        self._show_cash(datos)
        self._show_attention(datos)
        self._show_chart(datos)
        self._update_buttons()
        self.summaryChanged.emit()

    def _show_mandate(self, d: PanelData) -> None:
        foto = d.snapshot
        token = STATE_TOKENS[foto.state]
        self.state_label.setText(STATE_LABELS[foto.state])
        restyle(self.state_label, f"state{TOKEN_STYLE[token]}")
        self.drawdown_label.setText(f"drawdown {format_pct(foto.drawdown, truncate=True)}")
        # La barra llega hasta el 20 % (Bloqueo), con una marca en cada cambio de estado.
        escala = LOCKDOWN_DRAWDOWN
        marcas = [
            (float(frontera / escala), format_limit_pct(frontera))
            for frontera in (ALERT_DRAWDOWN, INTENSIVE_CARE_DRAWDOWN, LOCKDOWN_DRAWDOWN)
        ]
        self.drawdown_bar.set_values(float(foto.drawdown / escala), token, marcas)
        self.limits_label.setText(state_limits_text(foto.state, d.rules))
        self.unit_label.setText(
            f"Valor por participación {format_number(foto.unit_value)} · máximo "
            f"{format_number(foto.high_water_mark)}"
        )
        cobertura = format_pct(foto.coverage, truncate=True)
        if foto.reliable:
            aviso = ""
        elif d.held_since is not None:
            aviso = (
                f"Valoración no fiable (cobertura del {cobertura}): se mantienen el estado y el "
                f"máximo de {day_text(d.held_since, d.today)}."
            )
        else:
            aviso = (
                f"Valoración no fiable (cobertura del {cobertura}): el valor por participación "
                "empezará en 100 con la primera valoración fiable."
            )
        set_state(self.held_label, aviso, "warnText")

    def _show_nav(self, d: PanelData) -> None:
        v = d.valuation
        self.nav_label.setText(format_eur(v.nav_eur))
        cambio = d.change
        if cambio is None:
            texto = (
                "Sin variación: la valoración no es fiable"
                if not d.snapshot.reliable
                else "Todavía no hay una foto anterior con la que comparar"
            )
            set_state(self.change_label, texto, "muted")
        else:
            cuando = (
                "hoy"
                if cambio.since == d.today - timedelta(days=1)
                else f"desde el {cambio.since:%d/%m}"
            )
            importe = format_signed_amount(cambio.amount_eur)
            texto = f"{importe} € {cuando} ({format_pct(cambio.fraction, 2, signed=True)})"
            estado = "okText" if importe[0] == "+" else ("muted" if importe[0] == "0" else
                                                           "dangerText")
            set_state(self.change_label, texto, estado)

        medidas = [p for p in v.positions if p.pnl_eur is not None]
        if not v.positions:
            self.pnl_label.setText("Sin posiciones: todo es efectivo.")
        elif not medidas:
            coste = sum((p.cost_eur for p in v.positions), Decimal(0))
            self.pnl_label.setText(f"Coste {format_eur(coste)} · PnL sin precios todavía")
        else:
            coste = sum((p.cost_eur for p in v.positions), Decimal(0))
            pnl = sum((p.pnl_eur for p in medidas if p.pnl_eur is not None), Decimal(0))
            base = sum((p.cost_eur for p in medidas), Decimal(0))
            porcentaje = format_pct(pnl / base, signed=True) if base else "—"
            self.pnl_label.setText(
                f"Coste {format_eur(coste)} · PnL {format_signed_amount(pnl)} € ({porcentaje})"
            )
        self.coverage_label.setText(
            f"Cobertura de datos: {format_pct(v.coverage, truncate=True)} del NAV con precio "
            "fiable"
        )

    def _show_cash(self, d: PanelData) -> None:
        v = d.valuation
        estado = d.state
        minimo = d.rules.min_cash(estado)
        maximo = d.rules.max_cash(estado)
        falta = d.finding(BreachRule.CASH, CASH_BELOW)
        sobra = d.finding(BreachRule.CASH, CASH_ABOVE)
        token = "danger" if falta else ("warn" if sobra else "ok")
        self.cash_label.setText(format_eur(v.cash_eur))
        set_state(
            self.cash_weight_label,
            f"{format_pct(v.cash_weight)} del patrimonio",
            f"{token}Text",
        )
        marcas = [(float(minimo), "")] + ([(float(maximo), "")] if maximo is not None else [])
        self.cash_bar.set_values(float(v.cash_weight), token, marcas)
        if maximo is not None:
            banda = (
                f"Banda del mandato: {format_limit_pct(minimo).removesuffix(' %')}–"
                f"{format_limit_pct(maximo)}"
            )
        else:
            banda = f"Mínimo del mandato en {STATE_LABELS[estado]}: {format_limit_pct(minimo)}"
        self.band_label.setText(banda)
        if falta is not None:
            texto = f"Faltan unos {format_eur(falta.amount_eur, 0)} para el mínimo del mandato"
        elif sobra is not None:
            texto = (
                f"Sobran unos {format_eur(sobra.amount_eur, 0)} por encima del máximo del mandato"
            )
        elif maximo is not None:
            texto = "Dentro de la banda del mandato"
        else:
            texto = "Por encima del mínimo del mandato"
        set_state(self.cash_status_label, texto, f"{token}Text")

    def _show_attention(self, d: PanelData) -> None:
        while self._attention_box.count():
            elemento = self._attention_box.takeAt(0)
            if elemento.widget() is not None:
                elemento.widget().deleteLater()
        self.attention_rows = []
        n = len(d.items)
        if n == 0:
            subtitulo = "Nada que mirar: la cartera cumple el mandato."
        else:
            subtitulo = "1 cosa que mirar" if n == 1 else f"{n} cosas que mirar"
        self.attention_subtitle.setText(subtitulo)
        for numero, item in enumerate(d.items):
            if numero:
                linea = QFrame()
                linea.setObjectName("separator")
                linea.setFrameShape(QFrame.Shape.HLine)
                self._attention_box.addWidget(linea)
            self._attention_box.addWidget(self._attention_row(item))

    def _attention_row(self, item: AttentionItem) -> QWidget:
        fila = QWidget()
        caja = QHBoxLayout(fila)
        caja.setContentsMargins(0, 8, 0, 8)
        caja.setSpacing(12)
        punto = QLabel()
        punto.setObjectName(f"dot{TOKEN_STYLE[item.token]}")
        punto.setAccessibleName("Exige actuar" if item.token == "danger" else "Aviso")
        caja.addWidget(punto, 0, Qt.AlignmentFlag.AlignTop)
        textos = QVBoxLayout()
        textos.setSpacing(2)
        titulo = QLabel(item.title)
        titulo.setObjectName("itemTitle")
        titulo.setWordWrap(True)
        textos.addWidget(titulo)
        textos.addWidget(muted(item.detail))
        caja.addLayout(textos, 1)
        ver = QPushButton("Ver")
        ver.setObjectName("link")
        ver.setCursor(Qt.CursorShape.PointingHandCursor)
        ver.clicked.connect(
            lambda _checked=False, s=item.section, t=item.ticker: self.navigateRequested.emit(s, t)
        )
        caja.addWidget(ver, 0, Qt.AlignmentFlag.AlignVCenter)
        self.attention_rows.append((item, ver))
        return fila

    def _show_chart(self, d: PanelData) -> None:
        self.chart.set_series(d.series)
        if not d.series:
            nota = "Todavía no hay ninguna valoración fiable: pulsa «Actualizar precios»."
        elif len(d.series) == 1:
            nota = "La evolución aparece a partir del segundo día con valoración fiable."
        else:
            nota = ""
        self.chart_note.setText(nota)
        self.chart_note.setVisible(bool(nota))

    # -- actualizar precios ------------------------------------------------------------

    def start_refresh(self) -> None:
        self._refresher.start()

    def _update_buttons(self, *_args: object) -> None:
        self.refresh_button.setEnabled(not self.refreshing)

    def _show_messages(self, lines: list[str], state: str = "warnBox") -> None:
        restyle(self.messages_box, state)
        self.messages_label.setText("\n".join(lines))
        self.messages_box.setVisible(bool(lines))

    def _on_refresh_started(self) -> None:
        self._show_messages([])
        self._update_buttons()

    def _on_refresh_done(self, outcome: RefreshOutcome) -> None:
        self._show(outcome.valuation, self._now())
        self._show_messages(outcome.refresh.messages)

    def _on_refresh_failed(self, message: str) -> None:
        self._show_messages([f"No se han podido actualizar los precios: {message}"], "dangerBox")

    def _on_refresh_stopped(self) -> None:
        self._update_buttons()
        self.refreshFinished.emit()

    def shutdown(self) -> None:
        self._refresher.shutdown()

    # -- tema y visibilidad ------------------------------------------------------------

    def _on_theme_changed(self, *_args: object) -> None:
        self.drawdown_bar.update()
        self.cash_bar.update()

    def showEvent(self, event: object) -> None:
        super().showEvent(event)
        if not self.refreshing:
            self.reload()
