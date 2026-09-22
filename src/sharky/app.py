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
from collections.abc import Callable, Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

from sharky import __version__, paths

log = logging.getLogger(__name__)

LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 5
LOG_FORMAT = "%(asctime)s  %(levelname)-8s %(name)s: %(message)s"


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

    from sharky.ui.main_window import MainWindow
    from sharky.ui.pages import SECTIONS
    from sharky.ui.theme import Theme, ThemeController

    app = QApplication.instance() or QApplication([])
    tema = ThemeController(app, Theme.LIGHT)
    ventana = MainWindow(tema, __version__)
    for seccion in SECTIONS:
        ventana.show_section(seccion.key)
    tema.set_theme(Theme.DARK)
    ventana.close()
    ventana.deleteLater()
    return f"ventana creada, {len(SECTIONS)} secciones recorridas, temas claro y oscuro"


def run_selftest(online: bool) -> int:
    """`--selftest`: las comprobaciones de services más la de la interfaz."""
    from sharky.services.selftest import Check, build_checks, run_selftest_cli

    comprobaciones = [*build_checks(online), Check("Interfaz", _check_ui)]
    return run_selftest_cli(online=online, checks=comprobaciones)


def run_gui() -> int:
    """Abre la ventana. Devuelve el código de salida del programa."""
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

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

    tema = ThemeController(app, Theme.SYSTEM)
    ventana = MainWindow(tema, __version__)
    unica.listen(ventana.bring_to_front)
    bandeja = create_tray(ventana, ventana.bring_to_front, app.quit)
    ventana.show()
    log.info("Ventana abierta (tema %s, bandeja %s)", tema.effective, "sí" if bandeja else "no")
    return app.exec()


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
