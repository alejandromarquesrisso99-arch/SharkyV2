"""El cliente de Claude con un doble sin red y sin clave real: «Probar clave» (`models.list`),
los informes por streaming con sus reintentos, la búsqueda web con `pause_turn` y sus fuentes, y
la extracción estructurada del mensual (GUIA §5.7)."""

import logging
import threading
from dataclasses import replace

import anthropic
import pydantic
import pytest

from fakes import (
    api_status_error,
    busqueda,
    connection_error,
    extraccion,
    mensaje_claude,
    texto_citado,
    timeout_error,
)
from sharky.services.ai import (
    KEY_CHECK_RETRIES,
    KEY_CHECK_TIMEOUT_S,
    RETRY_WAITS_S,
    AIError,
    AIErrorKind,
    AIRequest,
    AIUsage,
    ClaudeClient,
    ExtractRequest,
    KeyStatus,
    check_api_key,
    lower_effort,
    web_search_tool,
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


# -- búsqueda web y pause_turn (H10) ------------------------------------------------------------

SEMANAL = replace(
    PETICION, effort="medium", max_tokens=32_000, prompt="Escaneo semanal de noticias.",
    tools=(web_search_tool(3),),
)


def test_la_herramienta_de_busqueda_de_la_guia():
    assert web_search_tool(3) == {"type": "web_search_20250305", "name": "web_search",
                                  "max_uses": 9}
    assert web_search_tool(20)["max_uses"] == 30  # min(30, 3 × posiciones)
    assert web_search_tool(1)["max_uses"] == 3


def test_pause_turn_reenvia_la_respuesta_tal_cual_y_junta_las_partes(cliente, claude_falso):
    primera = mensaje_claude(bloques=[
        texto_citado("Voy a buscar. "),
        *busqueda("Banco Santander noticias", "https://ejemplo.es/san"),
        texto_citado("## SAN\nResultados mejores de lo esperado.",
                     ("https://ejemplo.es/san", "SAN mejora")),
    ], stop="pause_turn", entrada=5_000, salida=300, busquedas=2)
    segunda = mensaje_claude(bloques=[
        *busqueda("iShares MSCI World", "https://ejemplo.es/iwda"),
        texto_citado("\n\n## IWDA\nNada relevante.", ("https://ejemplo.es/iwda", "IWDA"),
                     ("https://ejemplo.es/san", "SAN mejora (repetida)")),
        texto_citado("\n\n## Conclusión de la semana\n- Vigilar SAN."),
    ], entrada=7_000, salida=500, busquedas=1)
    claude_falso.respuestas = [primera, segunda]
    resultado = cliente.generate(SEMANAL)
    primera_llamada, segunda_llamada = claude_falso.streams
    # La primera, con la herramienta; la segunda, con la respuesta tal cual y nada más.
    assert primera_llamada["tools"] == [web_search_tool(3)]
    assert primera_llamada["messages"] == [{"role": "user",
                                            "content": "Escaneo semanal de noticias."}]
    usuario, asistente = segunda_llamada["messages"]
    assert usuario == primera_llamada["messages"][0]
    assert asistente["role"] == "assistant"
    assert len(asistente["content"]) == len(primera.content)
    assert all(a is b for a, b in zip(asistente["content"], primera.content, strict=True))
    assert segunda_llamada["tools"] == [web_search_tool(3)]
    # El texto y las fuentes salen de las dos partes; los costes se suman.
    assert "## SAN" in resultado.text and "## Conclusión de la semana" in resultado.text
    assert [(s.url, s.title) for s in resultado.sources] == [
        ("https://ejemplo.es/san", "SAN mejora"), ("https://ejemplo.es/iwda", "IWDA"),
    ]
    assert resultado.usage == AIUsage(12_000, 800, 3)
    assert resultado.continuations == 1 and resultado.attempts == 2
    assert resultado.stop_reason == "end_turn" and not resultado.paused


def test_pause_turn_como_mucho_tres_reenvios(cliente, claude_falso):
    claude_falso.respuestas = [
        mensaje_claude(f"parte {n}. ", stop="pause_turn") for n in range(5)
    ]
    resultado = cliente.generate(SEMANAL)
    assert len(claude_falso.streams) == 4  # la primera y tres reenvíos
    assert resultado.continuations == 3 and resultado.paused
    assert resultado.text == "parte 0. parte 1. parte 2. parte 3."
    # El último reenvío lleva todo lo escrito antes, en orden.
    ultimo = claude_falso.streams[-1]["messages"][1]["content"]
    assert [b.text for b in ultimo] == ["parte 0. ", "parte 1. ", "parte 2. "]


def test_en_pausa_y_sin_texto_es_un_fallo(cliente, claude_falso):
    claude_falso.respuestas = [
        mensaje_claude(bloques=busqueda(f"q{n}", "https://x.es"), stop="pause_turn",
                       busquedas=1)
        for n in range(4)
    ]
    with pytest.raises(AIError) as fallo:
        cliente.generate(SEMANAL)
    assert fallo.value.kind is AIErrorKind.EMPTY
    assert fallo.value.usage.web_searches == 4  # lo gastado se cuenta igual


def test_un_fallo_en_el_reenvio_lleva_lo_gastado_antes(cliente, claude_falso):
    claude_falso.respuestas = [
        mensaje_claude("## SAN\n", stop="pause_turn", entrada=5_000, salida=100, busquedas=2),
        api_status_error(anthropic.AuthenticationError, 401),
    ]
    with pytest.raises(AIError) as fallo:
        cliente.generate(SEMANAL)
    assert fallo.value.kind is AIErrorKind.INVALID_KEY
    assert fallo.value.usage == AIUsage(5_000, 100, 2)


def test_la_red_falla_en_el_reenvio_y_se_reintenta_el_reenvio(cliente, claude_falso, esperas):
    primera = mensaje_claude("## SAN\nAlgo. ", stop="pause_turn")
    claude_falso.respuestas = [primera, connection_error(), mensaje_claude("Fin.")]
    resultado = cliente.generate(SEMANAL)
    assert esperas.segundos == [RETRY_WAITS_S[0]]
    assert resultado.text == "## SAN\nAlgo. Fin."
    # El reintento vuelve a llevar la parte ya escrita.
    assert claude_falso.streams[2]["messages"][1]["content"][0] is primera.content[0]


def test_el_texto_de_antes_y_despues_de_una_busqueda_no_se_pega(cliente, claude_falso):
    claude_falso.respuestas = [mensaje_claude(bloques=[
        texto_citado("Voy a buscar."),
        *busqueda("SAN", "https://ejemplo.es"),
        texto_citado("## SAN\nUna frase "),
        texto_citado("con cita.", ("https://ejemplo.es", "Web")),
    ])]
    assert cliente.generate(SEMANAL).text == "Voy a buscar.\n\n## SAN\nUna frase con cita."


def test_sin_herramientas_no_se_manda_tools(cliente, claude_falso):
    pedir(cliente)
    assert "tools" not in claude_falso.streams[0]


def test_busqueda_web_desactivada_en_la_organizacion(cliente, claude_falso):
    claude_falso.respuestas = [api_status_error(
        anthropic.BadRequestError, 400,
        body={"type": "error", "error": {"type": "invalid_request_error",
                                         "message": "Web search is not enabled for this org"}},
    )]
    with pytest.raises(AIError) as fallo:
        cliente.generate(SEMANAL)
    assert "búsqueda web no está activada" in fallo.value.message


# -- extracción estructurada (paso B del mensual) ------------------------------------------------


class Veredictos(pydantic.BaseModel):
    verdicts: list[str]


EXTRACCION = ExtractRequest(model="claude-sonnet-5", max_tokens=8_000,
                            prompt="Del estudio siguiente, extrae…")


def error_de_validacion() -> pydantic.ValidationError:
    try:
        Veredictos.model_validate_json('{"verdicts": [')
    except pydantic.ValidationError as error:
        return error
    raise AssertionError("tenía que fallar")


def test_extraccion_con_parse_sin_razonamiento(cliente, claude_falso):
    claude_falso.extracciones = [extraccion(Veredictos(verdicts=["MANTENER"]))]
    hecho = cliente.extract(EXTRACCION, Veredictos)
    assert hecho.value.verdicts == ["MANTENER"]
    assert hecho.usage == AIUsage(3_000, 400) and hecho.thinking_disabled
    assert claude_falso.parses == [{
        "model": "claude-sonnet-5",
        "max_tokens": 8_000,
        "messages": [{"role": "user", "content": "Del estudio siguiente, extrae…"}],
        "output_format": Veredictos,
        "thinking": {"type": "disabled"},
    }]
    assert claude_falso.streams == []  # no va por streaming ni con esfuerzo


def test_si_el_modelo_no_admite_desactivar_el_razonamiento_se_quita(cliente, claude_falso):
    claude_falso.extracciones = [
        api_status_error(anthropic.BadRequestError, 400, body={
            "type": "error", "error": {"type": "invalid_request_error",
                                       "message": "thinking.type: disabled is not supported"}}),
        extraccion(Veredictos(verdicts=[])),
    ]
    hecho = cliente.extract(EXTRACCION, Veredictos)
    primera, segunda = claude_falso.parses
    assert primera["thinking"] == {"type": "disabled"}
    assert "thinking" not in segunda and segunda["max_tokens"] == 16_000
    assert not hecho.thinking_disabled


def test_otro_400_no_se_confunde_con_el_razonamiento(cliente, claude_falso):
    claude_falso.extracciones = [api_status_error(anthropic.BadRequestError, 400)]
    with pytest.raises(AIError) as fallo:
        cliente.extract(EXTRACCION, Veredictos)
    assert fallo.value.kind is AIErrorKind.REQUEST and len(claude_falso.parses) == 1


def test_extraccion_ilegible_no_inventa_el_coste(cliente, claude_falso):
    claude_falso.extracciones = [error_de_validacion()]
    with pytest.raises(AIError) as fallo:
        cliente.extract(EXTRACCION, Veredictos)
    assert fallo.value.kind is AIErrorKind.INVALID_OUTPUT
    assert fallo.value.unmeasured and fallo.value.usage == AIUsage()


def test_extraccion_con_reintentos_de_red(cliente, claude_falso, esperas):
    claude_falso.extracciones = [connection_error(), extraccion(Veredictos(verdicts=[]))]
    assert cliente.extract(EXTRACCION, Veredictos).attempts == 2
    assert esperas.segundos == [RETRY_WAITS_S[0]]


def test_extraccion_vacia_o_rechazada(cliente, claude_falso):
    claude_falso.extracciones = [extraccion(None)]
    with pytest.raises(AIError) as fallo:
        cliente.extract(EXTRACCION, Veredictos)
    assert fallo.value.kind is AIErrorKind.EMPTY and fallo.value.usage.input_tokens == 3_000
    claude_falso.extracciones = [extraccion(None, stop="refusal")]
    with pytest.raises(AIError) as fallo:
        cliente.extract(EXTRACCION, Veredictos)
    assert fallo.value.kind is AIErrorKind.REFUSAL
