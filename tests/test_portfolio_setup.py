"""Crear la cartera inicial: una sola transacción, o todo o nada (GUIA §5.2)."""

from datetime import date
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from sharky.core.csv_import import TEMPLATE_CSV, build_opening, parse_positions_csv
from sharky.core.ledger import build_ledger
from sharky.core.models import CashKind, CashMovement
from sharky.services import secrets
from sharky.services.repositories import (
    AssetRepository,
    CashMovementRepository,
    NavSnapshotRepository,
    NotInTransactionError,
    PortfolioExistsError,
    TradeRepository,
    create_portfolio,
    has_portfolio,
)
from sharky.services.settings import Settings, SettingsStore
from sharky.ui.wizard import SetupRequest, apply_setup

HOY = date(2026, 9, 24)
CLAVE = "sk-ant-inventada-1234"


def apertura_de_plantilla(efectivo: str = "2900"):
    lectura = parse_positions_csv(TEMPLATE_CSV.encode())
    return build_opening(lectura.positions, D(efectivo), HOY, "Trade Republic")


def nada_escrito(db) -> bool:
    conn = db.connection()
    return (
        not has_portfolio(conn)
        and AssetRepository(conn).list_all() == []
        and NavSnapshotRepository(conn).list_all() == []
    )


# -- repositorio ------------------------------------------------------------------------


def test_una_base_de_datos_nueva_no_tiene_cartera(db):
    assert has_portfolio(db.connection()) is False


def test_crear_la_cartera_guarda_todo(db):
    apertura = apertura_de_plantilla()
    with db.transaction() as conn:
        create_portfolio(conn, apertura)
    conn = db.connection()
    assert has_portfolio(conn)
    assert [a.ticker for a in AssetRepository(conn).list_all()] == ["AAPL", "IWDA", "SAN"]
    assert AssetRepository(conn).get("AAPL").currency == "USD"
    libro = build_ledger(TradeRepository(conn).list_all())
    assert {t: p.avg_cost_eur for t, p in libro.positions.items()} == {
        "AAPL": D("150"), "SAN": D("4.5"), "IWDA": D("80")
    }
    assert CashMovementRepository(conn).balance() == D("2900")
    foto = NavSnapshotRepository(conn).get(HOY)
    assert foto.unit_value == D("100") and foto.nav_eur == D("5700")
    assert foto.reliable is False


def test_solo_efectivo_tambien_es_una_cartera(db):
    with db.transaction() as conn:
        create_portfolio(conn, build_opening([], D("500"), HOY))
    conn = db.connection()
    assert has_portfolio(conn)
    assert TradeRepository(conn).list_all() == []
    assert NavSnapshotRepository(conn).get(HOY).reliable is True


def test_hay_cartera_con_cualquier_movimiento(db):
    with db.transaction() as conn:
        CashMovementRepository(conn).add(CashMovement(HOY, CashKind.DEPOSIT, D("10")))
    assert has_portfolio(db.connection())


def test_fuera_de_una_transaccion_no_se_escribe(db):
    with pytest.raises(NotInTransactionError):
        create_portfolio(db.connection(), apertura_de_plantilla())
    assert nada_escrito(db)


def test_si_algo_falla_a_mitad_no_queda_nada(db, monkeypatch):
    def revienta(self, snapshot):
        raise RuntimeError("el disco se ha llenado justo ahora")

    monkeypatch.setattr(NavSnapshotRepository, "save", revienta)
    with pytest.raises(RuntimeError), db.transaction() as conn:
        create_portfolio(conn, apertura_de_plantilla())
    assert nada_escrito(db)
    assert TradeRepository(db.connection()).list_all() == []


def test_no_escribe_encima_de_una_cartera(db):
    with db.transaction() as conn:
        create_portfolio(conn, apertura_de_plantilla())
    with pytest.raises(PortfolioExistsError), db.transaction() as conn:
        create_portfolio(conn, build_opening([], D("1"), HOY))
    assert CashMovementRepository(db.connection()).balance() == D("2900")


# -- lo que guarda el asistente al confirmar --------------------------------------------


def peticion(**cambios) -> SetupRequest:
    datos = dict(
        opening=apertura_de_plantilla(),
        api_key=CLAVE,
        start_with_windows=False,
        broker="Mi Bróker",
    )
    datos.update(cambios)
    return SetupRequest(**datos)


def test_guarda_cartera_ajustes_y_clave(db, keyring_falso):
    almacen = SettingsStore()
    ajustes = Settings()
    resultado = apply_setup(db, almacen, ajustes, peticion())
    assert resultado.warnings == ()
    assert has_portfolio(db.connection())
    guardados = almacen.load()
    assert guardados.portfolio.broker == "Mi Bróker"
    assert guardados.automation.start_with_windows is False
    assert secrets.load_api_key() == CLAVE
    # Los ajustes que recibe no se tocan: se guarda una copia.
    assert ajustes == Settings()


def test_sin_clave_no_se_toca_el_administrador_de_credenciales(db, keyring_falso):
    keyring_falso.set_password(secrets.SERVICE, secrets.API_KEY_USER, "la-de-antes")
    apply_setup(db, SettingsStore(), Settings(), peticion(api_key=None))
    assert secrets.load_api_key() == "la-de-antes"


def test_si_la_transaccion_falla_no_se_guarda_nada_mas(db, keyring_falso, monkeypatch):
    def revienta(conn, opening):
        raise RuntimeError("fallo en la base de datos")

    monkeypatch.setattr("sharky.ui.wizard.create_portfolio", revienta)
    almacen = SettingsStore()
    with pytest.raises(RuntimeError):
        apply_setup(db, almacen, Settings(), peticion())
    assert nada_escrito(db)
    assert not almacen.path.exists()
    assert keyring_falso.almacen == {}


def test_un_broker_imposible_falla_antes_de_escribir(db, keyring_falso):
    with pytest.raises(ValidationError):
        apply_setup(db, SettingsStore(), Settings(), peticion(broker=""))
    assert nada_escrito(db)
    assert keyring_falso.almacen == {}


def test_si_los_ajustes_no_se_guardan_la_cartera_sigue_y_se_avisa(db, monkeypatch):
    def sin_disco(self, settings):
        raise OSError("disco lleno")

    monkeypatch.setattr(SettingsStore, "save", sin_disco)
    resultado = apply_setup(db, SettingsStore(), Settings(), peticion())
    assert has_portfolio(db.connection())
    assert len(resultado.warnings) == 1 and "ajustes" in resultado.warnings[0]
    assert secrets.load_api_key() == CLAVE


def test_si_la_clave_no_se_guarda_la_cartera_sigue_y_se_avisa(db):
    def sin_credenciales(clave):
        raise secrets.SecretsError("no responde")

    resultado = apply_setup(db, SettingsStore(), Settings(), peticion(), save_key=sin_credenciales)
    assert has_portfolio(db.connection())
    assert len(resultado.warnings) == 1 and "clave" in resultado.warnings[0]
