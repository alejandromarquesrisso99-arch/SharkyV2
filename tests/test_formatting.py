"""Cifras en formato español."""

from decimal import Decimal as D

import pytest

from sharky.core.formatting import (
    format_amount,
    format_eur,
    format_price,
    format_units,
    parse_decimal,
)


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


@pytest.mark.parametrize(
    ("valor", "texto"),
    [("2900", "2.900,00 €"), ("1234.5", "1.234,50 €"), ("0.005", "0,01 €"), ("-0.001", "0,00 €")],
)
def test_euros(valor, texto):
    assert format_eur(D(valor)) == texto


@pytest.mark.parametrize(
    ("valor", "texto"),
    [("4.5", "4,50"), ("150", "150,00"), ("162.3333", "162,3333"), ("0.1234567", "0,123457"),
     ("1234.5", "1.234,50")],
)
def test_precios_de_2_a_6_decimales(valor, texto):
    assert format_price(D(valor)) == texto


def test_importe_sin_simbolo():
    assert format_amount(D("2900")) == "2.900,00"


@pytest.mark.parametrize(
    ("texto", "valor"),
    [
        ("10", "10"),
        ("4,5", "4.5"),
        ("4.5", "4.5"),
        (",5", "0.5"),
        ("1.234,56", "1234.56"),
        ("1,234.56", "1234.56"),
        ("1.234.567,8", "1234567.8"),
        ("2.900", "2.9"),  # un solo signo: es el decimal
        (" 2 900,00 ", "2900.00"),
        ("2 900,5", "2900.5"),  # espacio no separable de Excel
        ("-3", "-3"),
        ("+7,25", "7.25"),
    ],
)
def test_numeros_con_coma_o_punto(texto, valor):
    assert parse_decimal(texto) == D(valor)


@pytest.mark.parametrize(
    "texto", ["", "  ", "abc", "1.2.3", "1,2,3", "12,34.56", "1.23,4.5", "1e5", "NaN", "--1", "€5"]
)
def test_lo_que_no_es_un_numero(texto):
    with pytest.raises(ValueError):
        parse_decimal(texto)
