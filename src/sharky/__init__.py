"""Sharky: vigilancia de una cartera real de inversión en euros."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sharky")
except PackageNotFoundError:  # pragma: no cover - solo si el paquete no está instalado
    __version__ = "0.0.0"

__all__ = ["__version__"]
