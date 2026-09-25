"""La parte de los informes que calcula el código (core/reports.py): la conclusión, el coste, la
estimación de cada botón, el tope de gasto y el mandato del prompt de sistema."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from sharky.core.formatting import format_usd, format_usd_range, month_name
from sharky.core.models import Price, PriceSource, Trade, TradeKind
from sharky.core.reports import (
    DAILY_CONCLUSION,
    AIAction,
    BudgetCheck,
    CostEstimate,
    TokenPrice,
    call_cost,
    estimate_cost,
    extract_section,
    find_price,
    is_forced_since,
    mandate_text,
    position_move,
    trade_text,
    usage_text,
)
from sharky.core.valuation import PositionValue
from sharky.services.settings import MandateSettings

MADRID = timezone(timedelta(hours=2))
HOY = date(2026, 9, 25)


# -- la conclusión ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texto",
    [
        "Análisis.\n\n## Conclusión del día\n- Uno.\n- Dos.\n",
        "Análisis.\n\n### conclusion del dia\n- Uno.\n- Dos.",
        "Análisis.\n\n# CONCLUSIÓN DEL DÍA:\n\n- Uno.\n- Dos.\n\n",
        "Análisis.\n\n**Conclusión del día**\n- Uno.\n- Dos.",
    ],
    ids=["h2", "h3_sin_tildes", "h1_mayusculas", "negrita"],
)
def test_la_conclusion_se_encuentra_como_venga(texto):
    assert extract_section(texto, DAILY_CONCLUSION) == "- Uno.\n- Dos."


def test_la_conclusion_acaba_en_el_siguiente_titulo_o_en_una_raya():
    texto = "## Conclusión del día\n- Uno.\n\n## Otra cosa\nNo.\n"
    assert extract_section(texto, DAILY_CONCLUSION) == "- Uno."
    texto = "## Conclusión del día\n- Uno.\n\n---\nNo.\n"
    assert extract_section(texto, DAILY_CONCLUSION) == "- Uno."
    # Un subtítulo dentro sigue siendo parte de la sección.
    texto = "## Conclusión del día\n- Uno.\n### Detalle\n- Dos.\n# Fin\n"
    assert extract_section(texto, DAILY_CONCLUSION) == "- Uno.\n### Detalle\n- Dos."


@pytest.mark.parametrize(
    "texto",
    ["Solo análisis, sin conclusión.", "## Conclusión del día\n\n## Otra\n- x", ""],
    ids=["sin_titulo", "vacia", "nada"],
)
def test_sin_conclusion(texto):
    assert extract_section(texto, DAILY_CONCLUSION) is None


def test_no_confunde_un_titulo_que_solo_empieza_igual():
    texto = "## Conclusión del día anterior\n- No.\n"
    assert extract_section(texto, DAILY_CONCLUSION) is None


# -- coste ------------------------------------------------------------------------------------

PRECIOS = {
    "claude-sonnet-5": TokenPrice(D(2), D(10)),
    "claude-opus-5": TokenPrice(D(5), D(25)),
    "claude-haiku-4-5": TokenPrice(D(1), D(5)),
}


def test_precio_de_cada_modelo():
    assert find_price(PRECIOS, "claude-sonnet-5") == TokenPrice(D(2), D(10))
    # La lista de modelos puede darlo con fecha: vale el del nombre sin ella…
    assert find_price(PRECIOS, "claude-haiku-4-5-20251001") == TokenPrice(D(1), D(5))
    # …pero nunca el de otro modelo que empiece igual.
    assert find_price(PRECIOS, "claude-opus-5-5") is None
    assert find_price(PRECIOS, "claude-nuevo") is None


def test_coste_de_una_llamada():
    # 12.000 × 2 $/M + 3.000 × 10 $/M + 5 búsquedas × 10 $/1.000
    assert call_cost(12_000, 3_000, 5, PRECIOS["claude-sonnet-5"], D(10)) == D("0.104")
    assert call_cost(0, 0, 0, PRECIOS["claude-opus-5"], D(10)) == 0


def test_la_estimacion_es_la_tabla_hasta_tener_tres_ejecuciones():
    assert estimate_cost(AIAction.DAILY, []) == CostEstimate(D("0.02"), D("0.05"), False)
    assert estimate_cost(AIAction.WEEKLY, [D("0.5"), D("0.6")]).text == "≈ 0,30–0,80 $"
    assert estimate_cost(AIAction.EXPLORER, []).text == "≈ 0,80–2,50 $"
    assert estimate_cost(AIAction.MONTHLY, []).ceiling == D("0.60")


def test_la_estimacion_es_la_media_de_las_tres_ultimas():
    estimacion = estimate_cost(AIAction.DAILY, [D("0.03"), D("0"), D("0.04"), D("0.05"),
                                                D("9")])
    assert estimacion == CostEstimate(D("0.04"), D("0.04"), True)
    assert estimacion.text == "≈ 0,04 $"


def test_el_tope_cuenta_con_lo_mas_caro_de_la_horquilla():
    tabla = estimate_cost(AIAction.DAILY, [])
    assert BudgetCheck(D("9.95"), D("10"), tabla).allowed
    no_cabe = BudgetCheck(D("9.96"), D("10"), tabla)
    assert not no_cabe.allowed
    assert no_cabe.spent_text == "9,96 $ de 10,00 $"
    assert "se superaría el tope de gasto mensual" in no_cabe.reason
    assert "≈ 0,02–0,05 $" in no_cabe.reason
    assert not BudgetCheck(D("0"), D("0"), tabla).allowed  # tope 0: nada con IA


def test_formato_de_los_dolares():
    assert format_usd(D("0.0312")) == "0,03 $"
    assert format_usd(D("1234.5")) == "1.234,50 $"
    assert format_usd(D("0.004")) == "< 0,01 $"
    assert format_usd(D("0")) == "0,00 $"
    assert format_usd_range(D("0.02"), D("0.05")) == "0,02–0,05 $"
    assert month_name(9) == "septiembre"


def test_uso_en_texto():
    assert usage_text(12_345, 678) == "12.345 tokens de entrada · 678 de salida"
    assert usage_text(1, 2, 1) == "1 tokens de entrada · 2 de salida · 1 búsqueda"


# -- el mandato del prompt de sistema ---------------------------------------------------------


def test_el_mandato_sale_de_los_ajustes():
    texto = mandate_text(MandateSettings(max_asset_weight_other_pct=4.5,
                                         breach_escalation_days=10).rules())
    assert "- Peso máximo por activo: 10 % en Óptimo; 4,5 % en los demás estados." in texto
    assert "- Efectivo: entre 15 % y 30 % del NAV en Óptimo; mínimo del 30 %" in texto
    assert "- Riesgo máximo por operación (hasta el stop): 1,5 % del NAV." in texto
    assert "- Ratio beneficio/riesgo mínimo: 2,0." in texto
    assert "abierto 10 días o más está escalado" in texto
    assert "  - Alerta: desde el 3 % hasta menos del 8 %; compras permitidas, con tope del " \
           "4,5 % por activo." in texto
    assert "  - Bloqueo: 20 % o más" in texto


# -- variación y operaciones forzadas --------------------------------------------------------


def posicion(precio: Price | None) -> PositionValue:
    return PositionValue("SAN", "Banco Santander", "Banca", D(100), D(450), D(400), D("0.04"),
                         PriceSource.MARKET, price=precio)


def cierre(dia, valor, divisa="EUR"):
    return Price("SAN", dia, D(valor), divisa, PriceSource.MARKET,
                 datetime(2026, 9, 25, 18, tzinfo=MADRID))


def test_variacion_frente_al_cierre_de_referencia():
    movimiento = position_move(posicion(cierre(HOY, "4")), cierre(HOY - timedelta(days=2), "5"))
    assert movimiento.fraction == D("-0.2") and movimiento.since == HOY - timedelta(days=2)


@pytest.mark.parametrize(
    ("actual", "referencia"),
    [
        (None, cierre(HOY, "4")),
        (cierre(HOY, "4"), None),
        (cierre(HOY, "4"), cierre(HOY, "5")),  # el mismo cierre: nada que comparar
        (cierre(HOY, "4", "GBp"), cierre(HOY - timedelta(days=1), "5")),  # otra divisa
    ],
    ids=["sin_precio", "sin_referencia", "mismo_dia", "otra_divisa"],
)
def test_sin_variacion_si_no_hay_con_que_comparar(actual, referencia):
    assert position_move(posicion(actual), referencia) is None


def forzada(dia, tipo=TradeKind.BUY, forced=True):
    return Trade(dia, "SAN", tipo, D(10), D("4.5"), "EUR", D(1), D(1), D(45), forced=forced,
                 reason="Me fío")


def test_operaciones_forzadas_desde_el_ultimo_control():
    ayer = HOY - timedelta(days=1)
    assert is_forced_since(forzada(ayer), ayer)  # el mismo día: pudo ser después del control
    assert not is_forced_since(forzada(ayer - timedelta(days=1)), ayer)
    assert is_forced_since(forzada(ayer - timedelta(days=30)), None)  # sin control: todas
    assert not is_forced_since(forzada(HOY, forced=False), None)
    assert trade_text(forzada(HOY)) == "25/09 COMPRA SAN: 10 u. a 4,50 EUR · motivo: «Me fío»"
