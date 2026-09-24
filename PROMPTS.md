# Prompts para Claude Code, hito a hito

Uno por sesión, en orden. Empieza cada hito con la conversación limpia (`/clear` o ventana
nueva): así Claude Code relee la guía y no arrastra ruido del hito anterior.

Todos siguen el mismo esqueleto —lee la guía, enseña el plan, espera tu OK, al terminar repasa
los criterios y hace commit— y añaden los avisos propios del hito, que son justo donde se suele
tropezar.

Al final hay tres prompts sueltos: cuando algo falla, cuando cambia la guía y cuando quieres
retomar un hito a medias.

---

## H0 · Repositorio y entorno

```
Sesión nueva. Lee GUIA_SHARKY.md entera: es la especificación del proyecto.

Vamos a ejecutar solo el Hito 0 (repositorio y entorno). Enséñame primero el plan y espera mi OK.

Recuerda: Python 3.12 fijado con uv, requires-python "==3.12.*", uv.lock versionado, CLAUDE.md
con el bloque de §2 tal cual, y un primer test de verdad (pytest falla si no encuentra ninguno).

Al terminar: ruff y pytest en verde, repasa uno a uno los criterios de aceptación del hito,
dime el resultado de cada uno y haz commit "H0: repositorio y entorno". No empieces el Hito 1.
```

## H1 · App vacía empaquetada

```
Sesión nueva. Lee GUIA_SHARKY.md entera y mira las capturas de docs/maqueta: son la referencia
visual de las pantallas.

Vamos a ejecutar solo el Hito 1 (app vacía empaquetada). Enséñame primero el plan y espera mi OK.

Recuerda: siete secciones vacías, ui/theme.py con los tokens de los dos temas desde el primer
día, sys.stdout y sys.stderr nulos en una app sin consola, rutas absolutas siempre, instancia
única, bandeja, y un --selftest que compruebe Qt, SQLite, keyring y los imports dentro del exe.
El script de entrada de PyInstaller es packaging/launcher.py con imports absolutos.

Al terminar: ruff, pytest, build y selftest en verde; repasa uno a uno los criterios de
aceptación, dime el resultado de cada uno y marca los que tengo que comprobar yo a mano; haz
commit "H1: app vacía empaquetada". No empieces el Hito 2.
```

## H2 · Instalador, CI y prueba en el otro PC

```
Sesión nueva. Lee GUIA_SHARKY.md entera.

Vamos a ejecutar solo el Hito 2 (instalador, CI y prueba en el otro PC). Enséñame primero el
plan y espera mi OK.

Recuerda: instalación por usuario sin permisos de administrador (PrivilegesRequired=lowest),
AppId fijo, el desinstalador borra el valor Sharky de la clave Run y NO toca la carpeta de
datos, y scripts/build.py compila exe e instalador y pasa el selftest del exe.

Al terminar: build y selftest en verde; repasa los criterios uno a uno y dime cuáles tengo que
probar yo (instalar aquí, instalar en el otro PC). Haz commit "H2: instalador y CI". No empieces
el Hito 3.
```

## H3 · Base de datos, ajustes y clave

```
Sesión nueva. Lee GUIA_SHARKY.md entera, en especial §5.1.

Vamos a ejecutar solo el Hito 3 (base de datos, ajustes y clave). Enséñame primero el plan y
espera mi OK.

Recuerda: una conexión por hilo, WAL, busy_timeout, migraciones por user_version, una
transacción por operación, settings.json con escritura atómica, la clave en keyring y copia de
seguridad con la API de backup de SQLite. Las posiciones se derivan de trades, no se guardan.

Al terminar: ruff, pytest, build y selftest en verde; repasa los criterios uno a uno y haz
commit "H3: datos, ajustes y clave". No empieces el Hito 4.
```

## H4 · Asistente de primer arranque

```
Sesión nueva. Lee GUIA_SHARKY.md entera, en especial §5.2, y mira docs/maqueta/08-primer-arranque.png.

Vamos a ejecutar solo el Hito 4 (asistente de primer arranque). Enséñame primero el plan y
espera mi OK.

Recuerda: primero la clave de Claude y después el CSV; todos los errores del CSV a la vez con su
línea; no se escribe nada hasta confirmar y todo va en una transacción; se puede empezar sin
posiciones; la plantilla sale de una constante en código. La casilla de inicio con Windows solo
se guarda en los ajustes: se aplica en el Hito 12.

Al terminar: ruff, pytest, build y selftest en verde; repasa los criterios uno a uno, dime
cuáles compruebo yo, y haz commit "H4: asistente de primer arranque". No empieces el Hito 5.
```

## H5 · Precios y Cartera

```
Sesión nueva. Lee GUIA_SHARKY.md entera, en especial §5.3, y mira docs/maqueta/02-cartera.png.

Vamos a ejecutar solo el Hito 5 (precios y pantalla Cartera). Enséñame primero el plan y espera
mi OK.

Recuerda: descargas por lotes con reintentos, la divisa real la manda el proveedor, GBp son
peniques (GBP/100), las cuatro procedencias en orden (MERCADO, CACHE, ANTIGUO, COSTE) y jamás un
precio de ejemplo en el NAV. core/valuation.py es pura y recibe los precios. La descarga va en
un hilo de trabajo: la ventana no se congela.

Al terminar: ruff, pytest, build y selftest en verde; repasa los criterios uno a uno y haz
commit "H5: precios y cartera". No empieces el Hito 6.
```

## H6 · Mandato y Panel

```
Sesión nueva. Lee GUIA_SHARKY.md entera, en especial §5.4 y §5.5, y mira docs/maqueta/01-panel.png
y oscuro-01-panel.png.

Vamos a ejecutar solo el Hito 6 (mandato, estados y Panel). Enséñame primero el plan y espera mi OK.

Recuerda: el drawdown se mide sobre el valor por participación (empieza en 100); ingresos y
retiradas cambian las participaciones, no su valor; con cobertura por debajo del 90 % no se
actualizan ni el estado ni el máximo; los tests cubren las fronteras exactas 2,99 / 3,00 / 7,99 /
8,00 / 19,99 / 20,00. La validación de compras va en core/mandate.py aunque su pantalla llegue
en el Hito 8.

Al terminar: ruff, pytest, build y selftest en verde; repasa los criterios uno a uno y haz
commit "H6: mandato y panel". No empieces el Hito 7.
```

## H7 · Tesis y avisos de niveles

```
Sesión nueva. Lee GUIA_SHARKY.md entera, en especial §5.6, y mira docs/maqueta/04-tesis.png.

Vamos a ejecutar solo el Hito 7 (tesis y avisos de niveles). Enséñame primero el plan y espera
mi OK.

Recuerda: la comparación va siempre en EUR; el stop tiene prioridad sobre el objetivo; precio no
fiable es NO VERIFICABLE, nunca "a salvo"; el stop propuesto al alcanzar el objetivo es el mayor
de break-even y trailing y nunca baja del stop vigente; los números de una tesis solo los cambia
el usuario y cada cambio queda en el historial. Aviso modal para el stop, notificación para el
objetivo, una vez al día.

Al terminar: ruff, pytest, build y selftest en verde; repasa los criterios uno a uno, dime
cuáles compruebo yo, y haz commit "H7: tesis y niveles". No empieces el Hito 8.
```

## H8 · Operaciones y efectivo

```
Sesión nueva. Lee GUIA_SHARKY.md entera, en especial §5.5, y mira docs/maqueta/03-operar.png.

Vamos a ejecutar solo el Hito 8 (operaciones y efectivo). Enséñame primero el plan y espera mi OK.

Recuerda: las ocho validaciones en el orden exacto de la guía y la primera que falla se muestra
con su motivo; la operación y su movimiento de efectivo van en la misma transacción; comprar un
ticker sin tesis abre el editor de tesis; vender todo la cierra y una venta parcial no; coste
medio ponderado con las comisiones de compra sumando al coste; "registrar igualmente" exige
motivo y queda marcada como forzada.

Al terminar: ruff, pytest, build y selftest en verde; repasa los criterios uno a uno y haz
commit "H8: operaciones y efectivo". No empieces el Hito 9.
```

## H9 · Claude e informe diario

```
Sesión nueva. Lee GUIA_SHARKY.md entera, en especial §5.7 y los apéndices B y C, y mira
docs/maqueta/06-informes.png.

Vamos a ejecutar solo el Hito 9 (cliente de Claude e informe diario). Enséñame primero el plan y
espera mi OK. Antes de programar services/ai.py, contrasta effort, búsqueda web y salidas
estructuradas con la documentación oficial vigente en platform.claude.com/docs.

Recuerda: siempre streaming; leer stop_reason y reintentar una vez con menos esfuerzo si se
corta sin texto; los avisos de niveles se guardan ANTES de llamar a Claude; sin clave o con
fallo, el informe se guarda igual con su etiqueta; cada botón enseña el coste aproximado antes y
el real después; hay tope mensual de gasto.

Al terminar: ruff, pytest, build y selftest en verde; repasa los criterios uno a uno, dime
cuáles compruebo yo, y haz commit "H9: claude e informe diario". No empieces el Hito 10.
```

## H10 · Noticias semanales y estudio mensual

```
Sesión nueva. Lee GUIA_SHARKY.md entera, en especial §5.7 y el apéndice B.

Vamos a ejecutar solo el Hito 10 (noticias semanales y estudio mensual). Enséñame primero el
plan y espera mi OK.

Recuerda: el semanal usa la herramienta de búsqueda web, maneja stop_reason pause_turn
reenviando el turno (máximo 3 veces) y junta las fuentes citadas; el mensual va en dos pasos y,
si la extracción falla, el estudio se guarda igual; las revisiones se añaden a la tesis y no
tocan ni un número; core/schedule.py decide cuándo toca cada informe y se prueba con reloj falso.

Al terminar: ruff, pytest, build y selftest en verde; repasa los criterios uno a uno, dime
cuáles compruebo yo, y haz commit "H10: semanal y mensual". No empieces el Hito 11.
```

## H11 · Radar de oportunidades

```
Sesión nueva. Lee GUIA_SHARKY.md entera, en especial §5.8, y mira docs/maqueta/05-radar.png.

Vamos a ejecutar solo el Hito 11 (radar: explorador y filtro). Enséñame primero el plan y espera
mi OK.

Recuerda: Claude solo aporta la idea y tiene prohibido proponer precios, stops u objetivos —el
esquema del candidato no tiene esos campos—; los niveles los calcula el filtro determinista con
precios reales; lo que ya está en cartera queda fuera; las alertas caducan en la rutina diaria y
la caducidad corre antes del filtro. El test que más importa es el que demuestra que el filtro
PUEDE disparar: si ningún caso realista lo pasa, los umbrales están mal.

Al terminar: ruff, pytest, build y selftest en verde; repasa los criterios uno a uno, dime
cuáles compruebo yo, y haz commit "H11: radar de oportunidades". No empieces el Hito 12.
```

## H12 · Inicio con Windows y rutina

```
Sesión nueva. Lee GUIA_SHARKY.md entera, en especial §5.9, y mira docs/maqueta/09-avisos.png.

Vamos a ejecutar solo el Hito 12 (inicio con Windows y rutina automática). Enséñame primero el
plan y espera mi OK.

Recuerda: el arranque se registra en la clave Run del usuario y solo en la versión instalada;
los siete pasos de la rutina en orden y cada uno independiente; el explorador del radar no se
lanza solo nunca; el segundo arranque del día no repite el diario pero sí revisa niveles; con la
app abierta, cada hora se repiten los pasos 2 y 3 y al cambiar el día, la rutina entera.

Al terminar: ruff, pytest, build y selftest en verde; repasa los criterios uno a uno, dime
cuáles compruebo yo (cerrar sesión y volver a entrar, desinstalar), y haz commit "H12: inicio
con Windows y rutina". No empieces el Hito 13.
```

## H13 · Ajustes completos y versión 1.0

```
Sesión nueva. Lee GUIA_SHARKY.md entera, en especial §5.10, y mira docs/maqueta/07-ajustes.png
y oscuro-07-ajustes.png.

Vamos a ejecutar el Hito 13 (ajustes completos y versión 1.0). Enséñame primero el plan y espera
mi OK.

Recuerda: Ajustes completo con Apariencia, Mandato, Radar, Claude con precios y gasto del mes,
Datos y Registro; ningún color escrito a mano fuera de ui/theme.py; versión 1.0.0 en un solo
sitio; README final.

Al terminar: ruff, pytest, build y selftest en verde; prepárame la lista de comprobación final
del hito para hacerla yo en el otro PC con el instalador, y haz commit "H13: versión 1.0".
```

---

## Prompts sueltos

**Cuando un criterio falla**

```
El criterio «...» del Hito N falla con este error:

<pega aquí la salida tal cual>

Arréglalo sin salirte del hito y sin añadir nada que la guía no pida. Cuando esté, vuelve a
pasar ruff, pytest, build y selftest y dime el resultado.
```

**Cuando he cambiado la guía**

```
GUIA_SHARKY.md ha cambiado desde que la leíste. Vuelve a leerla entera antes de seguir y dime
en dos líneas qué ha cambiado respecto a lo que tenías.
```

**Para retomar un hito a medias**

```
Sesión nueva. Lee GUIA_SHARKY.md entera y mira el estado del repositorio (git log y git status).
Estábamos a mitad del Hito N. Dime qué hay hecho, qué falta para cumplir sus criterios de
aceptación y enséñame el plan de lo que queda. No toques nada hasta que te dé el OK.
```
