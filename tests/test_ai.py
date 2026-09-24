"""«Probar clave»: `models.list` con un cliente de Claude falso, sin red y sin clave real."""

import logging

import anthropic
import pytest

from fakes import api_status_error, connection_error, timeout_error
from sharky.services.ai import KEY_CHECK_RETRIES, KEY_CHECK_TIMEOUT_S, KeyStatus, check_api_key

CLAVE = "sk-ant-inventada-0000"


def test_clave_valida(claude_falso):
    resultado = check_api_key(f"  {CLAVE}  ")
    assert resultado.status is KeyStatus.VALID and resultado.usable
    # Una sola llamada barata, con la clave limpia, poco tiempo de espera y cerrando el cliente.
    assert claude_falso.creados == [
        {"api_key": CLAVE, "timeout": KEY_CHECK_TIMEOUT_S, "max_retries": KEY_CHECK_RETRIES}
    ]
    assert claude_falso.llamadas == [("models.list", {"limit": 1})]
    assert claude_falso.cerrados == 1


@pytest.mark.parametrize(
    "error",
    [
        api_status_error(anthropic.AuthenticationError, 401),
        api_status_error(anthropic.PermissionDeniedError, 403),
    ],
    ids=["401", "403"],
)
def test_clave_rechazada(claude_falso, error):
    claude_falso.error = error
    resultado = check_api_key(CLAVE)
    assert resultado.status is KeyStatus.INVALID and not resultado.usable
    assert "Clave no válida" in resultado.message


@pytest.mark.parametrize("error", [connection_error(), timeout_error()], ids=["red", "espera"])
def test_sin_conexion_se_guarda_sin_comprobar(claude_falso, error):
    claude_falso.error = error
    resultado = check_api_key(CLAVE)
    assert resultado.status is KeyStatus.OFFLINE and resultado.usable
    assert "sin comprobar" in resultado.message


@pytest.mark.parametrize(
    "error",
    [
        api_status_error(anthropic.RateLimitError, 429),
        api_status_error(anthropic.InternalServerError, 500),
    ],
    ids=["429", "500"],
)
def test_otros_errores_no_la_invalidan(claude_falso, error):
    claude_falso.error = error
    resultado = check_api_key(CLAVE)
    assert resultado.status is KeyStatus.UNVERIFIED and resultado.usable
    assert str(error.status_code) in resultado.message


def test_un_fallo_inesperado_tampoco_revienta(claude_falso):
    claude_falso.error = RuntimeError("algo raro")
    assert check_api_key(CLAVE).status is KeyStatus.UNVERIFIED


def test_clave_vacia_ni_se_intenta(claude_falso):
    assert check_api_key("   ").status is KeyStatus.INVALID
    assert claude_falso.creados == []


@pytest.mark.parametrize(
    "error",
    [None, api_status_error(anthropic.AuthenticationError, 401), connection_error()],
    ids=["valida", "rechazada", "sin_red"],
)
def test_la_clave_nunca_llega_al_registro(claude_falso, caplog, error):
    claude_falso.error = error
    with caplog.at_level(logging.DEBUG):
        check_api_key(CLAVE)
    assert caplog.records  # algo se registra…
    assert CLAVE not in caplog.text  # …pero nunca la clave
