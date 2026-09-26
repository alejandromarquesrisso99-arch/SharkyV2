"""Cuándo toca cada informe (GUIA §5.7 y §7, H10): el día elegido, 7 días sin semanal, el cambio
de mes y el primer mes incompleto. Con fechas fijas, como un reloj falso: core/schedule.py no
lee nunca la hora.

Septiembre de 2026: el 27 es domingo; el 1 de octubre, jueves.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from sharky.core.formatting import weekday_plural
from sharky.core.models import CashKind, CashMovement, Report, ReportKind
from sharky.core.schedule import (
    Month,
    daily_due,
    is_complete_study,
    manual_months,
    monthly_due,
    next_monthly,
    next_weekly,
    parse_day,
    weekly_due,
    weekly_rule_text,
    weekly_window,
)
from sharky.services.reports import completed_months, report_schedule
from sharky.services.repositories import CashMovementRepository, ReportRepository
from sharky.services.settings import Settings

DOMINGO = 6
MIERCOLES = 2
CREADA = date(2026, 8, 1)  # la cartera empezó un sábado 1 de agosto
DOM_27 = date(2026, 9, 27)
DOM_20 = date(2026, 9, 20)


# -- el semanal ---------------------------------------------------------------------------------


def test_el_dia_elegido_toca_si_hoy_no_hay_uno():
    assert DOM_27.weekday() == DOMINGO
    assert weekly_due(DOM_27, DOMINGO, DOM_20, CREADA)
    # Aunque el último sea de hace dos días (se hizo a mano): el día elegido manda.
    assert weekly_due(DOM_27, DOMINGO, date(2026, 9, 25), CREADA)
    # El de hoy ya está hecho: no se repite.
    assert not weekly_due(DOM_27, DOMINGO, DOM_27, CREADA)


def test_otro_dia_no_toca_si_no_han_pasado_7_dias():
    for dia in range(21, 27):  # de lunes 21 a sábado 26
        assert not weekly_due(date(2026, 9, dia), DOMINGO, DOM_20, CREADA)


def test_el_dia_se_elige_en_los_ajustes():
    miercoles = date(2026, 9, 23)
    assert weekly_due(miercoles, MIERCOLES, DOM_20, CREADA)
    assert not weekly_due(DOM_27, MIERCOLES, miercoles, CREADA)


def test_siete_dias_sin_semanal_toca_aunque_no_sea_el_dia():
    # El domingo 20 no se encendió el PC: el último es del domingo 13.
    lunes = date(2026, 9, 21)
    assert weekly_due(lunes, DOMINGO, date(2026, 9, 13), CREADA)  # 8 días
    assert weekly_due(date(2026, 9, 17), DOMINGO, date(2026, 9, 10), CREADA)  # justo 7
    assert not weekly_due(date(2026, 9, 16), DOMINGO, date(2026, 9, 10), CREADA)  # 6


def test_sin_semanal_previo_cuenta_desde_la_cartera():
    creada = date(2026, 9, 23)  # miércoles
    assert not weekly_due(date(2026, 9, 24), DOMINGO, None, creada)
    assert weekly_due(DOM_27, DOMINGO, None, creada)  # el día elegido
    # Si el domingo tampoco se abre, a los 7 días de crearla.
    assert weekly_due(date(2026, 9, 30), DOMINGO, None, creada)
    assert not weekly_due(date(2026, 9, 29), DOMINGO, None, creada)


def test_sin_cartera_no_toca_nada():
    assert not weekly_due(DOM_27, DOMINGO, None, None)
    assert next_weekly(DOM_27, DOMINGO, None, None) is None
    assert monthly_due(date(2026, 10, 1), None, set()) is None
    assert next_monthly(date(2026, 10, 1), None) is None


def test_el_proximo_semanal():
    viernes = date(2026, 9, 25)
    assert next_weekly(viernes, DOMINGO, DOM_20, CREADA) == DOM_27
    assert next_weekly(DOM_27, DOMINGO, DOM_20, CREADA) == DOM_27  # toca hoy
    assert next_weekly(DOM_27, DOMINGO, DOM_27, CREADA) == date(2026, 10, 4)  # hecho hoy
    # Si el último fue un jueves a mano, el próximo sigue siendo el domingo.
    assert next_weekly(date(2026, 9, 24), DOMINGO, date(2026, 9, 24), CREADA) == DOM_27


def test_la_semana_son_los_7_dias_que_acaban_hoy():
    assert weekly_window(DOM_27) == (date(2026, 9, 21), DOM_27)


def test_la_regla_en_palabras():
    assert weekly_rule_text(DOMINGO) == "los domingos, o a los 7 días del último"
    assert weekday_plural(0) == "lunes" and weekday_plural(5) == "sábados"


# -- el diario ----------------------------------------------------------------------------------


def test_el_diario_toca_si_no_hay_uno_de_hoy():
    assert daily_due(DOM_27, None)
    assert daily_due(DOM_27, date(2026, 9, 26))
    assert not daily_due(DOM_27, DOM_27)


# -- el mensual ---------------------------------------------------------------------------------


def test_cambio_de_mes_toca_el_mes_anterior():
    uno_oct = date(2026, 10, 1)
    assert monthly_due(uno_oct, CREADA, set()) == Month(2026, 9)
    # Hecho ya (completo): no se repite en todo el mes.
    for dia in (1, 2, 15, 31):
        assert monthly_due(date(2026, 10, dia), CREADA, {Month(2026, 9)}) is None
    # Si el PC estuvo apagado el día 1, toca el primer día que se mire.
    assert monthly_due(date(2026, 10, 5), CREADA, set()) == Month(2026, 9)


def test_solo_el_mes_inmediatamente_anterior():
    # Todo octubre sin encender el PC: el 2 de noviembre toca octubre, no septiembre.
    assert monthly_due(date(2026, 11, 2), CREADA, {Month(2026, 8)}) == Month(2026, 10)


def test_cambio_de_anio():
    assert monthly_due(date(2027, 1, 1), CREADA, set()) == Month(2026, 12)
    assert Month(2026, 12).next == Month(2027, 1) and Month(2027, 1).previous == Month(2026, 12)


def test_un_estudio_parcial_no_cuenta_como_hecho():
    septiembre = Month(2026, 9)
    assert not is_complete_study(septiembre, date(2026, 9, 15))  # a mano, a mitad de mes
    assert not is_complete_study(septiembre, date(2026, 9, 30))  # el último día, aún no acabó
    assert is_complete_study(septiembre, date(2026, 10, 1))


def test_primer_mes_incompleto_no_sale_solo():
    creada = date(2026, 9, 15)
    assert monthly_due(date(2026, 10, 1), creada, set()) is None  # septiembre, a medias
    assert monthly_due(date(2026, 11, 1), creada, set()) == Month(2026, 10)  # el primero entero
    assert next_monthly(date(2026, 10, 1), creada) == date(2026, 11, 1)
    # Creada el día 1: ese mes está entero y sí sale.
    assert monthly_due(date(2026, 10, 1), date(2026, 9, 1), set()) == Month(2026, 9)
    assert next_monthly(date(2026, 9, 20), date(2026, 9, 1)) == date(2026, 10, 1)


def test_a_mano_se_puede_estudiar_el_primer_mes_incompleto():
    creada = date(2026, 9, 15)
    assert manual_months(date(2026, 10, 3), creada) == (Month(2026, 9), Month(2026, 10))
    # Si la cartera es de este mes, no hay mes anterior que estudiar.
    assert manual_months(date(2026, 9, 20), creada) == (None, Month(2026, 9))


def test_los_meses():
    agosto = Month(2026, 8)
    assert agosto.key == "2026-08" and agosto.label == "agosto de 2026"
    assert agosto.first_day == date(2026, 8, 1) and agosto.last_day == date(2026, 8, 31)
    assert Month(2028, 2).last_day == date(2028, 2, 29)
    assert Month.parse("2026-08") == agosto and Month.parse("2026-W38") is None
    assert Month.parse("2026-13") is None
    assert Month.of(date(2026, 9, 27)) == Month(2026, 9)
    assert agosto.contains(date(2026, 8, 31)) and not agosto.contains(date(2026, 9, 1))


def test_el_periodo_de_un_diario_o_un_semanal_es_un_dia():
    assert parse_day("2026-09-27") == DOM_27
    assert parse_day("2026-W38") is None  # una semana ISO no es un día
    assert parse_day("2026-02-30") is None and parse_day("") is None


# -- con lo guardado y un reloj falso -----------------------------------------------------------


def _informe(db, tipo, periodo, cuando):
    with db.transaction() as conn:
        ReportRepository(conn).save(Report(tipo, periodo, cuando, "…", True, "- ok"))


@pytest.fixture
def cartera_de_agosto(db):
    with db.transaction() as conn:
        CashMovementRepository(conn).add(CashMovement(CREADA, CashKind.INITIAL, D("1000")))
    return db


def test_lo_que_toca_con_lo_guardado(cartera_de_agosto):
    db = cartera_de_agosto
    reloj = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    # Un parcial de septiembre, hecho a mano el 15: no cuenta como hecho.
    _informe(db, ReportKind.MONTHLY, "2026-09", reloj - timedelta(days=16))
    _informe(db, ReportKind.MONTHLY, "2026-08", datetime(2026, 9, 1, 9, 0, tzinfo=UTC))
    _informe(db, ReportKind.WEEKLY, "2026-09-27", reloj - timedelta(days=4))
    _informe(db, ReportKind.DAILY, "2026-10-01", reloj)
    assert completed_months(db.connection()) == {Month(2026, 8)}
    agenda = report_schedule(db.connection(), Settings(), reloj.date())
    assert not agenda.daily_due
    assert not agenda.weekly_due and agenda.next_weekly == date(2026, 10, 4)
    assert agenda.monthly_due == Month(2026, 9)
    assert (agenda.previous_month, agenda.current_month) == (Month(2026, 9), Month(2026, 10))
    # El completo de septiembre sustituye al parcial y ya no toca.
    _informe(db, ReportKind.MONTHLY, "2026-09", reloj)
    assert report_schedule(db.connection(), Settings(), reloj.date()).monthly_due is None


def test_el_dia_del_semanal_sale_de_los_ajustes(cartera_de_agosto):
    ajustes = Settings()
    ajustes.automation.weekly_report_weekday = MIERCOLES
    _informe(cartera_de_agosto, ReportKind.WEEKLY, "2026-09-20", datetime(2026, 9, 20,
                                                                          tzinfo=UTC))
    agenda = report_schedule(cartera_de_agosto.connection(), ajustes, date(2026, 9, 23))
    assert agenda.weekly_due and agenda.weekday == MIERCOLES
