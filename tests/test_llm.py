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
    assert attempts["n"] == 3        # 3 intentos del fast


def test_chat_json_propaga_error_http():
    def handler(request):
        return httpx.Response(500, text="boom")

    llm = OllamaClient(
        "http://ollama:11434", "qwen3.5:4b",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        llm.chat_json("s", "u")


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
