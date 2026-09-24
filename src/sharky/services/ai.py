"""El cliente de Claude: el único módulo del programa que habla con la API de Anthropic
(GUIA §3 y §5.7).

Por ahora solo tiene «Probar clave», del asistente de primer arranque (H4): una llamada a
`models.list`, que no gasta tokens. Los informes llegan aquí mismo en H9.

La clave nunca se registra: de los errores solo se anotan el tipo y el código HTTP.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum

import anthropic

log = logging.getLogger(__name__)

#: «Probar clave» es una pregunta rápida: si la red no contesta pronto, se guarda sin comprobar.
KEY_CHECK_TIMEOUT_S = 15.0
KEY_CHECK_RETRIES = 1


class KeyStatus(StrEnum):
    VALID = "VALIDA"
    INVALID = "NO_VALIDA"
    OFFLINE = "SIN_CONEXION"
    UNVERIFIED = "SIN_COMPROBAR"


@dataclass(frozen=True)
class KeyCheck:
    """Resultado de «Probar clave», con un mensaje que se puede enseñar."""

    status: KeyStatus
    message: str

    @property
    def usable(self) -> bool:
        """Se puede guardar: es válida o no se ha podido comprobar (sin conexión, por ejemplo)."""
        return self.status is not KeyStatus.INVALID


def check_api_key(key: str) -> KeyCheck:
    """Comprueba la clave con `models.list` (GUIA §5.2). Nunca lanza.

    - 401 o 403 → no válida.
    - Sin red o sin respuesta a tiempo → sin conexión: se guardará sin comprobar.
    - Cualquier otra respuesta de error (429, 5xx…) → no se ha podido comprobar.
    """
    limpia = key.strip()
    if not limpia:
        return KeyCheck(KeyStatus.INVALID, "La clave está vacía.")
    cliente = None
    try:
        cliente = anthropic.Anthropic(
            api_key=limpia, timeout=KEY_CHECK_TIMEOUT_S, max_retries=KEY_CHECK_RETRIES
        )
        cliente.models.list(limit=1)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as error:
        log.warning("Claude ha rechazado la clave (HTTP %s)", error.status_code)
        return KeyCheck(
            KeyStatus.INVALID,
            "Clave no válida: Claude la ha rechazado. Revísala, o déjala en blanco para "
            "seguir sin IA.",
        )
    except anthropic.APIConnectionError as error:  # incluye el tiempo de espera agotado
        log.info("Sin conexión con la API de Claude (%s)", type(error).__name__)
        return KeyCheck(KeyStatus.OFFLINE, "Sin conexión: la clave se guardará sin comprobar.")
    except anthropic.APIStatusError as error:
        log.warning("La API de Claude ha respondido HTTP %s al probar la clave", error.status_code)
        return KeyCheck(
            KeyStatus.UNVERIFIED,
            f"Claude no ha podido comprobarla ahora (código {error.status_code}). La clave se "
            "guardará sin comprobar.",
        )
    except Exception as error:
        log.warning("No se ha podido probar la clave de Claude (%s)", type(error).__name__)
        return KeyCheck(
            KeyStatus.UNVERIFIED,
            "No se ha podido comprobar la clave. Se guardará sin comprobar.",
        )
    finally:
        if cliente is not None:
            with suppress(Exception):
                cliente.close()
    log.info("Clave de Claude comprobada: válida")
    return KeyCheck(KeyStatus.VALID, "Clave válida.")
