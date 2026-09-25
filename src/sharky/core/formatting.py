"""Cifras en formato español: «1.234,56 €», «12,3 %» y unidades con hasta 6 decimales.

Y al revés: los campos numéricos aceptan coma o punto como separador decimal.
"""

from __future__ import annotations

import re
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

UNITS_DECIMALS = 6
PRICE_MIN_DECIMALS = 2
PRICE_MAX_DECIMALS = 6

_DIGITS = re.compile(r"^[+-]?(\d+([.,]\d*)?|[.,]\d+)$")
# Con separador de miles: «1.234.567,89». Las llaves dobles son para str.format.
_GROUPED = r"^[+-]?\d{{1,3}}(?:{sep}\d{{3}})+(?:{dec}\d*)?$"


def _spanish(
    value: Decimal,
    decimals: int,
    trim: bool,
    min_decimals: int = 0,
    rounding: str = ROUND_HALF_UP,
) -> str:
    redondeado = value.quantize(Decimal(1).scaleb(-decimals), rounding=rounding)
    if redondeado == 0:
        redondeado = abs(redondeado)  # nada de «-0,00»
    texto = f"{redondeado:,.{decimals}f}"  # formato inglés: 1,234.50
    entero, _, fraccion = texto.partition(".")
    if trim:
        fraccion = fraccion.rstrip("0")
        fraccion = fraccion.ljust(min_decimals, "0")
    entero = entero.replace(",", ".")
    return f"{entero},{fraccion}" if fraccion else entero


def format_units(value: Decimal) -> str:
    """Unidades: hasta 6 decimales, sin ceros de relleno. `Decimal("1234.5")` → «1.234,5»."""
    return _spanish(value, UNITS_DECIMALS, trim=True)


def format_amount(value: Decimal, decimals: int = 2) -> str:
    """Importe sin símbolo, con dos decimales: `Decimal("2900")` → «2.900,00»."""
    return _spanish(value, decimals, trim=False)


def format_eur(value: Decimal, decimals: int = 2) -> str:
    """Importe en euros: `Decimal("1234.5")` → «1.234,50 €». Con `decimals=0`, en euros
    enteros, para las cifras aproximadas («vender unos 712 €»)."""
    return f"{format_amount(value, decimals)} €"


def format_number(value: Decimal, decimals: int = 2, *, truncate: bool = False) -> str:
    """Una cifra sin unidad, con sus decimales justos: `Decimal("2.5")` → «2,50». Con
    `truncate`, los decimales que sobran se cortan en vez de redondearse: 1,999 → «1,99»."""
    return _spanish(value, decimals, trim=False, rounding=ROUND_DOWN if truncate else ROUND_HALF_UP)


#: Por debajo de un céntimo, el coste de Claude no se redondea a «0,00 $»: se ve «< 0,01 $».
_CENT = Decimal("0.01")


def format_usd(value: Decimal) -> str:
    """Lo que cuesta Claude, en dólares: `Decimal("0.0312")` → «0,03 $». Un coste que no llega
    al céntimo se ve «< 0,01 $», nunca «0,00 $» (no es gratis)."""
    if 0 < value < _CENT / 2:
        return f"< {format_amount(_CENT)} $"
    return f"{format_amount(value)} $"


def format_usd_range(low: Decimal, high: Decimal) -> str:
    """Una horquilla de coste: «0,02–0,05 $»."""
    return f"{format_amount(low)}–{format_amount(high)} $"


MONTH_NAMES: tuple[str, ...] = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
    "octubre", "noviembre", "diciembre",
)


def month_name(month: int) -> str:
    """El nombre del mes en español: 9 → «septiembre»."""
    return MONTH_NAMES[month - 1]


def format_limit_pct(fraction: Decimal) -> str:
    """Un límite del mandato, sin ceros de relleno: 0.10 → «10 %», 0.015 → «1,5 %»."""
    return f"{_spanish(fraction * 100, 2, trim=True)} %"


def pretty_sector(sector: str) -> str:
    """«Renta_Variable_Global» → «Renta Variable Global»."""
    return sector.replace("_", " ")


def format_price(value: Decimal) -> str:
    """Precio por unidad: al menos 2 decimales y hasta 6. `Decimal("4.5")` → «4,50»,
    `Decimal("0.123456")` → «0,123456»."""
    return _spanish(value, PRICE_MAX_DECIMALS, trim=True, min_decimals=PRICE_MIN_DECIMALS)


#: Signo menos tipográfico, para las cifras que llevan signo siempre («+3,2 %», «−4,1 %»).
MINUS = "−"


def _signed(value: Decimal, decimals: int) -> tuple[str, str]:
    """El signo («+», «−» o nada si redondea a cero) y la cifra sin él."""
    redondeado = value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    signo = "+" if redondeado > 0 else MINUS if redondeado < 0 else ""
    return signo, _spanish(abs(redondeado), decimals, trim=False)


def format_pct(
    fraction: Decimal, decimals: int = 1, *, signed: bool = False, truncate: bool = False
) -> str:
    """Una proporción como porcentaje: `Decimal("0.123")` → «12,3 %». Con `signed`, el signo
    va siempre delante: «+32,7 %», «−4,1 %» (y «0,0 %» si redondea a cero).

    Con `truncate`, los decimales que sobran se cortan: un drawdown de 2,97 % se ve «2,9 %»,
    nunca «3,0 %» mientras no llegue al 3 %."""
    porcentaje = fraction * 100
    if truncate:
        return f"{_spanish(porcentaje, decimals, trim=False, rounding=ROUND_DOWN)} %"
    if not signed:
        return f"{_spanish(porcentaje, decimals, trim=False)} %"
    signo, cifra = _signed(porcentaje, decimals)
    return f"{signo}{cifra} %"


def format_signed_amount(value: Decimal) -> str:
    """Importe con dos decimales y el signo siempre delante: «+1.234,56», «−12,00»."""
    signo, cifra = _signed(value, 2)
    return f"{signo}{cifra}"


def parse_decimal(text: str) -> Decimal:
    """Un número escrito a mano o leído de un CSV, con coma o punto decimal.

    - «1234,56», «1234.56», «0,5», «,5» y «10» valen lo que parece.
    - Si aparecen los dos signos, el último es el decimal y el otro separa miles, en grupos
      de tres: «1.234,56» y «1,234.56» son 1234,56.
    - Con un solo signo, ese signo es el decimal: «2.900» es 2,9 (no 2900).
    - Se ignoran los espacios (también el no separable que pone Excel).

    Lanza ValueError si el texto no es un número.
    """
    limpio = re.sub(r"\s", "", text.replace(" ", "").replace(" ", ""))
    if not limpio:
        raise ValueError("vacío")
    if "," in limpio and "." in limpio:
        decimal_sep = "," if limpio.rfind(",") > limpio.rfind(".") else "."
        miles = "." if decimal_sep == "," else ","
        patron = _GROUPED.format(sep=re.escape(miles), dec=re.escape(decimal_sep))
        if not re.match(patron, limpio):
            raise ValueError(f"«{text.strip()}» no es un número")
        limpio = limpio.replace(miles, "").replace(decimal_sep, ".")
    elif _DIGITS.match(limpio):
        limpio = limpio.replace(",", ".")
    else:
        raise ValueError(f"«{text.strip()}» no es un número")
    try:
        valor = Decimal(limpio)
    except InvalidOperation as error:  # pragma: no cover - los patrones ya lo impiden
        raise ValueError(f"«{text.strip()}» no es un número") from error
    if not valor.is_finite():  # pragma: no cover - los patrones ya lo impiden
        raise ValueError(f"«{text.strip()}» no es un número")
    return valor
