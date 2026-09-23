"""Cifras en formato español."""

from decimal import Decimal as D

import pytest

from sharky.core.formatting import format_units


@pytest.mark.parametrize(
    ("valor", "texto"),
    [
        ("10", "10"),
        ("1234.5", "1.234,5"),
        ("0.123456", "0,123456"),
        ("0.1234567", "0,123457"),  # hasta 6 decimales
        ("1234567.000001", "1.234.567,000001"),
        ("-12.50", "-12,5"),
        ("0", "0"),
        ("-0.0000001", "0"),  # nada de «-0»
    ],
)
def test_unidades(valor, texto):
    assert format_units(D(valor)) == texto
