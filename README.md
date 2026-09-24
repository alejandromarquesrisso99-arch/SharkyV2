# Sharky

Aplicación de escritorio para Windows que vigila una cartera real de inversión en euros.

Tú compras y vendes en tu bróker y registras la operación en Sharky. Sharky valora la cartera a
precio de mercado, comprueba el mandato de riesgo y los stop-loss y objetivos de cada tesis, te
avisa cuando algo exige actuar y le pide a Claude tres informes: diario, semanal de noticias y
estudio mensual. Además tiene un radar de oportunidades. Sharky nunca envía órdenes a un bróker.

La especificación completa y única del proyecto es [GUIA_SHARKY.md](GUIA_SHARKY.md).

## Estado

En construcción. Último hito cerrado: **H5 — Precios y Cartera** (precios reales de Yahoo
Finance por lotes y con reintentos, tipos de cambio a euros, procedencia de cada precio,
cobertura, pantalla Cartera con la exposición por sector y «Editar activo»). Antes: asistente
de primer arranque, base de datos con migraciones, libro derivado de las operaciones, ajustes,
clave en el Administrador de credenciales, copias de seguridad, ventana con las siete
secciones, temas claro y oscuro, autocomprobación, ejecutable, instalador y CI.

## Primer arranque

Mientras no hay cartera, Sharky abre un asistente en lugar de la ventana principal:

1. **Clave de Claude**, con «Probar clave» (no gasta nada). Se puede dejar en blanco.
2. **Tu cartera**: «Adjuntar CSV…» con vista previa y todos los errores a la vez, con su
   línea; «Guardar plantilla CSV…»; efectivo en euros y bróker. Se puede empezar solo con
   efectivo. El coste medio de cada posición se puede corregir en la vista previa.
3. **Resumen** y «Crear cartera».

Nada se escribe hasta «Crear cartera»; cancelar no deja rastro. El CSV lleva, en este orden,
`ticker;nombre;isin;unidades;coste_medio_eur;divisa;sector;simbolo;clase` (separador `;`, `,`
o tabulador, decimal con coma o punto, cabecera opcional, UTF-8 o ANSI de Excel). La plantilla
la genera el propio asistente.

## Cartera y precios

«Actualizar precios (gratis)» descarga el último cierre de cada posición (Yahoo Finance, en
lotes de 10 y hasta 3 reintentos si Yahoo limita) y los tipos de cambio a euros (`USDEUR=X`…;
las acciones de Londres cotizan en peniques, `GBp`, y valen la centésima parte de una libra).
Va en segundo plano, con barra de progreso y «Cancelar»: la ventana no se congela. No llama a
Claude.

Cada precio lleva su procedencia, y solo las dos primeras son fiables:

| Procedencia | Qué es |
| :--- | :--- |
| Mercado | Descargado en la última actualización |
| Caché | Guardado hace menos de 24 horas |
| Antiguo | El último conocido, más viejo |
| Coste | Nunca se obtuvo precio (por ejemplo, sin símbolo): se usa el coste medio |

Una posición es tan fiable como lo menos fiable entre su precio y su tipo de cambio. La
**cobertura** es la parte del patrimonio con valor fiable (el efectivo cuenta como fiable) y
se enseña siempre; por debajo del 90 %, la valoración no es fiable. Sin red, Sharky trabaja
con lo guardado y lo dice. Si Yahoo dice que un activo cotiza en otra divisa que la
declarada, manda Yahoo: se corrige y se avisa.

«Editar activo» cambia el símbolo de Yahoo (con «Probar», que enseña su último cierre, y
«Sugerir por ISIN»), el sector y la clase. Al cambiar el símbolo se borran los precios
guardados de ese activo, porque eran de otro valor.

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

```
sharky.db       fuente de verdad (SQLite, modo WAL, esquema versionado con migraciones)
settings.json   ajustes, sin secretos
logs\           sharky.log rotativo
backups\        copias de sharky.db (se guardan las 14 últimas)
cache\          caché del mercado
```

- Las posiciones no se guardan: se calculan a partir de las operaciones con coste medio
  ponderado. El efectivo es la suma de los movimientos de efectivo.
- La clave de Claude vive en el Administrador de credenciales de Windows (servicio `Sharky`),
  nunca en un fichero. Las copias de seguridad no la llevan.
- Ajustes → Datos: «Copia de seguridad ahora» y «Restaurar copia…». Restaurar pide
  confirmación, guarda antes una copia de lo que hay y reinicia Sharky.
- Si `settings.json` se estropea, Sharky arranca con los valores por defecto y aparta el
  fichero dañado como `settings.danado.json`.
