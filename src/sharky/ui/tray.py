"""Icono en la bandeja del sistema. Si el sistema no tiene bandeja, Sharky sigue sin ella."""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from sharky import paths

log = logging.getLogger(__name__)


def create_tray(
    parent: QWidget,
    on_show: Callable[[], None],
    on_quit: Callable[[], None],
) -> QSystemTrayIcon | None:
    """Crea el icono de la bandeja. Devuelve None si no hay bandeja disponible."""
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.info("No hay bandeja del sistema: Sharky funciona igual, pero sin icono")
        return None

    bandeja = QSystemTrayIcon(QIcon(str(paths.icon_path())), parent)
    bandeja.setToolTip("Sharky")

    menu = QMenu(parent)
    mostrar = menu.addAction("Mostrar Sharky")
    mostrar.triggered.connect(on_show)
    menu.addSeparator()
    salir = menu.addAction("Salir")
    salir.triggered.connect(on_quit)
    bandeja.setContextMenu(menu)

    def al_pulsar(razon: QSystemTrayIcon.ActivationReason) -> None:
        if razon in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            on_show()

    bandeja.activated.connect(al_pulsar)
    bandeja.show()
    log.info("Icono de la bandeja creado")
    return bandeja
