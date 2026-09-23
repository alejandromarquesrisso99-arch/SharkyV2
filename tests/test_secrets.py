"""La clave de Claude: en el Administrador de credenciales (aquí, el falso) y nunca en el log."""

import logging

import pytest
from keyring.errors import KeyringError

from sharky.services import secrets
from sharky.services.secrets import (
    API_KEY_USER,
    SERVICE,
    SecretsError,
    delete_api_key,
    has_api_key,
    load_api_key,
    save_api_key,
)

CLAVE_INVENTADA = "sk-ant-api03-ESTO-NO-ES-UNA-CLAVE-DE-VERDAD-0123456789"


def test_el_servicio_se_llama_sharky():
    assert SERVICE == "Sharky"


def test_guardar_leer_y_borrar(keyring_falso):
    assert load_api_key() is None
    assert not has_api_key()

    save_api_key(f"  {CLAVE_INVENTADA}\n")  # lo que venga pegado con espacios, limpio
    assert keyring_falso.almacen == {(SERVICE, API_KEY_USER): CLAVE_INVENTADA}
    assert load_api_key() == CLAVE_INVENTADA
    assert has_api_key()

    assert delete_api_key() is True
    assert load_api_key() is None
    assert keyring_falso.almacen == {}


def test_borrar_sin_clave_no_da_error():
    assert delete_api_key() is False


def test_una_clave_vacia_no_se_guarda(keyring_falso):
    with pytest.raises(ValueError):
        save_api_key("   ")
    assert keyring_falso.almacen == {}


def test_guardar_otra_clave_sustituye_a_la_anterior():
    save_api_key("sk-ant-primera-inventada")
    save_api_key("sk-ant-segunda-inventada")
    assert load_api_key() == "sk-ant-segunda-inventada"


def test_la_clave_nunca_aparece_en_el_registro(caplog):
    with caplog.at_level(logging.DEBUG):
        save_api_key(CLAVE_INVENTADA)
        load_api_key()
        has_api_key()
        delete_api_key()
    assert caplog.records  # algo se ha registrado…
    assert CLAVE_INVENTADA not in caplog.text  # …pero nunca la clave
    assert "sk-ant" not in caplog.text


class KeyringRoto:
    """Un Administrador de credenciales que falla y mete la clave en el mensaje de error."""

    def set_password(self, servicio, usuario, clave):
        raise KeyringError(f"fallo guardando {clave}")

    def get_password(self, servicio, usuario):
        raise KeyringError("fallo leyendo")

    def delete_password(self, servicio, usuario):
        raise KeyringError("fallo borrando")


def test_si_el_administrador_falla_la_app_sigue_sin_ia_y_sin_filtrar_la_clave(
    monkeypatch, caplog
):
    monkeypatch.setattr(secrets, "keyring", KeyringRoto())
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SecretsError) as error:
            save_api_key(CLAVE_INVENTADA)
        assert load_api_key() is None  # sin clave legible, la app funciona sin IA
        with pytest.raises(SecretsError):
            delete_api_key()
    assert CLAVE_INVENTADA not in caplog.text
    assert CLAVE_INVENTADA not in str(error.value)
    assert error.value.__cause__ is None  # tampoco viaja en la excepción encadenada
