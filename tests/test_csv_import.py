"""El CSV de la cartera inicial y lo que se guarda al crearla (GUIA §5.2). Datos inventados."""

from datetime import date
from decimal import Decimal as D

import pytest

from sharky.core.csv_import import (
    TEMPLATE_CSV,
    CsvPosition,
    OpeningError,
    Severity,
    build_opening,
    normalize_currency,
    parse_positions_csv,
)
from sharky.core.ledger import build_ledger
from sharky.core.models import AssetClass, CashKind, MandateState, TradeKind

HOY = date(2026, 9, 24)
CABECERA = "ticker;nombre;isin;unidades;coste_medio_eur;divisa;sector;simbolo;clase"


def csv(*lineas: str, encoding: str = "utf-8") -> bytes:
    return ("\n".join(lineas) + "\n").encode(encoding)


def lineas_con_error(lectura) -> dict[int, list[str]]:
    errores: dict[int, list[str]] = {}
    for e in lectura.errors:
        errores.setdefault(e.line, []).append(e.message)
    return errores


# -- la plantilla -----------------------------------------------------------------------


def test_la_plantilla_se_lee_sin_errores_ni_avisos():
    lectura = parse_positions_csv(TEMPLATE_CSV.encode("utf-8-sig"))
    assert lectura.ok and not lectura.issues
    assert lectura.has_header and lectura.delimiter == ";"
    assert [p.ticker for p in lectura.positions] == ["AAPL", "SAN", "IWDA"]
    aapl, san, iwda = lectura.positions
    assert (aapl.units, aapl.avg_cost_eur, aapl.currency) == (D("10"), D("150.00"), "USD")
    assert aapl.isin == "US0378331005" and aapl.yahoo_symbol == "AAPL"
    assert (san.avg_cost_eur, san.yahoo_symbol, san.sector) == (D("4.50"), "SAN.MC", "Banca")
    assert iwda.asset_class is AssetClass.ETF
    assert iwda.sector == "Renta_Variable_Global"


def test_la_plantilla_es_la_del_apendice_a():
    primera, *filas = TEMPLATE_CSV.splitlines()
    assert primera == CABECERA
    assert len(filas) == 3


# -- separadores, decimales, cabecera y codificación ------------------------------------


@pytest.mark.parametrize(
    "contenido",
    [
        "ticker;nombre;isin;unidades;coste_medio_eur;divisa\nSAN;Banco;;200;4,5;EUR\n",
        "ticker,nombre,isin,unidades,coste_medio_eur,divisa\nSAN,Banco,,200,4.5,EUR\n",
        "ticker\tnombre\tisin\tunidades\tcoste_medio_eur\tdivisa\nSAN\tBanco\t\t200\t4,5\tEUR\n",
    ],
    ids=["punto_y_coma", "coma", "tabulador"],
)
def test_los_tres_separadores(contenido):
    lectura = parse_positions_csv(contenido.encode())
    assert lectura.ok, [str(i) for i in lectura.issues]
    (san,) = lectura.positions
    assert (san.ticker, san.units, san.avg_cost_eur) == ("SAN", D("200"), D("4.5"))


def test_con_separador_coma_los_nombres_con_coma_van_entre_comillas():
    lectura = parse_positions_csv(csv('AAPL,"Apple, Inc.",,10,150.25,USD,,AAPL'))
    assert lectura.ok
    assert lectura.positions[0].name == "Apple, Inc."


@pytest.mark.parametrize(
    ("unidades", "coste", "esperado_u", "esperado_c"),
    [
        ("10", "150,25", "10", "150.25"),
        ("10", "150.25", "10", "150.25"),
        ("0,5", "1.234,56", "0.5", "1234.56"),
        ("0.123456", "1,234.56", "0.123456", "1234.56"),
    ],
)
def test_coma_o_punto_decimal(unidades, coste, esperado_u, esperado_c):
    lectura = parse_positions_csv(csv(f"BTC;Bitcoin;;{unidades};{coste};EUR;;BTC-EUR;CRIPTO"))
    assert lectura.ok, [str(i) for i in lectura.issues]
    (btc,) = lectura.positions
    assert btc.units == D(esperado_u)
    assert btc.avg_cost_eur == D(esperado_c)


def test_sin_cabecera_la_primera_linea_es_una_posicion():
    lectura = parse_positions_csv(csv("SAN;Banco;;200;4,5;EUR;;SAN.MC", "BBVA;BBVA;;10;9;EUR"))
    assert not lectura.has_header
    assert [(p.ticker, p.line) for p in lectura.positions] == [("SAN", 1), ("BBVA", 2)]


def test_con_cabecera_la_primera_posicion_esta_en_la_linea_2():
    lectura = parse_positions_csv(csv(CABECERA, "SAN;Banco;;200;4,5;EUR;;SAN.MC"))
    assert lectura.has_header
    assert lectura.positions[0].line == 2


def test_la_cabecera_admite_mayusculas_y_tildes():
    cabecera = "TICKER;Nombre;ISIN;Unidades;Coste medio EUR;Divisa;Sector;Símbolo;Clase"
    lectura = parse_positions_csv(csv(cabecera, "SAN;Banco;;200;4,5;EUR;;SAN.MC"))
    assert lectura.ok and lectura.has_header


def test_una_cabecera_en_otro_orden_es_un_error_en_su_linea():
    lectura = parse_positions_csv(csv("ticker;nombre;unidades;isin;coste_medio_eur;divisa"))
    assert [(e.line, "cabecera" in e.message) for e in lectura.errors] == [(1, True)]


@pytest.mark.parametrize(
    ("codificacion", "etiqueta"),
    [("utf-8", "UTF-8"), ("utf-8-sig", "UTF-8"), ("cp1252", "ANSI (Windows-1252)")],
)
def test_utf8_con_y_sin_bom_y_ansi_de_excel(codificacion, etiqueta):
    datos = csv(CABECERA, "CAF;Café Ñandú Pingüino;;3;12,5;EUR;Alimentación_y_bebidas",
                encoding=codificacion)
    lectura = parse_positions_csv(datos)
    assert lectura.encoding == etiqueta
    assert lectura.has_header  # el BOM no estropea la cabecera
    assert lectura.ok
    assert lectura.positions[0].name == "Café Ñandú Pingüino"
    assert lectura.positions[0].sector == "Alimentación_y_bebidas"


def test_fin_de_linea_de_windows():
    datos = (CABECERA + "\r\nSAN;Banco;;200;4,5;EUR;;SAN.MC\r\n").encode("cp1252")
    lectura = parse_positions_csv(datos)
    assert lectura.ok and lectura.positions[0].line == 2


def test_separadores_vacios_al_final_no_cuentan_como_columnas():
    lectura = parse_positions_csv(csv("SAN;Banco;;200;4,5;EUR;;SAN.MC;;;;;"))
    assert lectura.ok


# -- errores: todos a la vez y con su línea ---------------------------------------------


def test_todos_los_errores_a_la_vez_con_su_linea():
    lectura = parse_positions_csv(
        csv(
            CABECERA,  # 1
            "SAN;Banco Santander;;200;4,50;EUR;Banca;SAN.MC;ACCION",  # 2: correcta
            "MAL TICKER;Uno;;10;5;EUR",  # 3
            "",  # 4: en blanco
            "XYZ;;;0;abc;EURO;Mi sector;;FONDO",  # 5: varios errores en la misma fila
            "ABC;Otra;ES123;-3;7;USD",  # 6
        )
    )
    errores = lineas_con_error(lectura)
    assert set(errores) == {3, 5, 6}
    assert any("ticker" in m for m in errores[3])
    assert len(errores[5]) == 6  # nombre, unidades, coste, divisa, sector, clase
    assert any("falta el nombre" in m for m in errores[5])
    assert any("unidades tienen que ser mayores que 0" in m for m in errores[5])
    assert any("«abc» no es un número" in m for m in errores[5])
    assert any("divisa «EURO»" in m for m in errores[5])
    assert any("sector «Mi sector»" in m for m in errores[5])
    assert any("clase «FONDO»" in m for m in errores[5])
    assert any("ISIN" in m for m in errores[6])
    assert any("unidades tienen que ser mayores que 0" in m for m in errores[6])
    # La fila buena sigue ahí, pero con errores no se puede crear la cartera.
    assert [p.ticker for p in lectura.positions] == ["SAN"]
    assert not lectura.ok
    assert lectura.data_rows == 4


def test_cada_error_dice_su_linea_y_su_ticker():
    lectura = parse_positions_csv(csv(CABECERA, "SAN;Banco;;cero;4,5;EUR"))
    (error,) = lectura.errors
    assert str(error) == "Línea 2 (SAN): las unidades «cero» no son un número."


def test_tickers_repetidos():
    lectura = parse_positions_csv(
        csv(CABECERA, "SAN;Banco;;1;4;EUR;;SAN.MC", "BBVA;BBVA;;1;9;EUR;;BBVA.MC",
            "san;Otra vez;;2;5;EUR;;SAN.MC")
    )
    (error,) = lectura.errors
    assert error.line == 4
    assert "repetido" in error.message and "línea 2" in error.message


@pytest.mark.parametrize(
    "ticker", ["SAN X", "-SAN", ".SAN", "SAN/B", "SAN€"], ids=lambda t: repr(t)
)
def test_tickers_no_validos(ticker):
    lectura = parse_positions_csv(csv(f"{ticker};Banco;;1;4;EUR;;SAN.MC"))
    assert any("ticker" in e.message for e in lectura.errors)


@pytest.mark.parametrize("ticker", ["SAN", "BRK.B", "RDS-A", "X_1", "7203"])
def test_tickers_validos(ticker):
    assert parse_positions_csv(csv(f"{ticker};Algo;;1;4;EUR;;SYM")).ok


def test_isin_en_minusculas_se_acepta_en_mayusculas():
    lectura = parse_positions_csv(csv("SAN;Banco;es0113900j37;1;4;EUR;;SAN.MC"))
    assert lectura.ok
    assert lectura.positions[0].isin == "ES0113900J37"


@pytest.mark.parametrize("isin", ["ES011390", "1S0113900J37", "ES0113900J3X", "ES0113900J377"])
def test_isin_no_validos(isin):
    lectura = parse_positions_csv(csv(f"SAN;Banco;{isin};1;4;EUR;;SAN.MC"))
    assert any("ISIN" in e.message for e in lectura.errors)


@pytest.mark.parametrize(
    ("linea", "fragmento"),
    [
        ("SAN;Banco;;;4;EUR", "faltan las unidades"),
        ("SAN;Banco;;1;;EUR", "falta el coste medio"),
        ("SAN;Banco;;1;0;EUR", "coste medio tiene que ser mayor que 0"),
        ("SAN;Banco;;1;4;", "falta la divisa"),
        ("SAN;Banco;;1;4;E1R", "divisa «E1R»"),
        (";Banco;;1;4;EUR", "falta el ticker"),
        ("SAN;Banco;;1;1.2.3;EUR", "«1.2.3» no es un número"),
    ],
)
def test_validaciones_de_cada_columna(linea, fragmento):
    lectura = parse_positions_csv(csv(linea))
    assert any(fragmento in e.message for e in lectura.errors), [str(e) for e in lectura.errors]


def test_faltan_columnas():
    lectura = parse_positions_csv(csv("SAN;Banco;;200;4,5"))
    (error,) = lectura.errors
    assert "hacen falta al menos 6" in error.message


def test_coma_decimal_con_separador_coma_da_columnas_de_mas():
    lectura = parse_positions_csv(csv("AAPL,Apple,,10,150,25,USD,Tecnologia,AAPL,ACCION"))
    (error,) = lectura.errors
    assert "10 columnas" in error.message and "«;»" in error.message


@pytest.mark.parametrize(
    ("clase", "esperada"),
    [("", AssetClass.STOCK), ("etf", AssetClass.ETF), ("Acción", AssetClass.STOCK),
     ("CRIPTO", AssetClass.CRYPTO), ("ETC", AssetClass.ETC)],
)
def test_clase_por_defecto_accion_y_sin_distinguir_mayusculas(clase, esperada):
    lectura = parse_positions_csv(csv(f"X;Algo;;1;4;EUR;;X.MC;{clase}"))
    assert lectura.positions[0].asset_class is esperada


# -- divisas ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("escrita", "guardada"),
    [("GBX", "GBp"), ("gbx", "GBp"), ("GBp", "GBp"), ("GBP", "GBP"), ("usd", "USD"),
     (" HKD ", "HKD")],
)
def test_divisas_gbx_son_peniques(escrita, guardada):
    assert normalize_currency(escrita) == guardada
    lectura = parse_positions_csv(csv(f"VOD;Vodafone;;100;0,80;{escrita};;VOD.L"))
    assert lectura.positions[0].currency == guardada


# -- sin símbolo: aviso, no error -------------------------------------------------------


def test_sin_simbolo_es_un_aviso_y_con_isin_se_sugiere_uno():
    lectura = parse_positions_csv(
        csv(CABECERA, "VUSA;Vanguard S&P 500;IE00B3XXRP09;12;95;EUR", "PRIV;Privada;;1;10;EUR")
    )
    assert lectura.ok
    assert [p.ticker for p in lectura.positions] == ["VUSA", "PRIV"]
    vusa, priv = lectura.warnings
    assert vusa.severity is Severity.WARNING
    assert str(vusa).startswith("Línea 2 (VUSA): sin símbolo de cotización")
    assert "con el ISIN, Sharky te sugiere uno" in vusa.message
    assert "en Cartera" in priv.message


# -- CSV vacío = solo efectivo ----------------------------------------------------------


@pytest.mark.parametrize(
    "datos",
    [b"", b"\n\n  \n", (CABECERA + "\n").encode(), ("﻿" + CABECERA).encode(), b";;;;;;\n"],
    ids=["vacio", "lineas_en_blanco", "solo_cabecera", "cabecera_con_bom", "solo_separadores"],
)
def test_csv_vacio_es_solo_efectivo(datos):
    lectura = parse_positions_csv(datos)
    assert lectura.ok and not lectura.issues
    assert lectura.positions == ()
    assert lectura.data_rows == 0


# -- ficheros que no son un CSV ---------------------------------------------------------


def test_un_fichero_binario_se_explica():
    lectura = parse_positions_csv(b"PK\x03\x04\x00\x00 esto es un xlsx")
    (error,) = lectura.errors
    assert error.line is None and "CSV (delimitado por comas)" in str(error)


def test_un_fichero_enorme_no_se_lee():
    lectura = parse_positions_csv(b"x" * 1_000_001)
    (error,) = lectura.errors
    assert "no parece una lista de posiciones" in error.message


# -- la cartera inicial -----------------------------------------------------------------


def posiciones_de(texto: str) -> list[CsvPosition]:
    lectura = parse_positions_csv(texto.encode())
    assert lectura.ok
    return list(lectura.positions)


def test_aperturas_en_euros_a_coste_medio_exacto():
    posiciones = posiciones_de("AAPL;Apple;;3;162,3333;USD;;AAPL\nSAN;Banco;;200;4,5;EUR;;SAN.MC")
    apertura = build_opening(posiciones, D("1000"), HOY, "Trade Republic")
    aapl, san = apertura.trades
    assert aapl.kind is TradeKind.OPENING and aapl.trade_date == HOY
    assert (aapl.price, aapl.currency, aapl.fx_to_eur, aapl.fee_eur) == (
        D("162.3333"), "EUR", D("1"), D("0")
    )
    assert aapl.amount_eur == D("486.9999")  # sin redondear
    assert aapl.stop is None and aapl.target is None and not aapl.forced
    assert aapl.reason == "Posición inicial importada del CSV"
    # La divisa de cotización vive en el activo.
    assert {a.ticker: a.currency for a in apertura.assets} == {"AAPL": "USD", "SAN": "EUR"}
    libro = build_ledger(apertura.trades)
    assert libro.position("AAPL").avg_cost_eur == D("162.3333")
    assert libro.position("SAN").units == D("200")
    assert apertura.invested_eur == D("486.9999") + D("900.0")


def test_efectivo_inicial_con_el_broker_en_la_nota():
    apertura = build_opening(posiciones_de("SAN;Banco;;10;4;EUR"), D("2900"), HOY, "Trade Republic")
    assert apertura.initial_cash.kind is CashKind.INITIAL
    assert apertura.initial_cash.amount_eur == D("2900")
    assert apertura.initial_cash.movement_date == HOY
    assert apertura.initial_cash.note == "Efectivo inicial en Trade Republic"


def test_sin_efectivo_tambien_hay_movimiento_inicial():
    apertura = build_opening(posiciones_de("SAN;Banco;;10;4;EUR"), D("0"), HOY)
    assert apertura.initial_cash.amount_eur == D("0")


def test_primera_foto_a_coste_y_no_fiable_con_posiciones():
    posiciones = posiciones_de("SAN;Banco;;200;4,5;EUR;;SAN.MC\nX;Otra;;10;210;USD;;X")
    apertura = build_opening(posiciones, D("1000"), HOY)  # 900 + 2100 + 1000
    foto = apertura.snapshot
    assert foto.snapshot_date == HOY
    assert foto.nav_eur == D("4000.0")
    assert foto.cash_eur == D("1000")
    assert foto.unit_value == D("100")
    assert foto.fund_units == D("40.000")
    assert foto.high_water_mark == D("100") and foto.drawdown == D("0")
    assert foto.state is MandateState.OPTIMAL
    assert foto.coverage == D("0.25")  # solo el efectivo tiene valor seguro
    assert foto.reliable is False


def test_solo_efectivo_la_foto_es_fiable():
    apertura = build_opening([], D("2500"), HOY)
    assert apertura.trades == () and apertura.assets == ()
    foto = apertura.snapshot
    assert (foto.nav_eur, foto.fund_units, foto.coverage) == (D("2500"), D("25"), D("1"))
    assert foto.reliable is True


@pytest.mark.parametrize(("efectivo", "fiable"), [("900", True), ("899.99", False)])
def test_fiable_desde_el_90_por_ciento(efectivo, fiable):
    apertura = build_opening(posiciones_de("SAN;Banco;;1;100;EUR"), D(efectivo), HOY)
    assert apertura.snapshot.reliable is fiable


def test_cartera_vacia_o_efectivo_negativo_no_se_crea():
    with pytest.raises(OpeningError, match="vacía"):
        build_opening([], D("0"), HOY)
    with pytest.raises(OpeningError, match="negativo"):
        build_opening([], D("-1"), HOY)


def test_corregir_el_coste_medio():
    (san,) = posiciones_de("SAN;Banco;;200;4,5;EUR")
    corregida = san.with_avg_cost(D("3.95"))
    assert corregida.avg_cost_eur == D("3.95") and corregida.cost_eur == D("790.00")
    assert san.avg_cost_eur == D("4.5")  # la original no cambia
    with pytest.raises(ValueError):
        san.with_avg_cost(D("0"))
    apertura = build_opening([corregida], D("0"), HOY)
    assert apertura.trades[0].price == D("3.95")
    assert apertura.snapshot.nav_eur == D("790.00")
