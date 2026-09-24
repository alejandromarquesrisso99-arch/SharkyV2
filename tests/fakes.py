"""Dobles de prueba: los tests no tocan la red, ni la clave, ni el Administrador de credenciales."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2
from keyring.errors import PasswordDeleteError


class FakeKeyring:
    """Administrador de credenciales de mentira, solo en memoria.

    Tiene la misma cara que el módulo `keyring` en lo que usa Sharky, así que se puede poner
    en su sitio con monkeypatch. Como el de verdad, borrar lo que no existe da error.
    """

    def __init__(self) -> None:
        self.almacen: dict[tuple[str, str], str] = {}
        self.backend_fijado = False

    def set_keyring(self, backend: object) -> None:
        self.backend_fijado = True

    def get_keyring(self) -> FakeKeyring:
        return self

    def set_password(self, servicio: str, usuario: str, clave: str) -> None:
        self.almacen[(servicio, usuario)] = clave

    def get_password(self, servicio: str, usuario: str) -> str | None:
        return self.almacen.get((servicio, usuario))

    def delete_password(self, servicio: str, usuario: str) -> None:
        if (servicio, usuario) not in self.almacen:
            raise PasswordDeleteError("Password not found")
        del self.almacen[(servicio, usuario)]


class FakeClaude:
    """Cliente de Claude de mentira: hace de `anthropic.Anthropic` y nunca sale a la red.

    Se usa como la clase (se llama con los mismos argumentos) y apunta con qué se creó cada
    cliente y qué se le pidió. Con `error`, `models.list` lanza esa excepción.
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.creados: list[dict] = []
        self.llamadas: list[tuple[str, dict]] = []
        self.cerrados = 0

    def __call__(self, **kwargs: object) -> FakeClaude:
        self.creados.append(kwargs)
        return self

    @property
    def models(self) -> FakeClaude:
        return self

    def list(self, **kwargs: object) -> list[SimpleNamespace]:
        self.llamadas.append(("models.list", kwargs))
        if self.error is not None:
            raise self.error
        return [SimpleNamespace(id="claude-sonnet-5")]

    def close(self) -> None:
        self.cerrados += 1


_PETICION = httpx2.Request("GET", "https://api.anthropic.com/v1/models")


def api_status_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    """Un error HTTP del SDK, como los que lanza de verdad (401, 403, 429, 500…)."""
    return cls(f"HTTP {status}", response=httpx2.Response(status, request=_PETICION), body=None)


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=_PETICION)


def timeout_error() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=_PETICION)
