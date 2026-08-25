"""Tests del cliente de Ollama (sin red real: httpx.MockTransport).

Modo elegido (empirico, ver plan): think=false + format='json' generico + la
forma del JSON pedida en el prompt. El schema-grammar de Ollama rompe con
modelos de thinking (bug #15260 / thinking se come el budget), asi que NO se usa;
validamos las claves nosotros en el scorer.
"""
import json

import httpx
import pytest

from src.llm import EmptyCompletionError, OllamaClient


def _client_capturando(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        body = {"message": {"content": json.dumps({"rating_label": "buena"})}}
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_chat_json_envia_params_correctos_y_parsea():
    captured = {}
    llm = OllamaClient(
        "http://ollama:11434", "qwen3.5:4b",
        num_ctx=16384, num_predict=2048, client=_client_capturando(captured),
    )

    out = llm.chat_json("system prompt", "user prompt")

    assert out == {"rating_label": "buena"}
    assert captured["url"].endswith("/api/chat")
    p = captured["payload"]
    assert p["model"] == "qwen3.5:4b"
    assert p["format"] == "json"          # JSON generico, NO schema
    assert p["think"] is False            # sin thinking: rapido y no rompe el JSON
    assert p["stream"] is False
    assert p["options"]["temperature"] == 0
    assert p["options"]["num_ctx"] == 16384
    assert p["options"]["num_predict"] == 2048
    assert p["messages"][1]["content"] == "user prompt"


def test_chat_json_extrae_json_entre_fences():
    def handler(request):
        content = "```json\n{\"rating_label\": \"mala\"}\n```"
        return httpx.Response(200, json={"message": {"content": content}})

    llm = OllamaClient(
        "http://ollama:11434", "qwen3.5:4b",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert llm.chat_json("s", "u") == {"rating_label": "mala"}


def test_chat_json_reintenta_fast_y_cae_a_grammar():
    calls = []

    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        if p["format"] == "json":            # fast path -> prosa no parseable (flaky)
            return httpx.Response(200, json={"message": {"content": "Basado en el historial..."}})
        # grammar (format=schema) -> JSON valido
        return httpx.Response(200, json={"message": {"content": '{"ok": true}'}})

    llm = OllamaClient(
        "http://ollama:11434", "qwen3.5:4b", num_predict=1024,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    out = llm.chat_json("s", "u", schema={"type": "object"})

    assert out == {"ok": True}
    fast = [c for c in calls if c["format"] == "json"]
    grammar = [c for c in calls if c["format"] != "json"]
    assert len(fast) == 3          # reintenta el fast varias veces
    assert len(grammar) == 1       # y cae al grammar una vez
    assert grammar[0].get("think") is not False  # grammar deja thinking activo


def test_chat_json_sin_schema_y_sin_salida_levanta_error():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(200, json={"message": {"content": ""}})

    llm = OllamaClient(
        "http://ollama:11434", "qwen3.5:4b", num_predict=1024,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(EmptyCompletionError):
        llm.chat_json("s", "u")     # sin schema -> no hay fallback grammar
    assert attempts["n"] == 3        # 3 intentos del fast (default)


def test_fast_attempts_configurable_acota_los_reintentos():
    """fast_attempts recorta cuántas veces se prueba el camino rápido (cada intento
    fallido gasta hasta el timeout completo cuando el endpoint corta)."""
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(200, json={"message": {"content": ""}})

    llm = OllamaClient(
        "http://ollama:11434", "qwen3.5:4b", fast_attempts=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(EmptyCompletionError):
        llm.chat_json("s", "u")
    assert attempts["n"] == 1        # un solo intento, no 3


def test_chat_json_propaga_error_http():
    def handler(request):
        return httpx.Response(500, text="boom")

    llm = OllamaClient(
        "http://ollama:11434", "qwen3.5:4b",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        llm.chat_json("s", "u")


def test_contador_fast_incrementa_en_camino_rapido():
    captured = {}
    llm = OllamaClient("http://ollama:11434", "qwen3.5:4b", client=_client_capturando(captured))
    assert llm.calls == {"fast": 0, "fallback": 0, "empty": 0}
    llm.chat_json("s", "u")
    llm.chat_json("s", "u")
    assert llm.calls == {"fast": 2, "fallback": 0, "empty": 0}


def test_contador_fallback_incrementa_cuando_cae_al_grammar():
    def handler(request):
        p = json.loads(request.content)
        if p["format"] == "json":
            return httpx.Response(200, json={"message": {"content": "prosa no parseable"}})
        return httpx.Response(200, json={"message": {"content": '{"ok": true}'}})

    llm = OllamaClient("http://ollama:11434", "qwen3.5:4b", num_predict=512,
                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    llm.chat_json("s", "u", schema={"type": "object"})
    assert llm.calls == {"fast": 0, "fallback": 1, "empty": 0}


def test_contador_empty_incrementa_cuando_no_hay_salida():
    def handler(request):
        return httpx.Response(200, json={"message": {"content": ""}})

    llm = OllamaClient("http://ollama:11434", "qwen3.5:4b", num_predict=512,
                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(EmptyCompletionError):
        llm.chat_json("s", "u")
    assert llm.calls == {"fast": 0, "fallback": 0, "empty": 1}


def test_check_model_presente():
    def handler(request):
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "qwen3.5:4b"}, {"name": "llama3:8b"}]})

    llm = OllamaClient(
        "http://ollama:11434", "qwen3.5:4b",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    ok, msg = llm.check_model()
    assert ok is True
    assert "qwen3.5:4b" in msg


def test_check_model_ausente_lista_disponibles():
    def handler(request):
        return httpx.Response(200, json={"models": [{"name": "llama3:8b"}]})

    llm = OllamaClient(
        "http://ollama:11434", "qwen3.5:4b",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    ok, msg = llm.check_model()
    assert ok is False
    assert "llama3:8b" in msg          # dice que hay disponible, para diagnosticar


def test_check_model_ollama_caido_no_levanta():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    llm = OllamaClient(
        "http://ollama:11434", "qwen3.5:4b",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    ok, msg = llm.check_model()               # no debe propagar: devuelve (False, msg)
    assert ok is False
    assert "ollama" in msg.lower()


def _client_capturando_headers(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"message": {"content": "{}"}})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_con_token_manda_authorization_bearer():
    captured = {}
    llm = OllamaClient(
        "https://ollama-proxy", "qwen3:14b", token="secreto123",
        client=_client_capturando_headers(captured),
    )
    llm.chat_json("s", "u")
    assert captured["auth"] == "Bearer secreto123"   # auth para el Ollama detras de proxy


def test_sin_token_no_manda_authorization():
    captured = {}
    llm = OllamaClient(
        "http://ollama:11434", "qwen3.5:4b",
        client=_client_capturando_headers(captured),
    )
    llm.chat_json("s", "u")
    assert captured["auth"] is None                  # Ollama local sin auth: como antes


# ---------------------------------------------------------------------------
# JSON PARSEABLE PERO INCOMPLETO
#
# EL SINTOMA (produccion, 2026-08-25): la sesion 36061874 fallo ~15 ciclos
# seguidos con `ValueError: salida del LLM sin la clave requerida:
# 'atendio_el_motivo'`, y en cada ciclo el log decia `fallback=0`. El fast
# devolvia JSON SINTACTICAMENTE VALIDO pero sin una clave del schema, asi que
# `chat_json` lo aceptaba y devolvia al primer intento: nunca reintentaba el
# fast ni llegaba al grammar -- que es justo el nivel que FUERZA la estructura.
# El scorer lo rechazaba despues, sin fila persistida, y la sesion volvia a la
# cabeza de la cola (worker.PENDING_SESSIONS_SQL ordena por end_at DESC).
# Un JSON incompleto es tan inservible como uno roto: se trata igual.
# ---------------------------------------------------------------------------

_SCHEMA_CON_REQUIRED = {
    "type": "object",
    "properties": {
        "motivo": {"type": "string"},
        "dimensions": {
            "type": "object",
            "properties": {"resolucion": {"type": "string"},
                           "cortesia": {"type": "string"}},
            "required": ["resolucion", "cortesia"],
        },
        "atendio_el_motivo": {"type": "boolean"},
    },
    "required": ["motivo", "dimensions", "atendio_el_motivo"],
}

_COMPLETO = {"motivo": "deposito", "atendio_el_motivo": True,
             "dimensions": {"resolucion": "alta", "cortesia": "alta"}}
# Le falta `atendio_el_motivo`: exactamente lo que trajo produccion.
_INCOMPLETO = {"motivo": "deposito",
               "dimensions": {"resolucion": "alta", "cortesia": "alta"}}


def test_json_incompleto_en_el_fast_cae_al_grammar():
    """Falta una clave REQUIRED -> se agota el fast y se usa el grammar."""
    calls = []

    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        cuerpo = _INCOMPLETO if p["format"] == "json" else _COMPLETO
        return httpx.Response(200, json={"message": {"content": json.dumps(cuerpo)}})

    llm = OllamaClient("http://ollama:11434", "qwen3.5:4b", num_predict=1024,
                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = llm.chat_json("s", "u", schema=_SCHEMA_CON_REQUIRED)

    assert out == _COMPLETO
    assert len([c for c in calls if c["format"] == "json"]) == 3
    assert len([c for c in calls if c["format"] != "json"]) == 1
    assert llm.calls == {"fast": 0, "fallback": 1, "empty": 0}


def test_json_incompleto_se_resuelve_en_el_reintento_del_fast():
    """El reintento del fast DUPLICA num_predict, asi que una salida cortada por
    presupuesto (JSON valido pero sin las ultimas claves) se puede resolver sin
    pagar el grammar. No se salta al nivel 2 antes de agotar el nivel 1."""
    calls = []

    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        cuerpo = _INCOMPLETO if len(calls) == 1 else _COMPLETO
        return httpx.Response(200, json={"message": {"content": json.dumps(cuerpo)}})

    llm = OllamaClient("http://ollama:11434", "qwen3.5:4b", num_predict=1024,
                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = llm.chat_json("s", "u", schema=_SCHEMA_CON_REQUIRED)

    assert out == _COMPLETO
    assert len(calls) == 2                                  # sin grammar
    assert all(c["format"] == "json" for c in calls)
    assert calls[1]["options"]["num_predict"] == 2048        # el doble del primero
    assert llm.calls == {"fast": 1, "fallback": 0, "empty": 0}


def test_dimension_requerida_faltante_tambien_cae_al_grammar():
    """El scorer valida las dimensiones ADEMAS de las claves de primer nivel; si
    chat_json no mira lo mismo, la sesion queda atascada igual."""
    sin_dimension = {"motivo": "deposito", "atendio_el_motivo": True,
                     "dimensions": {"resolucion": "alta"}}   # falta `cortesia`
    calls = []

    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        cuerpo = sin_dimension if p["format"] == "json" else _COMPLETO
        return httpx.Response(200, json={"message": {"content": json.dumps(cuerpo)}})

    llm = OllamaClient("http://ollama:11434", "qwen3.5:4b", num_predict=1024,
                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert llm.chat_json("s", "u", schema=_SCHEMA_CON_REQUIRED) == _COMPLETO
    assert llm.calls == {"fast": 0, "fallback": 1, "empty": 0}


def test_grammar_tambien_incompleto_levanta_y_nombra_la_clave():
    """Si NI el grammar completa el schema, no se devuelve un dict a medias: eso
    solo mueve el ValueError al scorer, que es el bucle que esto viene a cerrar.
    El mensaje nombra la clave para que el log sirva de diagnostico."""
    def handler(request):
        return httpx.Response(200, json={"message": {"content": json.dumps(_INCOMPLETO)}})

    llm = OllamaClient("http://ollama:11434", "qwen3.5:4b", num_predict=1024,
                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(EmptyCompletionError, match="atendio_el_motivo"):
        llm.chat_json("s", "u", schema=_SCHEMA_CON_REQUIRED)
    assert llm.calls == {"fast": 0, "fallback": 0, "empty": 1}


def test_schema_sin_required_acepta_cualquier_json():
    """Guard de no-regresion: sin `required` no hay nada que exigir, y el fast
    resuelve al primer intento como siempre."""
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": '{"ok": true}'}})

    llm = OllamaClient("http://ollama:11434", "qwen3.5:4b", num_predict=1024,
                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert llm.chat_json("s", "u", schema={"type": "object"}) == {"ok": True}
    assert len(calls) == 1
    assert llm.calls == {"fast": 1, "fallback": 0, "empty": 0}
