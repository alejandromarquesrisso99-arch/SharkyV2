"""Autocomprobación del programa: `Sharky.exe --selftest` (GUIA §7, H1).

Comprueba que dentro del programa está todo lo que necesita para arrancar: Qt, la base de
datos (esquema, migraciones, copia y restauración), los ajustes, la plantilla CSV del asistente,
los precios y la valoración (con un Yahoo simulado), el mandato y la foto del NAV, el
Administrador de credenciales, las bibliotecas de mercado e IA y los recursos del paquete. Todo
lo que escribe va a una carpeta temporal: nunca toca los datos del usuario ni su clave.
Con `--online` añade una cotización real con su tipo de cambio y una conexión TLS con la API
de Claude.

Distingue dos cosas que no se parecen en nada:

- **FALLO**: falta algo dentro del programa (una biblioteca, un recurso, un certificado).
  El empaquetado está mal y hay que arreglarlo.
- **AVISO**: algo de fuera no responde ahora (no hay red, Yahoo va lento). El programa está
  bien; el aviso no cuenta como fallo y la salida sigue siendo 0.

Cada hito posterior añade aquí sus comprobaciones.
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import sqlite3
import ssl
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

import keyring

from sharky import __version__, paths
from sharky.services import secrets as secret_store

log = logging.getLogger(__name__)

REPORT_FILE = "sharky_selftest.txt"
KEYRING_SERVICE = "Sharky (autocomprobacion)"
KEYRING_USER = "selftest"
ONLINE_TICKER = "MSFT"
ANTHROPIC_HOST = "api.anthropic.com"
ANTHROPIC_PORT = 443
NETWORK_TIMEOUT = 15


class Status(StrEnum):
    """Resultado de una comprobación, tal cual se escribe en el informe."""

    OK = "OK"
    WARNING = "AVISO"
    FAILURE = "FALLO"


class CheckFailure(Exception):
    """Falta algo dentro del programa: esto sí es un fallo."""


class CheckWarning(Exception):
    """Algo de fuera no responde ahora: es un aviso, no un fallo."""


@dataclass(frozen=True)
class CheckResult:
    """Lo que ha dado una comprobación."""

    name: str
    status: Status
    detail: str


@dataclass(frozen=True)
class Check:
    """Una comprobación: su nombre, qué ejecuta y cómo se clasifican sus errores."""

    name: str
    run: Callable[[], str]
    #: Si es cierto, un error que no sea de import se queda en aviso (depende de la red).
    network: bool = False


@dataclass(frozen=True)
class SelftestReport:
    """El informe completo."""

    results: list[CheckResult]
    online: bool = False
    version: str = __version__
    packaged: bool = field(default_factory=lambda: bool(getattr(sys, "frozen", False)))

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is Status.FAILURE]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is Status.WARNING]

    @property
    def ok(self) -> bool:
        """Correcto si no hay ningún FALLO. Los avisos no tumban la autocomprobación."""
        return not self.failures

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def to_text(self, now: datetime | None = None) -> str:
        """El informe en texto, que es lo que se guarda en `%TEMP%`."""
        momento = now or datetime.now()
        correctas = len(self.results) - len(self.failures) - len(self.warnings)
        lineas = [
            f"Sharky {self.version} — autocomprobación",
            f"Fecha: {momento.strftime('%d/%m/%Y %H:%M:%S')}",
            f"Modo: {'con red' if self.online else 'sin red'}",
            f"Programa: {'empaquetado (exe)' if self.packaged else 'desarrollo'}",
            f"Carpeta de datos: {paths.data_dir()}",
            "",
        ]
        ancho = max((len(r.name) for r in self.results), default=0)
        for r in self.results:
            lineas.append(f"[{r.status.value:<5}] {r.name.ljust(ancho)}  {r.detail}")
        lineas += [
            "",
            f"Resultado: {'CORRECTO' if self.ok else 'CON FALLOS'} · "
            f"{correctas} correctas, {len(self.warnings)} avisos, {len(self.failures)} fallos",
        ]
        if self.warnings:
            lineas.append(
                "Los avisos no son fallos del programa: algo de fuera (la red, Yahoo) no ha "
                "respondido en este momento."
            )
        if self.failures:
            lineas.append(
                "Un fallo significa que al programa le falta algo por dentro: revisa el "
                "empaquetado (packaging/sharky.spec)."
            )
        return "\n".join(lineas) + "\n"


# -- comprobaciones ---------------------------------------------------------------------


def _check_data_dir() -> str:
    carpeta = paths.ensure_data_dirs()
    prueba = carpeta / "selftest.tmp"
    prueba.write_text("sharky", encoding="utf-8")
    leido = prueba.read_text(encoding="utf-8")
    prueba.unlink()
    if leido != "sharky":
        raise CheckFailure(f"no se puede escribir en {carpeta}")
    return f"{carpeta} (se puede escribir; logs, backups y cache creadas)"


def _check_qt() -> str:
    # services no importa nada de ui: aquí solo se comprueba que Qt arranca y pinta.
    import PySide6
    from PySide6.QtWidgets import QApplication, QLabel

    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    etiqueta = QLabel("Sharky")
    etiqueta.resize(160, 40)
    imagen = etiqueta.grab()
    if imagen.isNull() or imagen.width() == 0:
        raise CheckFailure("Qt no ha podido pintar un widget")
    plataforma = app.platformName()
    return f"PySide6 {PySide6.__version__}, plataforma «{plataforma}», estilo Fusion, pinta"


def _check_database() -> str:
    """Esquema, migraciones, una escritura, el libro, la copia de ida y vuelta y borrar la
    cartera."""
    from sharky.core.ledger import build_ledger
    from sharky.core.models import Asset, Trade, TradeKind
    from sharky.services.backup import create_backup, restore_backup, wipe_portfolio
    from sharky.services.db import TABLES, Database, latest_version
    from sharky.services.repositories import AssetRepository, TradeRepository, has_portfolio

    with tempfile.TemporaryDirectory(
        prefix="sharky_selftest_db_", ignore_cleanup_errors=True
    ) as carpeta:
        raiz = Path(carpeta)
        db = Database(raiz / "sharky.db")
        try:
            db.migrate()
            if db.migrate() != 0 or db.user_version() != latest_version():
                raise CheckFailure("migrar dos veces no deja el esquema igual")
            faltan = set(TABLES) - db.tables()
            if faltan:
                raise CheckFailure(f"faltan tablas: {', '.join(sorted(faltan))}")
            conexion = db.connection()
            modo = conexion.execute("PRAGMA journal_mode").fetchone()[0]
            espera = conexion.execute("PRAGMA busy_timeout").fetchone()[0]
            foraneas = conexion.execute("PRAGMA foreign_keys").fetchone()[0]
            if str(modo).lower() != "wal" or espera < 5000 or foraneas != 1:
                raise CheckFailure(
                    f"conexión mal configurada: modo {modo}, espera {espera}, "
                    f"claves foráneas {foraneas}"
                )

            with db.transaction() as conn:
                AssetRepository(conn).add(Asset("PRUEBA", "Prueba", "EUR"))
                TradeRepository(conn).add(
                    Trade(date(2026, 1, 2), "PRUEBA", TradeKind.OPENING, Decimal("3"),
                          Decimal("10"), "EUR", Decimal("1"), Decimal("0"), Decimal("30"))
                )
            momento = datetime(2026, 1, 2, 12, 0, 0)
            copia = create_backup(db, momento, raiz / "backups")
            with db.transaction() as conn:
                conn.execute("DELETE FROM trades")
            restore_backup(db, copia, momento, raiz / "backups")
            libro = build_ledger(TradeRepository(db.connection()).list_all())
            posicion = libro.position("PRUEBA")
            if posicion is None or posicion.units != Decimal("3"):
                raise CheckFailure("la copia restaurada no trae lo que se guardó")
            wipe_portfolio(db, momento, raiz / "backups")
            if has_portfolio(db.connection()) or db.user_version() != latest_version():
                raise CheckFailure("borrar la cartera no deja la base de datos vacía y al día")
        finally:
            db.close_all()
    return (
        f"SQLite {sqlite3.sqlite_version}, modo {modo}, esquema {latest_version()} "
        f"({len(TABLES)} tablas); migrar dos veces, escribir, copiar, restaurar y borrar la "
        "cartera, correctos"
    )


def _check_settings() -> str:
    from sharky.services.settings import Settings, SettingsStore

    with tempfile.TemporaryDirectory(
        prefix="sharky_selftest_settings_", ignore_cleanup_errors=True
    ) as carpeta:
        almacen = SettingsStore(Path(carpeta) / "settings.json")
        ajustes = Settings()
        ajustes.appearance.theme = "oscuro"
        almacen.save(ajustes)
        if almacen.load() != ajustes:
            raise CheckFailure("settings.json no devuelve lo que se guardó")
        almacen.path.write_text("{ esto no es JSON", encoding="utf-8")
        if almacen.load() != Settings():
            raise CheckFailure("un settings.json dañado no vuelve a los valores por defecto")
    return "pydantic: guardar (atómico), leer y recuperarse de un fichero dañado, correctos"


def _check_csv_template() -> str:
    """H4: la plantilla CSV se lee sin errores y con ella se crea una cartera en una base de
    datos temporal, en una transacción."""
    from sharky.core.csv_import import TEMPLATE_CSV, build_opening, parse_positions_csv
    from sharky.core.formatting import format_eur
    from sharky.core.ledger import build_ledger
    from sharky.services.db import Database
    from sharky.services.repositories import (
        CashMovementRepository,
        TradeRepository,
        create_portfolio,
        has_portfolio,
    )

    lectura = parse_positions_csv(TEMPLATE_CSV.encode("utf-8-sig"))
    if not lectura.ok or lectura.warnings or len(lectura.positions) != 3:
        raise CheckFailure(
            "la plantilla CSV no se lee bien: " + "; ".join(str(i) for i in lectura.issues)
        )
    efectivo = Decimal("1000")
    apertura = build_opening(lectura.positions, efectivo, date(2026, 1, 2), "Bróker de prueba")

    with tempfile.TemporaryDirectory(
        prefix="sharky_selftest_csv_", ignore_cleanup_errors=True
    ) as carpeta:
        db = Database(Path(carpeta) / "sharky.db")
        try:
            db.migrate()
            with db.transaction() as conn:
                create_portfolio(conn, apertura)
            conexion = db.connection()
            if not has_portfolio(conexion):
                raise CheckFailure("tras crear la cartera, la base de datos dice que no hay")
            libro = build_ledger(TradeRepository(conexion).list_all())
            costes = {t: p.avg_cost_eur for t, p in libro.positions.items()}
            if costes != {p.ticker: p.avg_cost_eur for p in lectura.positions}:
                raise CheckFailure("el coste medio del libro no es el de la plantilla")
            if CashMovementRepository(conexion).balance() != efectivo:
                raise CheckFailure("el efectivo inicial no cuadra")
        finally:
            db.close_all()
    return (
        f"plantilla con {len(lectura.positions)} posiciones y sin errores; cartera creada en una "
        f"transacción (patrimonio a coste {format_eur(apertura.snapshot.nav_eur)})"
    )


def _check_market() -> str:
    """H5: precios y valoración sin red. Un yfinance simulado (con pandas, como el de verdad)
    da precios en EUR, USD y GBp; se guardan en una base de datos temporal y se valoran con sus
    procedencias. También comprueba que la caché de yfinance se deja configurar."""
    import pandas

    from sharky.core.formatting import format_pct
    from sharky.core.models import (
        Asset,
        CashKind,
        CashMovement,
        PriceSource,
        Trade,
        TradeKind,
    )
    from sharky.services.db import Database
    from sharky.services.market import (
        YahooMarket,
        configure_yfinance,
        load_valuation,
        refresh_market,
    )
    from sharky.services.repositories import (
        AssetRepository,
        CashMovementRepository,
        TradeRepository,
    )

    cierres = {
        "SAN.MC": (5.12, "EUR"),
        "AAPL": (215.4, "USD"),
        "VOD.L": (125.30000305175781, "GBp"),
        "USDEUR=X": (0.85, "EUR"),
        "GBPEUR=X": (1.16, "EUR"),
    }

    class TickerSimulado:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol
            self.history_metadata: dict[str, str] = {}

        def history(self, **_kwargs: object) -> pandas.DataFrame:
            if self.symbol not in cierres:
                return pandas.DataFrame()
            valor, divisa = cierres[self.symbol]
            self.history_metadata = {"currency": divisa}
            indice = pandas.DatetimeIndex([pandas.Timestamp("2026-01-02")])
            return pandas.DataFrame({"Close": [valor]}, index=indice.tz_localize("Europe/Madrid"))

    dia = date(2026, 1, 2)
    ahora = datetime(2026, 1, 2, 18, 0, tzinfo=UTC)
    uno = Decimal("1")
    with tempfile.TemporaryDirectory(
        prefix="sharky_selftest_market_", ignore_cleanup_errors=True
    ) as carpeta:
        raiz = Path(carpeta)
        try:
            cache = configure_yfinance(raiz / "cache")
            if not cache.is_dir():
                raise CheckFailure("no se ha podido crear la caché de yfinance")
        finally:
            configure_yfinance()  # suelta la carpeta temporal
        db = Database(raiz / "sharky.db")
        try:
            db.migrate()
            with db.transaction() as conn:
                for ticker, divisa, simbolo in (
                    ("SAN", "EUR", "SAN.MC"),
                    ("AAPL", "USD", "AAPL"),
                    ("VOD", "GBp", "VOD.L"),
                    ("SINSIMBOLO", "EUR", None),
                ):
                    AssetRepository(conn).add(Asset(ticker, ticker, divisa, yahoo_symbol=simbolo))
                    TradeRepository(conn).add(
                        Trade(dia, ticker, TradeKind.OPENING, Decimal("10"), uno, "EUR", uno,
                              Decimal("0"), Decimal("10"))
                    )
                CashMovementRepository(conn).add(
                    CashMovement(dia, CashKind.INITIAL, Decimal("100"))
                )
            mercado = YahooMarket(
                ticker_factory=TickerSimulado,
                search_factory=lambda *_a, **_k: None,
                sleep=lambda _s: None,
            )
            refresco = refresh_market(db, mercado, mercado, ahora)
            valoracion = load_valuation(db.connection(), ahora, refresco.fetched_at)
        finally:
            db.close_all()

    esperado = {
        "SAN": (Decimal("51.2"), PriceSource.MARKET),
        "AAPL": (Decimal("1830.9"), PriceSource.MARKET),  # 10 × 215,4 × 0,85
        "VOD": (Decimal("14.5348"), PriceSource.MARKET),  # 10 × 125,3 × 1,16 / 100
        "SINSIMBOLO": (Decimal("10"), PriceSource.COST),
    }
    for ticker, (valor, origen) in esperado.items():
        posicion = valoracion.position(ticker)
        if posicion is None or posicion.value_eur != valor or posicion.source is not origen:
            obtenido = (posicion.value_eur, posicion.source) if posicion else None
            raise CheckFailure(f"{ticker} se valora mal: {obtenido}, se esperaba {valor, origen}")
    return (
        f"Yahoo simulado: EUR, USD y GBp a mercado y un activo sin símbolo a coste; cobertura "
        f"{format_pct(valoracion.coverage)}; caché de yfinance configurable"
    )


def _check_mandate() -> str:
    """H6: los estados en sus fronteras exactas, un ingreso que no mueve el valor por
    participación y la foto del día con su auditoría, guardadas en una base de datos temporal."""
    from sharky.core.mandate import snapshot_for, state_for
    from sharky.core.models import CashKind, CashMovement, MandateState, NavSnapshot
    from sharky.core.valuation import Valuation
    from sharky.services.db import Database
    from sharky.services.repositories import (
        CashMovementRepository,
        NavSnapshotRepository,
        record_valuation,
    )
    from sharky.services.settings import MandateSettings

    fronteras = {
        "0.0299": MandateState.OPTIMAL,
        "0.03": MandateState.ALERT,
        "0.0799": MandateState.ALERT,
        "0.08": MandateState.INTENSIVE_CARE,
        "0.1999": MandateState.INTENSIVE_CARE,
        "0.20": MandateState.LOCKDOWN,
    }
    for caida, estado in fronteras.items():
        if state_for(Decimal(caida)) is not estado:
            raise CheckFailure(f"un drawdown de {caida} no da el estado {estado}")

    cien = Decimal("100")
    ayer, hoy = date(2026, 1, 1), date(2026, 1, 2)
    ahora = datetime(2026, 1, 2, 18, 0, tzinfo=UTC)
    base = NavSnapshot(ayer, Decimal("10000"), Decimal("10000"), cien, cien, cien, Decimal("0"),
                       MandateState.OPTIMAL, Decimal("1"), True)
    ingreso = CashMovement(hoy, CashKind.DEPOSIT, Decimal("5000"))
    valoracion = Valuation(ahora, Decimal("15000"), Decimal("15000"), Decimal("1"), (), ())
    foto = snapshot_for(hoy, valoracion, [base], [ingreso])
    if foto.unit_value != cien or foto.fund_units != Decimal("150"):
        raise CheckFailure("un ingreso ha movido el valor por participación")

    with tempfile.TemporaryDirectory(
        prefix="sharky_selftest_mandate_", ignore_cleanup_errors=True
    ) as carpeta:
        db = Database(Path(carpeta) / "sharky.db")
        try:
            db.migrate()
            with db.transaction() as conn:
                NavSnapshotRepository(conn).save(base)
                CashMovementRepository(conn).add(ingreso)
                registro = record_valuation(conn, valoracion, MandateSettings().rules(), ahora)
            guardada = NavSnapshotRepository(db.connection()).get(hoy)
        finally:
            db.close_all()
    if not registro.saved or guardada != registro.snapshot:
        raise CheckFailure("la foto del NAV del día no se ha guardado")
    abiertos = {(b.rule, b.subject) for b in registro.breaches}
    if abiertos != {("EFECTIVO", "MAXIMO")}:  # todo en efectivo: por encima del 30 %
        raise CheckFailure(f"la auditoría no cuadra: {sorted(abiertos)}")
    return (
        "fronteras del drawdown exactas (2,99 / 3 / 7,99 / 8 / 19,99 / 20 %); un ingreso no "
        "mueve el valor por participación; foto del día e incumplimientos guardados"
    )


def configure_keyring() -> str:
    """Fija el backend de Windows en código: dentro del exe no se descubre solo (GUIA §3)."""
    return secret_store.configure_backend()


def _check_keyring() -> str:
    backend = configure_keyring()
    valor = secrets.token_hex(8)  # nunca se registra
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, valor)
    try:
        leido = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    finally:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    if leido != valor:
        raise CheckFailure("el Administrador de credenciales no devuelve lo que se guardó")
    if keyring.get_password(KEYRING_SERVICE, KEYRING_USER) is not None:
        raise CheckFailure("la credencial de prueba no se ha borrado")
    return f"backend {backend}: guardar, leer y borrar correctos"


def _check_libraries() -> str:
    import anthropic
    import pyqtgraph
    import yfinance

    return (
        f"yfinance {yfinance.__version__}, anthropic {anthropic.__version__}, "
        f"pyqtgraph {pyqtgraph.__version__}"
    )


def _check_resources() -> str:
    from PySide6.QtGui import QImageReader

    carpeta = paths.resources_dir()
    if not carpeta.is_dir():
        raise CheckFailure(f"no está la carpeta de recursos: {carpeta}")
    icono = paths.icon_path()
    if not icono.is_file():
        raise CheckFailure(f"no está el icono: {icono}")
    lector = QImageReader(str(icono))
    if not lector.canRead():
        raise CheckFailure(f"Qt no sabe leer el icono: {icono}")
    tamano = lector.size()
    return f"{carpeta} · icono de {tamano.width()}×{tamano.height()} legible"


def _check_quote() -> str:
    """Una cotización real y su tipo de cambio, por el mismo camino que «Actualizar precios»
    (lotes, divisa real, par XXXEUR=X). La caché de yfinance va a una carpeta temporal."""
    from sharky.core.formatting import format_price
    from sharky.core.valuation import fx_currency
    from sharky.services.market import YahooMarket, configure_yfinance

    with tempfile.TemporaryDirectory(
        prefix="sharky_selftest_yf_", ignore_cleanup_errors=True
    ) as carpeta:
        try:
            mercado = YahooMarket(cache_dir=Path(carpeta), sleep=lambda _s: None)
            precios = mercado.fetch_quotes([ONLINE_TICKER])
            cita = precios.quotes.get(ONLINE_TICKER)
            if cita is None:
                fallo = precios.failures.get(ONLINE_TICKER)
                motivo = fallo.message if fallo else "sin respuesta"
                raise CheckWarning(f"Yahoo no ha dado {ONLINE_TICKER}: {motivo}")
            base = fx_currency(cita.currency)
            cambio = mercado.fetch_fx([base]).rates.get(base) if base else None
            if base and cambio is None:
                raise CheckWarning(f"Yahoo no ha dado el cambio {base}→EUR")
        finally:
            configure_yfinance()  # suelta la carpeta temporal
    texto = (
        f"{ONLINE_TICKER}: {format_price(cita.price)} {cita.currency}, cierre del "
        f"{cita.close_date:%d/%m/%Y}"
    )
    if cambio is not None:
        texto += f"; {base}→EUR {format_price(cambio.rate_to_eur)}"
    return texto + " (no se guarda nada)"


def _check_anthropic_tls() -> str:
    contexto = ssl.create_default_context()
    try:
        with socket.create_connection(
            (ANTHROPIC_HOST, ANTHROPIC_PORT), timeout=NETWORK_TIMEOUT
        ) as crudo:
            with contexto.wrap_socket(crudo, server_hostname=ANTHROPIC_HOST) as seguro:
                protocolo = seguro.version()
                certificado = seguro.getpeercert()
    except ssl.SSLCertVerificationError as error:
        raise CheckFailure(
            f"no se puede verificar el certificado de {ANTHROPIC_HOST}: faltan los "
            f"certificados dentro del programa ({error})"
        ) from error
    except OSError as error:
        raise CheckWarning(
            f"no se ha podido conectar con {ANTHROPIC_HOST}: {type(error).__name__}: {error}"
        ) from error
    if not certificado:
        raise CheckFailure(f"{ANTHROPIC_HOST} no ha presentado certificado")
    return f"{ANTHROPIC_HOST}: {protocolo}, certificado verificado (no se envía ninguna clave)"


def build_checks(online: bool = False) -> list[Check]:
    """Las comprobaciones, en orden. Qt va primero porque las demás se apoyan en él."""
    comprobaciones = [
        Check("Carpeta de datos", _check_data_dir),
        Check("Qt (sin ventanas)", _check_qt),
        Check("Base de datos", _check_database),
        Check("Ajustes", _check_settings),
        Check("Plantilla CSV y cartera inicial", _check_csv_template),
        Check("Precios y valoración", _check_market),
        Check("Mandato y foto del NAV", _check_mandate),
        Check("Administrador de credenciales", _check_keyring),
        Check("Bibliotecas", _check_libraries),
        Check("Recursos del paquete", _check_resources),
    ]
    if online:
        comprobaciones += [
            Check("Cotización real", _check_quote, network=True),
            Check("TLS con la API de Claude", _check_anthropic_tls, network=True),
        ]
    return comprobaciones


# -- ejecución --------------------------------------------------------------------------


def _execute(check: Check) -> CheckResult:
    try:
        detalle = check.run()
    except CheckWarning as error:
        return CheckResult(check.name, Status.WARNING, str(error))
    except CheckFailure as error:
        return CheckResult(check.name, Status.FAILURE, str(error))
    except ImportError as error:
        return CheckResult(check.name, Status.FAILURE, f"falta en el programa: {error}")
    except Exception as error:
        estado = Status.WARNING if check.network else Status.FAILURE
        return CheckResult(check.name, estado, f"{type(error).__name__}: {error}")
    return CheckResult(check.name, Status.OK, detalle)


def run_selftest(
    *, online: bool = False, checks: Sequence[Check] | None = None
) -> SelftestReport:
    """Ejecuta todas las comprobaciones. Nunca lanza: los errores van en el informe."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    resultados = [_execute(c) for c in (checks if checks is not None else build_checks(online))]
    for r in resultados:
        log.info("Autocomprobación [%s] %s: %s", r.status.value, r.name, r.detail)
    return SelftestReport(results=resultados, online=online)


def report_path() -> Path:
    """`%TEMP%\\sharky_selftest.txt`."""
    return Path(tempfile.gettempdir()) / REPORT_FILE


def write_report(report: SelftestReport, now: datetime | None = None) -> Path:
    """Escribe el informe y devuelve su ruta."""
    ruta = report_path()
    ruta.write_text(report.to_text(now), encoding="utf-8")
    return ruta


def run_selftest_cli(online: bool = False, checks: Sequence[Check] | None = None) -> int:
    """Lo que ejecuta `--selftest`: comprueba, escribe el informe y devuelve 0 o 1.

    `checks` deja que quien llama añada las suyas (app.py añade las de la interfaz, que
    services no puede comprobar sin romper las capas).
    """
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    informe = run_selftest(online=online, checks=checks)
    try:
        ruta = write_report(informe)
        log.info("Informe de la autocomprobación en %s", ruta)
    except OSError:
        log.exception("No se ha podido escribir el informe de la autocomprobación")
        return 1
    return informe.exit_code
