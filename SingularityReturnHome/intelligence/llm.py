"""Model-server client for the LLM recovery agent.

Three backends, stdlib only (urllib):
  workers-ai - Cloudflare Workers AI REST API (default). POST
            /accounts/{account}/ai/run/{model} with a bearer token;
            structured output via response_format json_schema, compiled by
            Workers AI into a decoding grammar. Keywords the grammar does not
            implement (uniqueItems) are removed from the request; the Python
            re-check in harness.py still enforces them. Health: the model is
            looked up in the account's model catalog.
  ollama  - Ollama native API. POST /api/chat with stream=false; structured
            output by passing the JSON schema as "format". Health: GET
            /api/version; model presence: GET /api/tags.
  openai  - OpenAI-compatible chat completions, e.g. llama.cpp llama-server
            (docker/ compose service). Structured output via
            response_format json_schema. Health: GET /health.

Configuration comes from the environment so no model name, address or
credential is hard-coded:
  CLOUDFLARE_ACCOUNT_ID   workers-ai: account that runs the model.
  CLOUDFLARE_API_TOKEN    workers-ai: API token with Workers AI Read and
                          Workers AI Edit permissions.
  SINGULAROS_LLM_URL      server base URL (workers-ai default
                          https://api.cloudflare.com/client/v4; ollama
                          http://127.0.0.1:11434; openai http://127.0.0.1:8080).
                          From inside a QEMU guest with user-mode networking
                          the host is http://10.0.2.2:11434.
  SINGULAROS_LLM_MODEL    model name. workers-ai default @cf/qwen/qwen3.8-27b;
                          ollama default hf.co/unsloth/Qwen3.5-9B-GGUF:Q4_K_M
                          (`ollama pull` it once, 5.7 GB); not sent to the
                          openai backend unless set.
  SINGULAROS_LLM_BACKEND  workers-ai (default), ollama or openai.
  SINGULAROS_LLM_THINK    model reasoning: false (default), true, or
                          "default" to leave it to the server. workers-ai
                          sends chat_template_kwargs.enable_thinking; ollama
                          sends "think". On the recovery prompts,
                          qwen3.8-27b replied in about 7 s with thinking off
                          and 30-95 s with it on (reasoning_effort low);
                          Qwen3.5 9B on Ollama did not reply within 300 s
                          with thinking on.
  SINGULAROS_LLM_TIMEOUT  request timeout in seconds (default 180).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

BACKENDS = ("workers-ai", "ollama", "openai")
DEFAULT_URLS = {
    "workers-ai": "https://api.cloudflare.com/client/v4",
    "ollama": "http://127.0.0.1:11434",
    "openai": "http://127.0.0.1:8080",  # docker/ llm service
}
DEFAULT_MODELS = {
    "workers-ai": "@cf/qwen/qwen3.8-27b",
    "ollama": "hf.co/unsloth/Qwen3.5-9B-GGUF:Q4_K_M",
    "openai": None,
}
# JSON Schema keywords the Workers AI grammar compiler rejects.
WORKERS_AI_UNSUPPORTED = ("uniqueItems",)


def strip_keywords(schema, keywords):
    if isinstance(schema, dict):
        return {k: strip_keywords(v, keywords) for k, v in schema.items() if k not in keywords}
    if isinstance(schema, list):
        return [strip_keywords(v, keywords) for v in schema]
    return schema


class LLMError(Exception):
    """The model server could not be reached or returned an unusable reply."""


def workers_ai_payload(config, messages, schema=None):
    """Workers AI input for a chat; the same for the REST API and the AI
    binding of a Worker (env.AI.run(model, payload))."""
    payload = {"messages": messages, "temperature": config.temperature,
               # Reasoning tokens count against max_tokens.
               "max_tokens": 1024 if config.think is False else 8192}
    if schema is not None:
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": strip_keywords(schema, WORKERS_AI_UNSUPPORTED)}
    if config.think is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": config.think}
    return payload


def workers_ai_content(result):
    """Reply text from a Workers AI chat result."""
    try:
        choice = result["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"malformed workers-ai reply: missing {e}") from e
    if not content:
        raise LLMError(f"workers-ai: empty reply (finish_reason {choice.get('finish_reason')})")
    return content


@dataclass
class LLMConfig:
    base_url: str = DEFAULT_URLS["workers-ai"]
    model: str | None = DEFAULT_MODELS["workers-ai"]
    backend: str = "workers-ai"
    account_id: str | None = None
    api_token: str | None = field(default=None, repr=False)
    timeout_s: float = 180.0
    temperature: float = 0.2
    think: bool | None = False

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        think = env.get("SINGULAROS_LLM_THINK", "false").strip().lower()
        backend = env.get("SINGULAROS_LLM_BACKEND", "workers-ai")
        if backend not in BACKENDS:
            raise ValueError(f"SINGULAROS_LLM_BACKEND must be one of {BACKENDS}")
        return cls(
            base_url=env.get("SINGULAROS_LLM_URL", DEFAULT_URLS[backend]),
            model=env.get("SINGULAROS_LLM_MODEL") or DEFAULT_MODELS[backend],
            backend=backend,
            account_id=env.get("CLOUDFLARE_ACCOUNT_ID") or None,
            api_token=env.get("CLOUDFLARE_API_TOKEN") or None,
            timeout_s=float(env.get("SINGULAROS_LLM_TIMEOUT", "180")),
            think=None if think == "default" else think in ("1", "true", "yes"),
        )


class LLMClient:
    def __init__(self, config: LLMConfig | None = None):
        self.config = config or LLMConfig.from_env()

    def _url(self, path):
        return self.config.base_url.rstrip("/") + path

    def _request(self, path, payload=None, timeout_s=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.backend == "workers-ai":
            headers["Authorization"] = f"Bearer {self.config.api_token}"
        req = urllib.request.Request(
            self._url(path),
            data=data,
            headers=headers,
            method="GET" if payload is None else "POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s or self.config.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"{path}: HTTP {e.code}: {body}") from e
        except (urllib.error.URLError, OSError) as e:
            raise LLMError(f"{path}: {e}") from e
        except json.JSONDecodeError as e:
            raise LLMError(f"{path}: reply is not JSON: {e}") from e

    def chat(self, messages, schema=None):
        """Send a chat and return the reply text. With a schema, the server
        is asked to constrain the reply to it (structured output)."""
        c = self.config
        if c.backend == "workers-ai":
            return self._workers_ai_chat(messages, schema)
        if c.backend == "ollama":
            if not c.model:
                raise LLMError("no model configured (set SINGULAROS_LLM_MODEL)")
            payload = {"model": c.model, "messages": messages, "stream": False,
                       "options": {"temperature": c.temperature}}
            if schema is not None:
                payload["format"] = schema
            if c.think is not None:
                payload["think"] = c.think
            reply = self._request("/api/chat", payload)
            try:
                return reply["message"]["content"]
            except (KeyError, TypeError) as e:
                raise LLMError(f"malformed /api/chat reply: missing {e}") from e
        payload = {"messages": messages, "temperature": c.temperature, "max_tokens": 1024}
        if c.model:
            payload["model"] = c.model
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "recovery_proposal", "strict": True, "schema": schema},
            }
        reply = self._request("/v1/chat/completions", payload)
        try:
            return reply["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"malformed chat completion reply: missing {e}") from e

    def _workers_ai_missing(self):
        c = self.config
        missing = [name for name, value in (("CLOUDFLARE_ACCOUNT_ID", c.account_id),
                                            ("CLOUDFLARE_API_TOKEN", c.api_token)) if not value]
        return f"set {' and '.join(missing)}" if missing else None

    def _workers_ai_chat(self, messages, schema):
        c = self.config
        missing = self._workers_ai_missing()
        if missing:
            raise LLMError(f"workers-ai: {missing}")
        reply = self._request(f"/accounts/{c.account_id}/ai/run/{c.model}", workers_ai_payload(c, messages, schema))
        if not reply.get("success", True):
            raise LLMError(f"workers-ai: {reply.get('errors')}")
        return workers_ai_content(reply.get("result"))

    def health(self):
        """Server status: {"ok", "detail", "model_available"}. Never raises."""
        c = self.config
        try:
            if c.backend == "workers-ai":
                missing = self._workers_ai_missing()
                if missing:
                    return {"ok": False, "detail": f"workers-ai: {missing}", "model_available": False}
                query = urllib.parse.urlencode({"search": c.model.rsplit("/", 1)[-1]})
                found = self._request(f"/accounts/{c.account_id}/ai/models/search?{query}", timeout_s=10)
                available = any(m.get("name") == c.model for m in found.get("result", []))
                detail = "workers-ai" + ("" if available else f"; model {c.model!r} not in the catalog")
                return {"ok": available, "detail": detail, "model_available": available}
            if c.backend == "ollama":
                version = self._request("/api/version", timeout_s=5)["version"]
                # Compare case-insensitively (Hugging Face quant tags are
                # case-insensitive in Ollama); ":latest" may be omitted.
                names = {(m.get("name") or "").lower() for m in self._request("/api/tags", timeout_s=5).get("models", [])}
                names |= {n[:-len(":latest")] for n in names if n.endswith(":latest")}
                available = bool(c.model) and c.model.lower() in names
                detail = f"ollama {version}"
                if not c.model:
                    detail += "; no model configured (set SINGULAROS_LLM_MODEL)"
                elif not available:
                    detail += f"; model {c.model!r} not pulled"
                return {"ok": available, "detail": detail, "model_available": available}
            self._request("/health", timeout_s=5)
            return {"ok": True, "detail": "openai-compatible server healthy", "model_available": None}
        except (LLMError, KeyError, TypeError) as e:
            return {"ok": False, "detail": str(e), "model_available": False}
