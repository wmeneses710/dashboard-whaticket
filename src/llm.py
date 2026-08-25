"""Cliente de Ollama para scoring, con salida JSON confiable (dos niveles).

Nivel 1 (rapido, ~7s) — think=false + format="json" GENERICO:
  - sin thinking (el canal de thinking se come el num_predict y deja el content
    vacio; ademas es lentisimo ~120s vs ~7s),
  - format="json" garantiza JSON sintactico y es 3x mas barato que el grammar
    (28s contra 78s, medido el 2026-08-25), asi que sigue siendo el nivel 1,
  - la FORMA del JSON se pide en el prompt y las claves se validan en el scorer.
  Se reintenta varias veces porque el fast falla de forma intermitente (~5%).

Nivel 2 (fallback, ~78s) — format=<schema> con think=False:
  el grammar del schema FUERZA la estructura, asi que rescata los casos que el
  fast no logra. Requiere pasar `schema`; sin schema no hay fallback.

  DECIA "con thinking activo" Y ESO ERA EL BUG. Medido el 2026-08-25 contra el
  host real (192.168.100.183, gemma4:12b), mismo prompt y mismo schema:
      think=None (omite el campo -> default del modelo) -> 600s, ReadTimeout
      think=False                                       ->  78s, JSON completo
  Con `timeout=180` (worker.py) el nivel 2 no era lento: era INALCANZABLE, y por
  eso el log de produccion decia `fallback=0` en todos los ciclos. La nota vieja
  sobre el bug #15260 con think=false ya no reproduce con este modelo; lo que
  reproduce es lo contrario.

QUE PUEDE FALLAR, EN LA PRACTICA: como format="json" garantiza JSON SINTACTICO,
"no parsea" casi no ocurre (`empty=0` en todos los ciclos). El modo de falla real
es JSON VALIDO PERO INCOMPLETO -- le falta una clave del schema. Ese caso se trata
igual que uno roto (ver chat_json) y el reintento del fast NOMBRA la clave que
falto (instruccion_reparacion, ~28s) antes de pagar el grammar.
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


def claves_faltantes(raw: dict, schema: dict | None) -> list[str]:
    """Claves REQUIRED del schema que la salida del LLM no trae. [] = completa.

    Es la MISMA regla que aplica scorer._validate -- vive aca, en el nivel bajo,
    para que `chat_json` pueda mirarla antes de devolver. Si las dos capas usaran
    criterios distintos, el nivel 2 (grammar) rescataria salidas que el scorer
    igual rechaza, que es el bucle que esto viene a cerrar.

    ES SCHEMA-DRIVEN A PROPOSITO: sin `required` no exige nada (un schema
    generico como {"type": "object"} sigue aceptando cualquier JSON).
    """
    if not isinstance(raw, dict) or not schema:
        return []
    faltan = [k for k in schema.get("required", ()) if k not in raw]
    sub = (schema.get("properties", {}).get("dimensions", {}) or {}).get("required")
    if sub:
        dims = raw.get("dimensions")
        if not isinstance(dims, dict):
            # Ausente ya se reporto arriba si estaba en required; presente pero de
            # otro tipo tambien rompe el contrato y el grammar lo arregla.
            if "dimensions" in raw:
                faltan.append("dimensions (no es un objeto)")
        else:
            faltan += [f"dimensions.{k}" for k in sub if k not in dims]
    return faltan


def instruccion_reparacion(faltan: list[str]) -> str:
    """Apendice para el REINTENTO del fast: le dice al modelo QUE omitio.

    Reintentar el mismo prompt a ciegas con temperature=0 es apostar a que el
    modelo cambie de opinion solo. Nombrar la clave que falto es la senal mas
    directa que se le puede dar, y se paga en el camino barato: medido contra el
    host real (gemma4:12b) tarda 28s y devuelve el JSON completo, contra 78s del
    grammar. Por eso va ANTES del nivel 2, no despues.
    """
    return ("\n\nIMPORTANTE: tu respuesta anterior fue JSON valido pero OMITIO estas "
            f"claves OBLIGATORIAS: {', '.join(faltan)}. Devolve el JSON COMPLETO, con "
            "TODAS las claves obligatorias presentes. No agregues texto fuera del JSON.")


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
        fast_attempts: int = FAST_ATTEMPTS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        # Intentos del camino rapido antes del fallback lento. Configurable para
        # acotar el desperdicio cuando el endpoint corta por timeout (cada intento
        # fallido gasta hasta el timeout completo).
        self.fast_attempts = fast_attempts
        self.fallback_num_predict = fallback_num_predict
        self._client = client  # inyectable para tests (httpx.MockTransport)
        # Auth para un Ollama detras de proxy (p. ej. el compartido via Cloudflare).
        # Sin token, headers vacio y se comporta como antes (Ollama local sin auth).
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        # Contadores de observabilidad (los lee el worker para loguear por ciclo):
        # fast = resuelto por el camino rapido; fallback = necesito el grammar lento
        # (~10-20x mas lento -> un fallback alto delata un prompt que el modelo no
        # devuelve bien al primer intento); empty = ni fast ni fallback dieron JSON.
        self.calls = {"fast": 0, "fallback": 0, "empty": 0}

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
        """Nombres de los modelos presentes en Ollama (GET /api/tags).

        SI SONDEAS ESTA RUTA A MANO Y VES UN 403, ES EL WAF Y NO OLLAMA. Medido el
        2026-08-17 contra el endpoint de produccion, con el mismo token y la misma ruta,
        cambiando solo el User-Agent: `Python-urllib/3.14` y `Python-urllib/3.11` dan 403;
        `python-httpx`, `curl`, `wget` y `Mozilla` dan 200. El cliente de este repo usa
        httpx, asi que no lo toca — pero un diagnostico hecho con `urllib` desde una
        terminal miente, y costo una tarde creer que el proxy escondia la ruta.
        """
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
        """Devuelve el JSON parseado Y COMPLETO. Reintenta el fast y cae al grammar.

        UN JSON INCOMPLETO SE TRATA COMO UNO ROTO. Medido en produccion el
        2026-08-25: el fast devolvia JSON sintacticamente valido pero sin
        `atendio_el_motivo`, `chat_json` lo aceptaba al primer intento (`fallback=0`
        en TODOS los ciclos) y el scorer lo rechazaba despues. Sin fila persistida,
        la sesion volvia a la cabeza de la cola en el ciclo siguiente: la misma
        sesion fallo ~15 veces en tres horas, y a las 07:25 se sumo una segunda.
        El nivel 2 existe justo para FORZAR la estructura; el chequeo que detecta que
        falta estructura vivia fuera de su alcance.
        """
        faltan: list[str] = []
        # Nivel 1: rapido (think=false + json generico), varios intentos.
        for i in range(self.fast_attempts):
            num_predict = self.num_predict * (2 if i else 1)
            # REPARACION: si el intento anterior vino incompleto, el reintento nombra
            # las claves que falto. Se reconstruye desde `faltan` en cada vuelta (no
            # se apendicea sobre el prompt ya apendiceado): tres intentos no pueden
            # apilar tres instrucciones, el num_ctx es finito y el reproche no es la
            # evidencia. Un intento IMPARSEABLE deja `faltan` como estaba: no hay
            # claves que nombrar, asi que el siguiente va con el prompt limpio.
            usr = user + instruccion_reparacion(faltan) if faltan else user
            parsed = _extract_json(
                self._chat(system, usr, response_format="json",
                           num_predict=num_predict, think=False)
            )
            if parsed is not None:
                faltan = claves_faltantes(parsed, schema)
                if not faltan:
                    self.calls["fast"] += 1
                    return parsed
                # Incompleto: NO se salta al grammar todavia. El reintento duplica
                # num_predict (rescata una salida cortada por presupuesto) y ahora
                # ademas sabe que le falto.

        # Nivel 2: grammar del schema, que FUERZA la estructura. Prompt LIMPIO: la
        # grammar ya obliga la forma, el reproche solo gastaria contexto.
        #
        # `think=False` NO ES COSMETICO. Hasta el 2026-08-25 iba `think=None`, que
        # OMITE el campo del payload y deja decidir al modelo -- y el default de un
        # modelo de thinking es el thinking infinito que el docstring del modulo
        # advertia. Medido contra el host real, mismo prompt y schema:
        #     think=None  -> 600s y ReadTimeout (con timeout=180 en produccion, muere
        #                    antes y no devuelve nada NUNCA)
        #     think=False ->  78s y JSON completo
        # Eso explica el `fallback=0` de TODOS los ciclos del log: el nivel 2 no era
        # poco necesario, era inalcanzable.
        if schema is not None:
            parsed = _extract_json(
                self._chat(system, user, response_format=schema,
                           num_predict=self.fallback_num_predict, think=False)
            )
            if parsed is not None:
                faltan = claves_faltantes(parsed, schema)
                if not faltan:
                    self.calls["fallback"] += 1
                    return parsed

        self.calls["empty"] += 1
        # Se distinguen las dos causas: "no parseo nada" y "parseo pero le falta X"
        # necesitan arreglos distintos (presupuesto/red contra prompt/schema), y el
        # log del worker es lo unico que se ve desde afuera.
        motivo = (f"le faltan claves requeridas: {', '.join(faltan)}" if faltan
                  else "no devolvio JSON parseable")
        raise EmptyCompletionError(
            f"el modelo {motivo} ni en el camino rapido "
            f"({self.fast_attempts} intentos) ni en el fallback grammar"
        )
