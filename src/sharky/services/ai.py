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

La clave nunca se registra: de los errores solo se anotan el tipo y el código HTTP.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import anthropic

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
    de IA: …»); `usage` es lo que se ha gastado de todos modos."""

    def __init__(self, kind: AIErrorKind, message: str, usage: AIUsage | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.usage = usage or AIUsage()

    def with_usage(self, usage: AIUsage) -> AIError:
        return AIError(self.kind, self.message, usage)


@dataclass(frozen=True)
class AIRequest:
    """Una petición de texto a Claude."""

    model: str
    effort: str
    max_tokens: int
    system: str
    prompt: str


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

    @property
    def truncated(self) -> bool:
        """Se cortó por `max_tokens` con parte del texto ya escrito."""
        return self.stop_reason == "max_tokens"

    @property
    def effort_text(self) -> str:
        return f"{self.effort} sin razonamiento" if self.thinking_disabled else self.effort


def lower_effort(effort: str) -> str | None:
    """Un nivel menos de esfuerzo: high → medium → low. None si ya es low."""
    try:
        posicion = EFFORTS.index(effort)
    except ValueError:
        return EFFORTS[0]
    return EFFORTS[posicion - 1] if posicion > 0 else None


def _text_of(message: Any) -> str:
    return "".join(
        getattr(b, "text", "") for b in (message.content or []) if getattr(b, "type", "") == "text"
    )


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
        texto = str(getattr(error, "message", "") or "").lower()
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
        """El texto de Claude para `request`, con los reintentos de GUIA §5.7. Lanza AIError
        (con lo gastado hasta entonces) si no lo consigue."""
        esfuerzo = request.effort
        sin_razonar = False
        rebajado = False
        gastado = AIUsage()
        intentos = 0
        while True:
            try:
                mensaje, llamadas = self._stream_with_retries(request, esfuerzo, sin_razonar,
                                                              cancel)
            except AIError as error:
                raise error.with_usage(gastado + error.usage) from None
            intentos += llamadas
            gastado += _usage_of(mensaje)
            texto = _text_of(mensaje).strip()
            motivo = str(getattr(mensaje, "stop_reason", "") or "")
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
                "Claude ha respondido (%s, esfuerzo %s%s, %d intentos, %d+%d tokens, %s)",
                request.model, esfuerzo, " sin razonamiento" if sin_razonar else "", intentos,
                gastado.input_tokens, gastado.output_tokens, motivo,
            )
            return AIResult(
                texto,
                str(getattr(mensaje, "model", "") or request.model),
                esfuerzo,
                gastado,
                motivo,
                intentos,
                sin_razonar,
            )

    def _stream_with_retries(
        self,
        request: AIRequest,
        effort: str,
        no_thinking: bool,
        cancel: threading.Event | None,
    ) -> tuple[Any, int]:
        """Una respuesta completa, con dos reintentos con espera si falla la red, hay un 429 o
        un 5xx. Devuelve el mensaje y cuántas llamadas ha hecho falta."""
        esperas = list(RETRY_WAITS_S)
        llamadas = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise AIError(AIErrorKind.CANCELLED, "cancelado.")
            llamadas += 1
            try:
                return self._stream_once(request, effort, no_thinking, cancel), llamadas
            except AIError:
                raise
            except Exception as error:
                fallo = _classify(error, request.model)
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
        cancel: threading.Event | None,
    ) -> Any:
        parametros: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.prompt}],
            "output_config": {"effort": effort},
        }
        if no_thinking:
            parametros["thinking"] = {"type": "disabled"}
        with self._client.messages.stream(**parametros) as stream:
            for _evento in stream:
                if cancel is not None and cancel.is_set():
                    # Al salir de la app: se corta la respuesta y no se guarda nada.
                    raise AIError(AIErrorKind.CANCELLED, "cancelado.")
            return stream.get_final_message()


def _wait(seconds: float, cancel: threading.Event | None) -> None:
    """Espera, pero se despierta si se cancela."""
    if cancel is not None:
        cancel.wait(seconds)
    else:
        threading.Event().wait(seconds)
