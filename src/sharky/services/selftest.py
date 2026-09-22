"""Autocomprobación del programa: `Sharky.exe --selftest` (GUIA §7, H1).

Comprueba que dentro del programa está todo lo que necesita para arrancar: Qt, SQLite, el
Administrador de credenciales, las bibliotecas de mercado e IA y los recursos del paquete.
Con `--online` añade una cotización real y una conexión TLS con la API de Claude.

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
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import keyring

from sharky import __version__, paths

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


def _check_sqlite() -> str:
    with tempfile.TemporaryDirectory(prefix="sharky_selftest_") as carpeta:
        ruta = Path(carpeta) / "prueba.db"
        conexion = sqlite3.connect(ruta, timeout=5)
        try:
            modo = conexion.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            conexion.execute("PRAGMA busy_timeout=5000")
            with conexion:
                conexion.execute("CREATE TABLE prueba (valor TEXT NOT NULL)")
                conexion.execute("INSERT INTO prueba VALUES (?)", ("sharky",))
            fila = conexion.execute("SELECT valor FROM prueba").fetchone()
        finally:
            conexion.close()
    if not fila or fila[0] != "sharky":
        raise CheckFailure("SQLite no ha devuelto lo que se acababa de escribir")
    return f"SQLite {sqlite3.sqlite_version}, modo {modo}, escritura y lectura correctas"


def configure_keyring() -> str:
    """Fija el backend de Windows en código: dentro del exe no se descubre solo (GUIA §3)."""
    from keyring.backends.Windows import WinVaultKeyring

    keyring.set_keyring(WinVaultKeyring())
    return type(keyring.get_keyring()).__name__


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
    import yfinance

    try:
        datos = yfinance.Ticker(ONLINE_TICKER).history(period="5d", auto_adjust=False)
    except Exception as error:  # la red o Yahoo, no el programa
        raise CheckWarning(f"Yahoo no ha respondido: {type(error).__name__}: {error}") from error
    if datos is None or datos.empty:
        raise CheckWarning("Yahoo no ha devuelto cotizaciones en este momento")
    cierre = float(datos["Close"].iloc[-1])
    return f"{ONLINE_TICKER}: último cierre {cierre:.2f} (no se guarda nada)"


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
        Check("SQLite", _check_sqlite),
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
