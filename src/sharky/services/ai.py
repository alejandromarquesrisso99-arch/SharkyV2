"""El cliente de Claude: el único módulo del programa que habla con la API de Anthropic
(GUIA §3 y §5.7).

- «Probar clave» (H4): una llamada a `models.list`, que no gasta tokens.
- Los informes (H9): **siempre por streaming** (`messages.stream` + `get_final_message`), con el
  prompt de sistema y `output_config={"effort": …}`. Contrastado con la documentación oficial
  el 25/09/2026: el esfuerzo va en `output_config`, sin cabecera beta; en `claude-sonnet-5` el
  razonamiento está activo aunque no se pida y gasta del mismo `max_tokens` que el texto.
- **`stop_reason`.** `max_tokens` sin texto (se fue todo en razonar) → un reintento con un nivel
  menos de esfuerzo; si ya iba en low, otra vez en low sin razonamiento (decidido en el H9).
  `max_tokens` con texto → se da por bueno, marcado como cortado. `refusal` → sin reintentos.
- **Errores.** Red, 429 o 5xx → dos reintentos con espera (los hace este módulo, no el SDK, para
  que se puedan contar y probar). 401 o 403 → «Clave no válida», sin reintentos.
- **Coste.** Se suman los tokens de todos los intentos que llegaron a responder: los fallidos
  también se cobran. Un error lleva lo gastado hasta entonces (`AIError.usage`).
- **Búsqueda web** (H10, semanal): la herramienta `web_search_20250305` de la guía (vale con
  cualquier modelo de Ajustes). Si `stop_reason` es `pause_turn`, se reenvía la conversación con
  lo que Claude lleva escrito, tal cual y sin añadir nada, como mucho 3 veces; el texto y las
  fuentes salen de todas las partes. Las URL citadas (`citations` de los bloques de texto) se
  juntan sin repetir. Los fallos de una búsqueda llegan dentro de la respuesta: no son errores.
- **Extracción estructurada** (H10, paso B del mensual): `messages.parse` con un modelo pydantic
  y sin razonamiento; si el modelo no admite desactivarlo (400), se repite sin ese parámetro y
  con más `max_tokens`. Si la respuesta no se puede leer (cortada), el SDK no deja ver sus
  tokens: el error lo dice (`AIError.unmeasured`) en vez de inventar un coste.

Contrastado con la documentación oficial el 26/09/2026 (búsqueda web, `pause_turn`, salidas
estructuradas).

La clave nunca se registra: de los errores solo se anotan el tipo y el código HTTP.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Any

import anthropic
import pydantic

log = logging.getLogger(__name__)

#: «Probar clave» es una pregunta rápida: si la red no contesta pronto, se guarda sin comprobar.
KEY_CHECK_TIMEOUT_S = 15.0
KEY_CHECK_RETRIES = 1

#: Los niveles de esfuerzo que se pueden elegir en Ajustes, de menos a más (GUIA §5.7).
EFFORTS: tuple[str, ...] = ("low", "medium", "high")
#: Espera antes de cada uno de los dos reintentos por red, 429 o 5xx.
RETRY_WAITS_S: tuple[float, ...] = (3.0, 10.0)
#: Lo más que se espera si un 429 pide esperar (cabecera retry-after).
MAX_RETRY_AFTER_S = 60.0
#: Tiempo de espera de cada petición: una respuesta larga llega por streaming, a trozos.
REQUEST_TIMEOUT_S = 600.0
#: Errores que llegan dentro del streaming (HTTP 200) y que se reintentan como un 5xx o un 429.
_RETRYABLE_TYPES = frozenset({"overloaded_error", "api_error", "rate_limit_error", "timeout_error"})

#: La herramienta de búsqueda web (GUIA §5.7; versión decidida en el H10).
WEB_SEARCH_TOOL = "web_search_20250305"
#: Búsquedas por posición y tope de búsquedas de una llamada: min(30, 3 × posiciones).
SEARCHES_PER_POSITION = 3
MAX_WEB_SEARCHES = 30
#: Cuántas veces se reenvía un turno en pausa (`pause_turn`).
MAX_CONTINUATIONS = 3
#: `max_tokens` de la extracción si el modelo no admite desactivar el razonamiento.
EXTRACTION_THINKING_MAX_TOKENS = 16_000


class KeyStatus(StrEnum):
    VALID = "VALIDA"
    INVALID = "NO_VALIDA"
    OFFLINE = "SIN_CONEXION"
    UNVERIFIED = "SIN_COMPROBAR"


@dataclass(frozen=True)
class KeyCheck:
    """Resultado de «Probar clave», con un mensaje que se puede enseñar."""

    status: KeyStatus
    message: str

    @property
    def usable(self) -> bool:
        """Se puede guardar: es válida o no se ha podido comprobar (sin conexión, por ejemplo)."""
        return self.status is not KeyStatus.INVALID


def check_api_key(key: str) -> KeyCheck:
    """Comprueba la clave con `models.list` (GUIA §5.2). Nunca lanza.

    - 401 o 403 → no válida.
    - Sin red o sin respuesta a tiempo → sin conexión: se guardará sin comprobar.
    - Cualquier otra respuesta de error (429, 5xx…) → no se ha podido comprobar.
    """
    limpia = key.strip()
    if not limpia:
        return KeyCheck(KeyStatus.INVALID, "La clave está vacía.")
    cliente = None
    try:
        cliente = anthropic.Anthropic(
            api_key=limpia, timeout=KEY_CHECK_TIMEOUT_S, max_retries=KEY_CHECK_RETRIES
        )
        cliente.models.list(limit=1)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as error:
        log.warning("Claude ha rechazado la clave (HTTP %s)", error.status_code)
        return KeyCheck(
            KeyStatus.INVALID,
            "Clave no válida: Claude la ha rechazado. Revísala, o déjala en blanco para "
            "seguir sin IA.",
        )
    except anthropic.APIConnectionError as error:  # incluye el tiempo de espera agotado
        log.info("Sin conexión con la API de Claude (%s)", type(error).__name__)
        return KeyCheck(KeyStatus.OFFLINE, "Sin conexión: la clave se guardará sin comprobar.")
    except anthropic.APIStatusError as error:
        log.warning("La API de Claude ha respondido HTTP %s al probar la clave", error.status_code)
        return KeyCheck(
            KeyStatus.UNVERIFIED,
            f"Claude no ha podido comprobarla ahora (código {error.status_code}). La clave se "
            "guardará sin comprobar.",
        )
    except Exception as error:
        log.warning("No se ha podido probar la clave de Claude (%s)", type(error).__name__)
        return KeyCheck(
            KeyStatus.UNVERIFIED,
            "No se ha podido comprobar la clave. Se guardará sin comprobar.",
        )
    finally:
        if cliente is not None:
            with suppress(Exception):
                cliente.close()
    log.info("Clave de Claude comprobada: válida")
    return KeyCheck(KeyStatus.VALID, "Clave válida.")


# -- los informes ---------------------------------------------------------------------------


class AIErrorKind(StrEnum):
    INVALID_KEY = "CLAVE_NO_VALIDA"
    NO_CREDIT = "SIN_SALDO"
    NETWORK = "SIN_CONEXION"
    RATE_LIMIT = "LIMITE"
    SERVER = "SERVIDOR"
    REFUSAL = "RECHAZO"
    EMPTY = "SIN_TEXTO"
    INVALID_OUTPUT = "SALIDA_NO_VALIDA"
    REQUEST = "PETICION"
    CANCELLED = "CANCELADA"
    UNKNOWN = "DESCONOCIDO"


@dataclass(frozen=True)
class AIUsage:
    """Tokens y búsquedas web de una o varias llamadas."""

    input_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0

    def __add__(self, other: AIUsage) -> AIUsage:
        return AIUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.web_searches + other.web_searches,
        )

    @property
    def empty(self) -> bool:
        return not (self.input_tokens or self.output_tokens or self.web_searches)


class AIError(Exception):
    """Claude no ha dado el texto. `message` se puede enseñar (va en la etiqueta «Sin análisis
    de IA: …»); `usage` es lo que se ha gastado de todos modos. `unmeasured`: hubo una respuesta
    cuyo coste no se ha podido medir (no está en `usage`)."""

    def __init__(self, kind: AIErrorKind, message: str, usage: AIUsage | None = None,
                 unmeasured: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.usage = usage or AIUsage()
        self.unmeasured = unmeasured

    def with_usage(self, usage: AIUsage) -> AIError:
        return AIError(self.kind, self.message, usage, self.unmeasured)


@dataclass(frozen=True)
class AIRequest:
    """Una petición de texto a Claude. `tools`: herramientas del servidor (la búsqueda web)."""

    model: str
    effort: str
    max_tokens: int
    system: str
    prompt: str
    tools: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Source:
    """Una fuente citada por Claude."""

    url: str
    title: str


@dataclass(frozen=True)
class AIResult:
    """El texto de Claude y lo que ha costado conseguirlo."""

    text: str
    model: str
    effort: str  # el esfuerzo con el que salió el texto (puede ser menor que el pedido)
    usage: AIUsage  # la suma de todos los intentos
    stop_reason: str
    attempts: int
    thinking_disabled: bool = False  # el reintento en low fue sin razonamiento
    sources: tuple[Source, ...] = ()  # las URL citadas, sin repetir y por orden
    continuations: int = 0  # cuántas veces se reenvió un turno en pausa

    @property
    def truncated(self) -> bool:
        """Se cortó por `max_tokens` con parte del texto ya escrito."""
        return self.stop_reason == "max_tokens"

    @property
    def paused(self) -> bool:
        """Seguía en pausa después de los reenvíos: el texto puede estar a medias."""
        return self.stop_reason == "pause_turn"

    @property
    def effort_text(self) -> str:
        return f"{self.effort} sin razonamiento" if self.thinking_disabled else self.effort


def web_search_tool(positions: int) -> dict[str, Any]:
    """La búsqueda web del semanal, con `max_uses = min(30, 3 × posiciones)` (GUIA §5.7)."""
    return {
        "type": WEB_SEARCH_TOOL,
        "name": "web_search",
        "max_uses": max(1, min(MAX_WEB_SEARCHES, SEARCHES_PER_POSITION * positions)),
    }


@dataclass(frozen=True)
class ExtractRequest:
    """Una extracción estructurada (paso B del mensual): sin prompt de sistema, sin esfuerzo y
    sin razonamiento, como en la guía."""

    model: str
    max_tokens: int
    prompt: str


@dataclass(frozen=True)
class Extraction:
    """Lo extraído (ya validado con el modelo pydantic) y lo que ha costado."""

    value: Any
    model: str
    usage: AIUsage
    attempts: int
    thinking_disabled: bool  # False si el modelo no admitía desactivar el razonamiento


def lower_effort(effort: str) -> str | None:
    """Un nivel menos de esfuerzo: high → medium → low. None si ya es low."""
    try:
        posicion = EFFORTS.index(effort)
    except ValueError:
        return EFFORTS[0]
    return EFFORTS[posicion - 1] if posicion > 0 else None


def _text_of(blocks: Iterable[Any]) -> str:
    """El texto de unos bloques. Los seguidos se juntan tal cual (las citas parten una frase en
    varios bloques); entre dos separados por una búsqueda u otro bloque, un salto de párrafo,
    para que «voy a buscar…» y la sección que viene después no acaben en la misma línea."""
    partes: list[str] = []
    separar = False
    for bloque in blocks:
        if getattr(bloque, "type", "") != "text":
            separar = bool(partes)
            continue
        texto = getattr(bloque, "text", "") or ""
        if separar and partes:
            antes = len(partes[-1]) - len(partes[-1].rstrip("\n"))
            despues = len(texto) - len(texto.lstrip("\n"))
            partes.append("\n" * max(0, 2 - min(antes, 2) - min(despues, 2)))
        partes.append(texto)
        separar = False
    return "".join(partes)


def _sources_of(blocks: Iterable[Any]) -> tuple[Source, ...]:
    """Las URL citadas en los bloques de texto, sin repetir y en el orden en que salen."""
    vistas: dict[str, Source] = {}
    for bloque in blocks:
        if getattr(bloque, "type", "") != "text":
            continue
        for cita in getattr(bloque, "citations", None) or ():
            url = str(getattr(cita, "url", "") or "").strip()
            if url and url not in vistas:
                titulo = " ".join(str(getattr(cita, "title", "") or "").split())
                vistas[url] = Source(url, titulo or url)
    return tuple(vistas.values())


def _usage_of(message: Any) -> AIUsage:
    uso = getattr(message, "usage", None)
    if uso is None:
        return AIUsage()
    servidor = getattr(uso, "server_tool_use", None)
    busquedas = getattr(servidor, "web_search_requests", 0) if servidor is not None else 0
    return AIUsage(
        int(getattr(uso, "input_tokens", 0) or 0),
        int(getattr(uso, "output_tokens", 0) or 0),
        int(busquedas or 0),
    )


def _error_type(error: anthropic.APIStatusError) -> str:
    """El tipo de error de la API (`overloaded_error`…), si viene en el cuerpo."""
    cuerpo = error.body
    if isinstance(cuerpo, dict):
        interior = cuerpo.get("error", cuerpo)
        if isinstance(interior, dict):
            return str(interior.get("type", ""))
    return ""


def _error_text(error: anthropic.APIStatusError) -> str:
    """El mensaje de un error de la API, con el del cuerpo, en minúsculas (para buscar en él)."""
    partes = [str(getattr(error, "message", "") or "")]
    cuerpo = error.body
    if isinstance(cuerpo, dict):
        interior = cuerpo.get("error", cuerpo)
        if isinstance(interior, dict):
            partes.append(str(interior.get("message", "")))
    return " ".join(partes).lower()


def _retry_after(error: anthropic.APIStatusError) -> float | None:
    with suppress(Exception):
        valor = error.response.headers.get("retry-after")
        if valor is not None:
            return min(MAX_RETRY_AFTER_S, max(0.0, float(valor)))
    return None


class _Retryable(Exception):
    def __init__(self, error: AIError, wait: float | None = None) -> None:
        super().__init__(error.message)
        self.error = error
        self.wait = wait


def _classify(error: Exception, model: str) -> AIError | _Retryable:
    """Qué hacer con un error del SDK: fallar ya (AIError) o reintentar (_Retryable)."""
    if isinstance(error, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return AIError(AIErrorKind.INVALID_KEY, "Clave no válida: Claude la ha rechazado.")
    if isinstance(error, anthropic.APIConnectionError):  # incluye el tiempo de espera agotado
        return _Retryable(AIError(AIErrorKind.NETWORK, "sin conexión con Claude."))
    if isinstance(error, anthropic.RateLimitError):
        return _Retryable(
            AIError(AIErrorKind.RATE_LIMIT, "Claude está limitando las peticiones (429)."),
            _retry_after(error),
        )
    if isinstance(error, anthropic.APIStatusError):
        codigo = error.status_code
        tipo = _error_type(error)
        texto = _error_text(error)
        if codigo == 402 or tipo == "billing_error" or "credit balance" in texto:
            return AIError(AIErrorKind.NO_CREDIT, "sin saldo en la cuenta de Claude.")
        if codigo >= 500 or tipo in _RETRYABLE_TYPES:
            return _Retryable(AIError(
                AIErrorKind.SERVER, f"Claude no está disponible ahora (código {codigo})."
            ))
        if isinstance(error, anthropic.NotFoundError):
            return AIError(
                AIErrorKind.REQUEST,
                f"el modelo «{model}» no existe o tu cuenta no tiene acceso a él (404).",
            )
        if codigo == 400 and "web search" in texto:
            return AIError(
                AIErrorKind.REQUEST,
                "la búsqueda web no está activada en tu organización de Claude (se activa en la "
                "Claude Console, en Privacidad).",
            )
        return AIError(AIErrorKind.REQUEST, f"Claude ha rechazado la petición (código {codigo}).")
    return AIError(AIErrorKind.UNKNOWN, f"fallo inesperado al hablar con Claude "
                                        f"({type(error).__name__}).")


class ClaudeClient:
    """Un cliente de Claude con la clave del usuario. Se usa desde un hilo de trabajo."""

    def __init__(
        self,
        api_key: str,
        *,
        wait: Callable[[float, threading.Event | None], None] | None = None,
        timeout: float = REQUEST_TIMEOUT_S,
        http_client: Any = None,
        sdk: Callable[..., Any] | None = None,
    ) -> None:
        # Sin reintentos del SDK: los hace este módulo (GUIA §5.7), así no se multiplican.
        # `http_client` y `sdk` solo los cambia la autocomprobación, para servir una respuesta
        # en local al SDK de verdad.
        opciones: dict[str, Any] = {"api_key": api_key, "max_retries": 0, "timeout": timeout}
        if http_client is not None:
            opciones["http_client"] = http_client
        self._client = (sdk or anthropic.Anthropic)(**opciones)
        self._wait = wait or _wait

    def close(self) -> None:
        with suppress(Exception):
            self._client.close()

    def __enter__(self) -> ClaudeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def list_models(self) -> list[str]:
        """Los modelos que puede usar la clave (`models.list`), para Ajustes."""
        try:
            return [m.id for m in self._client.models.list(limit=100)]
        except Exception as error:
            fallo = _classify(error, "")
            raise (fallo.error if isinstance(fallo, _Retryable) else fallo) from None

    def generate(self, request: AIRequest, cancel: threading.Event | None = None) -> AIResult:
        """El texto de Claude para `request`, con los reintentos de GUIA §5.7 y, si hay
        herramientas, los reenvíos de un turno en pausa. Lanza AIError (con lo gastado hasta
        entonces) si no lo consigue."""
        esfuerzo = request.effort
        sin_razonar = False
        rebajado = False
        gastado = AIUsage()
        intentos = 0
        while True:
            try:
                turno = self._turn(request, esfuerzo, sin_razonar, cancel)
            except AIError as error:
                raise error.with_usage(gastado + error.usage) from None
            intentos += turno.calls
            gastado += turno.usage
            texto = _text_of(turno.blocks).strip()
            motivo = turno.stop_reason
            if motivo == "refusal":
                log.warning("Claude ha declinado la petición (%s)", request.model)
                raise AIError(AIErrorKind.REFUSAL, "Claude ha declinado hacer el informe.",
                              gastado)
            if motivo == "max_tokens" and not texto:
                if rebajado:
                    raise AIError(
                        AIErrorKind.EMPTY,
                        "Claude agotó el margen razonando y no llegó a escribir el informe, "
                        "tampoco con menos esfuerzo.",
                        gastado,
                    )
                rebajado = True
                menor = lower_effort(esfuerzo)
                if menor is None:
                    sin_razonar = True
                    log.warning("Claude se ha quedado sin margen razonando en low: se reintenta "
                                "en low sin razonamiento")
                else:
                    log.warning("Claude se ha quedado sin margen razonando en %s: se reintenta "
                                "en %s", esfuerzo, menor)
                    esfuerzo = menor
                continue
            if not texto:
                raise AIError(AIErrorKind.EMPTY, "Claude no ha devuelto texto.", gastado)
            log.info(
                "Claude ha respondido (%s, esfuerzo %s%s, %d llamadas, %d+%d tokens, "
                "%d búsquedas, %s)",
                request.model, esfuerzo, " sin razonamiento" if sin_razonar else "", intentos,
                gastado.input_tokens, gastado.output_tokens, gastado.web_searches, motivo,
            )
            return AIResult(
                texto,
                turno.model or request.model,
                esfuerzo,
                gastado,
                motivo,
                intentos,
                sin_razonar,
                _sources_of(turno.blocks),
                turno.continuations,
            )

    def _turn(
        self,
        request: AIRequest,
        effort: str,
        no_thinking: bool,
        cancel: threading.Event | None,
    ) -> _Turn:
        """Un turno completo de Claude. Si se pausa (`pause_turn`, en la búsqueda web), se
        reenvía la conversación con lo que lleva escrito, tal cual, como mucho 3 veces."""
        escrito: list[Any] = []
        uso = AIUsage()
        llamadas = 0
        reenvios = 0
        while True:
            try:
                mensaje, n = self._with_retries(
                    partial(self._stream_once, request, effort, no_thinking, escrito, cancel),
                    request.model,
                    cancel,
                )
            except AIError as error:
                raise error.with_usage(uso + error.usage) from None
            llamadas += n
            uso += _usage_of(mensaje)
            escrito.extend(getattr(mensaje, "content", None) or [])
            motivo = str(getattr(mensaje, "stop_reason", "") or "")
            if motivo == "pause_turn" and reenvios < MAX_CONTINUATIONS:
                reenvios += 1
                log.info("Claude ha pausado el turno (búsqueda web): reenvío %d de %d",
                         reenvios, MAX_CONTINUATIONS)
                continue
            if motivo == "pause_turn":
                log.warning("Claude sigue en pausa tras %d reenvíos: se usa lo que haya escrito",
                            MAX_CONTINUATIONS)
            return _Turn(tuple(escrito), motivo, uso, llamadas, reenvios,
                         str(getattr(mensaje, "model", "") or ""))

    def _with_retries(
        self, call: Callable[[], Any], model: str, cancel: threading.Event | None
    ) -> tuple[Any, int]:
        """Una respuesta, con dos reintentos con espera si falla la red, hay un 429 o un 5xx.
        Devuelve la respuesta y cuántas llamadas ha hecho falta."""
        esperas = list(RETRY_WAITS_S)
        llamadas = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise AIError(AIErrorKind.CANCELLED, "cancelado.")
            llamadas += 1
            try:
                return call(), llamadas
            except (AIError, _ThinkingRejected):
                raise
            except Exception as error:
                fallo = _classify(error, model)
                if isinstance(fallo, AIError):
                    log.warning("Claude ha fallado sin reintento posible (%s, %s)",
                                type(error).__name__, getattr(error, "status_code", "sin código"))
                    raise fallo from None
                if not esperas:
                    log.warning("Claude sigue fallando tras los reintentos (%s)",
                                type(error).__name__)
                    raise fallo.error from None
                espera = esperas.pop(0)
                if fallo.wait is not None:
                    espera = max(espera, fallo.wait)
                log.info("Claude ha fallado (%s, %s): nuevo intento en %.0f s",
                         type(error).__name__, getattr(error, "status_code", "sin código"),
                         espera)
                self._wait(espera, cancel)

    def _stream_once(
        self,
        request: AIRequest,
        effort: str,
        no_thinking: bool,
        written: list[Any],
        cancel: threading.Event | None,
    ) -> Any:
        mensajes: list[dict[str, Any]] = [{"role": "user", "content": request.prompt}]
        if written:
            # La continuación de un turno en pausa: lo escrito, tal cual y sin añadir nada.
            mensajes.append({"role": "assistant", "content": list(written)})
        parametros: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": mensajes,
            "output_config": {"effort": effort},
        }
        if request.tools:
            parametros["tools"] = [dict(t) for t in request.tools]
        if no_thinking:
            parametros["thinking"] = {"type": "disabled"}
        with self._client.messages.stream(**parametros) as stream:
            for _evento in stream:
                if cancel is not None and cancel.is_set():
                    # Al salir de la app: se corta la respuesta y no se guarda nada.
                    raise AIError(AIErrorKind.CANCELLED, "cancelado.")
            return stream.get_final_message()

    # -- extracción estructurada --------------------------------------------------------

    def extract(
        self,
        request: ExtractRequest,
        schema: type[pydantic.BaseModel],
        cancel: threading.Event | None = None,
    ) -> Extraction:
        """Datos estructurados con `messages.parse` y el modelo pydantic `schema`, sin
        razonamiento (GUIA §5.7, paso B del mensual). Si el modelo no admite desactivarlo, se
        repite una vez sin ese parámetro y con más `max_tokens`. Lanza AIError si no se
        consigue."""
        sin_razonar = True
        max_tokens = request.max_tokens
        gastado = AIUsage()
        intentos = 0
        while True:
            try:
                mensaje, n = self._with_retries(
                    partial(self._parse_once, request, schema, max_tokens, sin_razonar),
                    request.model,
                    cancel,
                )
            except _ThinkingRejected:
                if not sin_razonar:
                    raise AIError(AIErrorKind.REQUEST, "Claude ha rechazado la extracción.",
                                  gastado) from None
                intentos += 1
                sin_razonar = False
                max_tokens = max(max_tokens, EXTRACTION_THINKING_MAX_TOKENS)
                log.info("%s no admite desactivar el razonamiento: la extracción se repite sin "
                         "ese parámetro y con %d max_tokens", request.model, max_tokens)
                continue
            except AIError as error:
                raise error.with_usage(gastado + error.usage) from None
            intentos += n
            gastado += _usage_of(mensaje)
            motivo = str(getattr(mensaje, "stop_reason", "") or "")
            if motivo == "refusal":
                raise AIError(AIErrorKind.REFUSAL, "Claude ha declinado la extracción.", gastado)
            valor = getattr(mensaje, "parsed_output", None)
            if valor is None:
                raise AIError(AIErrorKind.EMPTY, "la extracción no ha devuelto datos.", gastado)
            log.info("Extracción hecha (%s, %d llamadas, %d+%d tokens)", request.model,
                     intentos, gastado.input_tokens, gastado.output_tokens)
            return Extraction(valor, str(getattr(mensaje, "model", "") or request.model),
                              gastado, intentos, sin_razonar)

    def _parse_once(
        self,
        request: ExtractRequest,
        schema: type[pydantic.BaseModel],
        max_tokens: int,
        no_thinking: bool,
    ) -> Any:
        parametros: dict[str, Any] = {
            "model": request.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": request.prompt}],
            "output_format": schema,
        }
        if no_thinking:
            parametros["thinking"] = {"type": "disabled"}
        try:
            return self._client.messages.parse(**parametros)
        except anthropic.BadRequestError as error:
            if no_thinking and "thinking" in _error_text(error):
                raise _ThinkingRejected from None
            raise
        except pydantic.ValidationError:
            # Cortada o no válida: el SDK no deja ver sus tokens, así que no se inventan.
            log.warning("La respuesta de la extracción no se ha podido leer")
            raise AIError(
                AIErrorKind.INVALID_OUTPUT,
                "la respuesta de la extracción no se ha podido leer (cortada o no válida).",
                unmeasured=True,
            ) from None


@dataclass(frozen=True)
class _Turn:
    """Un turno completo de Claude: todos sus bloques (de todas las partes, si se pausó)."""

    blocks: tuple[Any, ...]
    stop_reason: str
    usage: AIUsage
    calls: int
    continuations: int
    model: str


class _ThinkingRejected(Exception):
    """El modelo no admite `thinking={"type": "disabled"}` (400)."""


def _wait(seconds: float, cancel: threading.Event | None) -> None:
    """Espera, pero se despierta si se cancela."""
    if cancel is not None:
        cancel.wait(seconds)
    else:
        threading.Event().wait(seconds)
