# Sharky

Aplicación de escritorio para Windows que vigila una cartera real de inversión en euros.

Tú compras y vendes en tu bróker y registras la operación en Sharky. Sharky valora la cartera a
precio de mercado, comprueba el mandato de riesgo y los stop-loss y objetivos de cada tesis, te
avisa cuando algo exige actuar y le pide a Claude tres informes: diario, semanal de noticias y
estudio mensual. Además tiene un radar de oportunidades. Sharky nunca envía órdenes a un bróker.

La especificación completa y única del proyecto es [GUIA_SHARKY.md](GUIA_SHARKY.md).

## Estado

En construcción. Hito actual: **H0 — Repositorio y entorno**.

## Requisitos de desarrollo

- [uv](https://docs.astral.sh/uv/) (instala y fija Python 3.12 por ti)
- Git

El usuario final no necesita nada de esto: se instala con `Sharky-Setup-<versión>.exe`.

## Cómo probar

```bash
uv sync --locked      # entorno exacto de uv.lock
uv run ruff check .   # estilo
uv run pytest         # tests
```

## Cómo compilar

Disponible a partir del hito H1:

```bash
uv run python scripts/build.py
```

Genera `dist\Sharky\Sharky.exe` (PyInstaller, modo carpeta, sin consola) y, desde H2, el
instalador `dist\Sharky-Setup-<versión>.exe` (Inno Setup).

## Datos

El programa y los datos viven separados. Los datos están en `%LOCALAPPDATA%\Sharky\` y nunca se
commitean. La variable de entorno `SHARKY_DATA_DIR` sustituye esa ruta (la usan los tests).
