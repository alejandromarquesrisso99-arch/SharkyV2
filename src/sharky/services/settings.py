"""Ajustes: `settings.json` en la carpeta de datos, validado con pydantic (GUIA §3 y §7, H3).

- Sin secretos: la clave de Claude vive en el Administrador de credenciales (secrets.py).
- Escritura atómica: se escribe un fichero temporal en la misma carpeta, se vuelca a disco y
  se pone en su sitio con `os.replace`. Un corte a mitad deja el fichero anterior intacto.
- Si falta, valores por defecto. Si está dañado (JSON roto o valores imposibles), valores por
  defecto, aviso en el registro y el fichero dañado se aparta como `settings.danado.json`
  para no perder lo que hubiera.
- Los porcentajes se guardan como se ven: `10.0` es un 10 %.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from sharky import paths

log = logging.getLogger(__name__)

DAMAGED_SUFFIX = ".danado.json"
REPLACE_ATTEMPTS = 5
REPLACE_PAUSE_S = 0.05

Effort = Literal["low", "medium", "high"]
ThemeChoice = Literal["claro", "oscuro", "sistema"]


class _Section(BaseModel):
    # Una clave que no se conoce (de una versión más nueva) no daña el fichero: se ignora.
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class MandateSettings(_Section):
    """El mandato de riesgo (GUIA §5.4). Estos valores son la única fuente."""

    max_asset_weight_optimal_pct: float = Field(10.0, gt=0, le=100)
    max_asset_weight_other_pct: float = Field(5.0, gt=0, le=100)
    max_sector_weight_pct: float = Field(25.0, gt=0, le=100)
    min_cash_optimal_pct: float = Field(15.0, ge=0, le=100)
    max_cash_optimal_pct: float = Field(30.0, ge=0, le=100)
    min_cash_other_pct: float = Field(30.0, ge=0, le=100)
    max_risk_per_trade_pct: float = Field(1.5, gt=0, le=100)
    min_reward_risk: float = Field(2.0, gt=0)
    breach_escalation_days: int = Field(7, ge=1)

    @model_validator(mode="after")
    def _cash_band(self) -> Self:
        if self.min_cash_optimal_pct > self.max_cash_optimal_pct:
            raise ValueError("el efectivo mínimo en Óptimo no puede superar al máximo")
        return self


class ActionAI(_Section):
    """Modelo y esfuerzo de una acción que llama a Claude."""

    model: str = Field(min_length=1)
    effort: Effort


class ModelPrice(_Section):
    """Precio de un modelo en USD por millón de tokens."""

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)


def _default_prices() -> dict[str, ModelPrice]:
    return {
        "claude-sonnet-5": ModelPrice(input_per_mtok=2.0, output_per_mtok=10.0),
        "claude-opus-5": ModelPrice(input_per_mtok=5.0, output_per_mtok=25.0),
        "claude-haiku-4-5": ModelPrice(input_per_mtok=1.0, output_per_mtok=5.0),
    }


class AISettings(_Section):
    """Claude (GUIA §5.7)."""

    daily: ActionAI = Field(
        default_factory=lambda: ActionAI(model="claude-sonnet-5", effort="low")
    )
    weekly: ActionAI = Field(
        default_factory=lambda: ActionAI(model="claude-sonnet-5", effort="medium")
    )
    monthly: ActionAI = Field(
        default_factory=lambda: ActionAI(model="claude-sonnet-5", effort="high")
    )
    explorer: ActionAI = Field(
        default_factory=lambda: ActionAI(model="claude-opus-5", effort="high")
    )
    prices: dict[str, ModelPrice] = Field(default_factory=_default_prices)
    web_search_per_1000_usd: float = Field(10.0, ge=0)
    monthly_budget_usd: float = Field(10.0, ge=0)


class RadarSettings(_Section):
    """Radar (GUIA §5.8): vigencia de las alertas y umbrales del filtro."""

    alert_validity_days: int = Field(30, ge=1)
    min_drop_from_high_pct: float = Field(10.0, ge=0, lt=100)
    max_drop_from_high_pct: float = Field(40.0, gt=0, lt=100)
    min_stop_distance_pct: float = Field(8.0, gt=0, lt=100)
    max_stop_distance_pct: float = Field(15.0, gt=0, lt=100)

    @model_validator(mode="after")
    def _ranges(self) -> Self:
        if self.min_drop_from_high_pct >= self.max_drop_from_high_pct:
            raise ValueError("la caída mínima desde máximos tiene que ser menor que la máxima")
        if self.min_stop_distance_pct >= self.max_stop_distance_pct:
            raise ValueError("la distancia mínima del stop tiene que ser menor que la máxima")
        return self


class AutomationSettings(_Section):
    """Automatización (GUIA §5.9). El inicio con Windows se aplica en H12."""

    start_with_windows: bool = True
    weekly_report_weekday: int = Field(6, ge=0, le=6)  # 0 = lunes … 6 = domingo


class AppearanceSettings(_Section):
    """Apariencia (GUIA §5.10)."""

    theme: ThemeChoice = "sistema"


class Settings(_Section):
    """Todos los ajustes. Cada sección vuelve a sus valores por defecto con `Sección()`."""

    mandate: MandateSettings = Field(default_factory=MandateSettings)
    ai: AISettings = Field(default_factory=AISettings)
    radar: RadarSettings = Field(default_factory=RadarSettings)
    automation: AutomationSettings = Field(default_factory=AutomationSettings)
    appearance: AppearanceSettings = Field(default_factory=AppearanceSettings)


def atomic_write_text(path: Path, text: str) -> None:
    """Escribe `text` en `path` sin dejar nunca un fichero a medias."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as fichero:
            fichero.write(text)
            fichero.flush()
            os.fsync(fichero.fileno())
        _replace(Path(temporal), path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporal)
        raise


def _replace(origen: Path, destino: Path) -> None:
    # En Windows, un antivirus o el indexador pueden tener el destino abierto un instante.
    for intento in range(REPLACE_ATTEMPTS):
        try:
            os.replace(origen, destino)
            return
        except PermissionError:
            if intento == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_PAUSE_S)


class SettingsStore:
    """Lee y guarda `settings.json`."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        # Sin caché: la carpeta de datos puede cambiar (SHARKY_DATA_DIR, tests).
        return self._path if self._path is not None else paths.settings_path()

    def load(self) -> Settings:
        """Los ajustes guardados, o los de por defecto si faltan o están dañados."""
        ruta = self.path
        try:
            texto = ruta.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.info("No hay %s todavía: se usan los valores por defecto", ruta.name)
            return Settings()
        except OSError as error:
            log.warning("No se puede leer %s (%s): se usan los valores por defecto", ruta, error)
            return Settings()
        try:
            return Settings.model_validate_json(texto)
        except (ValidationError, ValueError) as error:
            apartado = self._set_aside(ruta)
            log.warning(
                "%s está dañado: se usan los valores por defecto%s. Motivo: %s",
                ruta.name,
                f" y el fichero dañado se ha apartado como {apartado.name}" if apartado else "",
                _short_reason(error),
            )
            return Settings()

    def save(self, settings: Settings) -> None:
        texto = json.dumps(settings.model_dump(mode="json"), ensure_ascii=False, indent=2)
        atomic_write_text(self.path, texto + "\n")
        log.info("Ajustes guardados en %s", self.path)

    @staticmethod
    def _set_aside(ruta: Path) -> Path | None:
        destino = ruta.with_name(ruta.stem + DAMAGED_SUFFIX)
        try:
            _replace(ruta, destino)
        except OSError:
            log.exception("No se ha podido apartar %s", ruta)
            return None
        return destino


def _short_reason(error: Exception) -> str:
    if isinstance(error, ValidationError):
        primeros = error.errors()[:3]
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'fichero'}: {e['msg']}" for e in primeros
        )
    return str(error)
