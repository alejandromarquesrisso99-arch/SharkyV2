# Maqueta de la interfaz de Sharky

Capturas de referencia de cómo debe verse la aplicación. **No son código ni datos reales**:
las cifras, los tickers y las posiciones son inventados a propósito.

La especificación manda siempre: `GUIA_SHARKY.md`, §5.10 (pantallas) y §5.2 (primer arranque).
Estas imágenes solo fijan el aspecto: distribución, jerarquía, tono y densidad.

| Imagen | Pantalla | Guía |
| :--- | :--- | :--- |
| `01-panel.png` | Panel | §5.10 punto 1 |
| `02-cartera.png` | Cartera | §5.10 punto 2 |
| `03-operar.png` | Operar, con una compra rechazada por el mandato | §5.5 y §5.10 punto 3 |
| `04-tesis.png` | Tesis, con el historial y una propuesta sin aplicar | §5.6 y §5.10 punto 4 |
| `05-radar.png` | Radar: alertas, descartes del filtro y lista de vigilancia | §5.8 y §5.10 punto 5 |
| `06-informes.png` | Informes, con el lector de un control diario | §5.10 punto 6 |
| `07-ajustes.png` | Ajustes | §5.10 punto 7 |
| `08-primer-arranque.png` | Asistente, paso 2 (CSV) | §5.2 |
| `09-avisos.png` | Aviso modal de stop, notificación y menú de la bandeja | §5.6 y §5.9 |
| `oscuro-01…07-*.png` | Las mismas pantallas en tema oscuro | §5.10, «Tema claro y tema oscuro» |

## Lo que hay que respetar

- Ventana con barra de título, navegación lateral de siete secciones y contenido a la derecha.
  Abajo del lateral, siempre visibles, el estado del mandato y la hora del último control.
- Color solo para estados: verde (cumple), ámbar (alerta) y rojo (exige actuar). Todo lo demás,
  neutro. Ningún estado se distingue solo por el color: siempre lleva su etiqueta.
- Cada botón que llama a Claude muestra su precio aproximado; los gratuitos dicen «gratis».
- Números alineados a la derecha y con cifras de ancho fijo (`tabular-nums`); formato español
  (`1.234,56 €`, `12,3 %`).
- Los dos temas salen de los mismos tokens (`ui/theme.py`). Ningún widget escribe un color a mano.

## Lo que NO hay que copiar

- Las cifras, los nombres de activos y los textos de ejemplo.
- El HTML con el que se generaron: la aplicación es PySide6 (Qt Widgets), no una página web.
