"""Ajustes: valores de la guía, ida y vuelta, escritura atómica y ficheros dañados."""

import json
import logging

import pytest
from pydantic import ValidationError

from sharky import paths
from sharky.services import settings as settings_module
from sharky.services.settings import (
    MandateSettings,
    RadarSettings,
    Settings,
    SettingsStore,
    atomic_write_text,
)


@pytest.fixture
def almacen(carpeta_de_datos):
    return SettingsStore()


def test_los_valores_por_defecto_son_los_de_la_guia():
    s = Settings()
    m = s.mandate  # GUIA §5.4
    assert m.max_asset_weight_optimal_pct == 10.0
    assert m.max_asset_weight_other_pct == 5.0
    assert m.max_sector_weight_pct == 25.0
    assert (m.min_cash_optimal_pct, m.max_cash_optimal_pct) == (15.0, 30.0)
    assert m.min_cash_other_pct == 30.0
    assert m.max_risk_per_trade_pct == 1.5
    assert m.min_reward_risk == 2.0
    assert m.breach_escalation_days == 7

    ia = s.ai  # GUIA §5.7
    assert (ia.daily.model, ia.daily.effort) == ("claude-sonnet-5", "low")
    assert (ia.weekly.model, ia.weekly.effort) == ("claude-sonnet-5", "medium")
    assert (ia.monthly.model, ia.monthly.effort) == ("claude-sonnet-5", "high")
    assert (ia.explorer.model, ia.explorer.effort) == ("claude-opus-5", "high")
    precios = {k: (v.input_per_mtok, v.output_per_mtok) for k, v in ia.prices.items()}
    assert precios == {
        "claude-sonnet-5": (2.0, 10.0),
        "claude-opus-5": (5.0, 25.0),
        "claude-haiku-4-5": (1.0, 5.0),
    }
    assert ia.web_search_per_1000_usd == 10.0
    assert ia.monthly_budget_usd == 10.0

    r = s.radar  # GUIA §5.8
    assert r.alert_validity_days == 30
    assert (r.min_drop_from_high_pct, r.max_drop_from_high_pct) == (10.0, 40.0)
    assert (r.min_stop_distance_pct, r.max_stop_distance_pct) == (8.0, 15.0)

    assert s.automation.start_with_windows is True  # GUIA §5.2: la casilla sale marcada
    assert s.automation.weekly_report_weekday == 6  # domingo
    assert s.appearance.theme == "sistema"  # GUIA §5.10: el de Windows, por defecto
    assert s.portfolio.broker == "Trade Republic"  # GUIA §5.2


def test_sin_fichero_salen_los_valores_por_defecto(almacen, caplog):
    with caplog.at_level(logging.WARNING):
        assert almacen.load() == Settings()
    assert not caplog.records  # que falte no es un problema: es el primer arranque


def test_guardar_y_leer_da_lo_mismo(almacen):
    ajustes = Settings()
    ajustes.mandate.max_sector_weight_pct = 20.0
    ajustes.ai.monthly_budget_usd = 25.5
    ajustes.ai.daily.model = "claude-haiku-4-5"
    ajustes.radar.alert_validity_days = 45
    ajustes.automation.weekly_report_weekday = 4
    ajustes.appearance.theme = "oscuro"
    ajustes.portfolio.broker = "Bróker de José"
    almacen.save(ajustes)
    assert almacen.path == paths.settings_path()
    assert almacen.load() == ajustes


def test_el_fichero_se_puede_leer_y_no_guarda_ningun_secreto(almacen):
    almacen.save(Settings())
    datos = json.loads(almacen.path.read_text(encoding="utf-8"))
    assert set(datos) == {"mandate", "ai", "radar", "automation", "appearance", "portfolio"}
    texto = almacen.path.read_text(encoding="utf-8").lower()
    for palabra in ("api_key", "clave", "sk-ant", "password", "secret"):
        assert palabra not in texto


def test_la_escritura_no_deja_temporales(almacen):
    almacen.save(Settings())
    almacen.save(Settings())
    assert [p.name for p in almacen.path.parent.iterdir()] == ["settings.json"]


def test_si_la_escritura_falla_el_fichero_anterior_queda_intacto(almacen, monkeypatch):
    anterior = Settings()
    anterior.appearance.theme = "claro"
    almacen.save(anterior)
    texto_anterior = almacen.path.read_text(encoding="utf-8")

    def falla(*_args):
        raise OSError("disco lleno (simulado)")

    monkeypatch.setattr(settings_module.os, "replace", falla)
    nuevo = Settings()
    nuevo.appearance.theme = "oscuro"
    with pytest.raises(OSError):
        almacen.save(nuevo)
    assert almacen.path.read_text(encoding="utf-8") == texto_anterior
    assert [p.name for p in almacen.path.parent.iterdir()] == ["settings.json"]


def test_un_bloqueo_momentaneo_de_windows_se_reintenta(tmp_path, monkeypatch):
    original = settings_module.os.replace
    intentos = []

    def bloqueado_una_vez(origen, destino):
        intentos.append(1)
        if len(intentos) == 1:
            raise PermissionError("en uso por otro proceso (simulado)")
        original(origen, destino)

    monkeypatch.setattr(settings_module.os, "replace", bloqueado_una_vez)
    monkeypatch.setattr(settings_module, "REPLACE_PAUSE_S", 0)
    destino = tmp_path / "prueba.json"
    atomic_write_text(destino, "hola")
    assert destino.read_text(encoding="utf-8") == "hola"
    assert len(intentos) == 2


@pytest.mark.parametrize(
    "contenido",
    [
        "{ esto no es JSON",
        "",
        "[1, 2, 3]",
        '{"mandate": {"max_sector_weight_pct": -5}}',
        '{"mandate": {"min_cash_optimal_pct": 40, "max_cash_optimal_pct": 30}}',
        '{"appearance": {"theme": "morado"}}',
        '{"ai": {"daily": {"model": "claude-sonnet-5", "effort": "extremo"}}}',
        '{"radar": {"min_stop_distance_pct": 20, "max_stop_distance_pct": 10}}',
    ],
    ids=["json-roto", "vacio", "no-es-objeto", "negativo", "banda-al-reves", "tema-raro",
         "esfuerzo-raro", "rango-al-reves"],
)
def test_ajustes_danados_valores_por_defecto_y_aviso(almacen, caplog, contenido):
    almacen.path.parent.mkdir(parents=True, exist_ok=True)
    almacen.path.write_text(contenido, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sharky.services.settings"):
        assert almacen.load() == Settings()
    avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(avisos) == 1
    assert "dañado" in avisos[0].getMessage()
    assert "valores por defecto" in avisos[0].getMessage()


def test_el_fichero_danado_se_aparta_para_no_perderlo(almacen):
    almacen.path.parent.mkdir(parents=True, exist_ok=True)
    almacen.path.write_text("{ mandato a medio escribir", encoding="utf-8")
    almacen.load()
    apartado = almacen.path.with_name("settings.danado.json")
    assert apartado.read_text(encoding="utf-8") == "{ mandato a medio escribir"
    assert not almacen.path.exists()
    almacen.save(Settings())  # y se puede volver a guardar con normalidad
    assert almacen.load() == Settings()


def test_una_clave_desconocida_no_dana_el_fichero(almacen, caplog):
    almacen.path.parent.mkdir(parents=True, exist_ok=True)
    almacen.path.write_text(
        '{"appearance": {"theme": "oscuro", "de_una_version_futura": 1}, "otra_cosa": true}',
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        ajustes = almacen.load()
    assert ajustes.appearance.theme == "oscuro"
    assert not caplog.records


def test_lo_que_falta_se_completa_con_los_valores_por_defecto(almacen):
    almacen.path.parent.mkdir(parents=True, exist_ok=True)
    almacen.path.write_text('{"mandate": {"max_sector_weight_pct": 20}}', encoding="utf-8")
    ajustes = almacen.load()
    assert ajustes.mandate.max_sector_weight_pct == 20
    assert ajustes.mandate.max_asset_weight_optimal_pct == 10.0
    assert ajustes.ai == Settings().ai


def test_asignar_un_valor_imposible_se_rechaza():
    ajustes = Settings()
    with pytest.raises(ValidationError):
        ajustes.mandate.max_risk_per_trade_pct = 0
    with pytest.raises(ValidationError):
        ajustes.appearance.theme = "morado"
    with pytest.raises(ValidationError):
        ajustes.portfolio.broker = ""


def test_restaurar_una_seccion_a_sus_valores_por_defecto():
    """Lo que hará el botón «Restaurar» del mandato (GUIA §5.4)."""
    ajustes = Settings()
    ajustes.mandate.max_sector_weight_pct = 12.0
    ajustes.radar.alert_validity_days = 3
    ajustes.mandate = MandateSettings()
    ajustes.radar = RadarSettings()
    assert ajustes == Settings()
