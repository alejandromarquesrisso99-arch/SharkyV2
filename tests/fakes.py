"""Dobles de prueba: los tests no tocan la red, ni la clave, ni el Administrador de credenciales."""

from __future__ import annotations

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
