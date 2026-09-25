"""El cliente de Claude con un doble sin red y sin clave real: «Probar clave» (`models.list`) y
los informes por streaming, con sus reintentos (GUIA §5.7)."""

import logging
import threading
from dataclasses import replace

import anthropic
import pytest

from fakes import api_status_error, connection_error, mensaje_claude, timeout_error
from sharky.services.ai import (
    KEY_CHECK_RETRIES,
    KEY_CHECK_TIMEOUT_S,
    RETRY_WAITS_S,
    AIError,
    AIErrorKind,
    AIRequest,
    ClaudeClient,
    KeyStatus,
    check_api_key,
    lower_effort,
)

CLAVE = "sk-ant-inventada-0000"


def test_clave_valida(claude_falso):
    resultado = check_api_key(f"  {CLAVE}  ")
    assert resultado.status is KeyStatus.VALID and resultado.usable
    # Una sola llamada barata, con la clave limpia, poco tiempo de espera y cerrando el cliente.
    assert claude_falso.creados == [
        {"api_key": CLAVE, "timeout": KEY_CHECK_TIMEOUT_S, "max_retries": KEY_CHECK_RETRIES}
    ]
    assert claude_falso.llamadas == [("models.list", {"limit": 1})]
    assert claude_falso.cerrados == 1


@pytest.mark.parametrize(
    "error",
    [
        api_status_error(anthropic.AuthenticationError, 401),
        api_status_error(anthropic.PermissionDeniedError, 403),
    ],
    ids=["401", "403"],
)
def test_clave_rechazada(claude_falso, error):
    claude_falso.error = error
    resultado = check_api_key(CLAVE)
    assert resultado.status is KeyStatus.INVALID and not resultado.usable
    assert "Clave no válida" in resultado.message


@pytest.mark.parametrize("error", [connection_error(), timeout_error()], ids=["red", "espera"])
def test_sin_conexion_se_guarda_sin_comprobar(claude_falso, error):
    claude_falso.error = error
    resultado = check_api_key(CLAVE)
    assert resultado.status is KeyStatus.OFFLINE and resultado.usable
    assert "sin comprobar" in resultado.message


@pytest.mark.parametrize(
    "error",
    [
        api_status_error(anthropic.RateLimitError, 429),
        api_status_error(anthropic.InternalServerError, 500),
    ],
    ids=["429", "500"],
)
def test_otros_errores_no_la_invalidan(claude_falso, error):
    claude_falso.error = error
    resultado = check_api_key(CLAVE)
    assert resultado.status is KeyStatus.UNVERIFIED and resultado.usable
    assert str(error.status_code) in resultado.message


def test_un_fallo_inesperado_tampoco_revienta(claude_falso):
    claude_falso.error = RuntimeError("algo raro")
    assert check_api_key(CLAVE).status is KeyStatus.UNVERIFIED


def test_clave_vacia_ni_se_intenta(claude_falso):
    assert check_api_key("   ").status is KeyStatus.INVALID
    assert claude_falso.creados == []


@pytest.mark.parametrize(
    "error",
    [None, api_status_error(anthropic.AuthenticationError, 401), connection_error()],
    ids=["valida", "rechazada", "sin_red"],
)
def test_la_clave_nunca_llega_al_registro(claude_falso, caplog, error):
    claude_falso.error = error
    with caplog.at_level(logging.DEBUG):
        check_api_key(CLAVE)
    assert caplog.records  # algo se registra…
    assert CLAVE not in caplog.text  # …pero nunca la clave


# -- informes por streaming ---------------------------------------------------------------------

PETICION = AIRequest(
    model="claude-sonnet-5", effort="low", max_tokens=16_000, system="Eres Sharky.",
    prompt="Control diario del 25/09/2026.",
)


class Esperas:
    """Apunta las esperas entre reintentos en vez de dormir."""

    def __init__(self):
        self.segundos: list[float] = []

    def __call__(self, segundos, _cancel):
        self.segundos.append(segundos)


@pytest.fixture
def esperas():
    return Esperas()


@pytest.fixture
def cliente(claude_falso, esperas):
    with ClaudeClient(CLAVE, wait=esperas) as c:
        yield c


def pedir(cliente, esfuerzo="low"):
    return cliente.generate(replace(PETICION, effort=esfuerzo))


def test_siempre_por_streaming_con_sistema_y_esfuerzo(cliente, claude_falso):
    resultado = pedir(cliente)
    assert "## Conclusión del día" in resultado.text
    assert claude_falso.streams == [{
        "model": "claude-sonnet-5",
        "max_tokens": 16_000,
        "system": "Eres Sharky.",
        "messages": [{"role": "user", "content": "Control diario del 25/09/2026."}],
        "output_config": {"effort": "low"},
    }]
    assert resultado.usage.input_tokens == 2_000 and resultado.usage.output_tokens == 600
    assert resultado.effort == "low" and resultado.attempts == 1 and not resultado.truncated


def test_el_sdk_no_reintenta_por_su_cuenta(cliente, claude_falso):
    # Los reintentos los cuenta Sharky: si el SDK también reintentara, se multiplicarían.
    assert claude_falso.creados[-1]["max_retries"] == 0
    assert claude_falso.creados[-1]["api_key"] == CLAVE


@pytest.mark.parametrize(("pedido", "reintento"), [("high", "medium"), ("medium", "low")])
def test_solo_razonamiento_y_max_tokens_reintenta_con_menos_esfuerzo(
    cliente, claude_falso, pedido, reintento
):
    claude_falso.respuestas = [
        mensaje_claude("", stop="max_tokens", razonamiento=True, entrada=3_000, salida=16_000),
        mensaje_claude(entrada=3_000, salida=900),
    ]
    resultado = pedir(cliente, pedido)
    esfuerzos = [s["output_config"]["effort"] for s in claude_falso.streams]
    assert esfuerzos == [pedido, reintento]
    assert resultado.effort == reintento and resultado.attempts == 2
    # Los dos intentos se cobran: se suman.
    assert resultado.usage.input_tokens == 6_000 and resultado.usage.output_tokens == 16_900


def test_en_low_el_reintento_va_sin_razonamiento(cliente, claude_falso):
    claude_falso.respuestas = [
        mensaje_claude("", stop="max_tokens", razonamiento=True),
        mensaje_claude(),
    ]
    resultado = pedir(cliente, "low")
    primero, segundo = claude_falso.streams
    assert "thinking" not in primero
    assert segundo["thinking"] == {"type": "disabled"}
    assert segundo["output_config"] == {"effort": "low"}
    assert resultado.thinking_disabled and resultado.effort_text == "low sin razonamiento"


def test_un_solo_reintento_por_falta_de_texto(cliente, claude_falso):
    claude_falso.respuestas = [
        mensaje_claude("", stop="max_tokens", razonamiento=True, salida=16_000),
        mensaje_claude("", stop="max_tokens", razonamiento=True, salida=16_000),
        mensaje_claude(),
    ]
    with pytest.raises(AIError) as fallo:
        pedir(cliente, "medium")
    assert fallo.value.kind is AIErrorKind.EMPTY
    assert fallo.value.usage.output_tokens == 32_000  # lo gastado viaja con el error
    assert len(claude_falso.streams) == 2


def test_cortado_con_texto_se_da_por_bueno(cliente, claude_falso):
    claude_falso.respuestas = [mensaje_claude("Análisis a medi", stop="max_tokens")]
    resultado = pedir(cliente)
    assert resultado.truncated and resultado.text == "Análisis a medi"
    assert len(claude_falso.streams) == 1


def test_red_dos_reintentos_con_espera_y_luego_falla(cliente, claude_falso, esperas):
    claude_falso.respuestas = [connection_error(), timeout_error(), connection_error()]
    with pytest.raises(AIError) as fallo:
        pedir(cliente)
    assert fallo.value.kind is AIErrorKind.NETWORK
    assert "sin conexión" in fallo.value.message
    assert len(claude_falso.streams) == 3
    assert esperas.segundos == list(RETRY_WAITS_S)


def test_la_red_vuelve_en_el_reintento(cliente, claude_falso, esperas):
    claude_falso.respuestas = [connection_error(), mensaje_claude()]
    assert pedir(cliente).attempts == 2
    assert esperas.segundos == [RETRY_WAITS_S[0]]


@pytest.mark.parametrize(
    "error",
    [
        api_status_error(anthropic.InternalServerError, 500),
        api_status_error(anthropic.OverloadedError, 529),
        # Un error dentro del streaming llega con HTTP 200 y su tipo en el cuerpo.
        api_status_error(anthropic.APIStatusError, 200,
                         body={"type": "error", "error": {"type": "overloaded_error"}}),
    ],
    ids=["500", "529", "sobrecarga_en_el_stream"],
)
def test_los_5xx_se_reintentan(cliente, claude_falso, error):
    claude_falso.respuestas = [error, mensaje_claude()]
    assert pedir(cliente).attempts == 2


def test_un_429_espera_lo_que_pide(cliente, claude_falso, esperas):
    claude_falso.respuestas = [
        api_status_error(anthropic.RateLimitError, 429, headers={"retry-after": "20"}),
        mensaje_claude(),
    ]
    pedir(cliente)
    assert esperas.segundos == [20.0]


@pytest.mark.parametrize(
    "error",
    [
        api_status_error(anthropic.AuthenticationError, 401),
        api_status_error(anthropic.PermissionDeniedError, 403),
    ],
    ids=["401", "403"],
)
def test_clave_no_valida_sin_reintentos(cliente, claude_falso, esperas, error):
    claude_falso.respuestas = [error, mensaje_claude()]
    with pytest.raises(AIError) as fallo:
        pedir(cliente)
    assert fallo.value.kind is AIErrorKind.INVALID_KEY
    assert fallo.value.message.startswith("Clave no válida")
    assert len(claude_falso.streams) == 1 and esperas.segundos == []


def test_sin_saldo_por_400(cliente, claude_falso):
    error = api_status_error(anthropic.BadRequestError, 400)
    error.message = "Your credit balance is too low to access the Anthropic API."
    claude_falso.respuestas = [error]
    with pytest.raises(AIError) as fallo:
        pedir(cliente)
    assert fallo.value.kind is AIErrorKind.NO_CREDIT
    assert len(claude_falso.streams) == 1


def test_sin_saldo_por_402(cliente, claude_falso):
    claude_falso.respuestas = [api_status_error(
        anthropic.APIStatusError, 402, body={"type": "error", "error": {"type": "billing_error"}}
    )]
    with pytest.raises(AIError) as fallo:
        pedir(cliente)
    assert fallo.value.kind is AIErrorKind.NO_CREDIT


def test_modelo_que_no_existe(cliente, claude_falso):
    claude_falso.respuestas = [api_status_error(anthropic.NotFoundError, 404)]
    with pytest.raises(AIError) as fallo:
        pedir(cliente)
    assert fallo.value.kind is AIErrorKind.REQUEST and "claude-sonnet-5" in fallo.value.message


def test_un_rechazo_no_se_reintenta(cliente, claude_falso):
    claude_falso.respuestas = [mensaje_claude("", stop="refusal", salida=10), mensaje_claude()]
    with pytest.raises(AIError) as fallo:
        pedir(cliente)
    assert fallo.value.kind is AIErrorKind.REFUSAL
    assert fallo.value.usage.output_tokens == 10
    assert len(claude_falso.streams) == 1


def test_sin_texto_y_sin_cortarse_es_un_fallo(cliente, claude_falso):
    claude_falso.respuestas = [mensaje_claude("", razonamiento=True)]
    with pytest.raises(AIError) as fallo:
        pedir(cliente)
    assert fallo.value.kind is AIErrorKind.EMPTY


def test_cancelar_corta_el_streaming(claude_falso):
    cancelar = threading.Event()
    claude_falso.durante_stream = lambda n: cancelar.set() if n == 1 else None
    with ClaudeClient(CLAVE, wait=Esperas()) as cliente:
        with pytest.raises(AIError) as fallo:
            cliente.generate(PETICION, cancelar)
    assert fallo.value.kind is AIErrorKind.CANCELLED


def test_las_busquedas_web_se_cuentan(cliente, claude_falso):
    claude_falso.respuestas = [mensaje_claude(busquedas=4)]
    assert pedir(cliente).usage.web_searches == 4


def test_lista_de_modelos(cliente, claude_falso):
    claude_falso.modelos = ["claude-sonnet-5", "claude-opus-5"]
    assert cliente.list_models() == ["claude-sonnet-5", "claude-opus-5"]


def test_un_nivel_menos_de_esfuerzo():
    assert lower_effort("high") == "medium"
    assert lower_effort("medium") == "low"
    assert lower_effort("low") is None


@pytest.mark.parametrize(
    "respuesta",
    [mensaje_claude(), api_status_error(anthropic.AuthenticationError, 401), connection_error()],
    ids=["bien", "clave_no_valida", "sin_red"],
)
def test_la_clave_nunca_llega_al_registro_en_los_informes(cliente, claude_falso, caplog,
                                                          respuesta):
    claude_falso.respuestas = [respuesta, respuesta, respuesta]
    with caplog.at_level(logging.DEBUG):
        try:
            pedir(cliente)
        except AIError:
            pass
    assert caplog.records
    assert CLAVE not in caplog.text
