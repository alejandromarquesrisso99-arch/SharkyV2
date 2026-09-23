"""Cifras en formato español: «1.234,56 €», «12,3 %» y unidades con hasta 6 decimales."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

UNITS_DECIMALS = 6


def _spanish(value: Decimal, decimals: int, trim: bool) -> str:
    redondeado = value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    if redondeado == 0:
        redondeado = abs(redondeado)  # nada de «-0,00»
    texto = f"{redondeado:,.{decimals}f}"  # formato inglés: 1,234.50
    entero, _, fraccion = texto.partition(".")
    if trim:
        fraccion = fraccion.rstrip("0")
    entero = entero.replace(",", ".")
    return f"{entero},{fraccion}" if fraccion else entero


def format_units(value: Decimal) -> str:
    """Unidades: hasta 6 decimales, sin ceros de relleno. `Decimal("1234.5")` → «1.234,5»."""
    return _spanish(value, UNITS_DECIMALS, trim=True)
