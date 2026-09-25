"""La cartera inicial: el CSV de posiciones y lo que se guarda al crear la cartera (GUIA §5.2).

Todo es puro: recibe los bytes del fichero, el efectivo y la fecha, y no lee el disco ni el
reloj. Quien lo llama (el asistente) lee el fichero y guarda el resultado en una transacción.

Formato del CSV:

- Separador `;`, `,` o tabulador (se toma de la primera línea con contenido, por ese orden).
- Decimal con coma o punto; cabecera opcional; UTF-8 (con BOM o sin él) o ANSI de Excel.
- Columnas, en este orden: ticker, nombre, isin, unidades, coste_medio_eur, divisa, sector,
  simbolo, clase. Las tres últimas pueden faltar al final de la fila.

Todos los problemas se devuelven a la vez, cada uno con su número de línea (la línea física
del fichero, contando la cabecera y las líneas en blanco). Un error impide crear la cartera; un
aviso (posición sin símbolo) no.

La cartera se guarda en euros: la columna `coste_medio_eur` ya lo está, y la divisa de
cotización (USD, GBp…) solo se apunta en el activo. El valor de mercado en euros llega con los
precios (H5).
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sharky.core.formatting import parse_decimal
from sharky.core.mandate import INITIAL_UNIT_VALUE
from sharky.core.models import (
    Asset,
    AssetClass,
    CashKind,
    CashMovement,
    MandateState,
    NavSnapshot,
    Trade,
    TradeKind,
)
from sharky.core.valuation import BASE_CURRENCY, RELIABLE_COVERAGE, normalize_currency

#: La plantilla del Apéndice A. Se genera desde aquí, así `*.csv` se ignora en git sin
#: excepciones.
TEMPLATE_CSV = (
    "ticker;nombre;isin;unidades;coste_medio_eur;divisa;sector;simbolo;clase\n"
    "AAPL;Apple Inc.;US0378331005;10;150,00;USD;Tecnologia;AAPL;ACCION\n"
    "SAN;Banco Santander;ES0113900J37;200;4,50;EUR;Banca;SAN.MC;ACCION\n"
    "IWDA;iShares Core MSCI World;IE00B4L5Y983;5;80,00;EUR;Renta_Variable_Global;IWDA.AS;ETF\n"
)
TEMPLATE_FILENAME = "plantilla_sharky.csv"

COLUMNS: tuple[str, ...] = (
    "ticker",
    "nombre",
    "isin",
    "unidades",
    "coste_medio_eur",
    "divisa",
    "sector",
    "simbolo",
    "clase",
)
#: Hasta «divisa», todas; las de detrás pueden faltar.
MIN_COLUMNS = 6

#: Un CSV de posiciones ocupa unos pocos KB. Algo mayor es otro fichero elegido por error.
MAX_BYTES = 1_000_000

OPENING_REASON = "Posición inicial importada del CSV"

_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_DELIMITERS: tuple[str, ...] = (";", "\t", ",")
_DELIMITER_NAMES = {";": "punto y coma", "\t": "tabulador", ",": "coma"}


class Severity(StrEnum):
    ERROR = "Error"
    WARNING = "Aviso"


@dataclass(frozen=True)
class CsvIssue:
    """Un problema del CSV. `line` es None cuando afecta al fichero entero."""

    line: int | None
    message: str
    severity: Severity = Severity.ERROR
    ticker: str | None = None

    def __str__(self) -> str:
        if self.line is None:
            return self.message
        sujeto = f" ({self.ticker})" if self.ticker else ""
        return f"Línea {self.line}{sujeto}: {self.message}"


@dataclass(frozen=True)
class CsvPosition:
    """Una posición leída del CSV, ya validada."""

    line: int
    ticker: str
    name: str
    units: Decimal
    avg_cost_eur: Decimal
    currency: str  # divisa de cotización
    asset_class: AssetClass = AssetClass.STOCK
    isin: str | None = None
    sector: str | None = None
    yahoo_symbol: str | None = None

    @property
    def cost_eur(self) -> Decimal:
        """Coste total de la posición, en EUR."""
        return self.units * self.avg_cost_eur

    def with_avg_cost(self, avg_cost_eur: Decimal) -> CsvPosition:
        """La misma posición con otro coste medio (el usuario lo corrige en la vista previa)."""
        if not avg_cost_eur.is_finite() or avg_cost_eur <= 0:
            raise ValueError("El coste medio tiene que ser mayor que 0.")
        return replace(self, avg_cost_eur=avg_cost_eur)

    def to_asset(self) -> Asset:
        return Asset(
            ticker=self.ticker,
            name=self.name,
            currency=self.currency,
            asset_class=self.asset_class,
            isin=self.isin,
            yahoo_symbol=self.yahoo_symbol,
            sector=self.sector,
        )


@dataclass(frozen=True)
class CsvImport:
    """Lo que ha dado leer el CSV."""

    positions: tuple[CsvPosition, ...] = ()
    issues: tuple[CsvIssue, ...] = ()
    encoding: str = "UTF-8"
    delimiter: str = ";"
    has_header: bool = False
    data_rows: int = 0  # filas de datos leídas, válidas o no

    @property
    def errors(self) -> tuple[CsvIssue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[CsvIssue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.WARNING)

    @property
    def ok(self) -> bool:
        """Se puede crear la cartera con esto (los avisos no lo impiden)."""
        return not self.errors

    @property
    def delimiter_name(self) -> str:
        return _DELIMITER_NAMES.get(self.delimiter, repr(self.delimiter))


# -- lectura ----------------------------------------------------------------------------


def parse_positions_csv(data: bytes) -> CsvImport:
    """Lee el CSV de posiciones. Nunca lanza: todo lo que falle va en `issues`."""
    if len(data) > MAX_BYTES:
        return CsvImport(
            issues=(
                CsvIssue(
                    None,
                    f"El fichero ocupa {len(data) // 1024} KB: no parece una lista de posiciones. "
                    "Elige el CSV con tus posiciones.",
                ),
            )
        )
    if b"\x00" in data:
        return CsvImport(
            issues=(
                CsvIssue(
                    None,
                    "El fichero no es un CSV de texto (UTF-8 o ANSI). Si es un Excel, guárdalo "
                    "como «CSV (delimitado por comas)» y vuelve a adjuntarlo.",
                ),
            )
        )
    texto, codificacion = _decode(data)
    separador = _detect_delimiter(texto)
    lector = csv.reader(io.StringIO(texto, newline=""), delimiter=separador)

    posiciones: list[CsvPosition] = []
    problemas: list[CsvIssue] = []
    vistos: dict[str, int] = {}  # ticker en mayúsculas → primera línea
    cabecera = False
    primera = True
    filas = 0
    ultima_linea = 0

    for fila in lector:
        linea = ultima_linea + 1  # la fila empieza aquí (puede ocupar varias líneas)
        ultima_linea = lector.line_num
        celdas = [c.strip() for c in fila]
        if not any(celdas):
            continue  # línea en blanco (o solo separadores)
        if primera:
            primera = False
            if _normalize(celdas[0]) == COLUMNS[0]:
                cabecera = True
                problema = _check_header(celdas, linea)
                if problema is not None:
                    problemas.append(problema)
                continue
        filas += 1
        posicion, de_la_fila = _parse_row(celdas, linea, vistos)
        problemas.extend(de_la_fila)
        if posicion is not None:
            posiciones.append(posicion)

    return CsvImport(
        positions=tuple(posiciones),
        issues=tuple(problemas),
        encoding=codificacion,
        delimiter=separador,
        has_header=cabecera,
        data_rows=filas,
    )


def _decode(data: bytes) -> tuple[str, str]:
    """UTF-8 si lo es; si no, el ANSI con el que guarda Excel en Windows."""
    try:
        return data.decode("utf-8-sig"), "UTF-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), "ANSI (Windows-1252)"
    except UnicodeDecodeError:
        # cp1252 deja cinco bytes sin definir; latin-1 los acepta todos.
        return data.decode("latin-1"), "ANSI (Latin-1)"


def _detect_delimiter(texto: str) -> str:
    """El separador de la primera línea con contenido: `;`, tabulador o `,`, por ese orden."""
    for linea in texto.splitlines():
        if linea.strip():
            for candidato in _DELIMITERS:
                if candidato in linea:
                    return candidato
            break
    return _DELIMITERS[0]


def _normalize(nombre: str) -> str:
    """«Símbolo », «COSTE MEDIO EUR» → «simbolo», «coste_medio_eur»."""
    sin_tildes = unicodedata.normalize("NFKD", nombre)
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return re.sub(r"[\s-]+", "_", sin_tildes.strip().lower())


def _check_header(celdas: list[str], linea: int) -> CsvIssue | None:
    nombres = [_normalize(c) for c in _trim_trailing(celdas)]
    if nombres == list(COLUMNS[: len(nombres)]) and len(nombres) >= MIN_COLUMNS:
        return None
    return CsvIssue(
        linea,
        "la cabecera no coincide con la plantilla. Las columnas van en este orden: "
        + "; ".join(COLUMNS)
        + ".",
    )


def _trim_trailing(celdas: list[str]) -> list[str]:
    recortadas = list(celdas)
    while recortadas and not recortadas[-1]:
        recortadas.pop()
    return recortadas


def _parse_row(
    celdas: list[str], linea: int, vistos: dict[str, int]
) -> tuple[CsvPosition | None, list[CsvIssue]]:
    """Valida una fila entera y devuelve todos sus problemas, no solo el primero."""
    ticker_bruto = celdas[0]
    sujeto = ticker_bruto or None
    problemas: list[CsvIssue] = []

    def error(mensaje: str) -> None:
        problemas.append(CsvIssue(linea, mensaje, Severity.ERROR, sujeto))

    # Excel a veces deja separadores vacíos al final: no cuentan como columnas.
    if len(celdas) > len(COLUMNS):
        celdas = celdas[: len(COLUMNS)] + _trim_trailing(celdas[len(COLUMNS) :])
    if len(celdas) > len(COLUMNS):
        error(
            f"tiene {len(celdas)} columnas y como mucho son {len(COLUMNS)}. ¿Hay un separador "
            "de más? Si usas coma decimal, separa las columnas con «;»."
        )
        return None, problemas
    if len(celdas) < MIN_COLUMNS:
        error(
            f"tiene {len(celdas)} columnas y hacen falta al menos {MIN_COLUMNS} (de ticker a "
            "divisa)."
        )
        return None, problemas
    celdas = celdas + [""] * (len(COLUMNS) - len(celdas))
    _, nombre, isin, unidades_txt, coste_txt, divisa_txt, sector, simbolo, clase_txt = celdas

    # ticker
    ticker: str | None = None
    if not ticker_bruto:
        error("falta el ticker.")
    elif not _TICKER.match(ticker_bruto):
        error(
            f"el ticker «{ticker_bruto}» no es válido: sin espacios, empieza por letra o número "
            "y solo lleva letras, números, «_», «.» o «-»."
        )
    else:
        clave = ticker_bruto.upper()
        if clave in vistos:
            error(
                f"el ticker {ticker_bruto} está repetido (ya aparece en la línea "
                f"{vistos[clave]})."
            )
        else:
            vistos[clave] = linea
            ticker = ticker_bruto

    if not nombre:
        error("falta el nombre.")

    isin_limpio = isin.upper() or None
    if isin_limpio is not None and not _ISIN.match(isin_limpio):
        error(
            f"el ISIN «{isin}» no es válido: son 12 caracteres, dos letras de país, nueve "
            "letras o números y un dígito final."
        )

    unidades = _positive(unidades_txt, _UNITS_MESSAGES, error)
    coste = _positive(coste_txt, _COST_MESSAGES, error)

    divisa = normalize_currency(divisa_txt)
    if not divisa_txt:
        error("falta la divisa.")
    elif divisa is None:
        error(f"la divisa «{divisa_txt}» no es válida: son 3 letras (EUR, USD, GBp, HKD…).")

    if sector and re.search(r"\s", sector):
        error(f"el sector «{sector}» no puede llevar espacios (usa «_»: Renta_Variable).")

    clase = _asset_class(clase_txt)
    if clase is None:
        error(f"la clase «{clase_txt}» no es válida: ACCION, ETF, ETC o CRIPTO.")

    if problemas:
        return None, problemas

    assert ticker is not None and unidades is not None and coste is not None
    assert divisa is not None and clase is not None
    posicion = CsvPosition(
        line=linea,
        ticker=ticker,
        name=nombre,
        units=unidades,
        avg_cost_eur=coste,
        currency=divisa,
        asset_class=clase,
        isin=isin_limpio,
        sector=sector or None,
        yahoo_symbol=simbolo or None,
    )
    if posicion.yahoo_symbol is None:
        problemas.append(
            CsvIssue(linea, _missing_symbol_text(posicion.isin), Severity.WARNING, ticker)
        )
    return posicion, problemas


def _missing_symbol_text(isin: str | None) -> str:
    base = "sin símbolo de cotización. Se valorará a coste hasta que se lo asignes"
    if isin:
        return base + "; con el ISIN, Sharky te sugiere uno."
    return base + " en Cartera."


#: Los mensajes de las dos columnas numéricas: (falta, no es un número, no es mayor que 0).
_UNITS_MESSAGES = (
    "faltan las unidades.",
    "las unidades «{}» no son un número.",
    "las unidades tienen que ser mayores que 0.",
)
_COST_MESSAGES = (
    "falta el coste medio.",
    "el coste medio «{}» no es un número.",
    "el coste medio tiene que ser mayor que 0.",
)


def _positive(
    texto: str, mensajes: tuple[str, str, str], error: Callable[[str], None]
) -> Decimal | None:
    falta, no_numero, no_positivo = mensajes
    if not texto:
        error(falta)
        return None
    try:
        valor = parse_decimal(texto)
    except ValueError:
        error(no_numero.format(texto))
        return None
    if valor <= 0:
        error(no_positivo)
        return None
    return valor


def _asset_class(texto: str) -> AssetClass | None:
    if not texto:
        return AssetClass.STOCK
    normalizado = _normalize(texto).upper()
    try:
        return AssetClass(normalizado)
    except ValueError:
        return None


# -- un activo dado de alta a mano --------------------------------------------------------


def asset_from_fields(
    ticker: str,
    name: str,
    currency: str,
    *,
    isin: str = "",
    yahoo_symbol: str = "",
    sector: str = "",
    asset_class: AssetClass = AssetClass.STOCK,
) -> tuple[Asset | None, list[str]]:
    """Un activo nuevo escrito a mano (la compra de un ticker nuevo en Operar), con las mismas
    reglas que el CSV (GUIA §5.2). Devuelve el activo, o None y todos los errores a la vez."""
    errores: list[str] = []
    ticker = ticker.strip()
    nombre = name.strip()
    isin_limpio = isin.strip().upper() or None
    simbolo = yahoo_symbol.strip() or None
    sector_limpio = sector.strip() or None
    if not ticker:
        errores.append("Falta el ticker.")
    elif not _TICKER.match(ticker):
        errores.append(
            f"El ticker «{ticker}» no es válido: sin espacios, empieza por letra o número y "
            "solo lleva letras, números, «_», «.» o «-»."
        )
    if not nombre:
        errores.append("Falta el nombre del activo.")
    if isin_limpio is not None and not _ISIN.match(isin_limpio):
        errores.append(
            f"El ISIN «{isin.strip()}» no es válido: son 12 caracteres, dos letras de país, "
            "nueve letras o números y un dígito final."
        )
    divisa = normalize_currency(currency)
    if not currency.strip():
        errores.append("Falta la divisa de cotización.")
    elif divisa is None:
        errores.append(
            f"La divisa de cotización «{currency.strip()}» no es válida: son 3 letras (EUR, "
            "USD, GBp, HKD…)."
        )
    if simbolo is not None and re.search(r"\s", simbolo):
        errores.append("El símbolo no puede llevar espacios (por ejemplo, SAN.MC).")
    if sector_limpio is not None and re.search(r"\s", sector_limpio):
        errores.append(
            f"El sector «{sector_limpio}» no puede llevar espacios (usa «_»: Renta_Variable)."
        )
    if errores or divisa is None:
        return None, errores
    return (
        Asset(ticker, nombre, divisa, asset_class, isin=isin_limpio, yahoo_symbol=simbolo,
              sector=sector_limpio),
        [],
    )


# -- la cartera inicial -----------------------------------------------------------------


class OpeningError(ValueError):
    """Con estos datos no se puede crear la cartera. El mensaje se puede enseñar."""


@dataclass(frozen=True)
class Opening:
    """Lo que se guarda al confirmar el asistente, todo en una transacción."""

    assets: tuple[Asset, ...]
    trades: tuple[Trade, ...]
    initial_cash: CashMovement
    snapshot: NavSnapshot

    @property
    def invested_eur(self) -> Decimal:
        """Coste de las posiciones, en EUR."""
        return sum((t.amount_eur for t in self.trades), Decimal("0"))


def build_opening(
    positions: Iterable[CsvPosition],
    cash_eur: Decimal,
    day: date,
    broker: str = "",
) -> Opening:
    """Monta la cartera inicial: activos, operaciones APERTURA, efectivo INICIAL y la primera
    foto del NAV.

    - Cada APERTURA va en EUR y a coste medio: precio = coste medio, cambio 1, sin comisión.
      El importe no se redondea, así el coste medio del libro es exactamente el del CSV.
    - El movimiento INICIAL se guarda siempre, aunque sea de 0 €: marca que la cartera existe.
    - La primera foto se valora a coste (todavía no hay precios): valor por participación
      100, participaciones = NAV / 100, máximo 100, drawdown 0 y estado Óptimo. Las
      posiciones a coste no son precio fiable, así que la cobertura es la parte en efectivo y
      la foto solo es fiable si llega al 90 %.
    """
    posiciones = list(positions)
    if not cash_eur.is_finite() or cash_eur < 0:
        raise OpeningError("El efectivo no puede ser negativo.")
    vistos: set[str] = set()
    for p in posiciones:
        if p.ticker.upper() in vistos:
            raise OpeningError(f"El ticker {p.ticker} está repetido.")
        vistos.add(p.ticker.upper())

    operaciones = tuple(
        Trade(
            trade_date=day,
            ticker=p.ticker,
            kind=TradeKind.OPENING,
            units=p.units,
            price=p.avg_cost_eur,
            currency=BASE_CURRENCY,
            fx_to_eur=Decimal("1"),
            fee_eur=Decimal("0"),
            amount_eur=p.cost_eur,
            reason=OPENING_REASON,
        )
        for p in posiciones
    )
    invertido = sum((t.amount_eur for t in operaciones), Decimal("0"))
    nav = cash_eur + invertido
    if nav <= 0:
        raise OpeningError("La cartera está vacía: añade posiciones o efectivo.")

    nota = f"Efectivo inicial en {broker.strip()}" if broker.strip() else "Efectivo inicial"
    cobertura = cash_eur / nav
    foto = NavSnapshot(
        snapshot_date=day,
        nav_eur=nav,
        cash_eur=cash_eur,
        fund_units=nav / INITIAL_UNIT_VALUE,
        unit_value=INITIAL_UNIT_VALUE,
        high_water_mark=INITIAL_UNIT_VALUE,
        drawdown=Decimal("0"),
        state=MandateState.OPTIMAL,
        coverage=cobertura,
        reliable=cobertura >= RELIABLE_COVERAGE,
    )
    return Opening(
        assets=tuple(p.to_asset() for p in posiciones),
        trades=operaciones,
        initial_cash=CashMovement(day, CashKind.INITIAL, cash_eur, note=nota),
        snapshot=foto,
    )
