"""Genera `src/sharky/resources/sharky.ico` (el icono del programa y del instalador).

Se ejecuta a mano cuando cambie el dibujo:

    uv run python scripts/make_icon.py

El icono se dibuja con los mismos tokens que el tema (`ui/theme.py`), así que aquí tampoco
se escribe ningún color a mano. Sale un .ico con varios tamaños, cada uno guardado como PNG
dentro del contenedor, que es lo que quieren Windows y PyInstaller.
"""

from __future__ import annotations

import logging
import os
import struct
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QPointF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
)

from sharky.ui.theme import LIGHT_TOKENS  # noqa: E402

log = logging.getLogger("make_icon")

DESTINO = RAIZ / "src" / "sharky" / "resources" / "sharky.ico"
TAMANOS = (16, 24, 32, 48, 64, 128, 256)
LIENZO = 256


def dibujar(lado: int) -> QImage:
    """El dibujo: fondo con el acento y una aleta clara sobre una ola."""
    imagen = QImage(lado, lado, QImage.Format.Format_ARGB32)
    imagen.fill(QColor(0, 0, 0, 0))
    pintor = QPainter(imagen)
    pintor.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    fondo = QColor(LIGHT_TOKENS["accent"])
    tinta = QColor(LIGHT_TOKENS["accent_text"])

    pintor.setPen(Qt.PenStyle.NoPen)
    pintor.setBrush(fondo)
    pintor.drawRoundedRect(0, 0, lado, lado, lado * 0.22, lado * 0.22)

    def p(x: float, y: float) -> QPointF:
        return QPointF(x * lado, y * lado)

    aleta = QPainterPath(p(0.63, 0.13))  # punta
    aleta.cubicTo(p(0.47, 0.30), p(0.34, 0.46), p(0.20, 0.62))  # borde de ataque, convexo
    aleta.lineTo(p(0.76, 0.62))  # base
    aleta.cubicTo(p(0.67, 0.50), p(0.64, 0.33), p(0.63, 0.13))  # borde de salida, cóncavo
    aleta.closeSubpath()
    pintor.setBrush(tinta)
    pintor.drawPath(aleta)

    ola = QPainterPath(p(0.16, 0.79))
    ola.cubicTo(p(0.33, 0.70), p(0.45, 0.88), p(0.62, 0.79))
    ola.cubicTo(p(0.72, 0.735), p(0.78, 0.755), p(0.84, 0.79))
    lapiz = QPen(tinta, lado * 0.075)
    lapiz.setCapStyle(Qt.PenCapStyle.RoundCap)
    lapiz.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pintor.setPen(lapiz)
    pintor.setBrush(Qt.BrushStyle.NoBrush)
    pintor.drawPath(ola)

    pintor.end()
    return imagen


def a_png(imagen: QImage) -> bytes:
    """Los bytes PNG de una imagen."""
    datos = QByteArray()
    bufer = QBuffer(datos)
    bufer.open(QBuffer.OpenModeFlag.WriteOnly)
    if not imagen.save(bufer, "PNG"):
        raise RuntimeError("Qt no ha podido guardar el PNG")
    bufer.close()
    return bytes(datos)


def construir_ico(marcos: list[tuple[int, bytes]]) -> bytes:
    """Mete los PNG en un contenedor .ico."""
    cabecera = struct.pack("<HHH", 0, 1, len(marcos))
    entradas = b""
    cuerpo = b""
    desplazamiento = 6 + 16 * len(marcos)
    for lado, datos in marcos:
        medida = 0 if lado >= 256 else lado  # 0 significa 256 en el formato .ico
        entradas += struct.pack(
            "<BBBBHHII", medida, medida, 0, 0, 1, 32, len(datos), desplazamiento
        )
        cuerpo += datos
        desplazamiento += len(datos)
    return cabecera + entradas + cuerpo


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    app = QGuiApplication.instance() or QGuiApplication([])
    grande = dibujar(LIENZO)
    marcos = []
    for lado in TAMANOS:
        imagen = (
            grande
            if lado == LIENZO
            else grande.scaled(
                lado,
                lado,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        marcos.append((lado, a_png(imagen)))
    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    DESTINO.write_bytes(construir_ico(marcos))
    log.info(
        "Icono escrito en %s · %d tamaños · %d bytes",
        DESTINO,
        len(marcos),
        DESTINO.stat().st_size,
    )
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
