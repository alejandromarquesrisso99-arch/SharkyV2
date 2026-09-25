# Guía de reconstrucción de Sharky (para Claude Code)

> Versión 1 · 22/09/2026
> Esta guía es la única especificación del proyecto. Del Sharky anterior no se reutiliza nada: ni código, ni datos, ni estructura.

---

## 0. Cómo usar esta guía

1. Crea una carpeta vacía, sin espacios ni tildes y fuera de OneDrive, por ejemplo `C:\dev\sharky`, y copia dentro este fichero (`GUIA_SHARKY.md`).
2. Recomendado: crea en GitHub un repositorio **nuevo y privado** llamado `sharky` y dale la URL a Claude Code en el Hito 0.
3. Abre la carpeta en Claude Code y pega:
   > Lee GUIA_SHARKY.md completa. Vamos a ejecutar solo el Hito 0. Primero enséñame tu plan y espera mi OK. Al terminar, comprueba uno a uno los criterios de aceptación, dime el resultado de cada uno y haz commit. No empieces el siguiente hito.
4. Para cada hito siguiente: «Ejecuta el Hito N de GUIA_SHARKY.md con las mismas reglas». Los criterios que empiezan por «Manual» los compruebas tú; Claude Code te dirá cómo.
5. Si un criterio falla: «El criterio "…" del Hito N falla con este error: … Arréglalo sin salir del hito».
6. Si se te ocurre algo nuevo, apúntalo en §9 y sigue. No se construye nada fuera de esta guía hasta cerrar la versión 1.0.

No tienes que instalar nada a mano: Claude Code instalará uv, Python 3.12 e Inno Setup y te pedirá permiso. El otro PC solo necesitará el instalador `Sharky-Setup-x.y.z.exe`. La consola la usa Claude Code para programar; el usuario de Sharky no la ve nunca.

---

## 1. Qué es Sharky

Una aplicación de escritorio para Windows que vigila una cartera real de inversión en euros. Tú compras y vendes en tu bróker y registras la operación en Sharky. Sharky valora la cartera a precio de mercado, comprueba el mandato de riesgo y los stop-loss y objetivos de cada tesis, te avisa cuando algo exige actuar y le pide a Claude tres informes: diario, semanal de noticias y estudio mensual. Además tiene un radar de oportunidades: un explorador que busca ideas nuevas en la web con Claude y un filtro determinista que decide, con precios reales, si hoy ofrecen asimetría.

**Principios (no negociables)**

1. El código calcula todos los números; Claude solo interpreta. Sin clave, o si Claude falla, todo lo demás funciona y el informe lo indica.
2. Una posición es un hecho; una tesis es una opinión. NAV, pesos y drawdown salen solo del libro de operaciones.
3. Nada estimado se presenta como medido: cada precio lleva su procedencia.
4. Sharky nunca envía órdenes a un bróker.
5. Claude nunca cambia un número (stop, objetivo, convicción): propone, y el usuario aplica.
6. Programa y datos separados: el programa se instala; los datos viven en otra carpeta y nunca van a git.

**Fuera de alcance** (no construir, aunque parezca buena idea): conexión con brókers u órdenes automáticas · motor de rebalanceo · comité, departamentos, «energía» o «salud» · panel web, servidor local o navegador · consola o comandos para el usuario · Docker, Raspberry o servicio 24/7 · Obsidian como base de datos · importar datos del Sharky anterior · multiusuario, móvil o nube.

---

## 2. Reglas para Claude Code

En el Hito 0 se copia este bloque tal cual a `CLAUDE.md`:

```markdown
# CLAUDE.md — Sharky
- La especificación es GUIA_SHARKY.md. Si algo no está ahí, pregunta; no lo inventes.
- Trabaja solo en el hito pedido. Enseña el plan antes de empezar. Al acabar: ruff y pytest en
  verde (y, desde H1, build y selftest); criterios de aceptación comprobados uno a uno;
  commit "H<n>: <título>".
- No leas ni copies nada del proyecto Sharky anterior.
- Stack cerrado (GUIA §3). Ninguna dependencia nueva sin permiso.
- Capas: core (lógica pura: sin Qt, sin red, sin disco) <- services (base de datos, mercado, IA,
  Windows) <- ui (Qt). Nunca al revés.
- La fecha y la hora actuales se inyectan (reloj); core nunca llama a datetime.now().
- El usuario solo interactúa con la interfaz gráfica. Nada de print(): usa logging.
  Nunca registres la clave de la API.
- Red, IA y disco lento siempre en hilos de trabajo (QThreadPool). Los widgets solo se tocan
  desde el hilo de la interfaz (señales).
- Tests sin red, sin clave y sin tocar el Administrador de credenciales: usa los dobles de
  tests/fakes.py. Datos de prueba inventados.
- Nunca commitees datos: *.db, *.csv, .env, logs, dist/, build/.
- Rutas con pathlib: carpeta de datos (paths.py) o recursos del paquete. Nunca el directorio de
  trabajo ni rutas de usuario fijas.
- Interfaz, informes y mensajes en español; nombres de código en inglés.
- Cantidades: «1.234,56 €» y «12,3 %» al mostrar; unidades hasta 6 decimales; los campos
  numéricos aceptan coma o punto.
```

---

## 3. Decisiones técnicas

| Tema | Decisión |
|---|---|
| Lenguaje y entorno | Python 3.12 fijado con uv (`.python-version`, `requires-python = "==3.12.*"`, `uv.lock` versionado) |
| Interfaz | PySide6 (Qt Widgets, estilo Fusion) con tema claro y oscuro (§5.10); gráfico con pyqtgraph (Qt Charts está obsoleto desde Qt 6.10) |
| Datos | Un fichero SQLite (`sharky.db`) en la carpeta de datos |
| Ajustes | `settings.json` validado con pydantic v2 |
| Clave de Claude | Administrador de credenciales de Windows, vía `keyring` |
| Mercado | `yfinance` detrás de las interfaces `PriceProvider` y `FxProvider` |
| IA | SDK oficial `anthropic` en un único módulo cliente |
| Tests y estilo | pytest, pytest-qt (`qt_api = pyside6`), ruff |
| Ejecutable | PyInstaller en modo carpeta (onedir), sin consola, sin UPX |
| Instalador | Inno Setup, instalación por usuario y sin permisos de administrador |
| Arranque con Windows | Valor `Sharky` en `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, escrito por la propia app |
| CI | GitHub Actions en `windows-latest` |

Dependencias permitidas: PySide6, pyqtgraph, anthropic, yfinance, pydantic y keyring (con las suyas). Para desarrollo: pytest, pytest-qt, ruff y pyinstaller.

**Trampas conocidas** (resolverlas desde el principio):

- En una app sin consola, `sys.stdout` y `sys.stderr` valen `None`: redirigirlos a `os.devnull` al arrancar, antes de importar nada más.
- Al arrancar con Windows, el directorio de trabajo no es el del programa: rutas siempre absolutas.
- keyring dentro del exe: fijar el backend en código (`keyring.set_keyring(WinVaultKeyring())`) y en la spec añadir `collect_submodules("keyring.backends")` y `collect_submodules("win32ctypes")` a `hiddenimports`.
- PyInstaller: el script de entrada es `packaging/launcher.py` con imports absolutos (`from sharky.app import main`), no un `__main__.py` con imports relativos.
- yfinance: Yahoo limita las peticiones (descargas por lotes y reintentos con espera); su caché va a la carpeta de datos; las acciones de Londres cotizan en peniques (`GBp`).
- SQLite: una conexión por hilo, modo WAL, `busy_timeout` de 5 s y cada escritura en una transacción.
- Claude: el razonamiento gasta del mismo `max_tokens` que el texto; usar streaming siempre; la búsqueda web puede devolver `pause_turn`; la salida estructurada no admite citas.
- Windows SmartScreen avisa con instaladores sin firmar: es normal («Más información → Ejecutar de todas formas»).

---

## 4. Qué falló en el Sharky anterior y cómo se evita

| Antes | Ahora |
|---|---|
| Instalar exigía Python, git, entorno virtual, pip, un `init` por consola y scripts .bat/.ps1 | Un instalador; ni Python ni consola |
| Código, datos (`vault/`, `.env`) y `.venv` en la misma carpeta; el `.venv` copiado a otro PC no funciona | Programa en `%LOCALAPPDATA%\Programs\Sharky`; datos en `%LOCALAPPDATA%\Sharky` |
| Dependencias con `>=` y sin fichero de bloqueo | `uv.lock` versionado; mismas versiones en todos los PC |
| Libro de posiciones en una nota Markdown que el código leía y reescribía | SQLite con transacciones; el Markdown solo sirve para leer informes |
| Automatización con schtasks, .bat y PowerShell (políticas de ejecución, permisos) | La app se registra sola en el inicio de sesión |
| Informes de Claude cortados por `max_tokens` durante días (modo simulado) | Streaming, margen amplio, un reintento con menos esfuerzo y lo crítico calculado antes y aparte |
| Demasiadas ideas a medias | Alcance cerrado (§1); lo nuevo espera en §9 |

---

## 5. Especificación

### 5.1 Datos

Carpeta de datos `%LOCALAPPDATA%\Sharky\` (la variable de entorno `SHARKY_DATA_DIR` la sustituye; la usan los tests):

```
sharky.db       fuente de verdad
settings.json   ajustes (sin secretos)
logs\           sharky.log rotativo (1 MB × 5)
backups\        copias de sharky.db (se guardan las 14 últimas)
cache\          caché de yfinance
```

Tablas (esquema versionado con `PRAGMA user_version` y migraciones numeradas):

- `assets`: ticker (clave), nombre, isin, símbolo de Yahoo, divisa de cotización, clase (ACCION, ETF, ETC, CRIPTO), sector.
- `trades`: fecha, ticker, tipo (APERTURA, COMPRA, VENTA), unidades, precio, divisa, cambio a EUR, comisión EUR, importe EUR, stop, objetivo, divisa de los niveles, forzada, motivo.
- `cash_movements`: fecha, tipo (INICIAL, INGRESO, RETIRADA, DIVIDENDO, INTERES, COMISION, IMPUESTO, AJUSTE, OPERACION), importe EUR con signo, operación asociada, nota. El efectivo es la suma de esta tabla.
- `prices` (ticker, fecha, precio, divisa, procedencia, hora) y `fx_rates` (divisa, fecha, cambio a EUR, procedencia).
- `nav_snapshots`: una por día (la última valoración fiable del día sustituye a la anterior) con NAV, efectivo, participaciones, valor por participación, máximo, drawdown, estado, cobertura y si es fiable.
- `theses` y `thesis_events` (historial en el que solo se añade: creación, cambio de niveles, revisión, propuesta, cierre; autor USUARIO, CLAUDE o SISTEMA).
- `level_alerts`: fecha, ticker, tipo, precio y nivel en EUR, stop propuesto, criterio, si ya se avisó.
- `breaches`: regla + sujeto, abierta desde, vista por última vez, abierta.
- `watchlist`: ticker, nombre, símbolo de Yahoo, divisa, sector, quién lo añadió (USUARIO o EXPLORADOR) y fecha. Es el universo del radar; lo que ya tienes en cartera queda fuera.
- `alerts`: fecha, ticker, origen (EXPLORADOR o VIGILANCIA), estado (ACTIVA, EJECUTADA, EXPIRADA, DESCARTADA), precio y divisa del día del cálculo, stop, objetivo, ratio, caída desde el máximo de 52 semanas, peso máximo sugerido, resumen de la idea, motivo de descarte o de caducidad e informe de exploración asociado.
- `reports`: tipo (DIARIO, SEMANAL, MENSUAL, EXPLORACION), periodo, markdown, conclusión, posiciones a vigilar (solo el diario), si usó IA, modelo, esfuerzo, tokens, búsquedas, coste en USD y error.
- `runs`: cada paso de la rutina (inicio, fin, disparador, paso, OK/OMITIDO/ERROR, detalle).

Las posiciones no se guardan: se derivan de `trades` con coste medio ponderado (la comisión de compra suma al coste; la de venta resta del importe). PnL realizado = importe neto de la venta − unidades × coste medio. La app avisa de que ese cálculo no sirve para la declaración de la renta (en España se usa FIFO).

### 5.2 Primer arranque

Si no hay cartera en la base de datos, la app abre un asistente en lugar de la ventana principal. No escribe nada hasta confirmar; cancelar deja todo como estaba. Para empezar de cero, «Borrar cartera…» (Ajustes → Datos, §5.10) vacía la base de datos y el asistente vuelve a salir.

1. **Clave de Claude.** Una línea de presentación (Sharky no opera por ti ni es asesoramiento financiero), campo oculto y «Probar clave» (`models.list`, no gasta tokens; sin conexión, se guarda sin comprobar). Se puede omitir: la app funciona sin IA y lo indica en cada informe.
2. **Cartera.** «Adjuntar CSV…» con vista previa en tabla; todos los errores a la vez, con su número de línea; «Guardar plantilla CSV…». Debajo: efectivo en EUR y bróker (por defecto, «Trade Republic»). Se puede empezar sin posiciones (solo efectivo).
3. **Resumen.** Casilla «Iniciar Sharky al iniciar sesión en Windows» (marcada) y botón «Crear cartera».

Al confirmar, en una sola transacción: activos, operaciones APERTURA a coste medio, movimiento INICIAL de efectivo y primera foto del NAV (valor por participación = 100).

Formato del CSV: separador `;` (también `,` o tabulador), decimal con coma o punto, cabecera opcional, UTF-8 o ANSI de Excel.

| # | Columna | Obligatoria | Contenido |
|---|---|---|---|
| 1 | ticker | sí | Nombre corto sin espacios (`SAN`) |
| 2 | nombre | sí | Empresa o fondo |
| 3 | isin | no | 12 caracteres |
| 4 | unidades | sí | Mayor que 0, con decimales si los hay |
| 5 | coste_medio_eur | sí | Precio medio de compra por unidad, en EUR |
| 6 | divisa | sí | Divisa de cotización: EUR, USD, GBp, HKD… |
| 7 | sector | no | Texto sin espacios |
| 8 | simbolo | no | Símbolo de Yahoo Finance (`SAN.MC`) |
| 9 | clase | no | ACCION (por defecto), ETF, ETC o CRIPTO |

Validaciones: ticker `^[A-Za-z0-9][A-Za-z0-9_.-]*$` y sin repetir; ISIN `^[A-Z]{2}[A-Z0-9]{9}[0-9]$`; números mayores que 0; divisa de 3 letras (`GBX` y `GBp` se guardan como `GBp`); clase válida. Sin símbolo: aviso, no error (la posición se valora a coste hasta asignarlo; si hay ISIN, la app sugiere uno). La plantilla (Apéndice A) se genera desde una constante en código, así `*.csv` se ignora en git sin excepciones.

### 5.3 Precios y valoración

- Descarga por lotes con yfinance (último cierre de los últimos 5 días) y divisa real del proveedor. Si no coincide con la declarada, manda la del proveedor y se avisa.
- Cambio a EUR con el par `XXXEUR=X`; `GBp` = GBP / 100.
- Procedencia, por orden de preferencia: **MERCADO** (descargado ahora) · **CACHE** (guardado hace menos de 24 h) · **ANTIGUO** (último precio conocido, más viejo) · **COSTE** (nunca se obtuvo precio; se usa el coste medio). Solo MERCADO y CACHE son fiables. Jamás se usan precios de ejemplo.
- Cobertura = porcentaje del NAV con precio fiable. Se muestra siempre.
- Hasta 3 reintentos con espera si Yahoo limita; sin red, la app trabaja con lo guardado y lo indica.

### 5.4 Mandato y estados

Valores por defecto (editables en Ajustes, con botón «Restaurar»):

| Regla | Valor |
|---|---|
| Peso máximo por activo | 10 % en Óptimo; 5 % en los demás estados |
| Peso máximo por sector | 25 % |
| Efectivo | Entre 15 % y 30 % en Óptimo; mínimo 30 % en los demás estados |
| Riesgo máximo por operación (hasta el stop) | 1,5 % del NAV |
| Ratio beneficio/riesgo mínimo | 2,0 |
| Incumplimiento escalado | Abierto 7 días o más |

| Estado | Drawdown | Compras |
|---|---|---|
| 🟢 Óptimo | menos del 3 % | Permitidas |
| 🟡 Alerta | desde 3 % hasta menos del 8 % | Permitidas, con tope del 5 % por activo |
| 🔴 Cuidados intensivos | desde 8 % hasta menos del 20 % | Prohibidas (vender siempre se puede) |
| ⛔ Bloqueo | 20 % o más | Prohibidas; revisión completa de la cartera |

- El drawdown se mide sobre el **valor por participación** frente a su máximo, como en un fondo: empieza en 100. Ingresos y retiradas cambian el número de participaciones, no su valor: meter o sacar dinero no es ganar ni perder. Dividendos, intereses, comisiones, impuestos y ajustes sí cuentan.
- El estado y el máximo solo se actualizan con una valoración fiable (cobertura del 90 % o más). Si no, se mantienen los anteriores y se avisa.
- Auditoría: una fila por incumplimiento (activo, sector, efectivo, cobertura inferior al 100 %), con la corrección en euros («Reducir RHM al 10 %: vender unos 712 €») y los días que lleva abierta.

### 5.5 Operaciones

Se registra lo ya ejecutado en el bróker. Compra o venta: ticker (autocompletado; si es nuevo, pide nombre, ISIN, símbolo, divisa, sector y clase), unidades, precio, divisa, comisión en EUR y, en las compras, stop, objetivo y divisa de los niveles.

Validación de una compra, en este orden (la primera que falla se muestra con su motivo exacto):

1. Estado Cuidados intensivos o Bloqueo → rechazada.
2. Precio y unidades mayores que 0.
3. Stop < precio < objetivo, comparados en la misma divisa (convirtiendo si hace falta).
4. (objetivo − precio) / (precio − stop) ≥ ratio mínimo.
5. Peso final del activo ≤ tope del estado.
6. (precio − stop) × unidades, en EUR, ≤ riesgo máximo por operación.
7. Peso final del sector ≤ tope.
8. Efectivo después de la compra y la comisión ≥ 0 y ≥ mínimo del estado.

Una venta solo exige el punto 2 y no vender más de lo que hay. «Registrar igualmente» pide confirmación y un motivo; la operación queda marcada como forzada y el siguiente informe diario lo menciona.

En la misma transacción se guardan la operación y su movimiento de efectivo. Comprar un ticker sin tesis abre el editor de tesis con precio, stop y objetivo ya rellenos. Ampliar una posición con tesis: si el stop o el objetivo de la compra no coinciden con los de la tesis, la app pregunta si actualizar entrada (nuevo coste medio), stop y objetivo; la decisión queda en el historial. Vender toda la posición cierra la tesis con el PnL realizado y el motivo (si hoy saltó su stop, lo dice); una venta parcial no la cierra. Movimientos sueltos de efectivo: ingreso, retirada, dividendo, interés, comisión, impuesto y ajuste («mi saldo real es X» crea el ajuste que cuadra).

### 5.6 Tesis y niveles

Una tesis ACTIVA por posición (las cerradas se conservan): precio de entrada, divisa de los niveles, stop, objetivo, convicción de 1 a 10, fecha, estado (ACTIVA o CERRADA) y textos (por qué, catalizadores, riesgos, qué la invalidaría). Solo el usuario cambia números; cada cambio queda en el historial con el valor anterior y el nuevo. Las revisiones se añaden debajo; nunca sobrescriben.

Vigilancia, en cada valoración y sin IA, para cada tesis ACTIVA con posición abierta:

- Precio no fiable o sin tipo de cambio → **NO VERIFICABLE** (nunca «a salvo»).
- Comparación en EUR. Precio ≤ stop → **STOP**: salida obligatoria; tiene prioridad. Precio ≥ objetivo → **OBJETIVO**: aviso, no obliga a vender.
- En OBJETIVO se propone subir el stop al mayor de dos valores: el precio de entrada (break-even) o el precio actual − (entrada − stop), que mantiene el riesgo inicial. Sin entrada o sin stop: precio actual − 8 %. Nunca por debajo del stop vigente. Se muestra como propuesta con botón «Aplicar».
- STOP → ventana modal por encima de todo + notificación. OBJETIVO → notificación. Cada aviso se notifica una vez al día; en el Panel sigue visible hasta que se resuelve.

### 5.7 Informes con Claude

Hay tres niveles. Cada uno termina con una conclusión corta que se guarda aparte y es lo único que relee el nivel siguiente; así el contexto no crece sin límite. Todos llevan una parte determinista (tablas que calcula el código) y el análisis de Claude. Sin clave, sin saldo o si Claude falla, el informe se guarda igualmente con la etiqueta «Sin análisis de IA: <motivo>».

| | Diario | Semanal (noticias) | Mensual (estudio) |
|---|---|---|---|
| Cuándo | Primera rutina del día | El día elegido (domingo por defecto) o si han pasado 7 días desde el último | Primera rutina del mes, sobre el mes anterior |
| Contexto | Solo hoy: estado, NAV, posiciones (precio, peso, PnL, variación desde el último diario), niveles, incumplimientos y operaciones forzadas | Posiciones y sectores; conclusiones diarias de los últimos 7 días; posiciones prioritarias (movimiento de ±7 % o con aviso) | Conclusiones diarias y semanales del mes; informes semanales del mes; conclusión del mes anterior; tesis con su última revisión; valoración; incumplimientos; operaciones del mes |
| Herramientas | — | Búsqueda web | — |
| Esfuerzo / `max_tokens` | low / 16 000 | medium / 32 000 | high / 48 000 |
| Salida | Hasta 300 palabras + «Conclusión del día» (1–3 viñetas) | Una sección por activo con fuentes + «Conclusión de la semana» (2–5 viñetas) | Estudio + veredicto por posición + «Conclusión del mes» (3–6 viñetas) |

El mensual va en dos pasos:

- **A.** El estudio en Markdown.
- **B.** Una llamada corta que extrae del estudio un veredicto por posición (MANTENER, REDUCIR, AMPLIAR o CERRAR; motivo; qué lo invalidaría; stop y objetivo propuestos, o nulo) con `client.messages.parse` y un modelo pydantic, `max_tokens` de 8 000 y sin razonamiento (`thinking={"type": "disabled"}`; si el modelo no lo admite, quitarlo y subir `max_tokens`). El veredicto se compara sin distinguir mayúsculas. Cada veredicto se añade a su tesis como revisión, con sus propuestas «no aplicadas». Si B falla, el estudio se guarda y los veredictos quedan como «no disponibles».

Cliente de IA (un solo módulo, `services/ai.py`):

- Siempre streaming: `client.messages.stream(...)` + `get_final_message()`, con el prompt de sistema y `output_config={"effort": ...}`.
- Leer `stop_reason`. Si es `max_tokens` y no hay texto → un reintento con un nivel menos de esfuerzo. Red, 429 o 5xx → dos reintentos con espera. 401 o 403 → sin reintentos, «Clave no válida».
- Semanal: herramienta `{"type": "web_search_20250305", "name": "web_search", "max_uses": min(30, 3 × número de posiciones)}`. Si `stop_reason` es `pause_turn`, se reenvía la conversación con la respuesta tal cual (máximo 3 veces). Las URL citadas se juntan en una sección «Fuentes».
- Modelo y esfuerzo de cada acción, en Ajustes; la lista de modelos sale de `models.list`. Por defecto, `claude-sonnet-5` (el explorador del radar usa `claude-opus-5`).
- Antes de programar este módulo, contrastar con la documentación oficial vigente (platform.claude.com/docs): *effort*, *web search tool* y *structured outputs*.

**El coste, siempre por delante.** Ninguna llamada a Claude empieza sin que el usuario haya visto antes lo que va a costar:

| Acción | Modelo y esfuerzo por defecto | Coste aproximado |
|---|---|---|
| Control diario | `claude-sonnet-5`, low | 0,02–0,05 $ |
| Noticias semanales | `claude-sonnet-5`, medium | 0,30–0,80 $ |
| Estudio mensual (pasos A y B) | `claude-sonnet-5`, high | 0,20–0,60 $ |
| Buscar oportunidades (§5.8) | `claude-opus-5`, high | 0,80–2,50 $ |
| Valorar, vigilar niveles, auditar, filtro del radar | — | Gratis: no llaman a Claude |

- Cada botón que llama a Claude lleva su precio aproximado al lado, y el diálogo de confirmación lo repite junto a lo que va a hacer. Los que no cuestan nada lo dicen igual («gratis»).
- La estimación es la media del coste real de las tres últimas ejecuciones de esa acción; mientras no las haya, el valor de esta tabla.
- Al terminar, la app enseña y guarda el coste real (tokens de entrada y de salida, búsquedas) en el informe y en el Registro.
- Ajustes muestra el gasto del mes frente al tope (por defecto, 10 $). Si una acción fuera a superarlo, no se lanza: se avisa, y los informes salen con su parte determinista y la etiqueta de siempre.
- Precios por millón de tokens de entrada / salida, editables en Ajustes: `claude-sonnet-5` 2 / 10, `claude-opus-5` 5 / 25, `claude-haiku-4-5` 1 / 5; búsqueda web, 10 $ por cada 1 000.
- «Ejecutar ahora» sobre un informe que ya existe avisa del coste y, si se confirma, lo sustituye.

Los prompts son recursos (`resources/prompts/*.md`) con `string.Template` (`$variable`) para no chocar con las llaves. El de sistema incluye el mandato vigente generado desde los ajustes, que son la única fuente. Textos base en el Apéndice B.

### 5.8 Radar de oportunidades

Dos piezas con responsabilidades separadas, y la frontera entre ellas no se mueve: **Claude aporta convicción cualitativa; los números salen siempre de precios reales.**

**Explorador (con Claude, solo cuando tú lo pides).** Botón «Buscar oportunidades nuevas» de la pantalla Radar, con su precio aproximado al lado. Va en dos pasos, como el estudio mensual:

- **A.** Claude busca en la web ideas que no estén ya en tu cartera ni en tu lista de vigilancia, con el contexto de la cartera (para no repetir lo que ya tienes), de los sectores saturados y del mandato. Modelo más capaz (`claude-opus-5` por defecto), esfuerzo high, `max_tokens` 32 000, streaming y búsqueda web con el mismo `pause_turn` y las mismas fuentes que el informe semanal. Se guarda como informe de tipo EXPLORACION.
- **B.** Extracción estructurada de como mucho 8 candidatos: ticker, símbolo de Yahoo, nombre, sector, por qué hay foso o catalizador y qué invalidaría la idea. **El prompt le prohíbe proponer precios, stops u objetivos, y el modelo de datos del candidato no tiene dónde guardarlos.**

**Filtro cuantitativo (determinista, gratis).** Cada candidato del explorador y cada valor de la lista de vigilancia pasa por el mismo filtro, con precios reales. Lo que ya tienes en cartera queda fuera: el radar busca entradas nuevas, y ampliar una posición es asunto del estudio mensual.

1. Cotización fiable y con al menos 12 meses de histórico; si no, se descarta como NO VERIFICABLE.
2. Caída desde el máximo de 52 semanas de entre el 10 % y el 40 %.
3. Stop = el mayor de: el mínimo de las últimas 8 semanas, o el precio menos 1,5 × ATR(14); acotado a una caída de entre el 8 % y el 15 % desde el precio.
4. Objetivo = el máximo de 52 semanas.
5. Ratio (objetivo − precio) / (precio − stop) ≥ el ratio mínimo del mandato.
6. Peso máximo sugerido = el tope por activo del estado vital, y nunca más.

Lo que pasa los seis puntos se guarda como **alerta** con sus niveles y el precio con el que se calcularon. Lo que no, se guarda con su motivo de descarte, para poder volver sobre la idea cuando el precio acompañe: **una exploración puede terminar con ocho candidatos y cero alertas, y eso es el filtro haciendo su trabajo**, no un fallo.

Los umbrales son coherentes entre sí a propósito: en la versión anterior, exigir a la vez un ratio de 2,8 con el objetivo en el máximo anual y cotizar pegado a la media de 200 sesiones hacía que el radar no pudiera disparar casi nunca. Hay un test que lo demuestra (§7, H11).

**Vida de una alerta.** En la rutina diaria, gratis y sin IA: caduca por edad (30 días, configurable), porque el precio rompió su stop antes de que entraras o porque alcanzó el objetivo sin ti. La caducidad corre **antes** del filtro, para que un ticker que caduca hoy pueda volver a emitirse hoy mismo con niveles de hoy. Los dos criterios de precio solo se aplican con cotización fiable y en la divisa que declaró la alerta.

**De la alerta a la posición.** «Comprar» en una alerta abre Operar con sus niveles ya puestos. Al registrar la compra, la alerta pasa a EJECUTADA y la tesis se abre con los niveles de la compra **real**, no con los de la alerta. La lista de vigilancia la edita el usuario (añadir o quitar tickers); un candidato del explorador se añade a ella con un botón, aunque no haya generado alerta.

### 5.9 Rutina automática

La casilla «Iniciar con Windows» escribe `"<ruta>\Sharky.exe" --autostart` en la clave Run del usuario. Solo existe en la versión instalada; en desarrollo aparece desactivada. Con `--autostart`, la app arranca minimizada en la bandeja; si se abre a mano, con la ventana visible. En los dos casos ejecuta esta rutina, paso a paso y en orden, cada paso independiente (un fallo no impide los siguientes):

1. Esperar a tener red (hasta 2 minutos).
2. Valorar, guardar la foto del NAV, auditar, vigilar niveles y avisar. Siempre, aunque ya exista el diario de hoy: no gasta IA.
3. Radar: caducar alertas y pasar el filtro sobre la lista de vigilancia (§5.8). Determinista y gratis; el explorador **no** se lanza solo nunca.
4. Informe diario, si no hay uno de hoy.
5. Semanal, si toca.
6. Mensual, si toca.
7. Copia de seguridad de la base de datos (API de backup de SQLite).

Cada paso queda en `runs`; si alguno falla, notificación «Algo no ha ido bien — ver Registro». Encender el PC varias veces el mismo día no repite el diario, pero sí vuelve a revisar los niveles. Mientras la app sigue abierta, cada hora repite los pasos 2 y 3 (así un stop cruzado a media tarde no espera a mañana) y, si ha cambiado el día, la rutina completa: el PC puede pasar días sin apagarse. Instancia única (QLocalServer): abrir Sharky otra vez muestra la ventana que ya existe. Cerrar la ventana la deja en la bandeja; «Salir», en el menú de la bandeja, la cierra.

### 5.10 Pantallas

1. **Panel**: estado, NAV y variación del día, barra de drawdown con marcas en 3, 8 y 20 %, efectivo frente a su banda, cobertura, «Requiere atención» (stops, objetivos, incumplimientos, datos no fiables, posiciones sin tesis), gráfico del valor por participación, tres tarjetas de informe (estado, fecha, conclusión, «Leer» y «Ejecutar ahora» con su precio aproximado y confirmación), tarjeta «Oportunidades en radar» (alertas activas y acceso al Radar) y botón «Actualizar precios (gratis)».
2. **Cartera**: tabla ordenable (ticker, nombre, unidades, precio y divisa, valor en EUR, peso, PnL en € y %, procedencia, stop, objetivo), exposición por sector frente al 25 % y «Editar activo» (símbolo con botón «Probar», sector, clase).
3. **Operar**: compra, venta y movimientos de efectivo; a la derecha, la posición actual y los límites; «Revisar» enseña la validación antes de registrar.
4. **Tesis**: activas y cerradas, editor, historial, propuestas pendientes con «Aplicar» o «Descartar» y «Alta rápida» para poner stop y objetivo a todas las posiciones sin tesis desde una tabla (entrada = coste medio y niveles en EUR por defecto).
5. **Radar**: alertas activas con sus niveles, ratio y caída desde el máximo, y botones «Comprar» y «Descartar»; historial de alertas caducadas y ejecutadas; candidatos descartados por el filtro con su motivo; lista de vigilancia editable; botón «Buscar oportunidades nuevas» con su precio aproximado.
6. **Informes**: lista por tipo y fecha (diario, semanal, mensual y exploraciones), lector Markdown (QTextBrowser), marca «Sin IA», coste de cada informe y exportar a `.md`.
7. **Ajustes**: Claude (clave, probar, borrar, modelo y esfuerzo de cada acción, precios, tope de gasto y gasto del mes), Mandato, Apariencia (tema), Radar (vigencia de las alertas y umbrales del filtro), Automatización (inicio con Windows, día del semanal), Datos (abrir carpeta, copia ahora, copia en otra carpeta, restaurar, borrar cartera), Registro de ejecuciones y Acerca de (versión).

**Borrar cartera** (Ajustes → Datos), para empezar de cero. Un aviso explica que se borra todo lo de la base de datos (posiciones, operaciones, efectivo, historial del NAV, tesis, informes, avisos y radar) y que se conservan los ajustes, la clave de Claude y las copias; el botón solo se activa tras escribir «BORRAR». Antes se guarda una copia `…-antes-de-borrar`, que se recupera con «Restaurar» y entra en la rotación de las 14 últimas: con la copia diaria dura unas dos semanas, y el aviso lo dice. Después, Sharky se reinicia y abre el asistente de primer arranque.

Diseño sobrio: tablas legibles y color solo para estados (verde, ámbar, rojo). Ninguna acción larga congela la ventana: barra de progreso y «Cancelar» cuando sea posible.

**Tema claro y tema oscuro**, los dos completos, más la opción «el que tenga Windows» (la de por defecto). Se elige en Ajustes → Apariencia y también con un botón en la cabecera de cada pantalla; el cambio se aplica al momento, sin reiniciar, y se recuerda. Toda la paleta vive en un único sitio (`ui/theme.py`: un diccionario de tokens por tema —fondos, superficies, líneas, texto, acento y los tres colores de estado—) y ningún widget escribe un color a mano. En los dos temas, el texto normal mantiene un contraste de 4,5:1 como mínimo y los estados se distinguen sin depender solo del color (llevan siempre su etiqueta). El gráfico y los iconos toman sus colores de los mismos tokens.

---

## 6. Estructura del proyecto

```
sharky/
├─ pyproject.toml · uv.lock · .python-version · CLAUDE.md · GUIA_SHARKY.md · README.md
├─ src/sharky/
│  ├─ __init__.py      # versión, leída de los metadatos del paquete
│  ├─ app.py           # main(): argumentos, instancia única, QApplication, bandeja, errores globales
│  ├─ paths.py         # carpeta de datos y recursos (también dentro del exe)
│  ├─ core/            # models, csv_import, ledger, valuation, mandate, levels, radar,
│  │                   # schedule, formatting
│  ├─ services/        # db, repositories, settings, secrets, market, ai, reports, explorer,
│  │                   # routine, backup, autostart, selftest
│  ├─ ui/              # main_window, wizard, panel, portfolio, trade, theses, radar, reports,
│  │                   # settings, tray, workers
│  └─ resources/       # icono, prompts/*.md
├─ tests/              # conftest.py (Qt offscreen), fakes.py, test_*.py
├─ packaging/          # launcher.py, sharky.spec, sharky.iss
├─ scripts/build.py    # exe + selftest + instalador
└─ .github/workflows/ci.yml
```

`core` no importa nada de `services` ni de `ui`; `services` no importa nada de `ui`. La versión vive solo en `pyproject.toml`.

---

## 7. Hitos

| Hito | Resultado visible |
|---|---|
| H0 | Repositorio con entorno reproducible |
| H1 | App vacía convertida en un .exe que se autoverifica |
| H2 | Instalador que funciona en el otro PC, y CI |
| H3 | Base de datos, ajustes y clave |
| H4 | Asistente de primer arranque |
| H5 | Precios reales y pantalla Cartera |
| H6 | Mandato, estados y Panel |
| H7 | Tesis y avisos de stop y objetivo |
| H8 | Operaciones y efectivo |
| H9 | Claude e informe diario |
| H10 | Noticias semanales y estudio mensual |
| H11 | Radar de oportunidades (explorador + filtro) |
| H12 | Inicio con Windows y rutina automática |
| H13 | Ajustes completos y versión 1.0 |

Cada hito termina con ruff y pytest en verde, build y selftest en verde (desde H1), los criterios comprobados uno a uno y un commit `H<n>: <título>`.

### H0 · Repositorio y entorno

1. Instalar uv si falta (`winget install --id=astral-sh.uv -e`).
2. `git init`, `uv init --package` (proyecto `sharky`, código en `src/sharky`), `uv python pin 3.12` y `requires-python = "==3.12.*"`.
3. `uv add PySide6 pyqtgraph anthropic yfinance pydantic keyring` y `uv add --dev pytest pytest-qt ruff pyinstaller`.
4. `.gitignore`: `.venv/`, cachés de Python, `build/`, `dist/`, `*.db*`, `*.csv`, `.env`, `logs/`, `*.log`.
5. `CLAUDE.md` con el bloque de §2; `README.md` breve (qué es, cómo probar, cómo compilar); ruff (py312, 100 columnas); pytest con `qt_api = pyside6`; `tests/conftest.py` que fija `QT_QPA_PLATFORM=offscreen`; un primer test (el paquete se importa y su versión coincide con la de `pyproject.toml`), porque pytest falla si no encuentra ninguno.
6. Si hay repositorio en GitHub, conectarlo y hacer push.

Aceptación:
- [ ] En un clon recién hecho pasan `uv sync --locked`, `uv run ruff check .` y `uv run pytest`.
- [ ] `uv.lock` y `.python-version` están en git; no hay datos ni secretos.

### H1 · App vacía empaquetada

1. Ventana principal con las siete secciones vacías, icono, versión en «Acerca de» y `ui/theme.py` con los tokens de los dos temas (§5.10) aplicados a la paleta de Qt desde el primer día: añadir el tema oscuro al final siempre sale mal.
2. `paths.py`: carpeta de datos (§5.1) y `resource_path()`, válido en desarrollo y dentro del exe.
3. Arranque robusto: `stdout` y `stderr` nulos → `os.devnull`; logging rotativo; `sys.excepthook` que registra el error y muestra un diálogo; instancia única; icono en la bandeja (si no hay bandeja disponible, seguir sin ella).
4. `--selftest`, sin ventana: comprueba Qt en modo offscreen, SQLite en una carpeta temporal, keyring (guardar, leer y borrar una credencial de prueba), los imports de yfinance, anthropic y pyqtgraph y los recursos del paquete. Cada hito posterior le añade lo suyo (por ejemplo, H4 lee la plantilla CSV). Con `--online`, además: una cotización real y una conexión TLS con api.anthropic.com. Escribe el resultado en `%TEMP%\sharky_selftest.txt` y sale con 0 o 1. Distingue «falta algo dentro del exe» (fallo) de «Yahoo no responde ahora» (aviso).
5. `packaging/launcher.py` y `packaging/sharky.spec`: onedir, `console=False`, `upx=False`, icono, recursos, `copy_metadata("sharky")`, lo de keyring de §3 y lo que el selftest demuestre que falta (por ejemplo, librerías de curl_cffi o certificados).
6. `scripts/build.py`: limpia, ejecuta PyInstaller y lanza el selftest del exe (`--selftest` y `--selftest --online`).

Aceptación:
- [ ] `uv run python scripts/build.py` termina bien y el selftest del exe devuelve 0.
- [ ] `dist\Sharky\Sharky.exe` abre la ventana sin ninguna consola; abrirlo por segunda vez trae la misma ventana.
- [ ] Con una carpeta de datos con espacios y tildes (`SHARKY_DATA_DIR=C:\Temp\Datos de José`) funciona igual.

### H2 · Instalador, CI y prueba en el otro PC

1. `packaging/sharky.iss`: `PrivilegesRequired=lowest`, `DefaultDirName={autopf}\Sharky`, `AppId` fijo, versión recibida del build, acceso en el menú Inicio (en el escritorio, opcional), `CloseApplications=yes`, salida en `dist\` y opción de abrir Sharky al terminar. Al desinstalar: borrar el valor `Sharky` de la clave Run y **no** tocar la carpeta de datos.
2. `scripts/build.py` compila también el instalador con `ISCC.exe` (buscarlo en el PATH y en las rutas habituales; si falta, `winget install JRSoftware.InnoSetup`) → `dist\Sharky-Setup-<versión>.exe`.
3. CI en `windows-latest`: uv (acción `astral-sh/setup-uv`), `uv sync --locked`, ruff, pytest, `choco install innosetup -y`, `scripts/build.py` y subir el instalador como artefacto.

Aceptación:
- [ ] En este PC: instalar, abrir desde Inicio, cerrar y desinstalar, sin consolas y sin pedir administrador.
- [ ] En el otro PC, sin Python: instalar y abrir.
- [ ] La CI está en verde y deja el instalador descargable.

### H3 · Base de datos, ajustes y clave

1. `services/db.py`: una conexión por hilo, WAL, `busy_timeout`, claves foráneas, migraciones por `user_version` y una transacción por operación.
2. Tablas de §5.1 y sus repositorios; el libro se deriva de `trades` en `core/ledger.py`.
3. `settings.json` con pydantic y escritura atómica (fichero temporal + `os.replace`). Si está dañado: valores por defecto y aviso en el log.
4. `services/secrets.py` con keyring (servicio `Sharky`).
5. Copia de seguridad y restauración (API de backup de SQLite; restaurar pide confirmación y reinicia la app).

Aceptación:
- [ ] Tests: migraciones idempotentes; coste medio y PnL realizado; efectivo = suma de movimientos; copia y restauración de ida y vuelta; ajustes dañados → valores por defecto.

### H4 · Asistente de primer arranque

1. `core/csv_import.py` (puro) con todo lo de §5.2 y la plantilla como constante.
2. Asistente (QWizard) con las tres páginas de §5.2; la escritura final, en una transacción. La casilla de inicio con Windows se guarda en los ajustes; se aplica en H12.
3. Al terminar se abre la ventana principal.

Aceptación:
- [ ] Tests del CSV: separadores `;`, `,` y tabulador; coma y punto decimal; con y sin cabecera; UTF-8 y ANSI; todos los errores con su línea; duplicados; `GBX` → `GBp`; CSV vacío = solo efectivo.
- [ ] pytest-qt: CSV válido + confirmar → hay cartera; cancelar → no hay cartera.
- [ ] Manual: con la plantilla se crea la cartera; al reabrir no sale el asistente; la clave aparece en el Administrador de credenciales de Windows.

### H5 · Precios y Cartera

1. `services/market.py`: `PriceProvider` y `FxProvider` con yfinance (lotes, divisa real, GBp, reintentos, caché en la carpeta de datos y en `prices` y `fx_rates`).
2. `core/valuation.py` (pura: recibe los precios) con procedencias y cobertura (§5.3).
3. Pantalla Cartera, «Actualizar precios» en segundo plano y «Editar activo» con «Probar»; sugerencia de símbolo por ISIN si yfinance ofrece búsqueda.

Aceptación:
- [ ] Tests con precios falsos: EUR, USD y GBp; sin símbolo → COSTE; fallo de descarga → CACHE o ANTIGUO; cobertura correcta.
- [ ] Manual: tus posiciones aparecen con precio real y la ventana no se congela durante la descarga.

### H6 · Mandato y Panel

1. `core/mandate.py`: estados, auditoría con la corrección en euros y validación de compras (§5.4 y §5.5).
2. Foto diaria del NAV con participaciones; incumplimientos con su antigüedad.
3. Panel (§5.10) con el gráfico en pyqtgraph; las tarjetas de informes y de radar quedan como «Próximamente».

Aceptación:
- [ ] Tests de frontera: drawdown de 2,99 / 3,00 / 7,99 / 8,00 / 19,99 / 20,00 %; ingresos y retiradas no mueven el valor por participación; con cobertura inferior al 90 % el estado no cambia; cada regla de auditoría y cada paso de la validación.
- [ ] Manual: el Panel cuadra con la Cartera.

### H7 · Tesis y avisos de niveles

1. Pantalla Tesis (§5.6) con historial y «Alta rápida».
2. `core/levels.py`; avisos guardados en `level_alerts`; notificación y ventana modal.

Aceptación:
- [ ] Tests: stop, objetivo, prioridad del stop, niveles en USD con cotización en EUR y al revés, los tres criterios del stop propuesto (y que nunca baja del vigente), NO VERIFICABLE.
- [ ] Manual: una tesis con el stop por encima del precio actual dispara la ventana de aviso una sola vez en el día.

### H8 · Operaciones y efectivo

1. Pantalla Operar (§5.5 y §5.10).
2. Registro en una transacción; apertura de tesis al comprar un ticker sin tesis; cierre al venderlo todo; movimientos de efectivo.

Aceptación:
- [ ] Tests: coste medio con comisiones; PnL realizado; efectivo; cada validación; compra en USD con niveles en EUR; operación forzada con motivo; la venta total cierra la tesis y la parcial no.
- [ ] Manual: una compra y una venta ficticias se ven bien en Cartera, Panel y Tesis.

### H9 · Claude e informe diario

1. `services/ai.py` (§5.7) y los prompts de sistema y diario (Apéndice B).
2. Informe diario: parte determinista + Claude + conclusión extraída de «## Conclusión del día» (si falta, una conclusión determinista con los datos medidos).
3. Pantalla Informes, tarjeta «Diario» del Panel, apartado Claude de Ajustes y tope de gasto.

Aceptación:
- [ ] Tests con un cliente falso: éxito; solo razonamiento y `max_tokens` → reintento con menos esfuerzo; error de red; clave no válida; sin clave; tope superado. En todos los casos el informe se guarda, y los avisos de niveles ya estaban guardados antes de llamar a Claude.
- [ ] Manual: el botón enseña el precio aproximado antes de lanzarse; «Ejecutar ahora» genera un diario real en segundo plano y se lee en Informes con su coste real.

### H10 · Noticias semanales y estudio mensual

1. `core/schedule.py`: cuándo toca cada informe.
2. Semanal con búsqueda web, `pause_turn` y fuentes.
3. Mensual en dos pasos, con las revisiones añadidas a las tesis. «Ejecutar ahora» deja elegir el mes anterior o el mes en curso (parcial).

Aceptación:
- [ ] Tests: cuándo toca (día elegido, 7 días sin semanal, cambio de mes, primer mes incompleto); `pause_turn`; veredictos válidos e inválidos; ninguna revisión cambia un número de la tesis.
- [ ] Manual: un semanal y un mensual reales; las propuestas aparecen en Tesis con «Aplicar».

### H11 · Radar de oportunidades

1. `core/radar.py` (puro): el filtro cuantitativo y la caducidad de alertas de §5.8, calculados sobre una serie de precios que recibe.
2. `services/market.py`: histórico diario de 12 meses (máximo y mínimo de 52 semanas, mínimo de 8 semanas, ATR(14)) con caché en la base de datos.
3. `services/explorer.py`: el explorador en dos pasos de §5.8, reutilizando `services/ai.py` (búsqueda web, `pause_turn`, fuentes, coste).
4. Pantalla Radar y tarjeta del Panel (§5.10): alertas, descartados con su motivo, lista de vigilancia y «Comprar», que abre Operar con los niveles de la alerta.
5. La rutina incorpora el paso 3 de §5.9 (caducar y filtrar, gratis).

Aceptación:
- [ ] Tests del filtro con series de precios inventadas: un caso que **sí** dispara —demuestra que los umbrales son alcanzables, que es justo lo que fallaba antes—, uno que falla por ratio, otro por caída fuera del 10–40 %, otro por stop fuera del 8–15 % y otro sin histórico suficiente.
- [ ] Tests de caducidad: por edad, por stop roto antes de entrar y por objetivo alcanzado; y que la caducidad corre antes del filtro, de modo que un ticker que caduca hoy puede volver a alertar hoy con niveles de hoy.
- [ ] Tests del explorador con cliente falso: candidatos válidos; extracción inválida → el informe se guarda igualmente; un candidato que mencione precios en su texto no los usa nadie (los niveles salen del filtro).
- [ ] Test: comprar desde una alerta la deja EJECUTADA y abre la tesis con los niveles de la compra real.
- [ ] Manual: «Buscar oportunidades nuevas» enseña su precio aproximado, corre en segundo plano y deja el informe en Informes con su coste real.

### H12 · Inicio con Windows y rutina

1. `services/autostart.py` (clave Run): aplicar la casilla guardada por el asistente y añadirla a Ajustes.
2. `services/routine.py` según §5.9; menú de la bandeja: Abrir, Actualizar precios, Ejecutar diario y Salir.

Aceptación:
- [ ] Tests de la rutina con servicios y reloj falsos: orden de los pasos; un paso que falla no detiene los demás; el segundo arranque del día no repite el diario; con la app abierta, el cambio de día relanza la rutina.
- [ ] Manual (versión instalada): cerrar sesión y volver a entrar → Sharky aparece en la bandeja, hace el diario y avisa si hay un stop; volver a entrar el mismo día no gasta IA.
- [ ] Desinstalar quita el inicio automático.

### H13 · Ajustes completos y versión 1.0

1. Ajustes completo (§5.10) y Registro de ejecuciones.
2. Revisión de textos y README final; versión 1.0.0; CI en verde; instalador publicado.

Aceptación, en el otro PC y solo con el instalador:
- [ ] Instalar sin Python ni consola; asistente con clave y CSV; Panel correcto.
- [ ] Registrar una compra, una venta y una tesis con stop.
- [ ] Generar un diario, un semanal y un mensual, y lanzar una búsqueda de oportunidades: cada botón enseña antes su precio aproximado y después el real.
- [ ] Cambiar a tema oscuro y recorrer las siete pantallas: ningún texto ilegible, ningún estado que se pierda, ningún color escrito a mano fuera de `ui/theme.py`.
- [ ] Reiniciar el PC → la rutina se ejecuta sola desde la bandeja.
- [ ] Hacer una copia de seguridad, restaurarla en una instalación limpia y comprobar que están los mismos datos (la clave se vuelve a pedir: no viaja en la copia).

---

## 8. Actualizar y cambiar de PC

- **Actualizar:** ejecutar el instalador nuevo encima del anterior. Los datos no se tocan.
- **Cambiar de PC:** Ajustes → Datos → «Copia en otra carpeta» (un .zip) → instalar Sharky en el PC nuevo → «Restaurar» → volver a poner la clave.

---

## 9. Ideas aparcadas hasta después de la versión 1.0

_(Apunta aquí lo que se te ocurra. Nada de esta lista se construye hasta cerrar H13.)_

- **Stop fijo o dinámico en cada tesis** (propuesto en el H7). En dinámico (trailing), el stop
  que se vigila es el mayor entre el del usuario y el precio más alto visto con precio fiable
  menos el riesgo inicial: sube con el precio real y nunca baja; el número del usuario no se
  toca. Necesita una migración (modo y máximo visto en `theses`).
- **Deshacer una operación mal registrada** (propuesto en el H8). Hoy una compra o una venta
  registrada por error no se puede quitar: solo se compensa con otra operación o con «Ajustar
  saldo». Deshacer tendría que retirar a la vez la operación, su movimiento de efectivo y lo que
  hizo en su tesis (apertura, cambio de niveles o cierre), dejando rastro en el historial, y
  solo si es la última operación de ese ticker (si no, cambiaría el coste medio de ventas ya
  hechas).

---

## Apéndice A · Plantilla CSV (datos ficticios)

```
ticker;nombre;isin;unidades;coste_medio_eur;divisa;sector;simbolo;clase
AAPL;Apple Inc.;US0378331005;10;150,00;USD;Tecnologia;AAPL;ACCION
SAN;Banco Santander;ES0113900J37;200;4,50;EUR;Banca;SAN.MC;ACCION
IWDA;iShares Core MSCI World;IE00B4L5Y983;5;80,00;EUR;Renta_Variable_Global;IWDA.AS;ETF
```

## Apéndice B · Prompts base

**Sistema** (`prompts/sistema.md`)

```
Eres Sharky, analista de una cartera personal de inversión denominada en EUR. No ejecutas
órdenes: analizas y propones; el usuario decide.

Reglas sobre los datos:
1. La divisa base es el EUR. Nunca compares importes en divisas distintas sin convertirlos.
2. La cartera es solo la que aparece en el contexto. Las tesis son opiniones, no posiciones.
3. Distingue lo medido de lo estimado: si un precio está marcado como no fiable o falta un
   dato, dilo.
4. No inventes cifras. Si no está en el contexto, no existe.
5. El drawdown se mide contra el máximo del valor por participación.

Mandato vigente:
$mandato

Cómo razonar: sé concreto y cuantitativo; ataca tus propias tesis, porque el sesgo de
confirmación es el riesgo principal; separa la opinión del dato; no se opera por impulso: la
única salida inmediata es un stop-loss alcanzado.

Responde en español, en Markdown y sin preámbulos.
```

**Diario** (`prompts/diario.md`)

```
Control diario del $fecha. Hoy solo posiciones, precios y mandato: nada de macro ni noticias.

ESTADO: $estado
POSICIONES: $posiciones
MOVIMIENTOS DE ±$umbral % O MÁS DESDE EL ÚLTIMO CONTROL: $movimientos
NIVELES DE LAS TESIS: $niveles
INCUMPLIMIENTOS: $incumplimientos
OPERACIONES FORZADAS FUERA DEL MANDATO: $forzadas

Escribe como mucho 300 palabras:
1. Qué se ha movido y por qué importa (solo lo relevante).
2. Qué norma se incumple, por cuánto y qué exige. Si todo cumple, una línea.
Termina SIEMPRE con «## Conclusión del día»: de 1 a 3 viñetas con lo que el informe semanal y
el mensual deben vigilar.
```

**Semanal** (`prompts/semanal.md`)

```
Escaneo semanal de noticias del $desde al $hasta. Busca en la web noticias relevantes de estos
días para cada activo:
$activos

Conclusiones de los controles diarios de la semana:
$conclusiones_diarias

Investiga primero: $prioritarias

Busca resultados y previsiones, fusiones y adquisiciones, cambios regulatorios, cambios de
recomendación o de precio objetivo, contratos relevantes y contexto sectorial o geopolítico que
afecte directamente. Si una posición se movió con fuerza, busca qué lo explica.

Escribe una sección «## TICKER» por activo, en el mismo orden, con 2 a 4 líneas y por qué le
importa a esta posición. Si no encuentras nada relevante, dilo; no rellenes. Termina SIEMPRE
con «## Conclusión de la semana»: de 2 a 5 viñetas que crucen las noticias con los controles
diarios.
```

**Mensual, paso A** (`prompts/mensual.md`)

```
Estudio mensual de $mes. Reevalúa cada posición con todo el contexto del mes.

VALORACIÓN ACTUAL: $valoracion
INCUMPLIMIENTOS: $incumplimientos
TESIS ACTIVAS (niveles y última revisión): $tesis
OPERACIONES DEL MES: $operaciones
CONCLUSIONES DIARIAS DEL MES: $conclusiones_diarias
INFORMES SEMANALES DEL MES: $semanales
CONCLUSIÓN DEL MES ANTERIOR: $conclusion_anterior

Para cada posición: veredicto (MANTENER, REDUCIR, AMPLIAR o CERRAR), qué ha cambiado, qué sigue
en pie y qué la invalidaría ahora. Si un stop o un objetivo debería cambiar, propón la cifra y
el motivo: decide el usuario. Si se incumple el mandato, prioriza: 1) liquidez mínima;
2) concentración por activo y por sector; 3) posiciones residuales; 4) nuevas convicciones.
Termina SIEMPRE con «## Conclusión del mes»: de 3 a 6 viñetas.
```

**Mensual, paso B** (`prompts/mensual_extraccion.md`)

```
Del estudio siguiente, extrae para cada uno de estos tickers: $tickers
el veredicto (MANTENER, REDUCIR, AMPLIAR o CERRAR), el motivo en 1 o 2 frases, qué lo
invalidaría y el stop y el objetivo propuestos (null si el estudio no propone una cifra).
No añadas nada que no esté en el texto.

$estudio
```

**Explorador, paso A** (`prompts/explorador.md`)

```
Busca en la web ideas de inversión NUEVAS para una cartera personal denominada en EUR. No
propongas nada que ya esté en estas listas:

EN CARTERA: $cartera
EN VIGILANCIA: $vigilancia
CON ALERTA ACTIVA: $alertas

Mandato que condiciona qué encaja: $mandato
Sectores ya saturados en la cartera: $sectores_saturados

Busca empresas o fondos con una ventaja competitiva defendible o un catalizador concreto y
verificable, que coticen en mercados accesibles desde un bróker europeo. De cada idea explica
en 3 a 6 líneas: qué hace, por qué hay foso o catalizador, qué riesgo tiene y qué la
invalidaría. Cita tus fuentes.

NO propongas precios de entrada, stops ni objetivos: esos números los calcula Sharky con la
estructura real de precios. Si no encuentras nada que merezca la pena, dilo: es una respuesta
válida.

Termina SIEMPRE con «## Conclusión de la exploración»: de 2 a 5 viñetas con qué has mirado, qué
has descartado y por qué.
```

**Explorador, paso B** (`prompts/explorador_extraccion.md`): mismo patrón que el paso B del
mensual. De la exploración anterior, extrae como mucho 8 candidatos con ticker, símbolo de
Yahoo, nombre, sector, tesis en 1 o 2 frases y qué la invalidaría. Sin precios, stops ni
objetivos: el esquema no tiene esos campos.

## Apéndice C · Llamadas a Claude (referencia)

```python
import anthropic

client = anthropic.Anthropic(api_key=clave)

# Diario, semanal y mensual (paso A): siempre por streaming.
with client.messages.stream(
    model="claude-sonnet-5",
    max_tokens=16000,                       # razonamiento + texto
    system=prompt_sistema,
    messages=[{"role": "user", "content": prompt}],
    output_config={"effort": "low"},        # low | medium | high
    # Solo en el semanal:
    # tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 30}],
) as stream:
    msg = stream.get_final_message()

texto = "".join(b.text for b in msg.content if b.type == "text")
# msg.stop_reason: "end_turn", "max_tokens", "pause_turn", "refusal"...
# msg.usage: input_tokens y output_tokens (y búsquedas web realizadas)

# Mensual, paso B: salida estructurada validada con pydantic.
res = client.messages.parse(
    model="claude-sonnet-5",
    max_tokens=8000,
    thinking={"type": "disabled"},
    messages=[{"role": "user", "content": prompt_extraccion}],
    output_format=VeredictosMensuales,      # modelo pydantic
)
veredictos = res.parsed_output
```
