# Sharky

Aplicación de escritorio para Windows que vigila una cartera real de inversión en euros.

Tú compras y vendes en tu bróker y registras la operación en Sharky. Sharky valora la cartera a
precio de mercado, comprueba el mandato de riesgo y los stop-loss y objetivos de cada tesis, te
avisa cuando algo exige actuar y le pide a Claude tres informes: diario, semanal de noticias y
estudio mensual. Además tiene un radar de oportunidades. Sharky nunca envía órdenes a un bróker.

La especificación completa y única del proyecto es [GUIA_SHARKY.md](GUIA_SHARKY.md).

## Estado

En construcción. Último hito cerrado: **H2 — Instalador, CI y prueba en el otro PC**
(ventana con las siete secciones vacías, temas claro y oscuro, autocomprobación, ejecutable e
instalador).

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

```bash
uv run python scripts/build.py
```

Genera `dist\Sharky\Sharky.exe` (PyInstaller, modo carpeta, sin consola), lanza la
autocomprobación del propio ejecutable —sin red y con red— y, solo si sale limpia, compila
`dist\Sharky-Setup-<versión>.exe` con Inno Setup.

Hace falta Inno Setup 6. Si no está:

```bash
winget install --id JRSoftware.InnoSetup -e
```

El icono se regenera solo cuando cambie el dibujo:

```bash
uv run python scripts/make_icon.py
```

## Autocomprobación

El ejecutable sabe revisarse a sí mismo, sin abrir ninguna ventana:

```bash
dist\Sharky\Sharky.exe --selftest
dist\Sharky\Sharky.exe --selftest --online
```

Escribe el resultado en `%TEMP%\sharky_selftest.txt` y sale con 0 o 1. Un **FALLO** significa
que al programa le falta algo por dentro; un **AVISO** significa que algo de fuera (la red,
Yahoo) no ha respondido, y no cuenta como fallo.

## Cómo se instala

El instalador es por usuario: **no pide administrador** y no necesita Python. Deja el programa
en `%LOCALAPPDATA%\Programs\Sharky` y un acceso en el menú Inicio (el del escritorio es
opcional). Al desinstalar se borra el programa y el arranque con Windows, pero **nunca la
carpeta de datos**.

Windows avisará de que el instalador no está firmado: «Más información → Ejecutar de todas
formas».

## Datos

El programa y los datos viven separados. Los datos están en `%LOCALAPPDATA%\Sharky\` y nunca se
commitean. La variable de entorno `SHARKY_DATA_DIR` sustituye esa ruta (la usan los tests).
