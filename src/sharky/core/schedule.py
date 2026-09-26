"""Cuándo toca cada informe (GUIA §5.7 y §5.9).

Pura: recibe el día de hoy y lo que ya hay guardado; nunca lee el reloj.

- **Diario:** si no hay uno de hoy.
- **Semanal:** el día elegido (domingo por defecto) si hoy no hay uno, o si han pasado 7 días
  o más desde el último. Sin semanal previo, los 7 días cuentan desde la creación de la cartera.
  Cubre los 7 días que acaban hoy.
- **Mensual:** el primer día en que se mira en un mes nuevo, sobre el mes anterior, mientras ese
  mes no tenga un estudio completo (uno hecho cuando ya había terminado). Solo el mes
  inmediatamente anterior: si el PC estuvo apagado un mes entero, ese se pierde. El primer mes
  de la cartera, si empezó después del día 1, no sale solo (decidido en el H10); a mano, sí.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from sharky.core.formatting import month_name, weekday_plural

#: Los días que cubre un semanal y los que pueden pasar sin uno.
WEEK_DAYS = 7


@dataclass(frozen=True, order=True)
class Month:
    """Un mes del calendario."""

    year: int
    month: int

    @classmethod
    def of(cls, day: date) -> Month:
        return cls(day.year, day.month)

    @classmethod
    def parse(cls, key: str) -> Month | None:
        """«2026-08» → agosto de 2026. None si no es un mes."""
        try:
            anio, mes = (int(x) for x in key.split("-"))
        except ValueError:
            return None
        return cls(anio, mes) if 1 <= mes <= 12 else None

    @property
    def key(self) -> str:
        """El periodo de su estudio mensual: «2026-08»."""
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def label(self) -> str:
        """«agosto de 2026»."""
        return f"{month_name(self.month)} de {self.year}"

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def next(self) -> Month:
        return Month(self.year + 1, 1) if self.month == 12 else Month(self.year, self.month + 1)

    @property
    def previous(self) -> Month:
        return Month(self.year - 1, 12) if self.month == 1 else Month(self.year, self.month - 1)

    @property
    def last_day(self) -> date:
        return self.next.first_day - timedelta(days=1)

    def contains(self, day: date) -> bool:
        return self.first_day <= day <= self.last_day


_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_day(text: str) -> date | None:
    """El periodo de un diario o un semanal («2026-09-27») como fecha. Solo AAAA-MM-DD: una
    semana ISO («2026-W38») no es un día."""
    if not _ISO_DAY.match(text or ""):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


# -- diario ------------------------------------------------------------------------------------


def daily_due(today: date, last_daily: date | None) -> bool:
    """Toca el control diario si no hay uno de hoy."""
    return last_daily is None or last_daily < today


# -- semanal -----------------------------------------------------------------------------------


def weekly_window(today: date) -> tuple[date, date]:
    """Los 7 días que cubre un semanal hecho hoy: (desde, hasta), los dos incluidos."""
    return today - timedelta(days=WEEK_DAYS - 1), today


def weekly_due(
    today: date, weekday: int, last_weekly: date | None, portfolio_start: date | None
) -> bool:
    """Toca el semanal: el día elegido (0 = lunes … 6 = domingo) si hoy no hay uno, o si han
    pasado 7 días o más desde el último (o, sin ninguno, desde la creación de la cartera)."""
    if portfolio_start is None or today < portfolio_start:
        return False
    if last_weekly is not None and last_weekly >= today:
        return False  # ya hay uno de hoy
    if today.weekday() == weekday:
        return True
    referencia = last_weekly if last_weekly is not None else portfolio_start
    return (today - referencia).days >= WEEK_DAYS


def next_weekly(
    today: date, weekday: int, last_weekly: date | None, portfolio_start: date | None
) -> date | None:
    """El próximo día en que tocará el semanal (hoy, si toca hoy). None sin cartera."""
    if portfolio_start is None:
        return None
    dia = max(today, portfolio_start)
    for _ in range(WEEK_DAYS + 1):  # el día elegido llega como mucho en una semana
        if weekly_due(dia, weekday, last_weekly, portfolio_start):
            return dia
        dia += timedelta(days=1)
    return None


def weekly_rule_text(weekday: int) -> str:
    """«los domingos, o a los 7 días del último»."""
    return f"los {weekday_plural(weekday)}, o a los {WEEK_DAYS} días del último"


# -- mensual -----------------------------------------------------------------------------------


def is_complete_study(month: Month, made_on: date) -> bool:
    """Un estudio de `month` hecho el `made_on` es completo si ese mes ya había terminado; si
    no, es parcial (el mes en curso, a mano)."""
    return made_on > month.last_day


def monthly_due(
    today: date, portfolio_start: date | None, completed: Iterable[Month]
) -> Month | None:
    """El mes que toca estudiar hoy, o None.

    El anterior al de hoy, si la cartera ya existía el día 1 de ese mes (el primer mes
    incompleto no sale solo) y todavía no tiene un estudio completo.
    """
    if portfolio_start is None:
        return None
    anterior = Month.of(today).previous
    if portfolio_start > anterior.first_day:
        return None
    if anterior in set(completed):
        return None
    return anterior


def next_monthly(today: date, portfolio_start: date | None) -> date | None:
    """El día en que tocará el próximo estudio automático: el día 1 del mes que sigue al primer
    mes completo de la cartera. None sin cartera."""
    if portfolio_start is None:
        return None
    primero = Month.of(portfolio_start)
    if portfolio_start > primero.first_day:
        primero = primero.next  # el primer mes incompleto no sale solo
    dia = max(primero.next.first_day, Month.of(today).next.first_day)
    return dia


def manual_months(today: date, portfolio_start: date | None) -> tuple[Month | None, Month]:
    """Lo que ofrece «Ejecutar ahora» del mensual: (mes anterior, mes en curso). El anterior es
    None si la cartera no existía entonces; a mano vale aunque sea el primer mes incompleto."""
    actual = Month.of(today)
    anterior = actual.previous
    if portfolio_start is None or portfolio_start > anterior.last_day:
        return None, actual
    return anterior, actual
