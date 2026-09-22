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
