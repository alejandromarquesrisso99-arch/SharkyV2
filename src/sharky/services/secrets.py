"""La clave de Claude, en el Administrador de credenciales de Windows (GUIA §3).

Nunca va a un fichero ni al registro: aquí no se escribe la clave en ningún mensaje, y de los
errores de keyring solo se anota el tipo, por si alguno la llevara dentro.
"""

from __future__ import annotations

import logging

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

log = logging.getLogger(__name__)

SERVICE = "Sharky"
API_KEY_USER = "claude-api-key"

_backend_ready = False


class SecretsError(Exception):
    """El Administrador de credenciales no ha respondido. El mensaje se puede enseñar."""


def configure_backend() -> str:
    """Fija el backend de Windows en código: dentro del exe no se descubre solo (GUIA §3)."""
    from keyring.backends.Windows import WinVaultKeyring

    keyring.set_keyring(WinVaultKeyring())
    return type(keyring.get_keyring()).__name__


def _ensure_backend() -> None:
    global _backend_ready
    if not _backend_ready:
        configure_backend()
        _backend_ready = True


def save_api_key(key: str) -> None:
    """Guarda la clave (sustituye a la anterior, si la había)."""
    limpia = key.strip()
    if not limpia:
        raise ValueError("La clave está vacía")
    _ensure_backend()
    try:
        keyring.set_password(SERVICE, API_KEY_USER, limpia)
    except KeyringError as error:
        log.error("No se ha podido guardar la clave de Claude (%s)", type(error).__name__)
        raise SecretsError(
            "No se ha podido guardar la clave en el Administrador de credenciales"
        ) from None
    log.info("Clave de Claude guardada en el Administrador de credenciales")


def load_api_key() -> str | None:
    """La clave guardada, o None si no hay (o no se puede leer: la app sigue sin IA)."""
    _ensure_backend()
    try:
        clave = keyring.get_password(SERVICE, API_KEY_USER)
    except KeyringError as error:
        log.warning("No se ha podido leer la clave de Claude (%s)", type(error).__name__)
        return None
    return clave or None


def has_api_key() -> bool:
    return load_api_key() is not None


def delete_api_key() -> bool:
    """Borra la clave. Devuelve False si no había ninguna."""
    _ensure_backend()
    try:
        keyring.delete_password(SERVICE, API_KEY_USER)
    except PasswordDeleteError:
        return False
    except KeyringError as error:
        log.error("No se ha podido borrar la clave de Claude (%s)", type(error).__name__)
        raise SecretsError(
            "No se ha podido borrar la clave del Administrador de credenciales"
        ) from None
    log.info("Clave de Claude borrada del Administrador de credenciales")
    return True
