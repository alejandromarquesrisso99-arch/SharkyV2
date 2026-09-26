"""La parte pura del semanal y del mensual (core/reports.py, H10): veredictos válidos e
inválidos, lo que añaden a la tesis (sin tocar ningún número), las fuentes, el texto de Claude
de un semanal guardado y la variación del mes. Datos inventados."""

from datetime import UTC, date, datetime
from decimal import Decimal as D

from sharky.core.levels import from_json
from sharky.core.models import (
    Author,
    MandateState,
    NavSnapshot,
    Report,
    ReportKind,
    Thesis,
    ThesisEventKind,
)
from sharky.core.reports import (
    PositionVerdict,
    RawVerdict,
    Verdict,
    check_verdicts,
    month_change,
    parse_verdict,
    sources_section,
    strip_preamble,
    verdict_events,
    weekly_analysis,
)
from sharky.core.schedule import Month

AHORA = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
SAN = Thesis("SAN", "EUR", date(2026, 9, 1), entry_price=D("4.5"), stop=D("4.2"),
             target=D("6"), conviction=7, id=1)
AAPL = Thesis("AAPL", "USD", date(2026, 9, 1), stop=D("150"), target=D("230"), id=2)
POSICIONES = ["SAN", "AAPL", "IWDA"]
TESIS = {"SAN": SAN, "AAPL": AAPL}


def raw(ticker, que, stop=None, objetivo=None):
    return RawVerdict(ticker, que, "Motivo.", "Invalida.", stop, objetivo)


# -- veredictos --------------------------------------------------------------------------------


def test_el_veredicto_sin_distinguir_mayusculas():
    assert parse_verdict("mantener") is Verdict.HOLD
    assert parse_verdict("  **Cerrar** ") is Verdict.CLOSE
    assert parse_verdict("«AMPLIAR»") is Verdict.ADD
    assert parse_verdict("Reducir.") is Verdict.REDUCE
    assert parse_verdict("VENDER") is None and parse_verdict("") is None


def test_veredictos_validos():
    hecho = check_verdicts(
        [raw("san", "mantener", D("4.3")), raw("AAPL", "Cerrar"), raw("IWDA", "AMPLIAR")],
        POSICIONES, TESIS,
    )
    assert [(v.ticker, v.verdict, v.stop) for v in hecho.verdicts] == [
        ("SAN", Verdict.HOLD, D("4.3")), ("AAPL", Verdict.CLOSE, None),
        ("IWDA", Verdict.ADD, None),
    ]
    assert hecho.rejected == () and hecho.missing == ()


def test_veredictos_invalidos():
    hecho = check_verdicts(
        [raw("XYZ", "MANTENER"), raw("SAN", "VENDER"), raw("SAN", "MANTENER"),
         raw("SAN", "CERRAR"), raw("", "")],
        POSICIONES, TESIS,
    )
    motivos = [r.text for r in hecho.rejected]
    assert motivos == [
        "«MANTENER» para «XYZ»: no es una posición de la cartera.",
        "«VENDER» para «SAN»: no es MANTENER, REDUCIR, AMPLIAR ni CERRAR.",
        "«CERRAR» para «SAN»: veredicto repetido: vale el primero.",
        "«—» para «—»: no es una posición de la cartera.",
    ]
    assert [v.ticker for v in hecho.verdicts] == ["SAN"]
    assert hecho.missing == ("AAPL", "IWDA")


def test_cifras_que_no_valen_se_quitan_y_el_veredicto_se_queda():
    hecho = check_verdicts([
        raw("SAN", "MANTENER", D("-1"), D("7")),  # stop negativo: fuera; objetivo, vale
        raw("AAPL", "MANTENER", D("240")),  # por encima del objetivo vigente (230)
    ], POSICIONES, TESIS)
    san, aapl = hecho.verdicts
    assert (san.stop, san.target) == (None, D("7"))
    assert san.dropped == "el stop propuesto no es mayor que 0"
    assert (aapl.stop, aapl.target) == (None, None)
    assert aapl.dropped == "el stop no quedaría por debajo del objetivo"
    # Si también sube el objetivo, el conjunto sí vale.
    (bien,) = check_verdicts([raw("AAPL", "AMPLIAR", D("240"), D("300"))], POSICIONES,
                             TESIS).verdicts
    assert (bien.stop, bien.target, bien.dropped) == (D("240"), D("300"), "")


# -- lo que se añade a la tesis -----------------------------------------------------------------


def test_una_revision_y_una_propuesta_sin_tocar_la_tesis():
    v = PositionVerdict("SAN", Verdict.HOLD, "Sigue en pie.", "Que pierda el soporte.",
                        stop=D("4.3"), target=D("6"))
    revision, propuesta = verdict_events(v, SAN, AHORA, "septiembre de 2026", "2026-09")
    assert (revision.kind, revision.author) == (ThesisEventKind.REVIEW, Author.CLAUDE)
    assert revision.text == ("Estudio mensual de septiembre de 2026: MANTENER.\n"
                             "Motivo: Sigue en pie.\nQué lo invalidaría: Que pierda el soporte.")
    assert from_json(revision.new_value) == {"veredicto": "MANTENER", "estudio": "2026-09"}
    assert (propuesta.kind, propuesta.author) == (ThesisEventKind.PROPOSAL, Author.CLAUDE)
    # Solo el stop cambia (el objetivo propuesto es el vigente), en la divisa de los niveles.
    assert from_json(propuesta.new_value) == {"stop": "4.3", "divisa": "EUR"}
    assert from_json(propuesta.old_value) == {"stop": "4.2"}
    assert propuesta.ref_event_id is None and revision.ref_event_id is None
    assert SAN.stop == D("4.2")  # la tesis no se toca


def test_sin_cifras_nuevas_solo_la_revision():
    v = PositionVerdict("SAN", Verdict.CLOSE, "Stop roto.", "", stop=D("4.2"))
    (revision,) = verdict_events(v, SAN, AHORA, "agosto de 2026", "2026-08")
    assert revision.text == "Estudio mensual de agosto de 2026: CERRAR.\nMotivo: Stop roto."


# -- el semanal ---------------------------------------------------------------------------------


def test_lo_de_antes_de_la_primera_seccion_sobra():
    assert strip_preamble("Voy a buscar.\n\n## SAN\nNoticia.") == "## SAN\nNoticia."
    assert strip_preamble("Sin títulos.") == "Sin títulos."
    assert strip_preamble("### Sub\ntexto\n## SAN\nx") == "## SAN\nx"


def test_las_fuentes():
    texto = sources_section([("https://ejemplo.es/a", "Título [con corchetes]"),
                             ("https://ejemplo.es/b (x)", "Otra")])
    assert texto == ("## Fuentes\n\n- [Título \\[con corchetes\\]](https://ejemplo.es/a)\n"
                     "- [Otra](<https://ejemplo.es/b (x)>)")
    assert sources_section([]) == "## Fuentes\n\nClaude no ha citado fuentes."


def test_el_analisis_de_un_semanal_guardado():
    md = ("**Estado:** Óptimo · drawdown 0,0 %\n\n## SAN\nNoticia.\n\n## Fuentes (de Claude)\n"
          "texto\n\n## Conclusión de la semana\n- Ojo.\n\n## Fuentes\n\n- [a](https://a.es)\n\n"
          "---\n\n## Datos de la semana\n\ntabla")
    informe = Report(ReportKind.WEEKLY, "2026-09-27", AHORA, md, True, "- Ojo.")
    assert weekly_analysis(informe) == ("## SAN\nNoticia.\n\n## Fuentes (de Claude)\ntexto\n\n"
                                        "## Conclusión de la semana\n- Ojo.")
    sin_ia = Report(ReportKind.WEEKLY, "2026-09-27", AHORA, "…", False, "- Stop en SAN.")
    assert weekly_analysis(sin_ia) == "(sin análisis de IA)\n- Stop en SAN."


# -- la variación del mes -----------------------------------------------------------------------


def foto(dia, valor, fiable=True):
    return NavSnapshot(dia, D("1000"), D("0"), D("10"), D(valor), D("110"), D("0"),
                       MandateState.OPTIMAL, D("1"), fiable)


def test_la_variacion_del_mes_sale_de_las_fotos_fiables():
    fotos = [foto(date(2026, 8, 31), "100"), foto(date(2026, 9, 10), "104"),
             foto(date(2026, 9, 30), "110"), foto(date(2026, 10, 1), "90"),
             foto(date(2026, 9, 29), "50", fiable=False)]
    cambio = month_change(fotos, Month(2026, 9), date(2026, 10, 1))
    assert (cambio.start.snapshot_date, cambio.end.snapshot_date) == (date(2026, 8, 31),
                                                                      date(2026, 9, 30))
    assert cambio.fraction == D("0.1")
    # A mitad de mes (parcial): hasta hoy.
    parcial = month_change(fotos, Month(2026, 9), date(2026, 9, 15))
    assert parcial.end.snapshot_date == date(2026, 9, 10)
    # Cartera creada en el mes: desde su primera foto fiable.
    creada = month_change(fotos[1:3], Month(2026, 9), date(2026, 10, 1))
    assert creada.start.snapshot_date == date(2026, 9, 10)
    assert month_change(fotos[1:2], Month(2026, 9), date(2026, 10, 1)) is None
