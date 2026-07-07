"""Cliente de Ollama para scoring, con salida JSON confiable (dos niveles).

Nivel 1 (rapido, ~7s) — think=false + format="json" GENERICO:
  - sin thinking (el canal de thinking se come el num_predict y deja el content
    vacio; ademas es lentisimo ~120s vs ~7s),
  - format="json" garantiza JSON sintactico (el schema-grammar rompe con este
    modelo: bug #15260 con think=false, thinking infinito con think=true),
  - la FORMA del JSON se pide en el prompt y las claves se validan en el scorer.
  Se reintenta varias veces porque el fast falla de forma intermitente (~5%).

Nivel 2 (fallback, ~120s) — format=<schema> con thinking activo:
  el grammar del schema FUERZA la estructura, asi que rescata los casos que el
  fast no logra. Lento, pero solo dispara en el ~1-5% que el fast no resuelve.
  Requiere pasar `schema`; sin schema no hay fallback.
"""
from __future__ import annotations

import json
import re

import httpx

# Cuantas veces se intenta el camino rapido antes de caer al fallback.
FAST_ATTEMPTS = 3


class EmptyCompletionError(RuntimeError):
    """Ni el camino rapido ni el fallback devolvieron JSON parseable."""


_FENCE_RE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(content: str) -> dict | None:
    """Parsea el JSON tolerando fences ```json y texto alrededor."""
    text = (content or "").strip()
    if not text:
        return None
    candidate = _FENCE_RE.sub("", text).strip()
    for chunk in (candidate, text):
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            pass
    m = _OBJECT_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        token: str | None = None,
        timeout: float = 180.0,
        client: httpx.Client | None = None,
        num_ctx: int = 16384,
        num_predict: int = 2048,
        fallback_num_predict: int = 16384,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.fallback_num_predict = fallback_num_predict
        self._client = client  # inyectable para tests (httpx.MockTransport)
        # Auth para un Ollama detras de proxy (p. ej. el compartido via Cloudflare).
        # Sin token, headers vacio y se comporta como antes (Ollama local sin auth).
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _chat(self, system, user, *, response_format, num_predict, think) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": response_format,
            "stream": False,
            "options": {
                "temperature": 0,
                "num_ctx": self.num_ctx,
                "num_predict": num_predict,
            },
        }
        if think is not None:
            payload["think"] = think
        url = f"{self.base_url}/api/chat"
        if self._client is not None:
            resp = self._client.post(url, json=payload, headers=self._headers)
        else:
            resp = httpx.post(url, json=payload, headers=self._headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()["message"].get("content") or ""

    def available_models(self) -> list[str]:
        """Nombres de los modelos presentes en Ollama (GET /api/tags)."""
        url = f"{self.base_url}/api/tags"
        if self._client is not None:
            resp = self._client.get(url, headers=self._headers)
        else:
            resp = httpx.get(url, headers=self._headers, timeout=self.timeout)
        resp.raise_for_status()
        return [m.get("name", "") for m in resp.json().get("models", [])]

    def check_model(self) -> tuple[bool, str]:
        """Pre-flight de arranque: Ollama responde y el modelo configurado existe.

        Devuelve (ok, mensaje) para loguear — NO levanta excepcion — para que el
        worker no falle silenciosamente score por score si el modelo no esta en el
        Ollama del despliegue (p. ej. EasyPanel con otro modelo)."""
        try:
            models = self.available_models()
        except Exception as e:  # noqa: BLE001 - cualquier fallo de red = no disponible
            return False, f"Ollama no responde en {self.base_url}: {type(e).__name__}: {e}"
        if self.model in models or f"{self.model}:latest" in models:
            return True, f"modelo '{self.model}' disponible en {self.base_url}"
        return False, (
            f"modelo '{self.model}' NO esta en Ollama ({self.base_url}); "
            f"disponibles: {', '.join(models) or 'ninguno'} — corre 'ollama pull "
            f"{self.model}' o ajusta OLLAMA_MODEL"
        )

    def chat_json(self, system: str, user: str, schema: dict | None = None) -> dict:
        """Devuelve el JSON parseado. Reintenta el fast y cae al grammar si falla."""
        # Nivel 1: rapido (think=false + json generico), varios intentos.
        for i in range(FAST_ATTEMPTS):
            num_predict = self.num_predict * (2 if i else 1)
            parsed = _extract_json(
                self._chat(system, user, response_format="json",
                           num_predict=num_predict, think=False)
            )
            if parsed is not None:
                return parsed

        # Nivel 2: fallback con grammar del schema (thinking activo, lento).
        if schema is not None:
            parsed = _extract_json(
                self._chat(system, user, response_format=schema,
                           num_predict=self.fallback_num_predict, think=None)
            )
            if parsed is not None:
                return parsed

        raise EmptyCompletionError(
            "el modelo no devolvio JSON parseable ni en el camino rapido "
            f"({FAST_ATTEMPTS} intentos) ni en el fallback grammar"
        )
