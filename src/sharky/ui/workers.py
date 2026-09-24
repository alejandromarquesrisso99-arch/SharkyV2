"""Trabajo en segundo plano con QThreadPool (CLAUDE.md: red, IA y disco lento, fuera del hilo
de la interfaz).

La función corre en un hilo del pool; el resultado vuelve por señales. Las señales viven en un
QObject creado en el hilo de la interfaz, así que lo que se conecte a ellas se ejecuta en ese
hilo y puede tocar widgets. Conecta siempre a métodos de un QObject (un widget), no a lambdas
sueltas.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

log = logging.getLogger(__name__)


class WorkerSignals(QObject):
    #: El resultado de la función.
    finished = Signal(object)
    #: El error, con un mensaje que se puede enseñar.
    failed = Signal(str)
    #: Avance: hechos y total (solo si se ha pedido con `pass_progress`).
    progress = Signal(int, int)


class Worker(QRunnable):
    """Ejecuta `fn(*args, **kwargs)` en un hilo del pool."""

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def pass_progress(self, keyword: str = "progress") -> Worker:
        """La función recibirá en `keyword` una función `(hechos, total)` que emite
        `signals.progress`; la señal llega al hilo de la interfaz."""
        self.kwargs[keyword] = self.signals.progress.emit
        return self

    def run(self) -> None:
        try:
            resultado = self.fn(*self.args, **self.kwargs)
        except Exception as error:
            log.exception("Ha fallado un trabajo en segundo plano (%s)", _name(self.fn))
            self.signals.failed.emit(str(error) or type(error).__name__)
        else:
            self.signals.finished.emit(resultado)


def _name(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__qualname__", None) or repr(fn)


def start(worker: Worker, pool: QThreadPool | None = None) -> Worker:
    """Lanza el trabajo y lo devuelve (para conectar sus señales antes, créalo aparte)."""
    (pool or QThreadPool.globalInstance()).start(worker)
    return worker
