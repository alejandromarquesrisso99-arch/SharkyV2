"""Arranque de Sharky: argumentos, instancia única, registro, errores y ventana (GUIA §7, H1).

Lo primero de todo es `ensure_std_streams()`: en una aplicación sin consola `sys.stdout` y
`sys.stderr` valen None y cualquier escritura revienta. El empaquetado
(`packaging/launcher.py`) hace lo mismo antes incluso de importar este módulo.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

from sharky import __version__, paths

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication

    from sharky.services.db import Database
    from sharky.services.settings import Settings, SettingsStore
    from sharky.ui.theme import ThemeController

log = logging.getLogger(__name__)

LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 5
LOG_FORMAT = "%(asctime)s  %(levelname)-8s %(name)s: %(message)s"
#: Al salir, cuánto se espera a que termine lo que va en segundo plano (una descarga cancelada
#: acaba en cuanto vuelve la petición en curso).
SHUTDOWN_WAIT_MS = 20_000


def ensure_std_streams() -> None:
    """Sin consola, `sys.stdout` y `sys.stderr` son None: se redirigen a `os.devnull`."""
    nulo = None
    for nombre in ("stdout", "stderr", "__stdout__", "__stderr__"):
        if getattr(sys, nombre, None) is None:
            if nulo is None:
                nulo = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
            setattr(sys, nombre, nulo)


def setup_logging(level: int = logging.INFO) -> Path:
    """Registro rotativo en la carpeta de datos (1 MB × 5). Devuelve el fichero."""
    paths.ensure_data_dirs()
    destino = paths.log_path()
    raiz = logging.getLogger()
    for manejador in list(raiz.handlers):
        if getattr(manejador, "sharky_handler", False):
            raiz.removeHandler(manejador)
            manejador.close()
    manejador = RotatingFileHandler(
        destino,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
        delay=True,
    )
    manejador.sharky_handler = True
    manejador.setFormatter(logging.Formatter(LOG_FORMAT))
    raiz.addHandler(manejador)
    raiz.setLevel(level)
    return destino


def install_excepthook() -> None:
    """Un error no controlado se registra y se enseña en un diálogo, nunca en una consola."""
    anterior = sys.excepthook

    def hook(
        tipo: type[BaseException],
        valor: BaseException,
        traza: TracebackType | None,
    ) -> None:
        if issubclass(tipo, KeyboardInterrupt):
            anterior(tipo, valor, traza)
            return
        log.critical("Error no controlado", exc_info=(tipo, valor, traza))
        show_error_dialog(valor)

    sys.excepthook = hook


def show_error_dialog(error: BaseException) -> None:
    """Diálogo de error. Si todavía no hay interfaz, se queda solo en el registro."""
    from PySide6.QtWidgets import QApplication, QMessageBox

    if QApplication.instance() is None:
        return
    try:
        QMessageBox.critical(
            None,
            "Sharky",
            "Sharky ha encontrado un error inesperado y puede que algo no funcione.\n\n"
            f"{type(error).__name__}: {error}\n\n"
            f"Queda anotado en {paths.log_path()}",
        )
    except Exception:  # pragma: no cover - si ni el diálogo se puede mostrar
        log.exception("No se ha podido mostrar el diálogo de error")


def instance_key() -> str:
    """Nombre del servidor local de la instancia única, distinto para cada usuario."""
    usuario = os.environ.get("USERNAME") or os.environ.get("USER") or "usuario"
    return f"sharky-{usuario}"


class SingleInstance:
    """Una sola ventana: la segunda ejecución avisa a la primera y se va (GUIA §5.9)."""

    MESSAGE = b"mostrar"

    def __init__(self, key: str) -> None:
        self.key = key
        self._server: object | None = None

    def signal_running_instance(self) -> bool:
        """True si ya hay otra instancia (y se le ha pedido que se muestre)."""
        from PySide6.QtNetwork import QLocalSocket

        socket = QLocalSocket()
        socket.connectToServer(self.key)
        if not socket.waitForConnected(500):
            return False
        socket.write(self.MESSAGE)
        socket.flush()
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        return True

    def listen(self, on_show: Callable[[], None]) -> bool:
        """Se queda escuchando. Cada aviso que llegue llama a `on_show`."""
        from PySide6.QtNetwork import QLocalServer

        QLocalServer.removeServer(self.key)  # restos de un cierre anterior brusco
        servidor = QLocalServer()
        if not servidor.listen(self.key):
            log.warning("No se ha podido abrir el servidor local: %s", servidor.errorString())
            return False

        def al_conectar() -> None:
            conexion = servidor.nextPendingConnection()
            if conexion is None:
                return
            conexion.readyRead.connect(conexion.readAll)
            conexion.disconnected.connect(conexion.deleteLater)
            log.info("Otra ejecución de Sharky pide la ventana que ya existe")
            on_show()

        servidor.newConnection.connect(al_conectar)
        self._server = servidor
        return True

    def close(self) -> None:
        """Deja de escuchar: otra ejecución ya puede quedarse con el puesto."""
        if self._server is not None:
            self._server.close()
            self._server = None


def restart_command() -> tuple[str, list[str]]:
    """Programa y argumentos para volver a abrir Sharky (en el exe y en desarrollo)."""
    if getattr(sys, "frozen", False):
        return sys.executable, []
    return sys.executable, ["-m", "sharky.app"]


def relaunch() -> bool:
    """Abre otra instancia de Sharky, independiente de esta. True si se ha lanzado."""
    from PySide6.QtCore import QProcess

    programa, argumentos = restart_command()
    lanzado = QProcess.startDetached(programa, argumentos)
    # startDetached devuelve un bool o (bool, pid) según la versión de PySide6.
    correcto = lanzado[0] if isinstance(lanzado, tuple) else bool(lanzado)
    if correcto:
        log.info("Sharky se reinicia")
    else:
        log.error("No se ha podido reiniciar Sharky (%s %s)", programa, argumentos)
    return correcto


def remember_theme(
    controller: ThemeController, store: SettingsStore, settings: Settings
) -> None:
    """El tema elegido se guarda en settings.json en cuanto cambia (GUIA §5.10: se recuerda)."""

    def guardar(*_args: object) -> None:
        eleccion = str(controller.choice)
        if settings.appearance.theme == eleccion:
            return  # cambió Windows, no la elección
        settings.appearance.theme = eleccion
        try:
            store.save(settings)
        except OSError:
            log.exception("No se ha podido guardar el tema elegido")

    controller.themeChanged.connect(guardar)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Argumentos de la línea de órdenes (el usuario normal no los usa nunca)."""
    parser = argparse.ArgumentParser(
        prog="Sharky",
        description="Sharky: vigilancia de una cartera real de inversión en euros.",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="comprueba el programa sin abrir ninguna ventana y sale con 0 o 1",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="con --selftest, añade una cotización real y una conexión con la API de Claude",
    )
    parser.add_argument("--version", action="version", version=f"Sharky {__version__}")
    return parser.parse_args(list(argv) if argv is not None else None)


def _check_ui() -> str:
    """Comprobación de la interfaz para el `--selftest` (app.py sí puede mirar la capa ui)."""
    from PySide6.QtWidgets import QApplication

    from sharky.services.db import Database
    from sharky.services.market import YahooMarket
    from sharky.services.selftest import CheckFailure
    from sharky.ui.main_window import MainWindow
    from sharky.ui.pages import SECTIONS
    from sharky.ui.portfolio import PortfolioPage
    from sharky.ui.theme import Theme, ThemeController

    def sin_red(_symbol: str) -> object:
        raise ConnectionError("autocomprobación: sin red a propósito")

    app = QApplication.instance() or QApplication([])
    tema = ThemeController(app, Theme.LIGHT)
    with tempfile.TemporaryDirectory(
        prefix="sharky_selftest_ui_", ignore_cleanup_errors=True
    ) as carpeta:
        db = Database(Path(carpeta) / "sharky.db")
        try:
            db.migrate()
            mercado = YahooMarket(ticker_factory=sin_red, search_factory=sin_red)
            ventana = MainWindow(tema, __version__, db=db, market=mercado, fx=mercado)
            for seccion in SECTIONS:
                ventana.show_section(seccion.key)
            cartera = ventana.page("cartera")
            if not isinstance(cartera, PortfolioPage):
                raise CheckFailure("la sección Cartera no se ha construido")
            cartera.reload()
            tema.set_theme(Theme.DARK)
            cartera.grab()
            ventana.close()
            ventana.deleteLater()
            _walk_setup_wizard(db, Path(carpeta))
        finally:
            db.close_all()
    return (
        f"ventana creada, {len(SECTIONS)} secciones recorridas (Cartera y Ajustes con Datos), "
        "temas claro y oscuro; asistente recorrido con la plantilla CSV"
    )


def _walk_setup_wizard(db: Database, folder: Path) -> None:
    """Recorre las tres páginas del asistente con la plantilla, sin red y sin guardar nada."""
    from datetime import date

    from sharky.core.csv_import import TEMPLATE_CSV
    from sharky.services.ai import KeyCheck, KeyStatus
    from sharky.services.selftest import CheckFailure
    from sharky.services.settings import Settings, SettingsStore
    from sharky.ui.wizard import SetupWizard

    asistente = SetupWizard(
        db,
        SettingsStore(folder / "settings.json"),
        Settings(),
        key_checker=lambda _clave: KeyCheck(KeyStatus.VALID, "Clave válida."),
        has_saved_key=False,
        today=lambda: date(2026, 1, 2),
    )
    try:
        asistente.restart()
        asistente.next()
        asistente.portfolio_page.load_csv_bytes(TEMPLATE_CSV.encode("utf-8-sig"), "plantilla")
        asistente.portfolio_page.cash_edit.setText("1000")
        if not asistente.portfolio_page.isComplete():
            raise CheckFailure("el asistente no acepta la plantilla CSV")
        asistente.next()
        if not asistente.summary_page.isComplete():
            raise CheckFailure("el resumen del asistente no queda listo")
    finally:
        asistente.deleteLater()  # nunca se ha enseñado: no hay nada que cerrar


def run_selftest(online: bool) -> int:
    """`--selftest`: las comprobaciones de services más la de la interfaz."""
    from sharky.services.selftest import Check, build_checks, run_selftest_cli

    comprobaciones = [*build_checks(online), Check("Interfaz", _check_ui)]
    return run_selftest_cli(online=online, checks=comprobaciones)


def run_setup_wizard(
    app: QApplication,
    db: Database,
    store: SettingsStore,
    settings: Settings,
    to_front: dict[str, Callable[[], None]],
) -> bool:
    """No hay cartera: el asistente de primer arranque (GUIA §5.2). True si se ha creado."""
    from PySide6.QtWidgets import QDialog

    from sharky.ui.wizard import SetupWizard

    log.info("No hay cartera: se abre el asistente de primer arranque")
    asistente = SetupWizard(db, store, settings)
    to_front["mostrar"] = asistente.bring_to_front
    # Al cerrarse el asistente todavía no hay ventana principal: que Qt no dé la app por
    # terminada por quedarse sin ventanas.
    anterior = app.quitOnLastWindowClosed()
    app.setQuitOnLastWindowClosed(False)
    try:
        creada = asistente.exec() == QDialog.DialogCode.Accepted
    finally:
        app.setQuitOnLastWindowClosed(anterior)
        to_front.pop("mostrar", None)
        asistente.deleteLater()
    if creada:
        log.info("Cartera creada con el asistente: se abre la ventana principal")
    return creada


def run_gui() -> int:
    """Abre la ventana. Devuelve el código de salida del programa."""
    from PySide6.QtCore import QThreadPool
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from sharky.services import secrets
    from sharky.services.db import Database, DatabaseError
    from sharky.services.market import YahooMarket, local_now
    from sharky.services.repositories import has_portfolio
    from sharky.services.settings import SettingsStore
    from sharky.ui.main_window import MainWindow
    from sharky.ui.theme import Theme, ThemeController
    from sharky.ui.tray import create_tray

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Sharky")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(str(paths.icon_path())))
    install_excepthook()

    unica = SingleInstance(instance_key())
    if unica.signal_running_instance():
        log.info("Ya hay una ventana de Sharky abierta: se le pide que salga al frente")
        return 0

    try:
        secrets.configure_backend()
    except Exception:  # sin Administrador de credenciales la app sigue, sin IA
        log.exception("No se ha podido preparar el Administrador de credenciales")

    almacen = SettingsStore()
    ajustes = almacen.load()

    db = Database(paths.db_path())
    try:
        aplicadas = db.migrate()
    except DatabaseError as error:
        log.exception("No se puede usar la base de datos")
        show_error_dialog(error)
        db.close_all()
        return 1
    log.info("Base de datos lista (esquema %d, %d migraciones aplicadas ahora)",
             db.user_version(), aplicadas)

    tema = ThemeController(app, Theme(ajustes.appearance.theme))
    # La instancia única trae al frente la ventana que haya: el asistente o la principal.
    al_frente: dict[str, Callable[[], None]] = {}
    unica.listen(lambda: al_frente["mostrar"]() if "mostrar" in al_frente else None)

    if not has_portfolio(db.connection()):
        if not run_setup_wizard(app, db, almacen, ajustes, al_frente):
            db.close_all()
            unica.close()
            return 0
        ajustes = almacen.load()  # el asistente ha guardado el bróker y el inicio con Windows

    remember_theme(tema, almacen, ajustes)
    mercado = YahooMarket()  # yfinance se carga la primera vez que se usa, no ahora
    ventana = MainWindow(
        tema, __version__, db=db, market=mercado, fx=mercado, settings=ajustes, now=local_now
    )
    al_frente["mostrar"] = ventana.bring_to_front
    bandeja = create_tray(ventana, ventana.bring_to_front, app.quit)

    reinicio = {"pedido": False}

    def pedir_reinicio() -> None:
        reinicio["pedido"] = True
        app.quit()

    ventana.restartRequested.connect(pedir_reinicio)
    ventana.show()
    log.info("Ventana abierta (tema %s, bandeja %s)", tema.effective, "sí" if bandeja else "no")
    codigo = app.exec()

    ventana.shutdown()
    # Que ningún trabajo en segundo plano siga usando la base de datos al cerrarla.
    if not QThreadPool.globalInstance().waitForDone(SHUTDOWN_WAIT_MS):
        log.warning("Quedan trabajos en segundo plano al salir")
    db.close_all()
    if reinicio["pedido"]:
        unica.close()  # si no, la instancia nueva creería que esta sigue abierta
        relaunch()
    return codigo


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada del programa."""
    ensure_std_streams()
    args = parse_args(argv)
    if args.selftest:
        setup_logging()
        return run_selftest(online=args.online)
    setup_logging()
    log.info("Sharky %s arranca · carpeta de datos: %s", __version__, paths.data_dir())
    try:
        return run_gui()
    except Exception as error:
        log.exception("Sharky no ha podido arrancar")
        show_error_dialog(error)
        return 1


if __name__ == "__main__":  # pragma: no cover - solo en desarrollo
    sys.exit(main())
