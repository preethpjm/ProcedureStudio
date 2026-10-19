"""Pluggable LLM providers. Selected by configuration; ``none`` is the default.

The procedure engine never needs an LLM: templates produce complete STE text. When a
provider is configured it is used only to *improve wording*, and every suggestion must pass
the same STE and fact checks as the template text — otherwise the template text is kept.

Providers:
* ``none``       — templates only (offline, deterministic)
* ``anthropic``  — Anthropic API (``ANTHROPIC_API_KEY``)
* ``bedrock``    — Amazon Bedrock Converse API (AWS credentials; model id configurable,
                   e.g. ``eu.anthropic.claude-sonnet-4-5-20250929-v1:0`` as in TechPub)
* ``ollama``     — a local Ollama server (offline): Qwen, Llama, Mistral… through its OpenAI-compatible API
* ``openai``     — any OpenAI-compatible endpoint (vLLM, LM Studio, LocalAI, an internal gateway)

Local models: steps are sent in small batches (``batch_size``) so they fit a small context window, and
"thinking" output (``<think>…</think>``, as Qwen 3 and DeepSeek-R1 write) is removed before the JSON is read.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

log = logging.getLogger("studio.llm")


class LLMError(Exception):
    pass


class LLMProvider:
    name = "none"

    def available(self) -> bool:
        return False

    def complete(self, system: str, user: str, max_tokens: int = 2000) -> tuple[str, dict]:
        raise LLMError("no LLM provider configured")

    def describe(self) -> dict:
        return {"provider": self.name, "available": self.available()}


class NoneProvider(LLMProvider):
    pass


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.environ.get("STUDIO_LLM_MODEL", "claude-sonnet-4-5")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def available(self) -> bool:
        return bool(self.api_key)

    def complete(self, system, user, max_tokens=2000):
        import httpx
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=120,
                       headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                                "content-type": "application/json"},
                       json={"model": self.model, "max_tokens": max_tokens, "system": system,
                             "messages": [{"role": "user", "content": user}]})
        if r.status_code >= 400:
            raise LLMError(f"Anthropic API {r.status_code}: {r.text[:300]}")
        d = r.json()
        text = "".join(b.get("text", "") for b in d.get("content", []))
        u = d.get("usage", {})
        return text, {"input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0)}

    def describe(self):
        return {**super().describe(), "model": self.model}


class BedrockProvider(LLMProvider):
    name = "bedrock"

    def __init__(self, model: str | None = None, region: str | None = None, endpoint_url: str | None = None,
                 profile: str | None = None):
        self.model = model or os.environ.get("STUDIO_LLM_MODEL", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")
        self.region = region or os.environ.get("AWS_REGION", "eu-west-3")
        vpce = os.environ.get("BEDROCK_VPCE_DNS")
        self.endpoint_url = endpoint_url or (f"https://{vpce}" if vpce else None)     # VPC interface endpoint
        self.profile = profile or os.environ.get("AWS_PROFILE")

    def _session(self):
        import boto3
        return boto3.Session(profile_name=self.profile, region_name=self.region) if self.profile else boto3.Session(region_name=self.region)

    def available(self) -> bool:
        try:
            return bool(self._session().get_credentials())
        except Exception:
            return False

    def complete(self, system, user, max_tokens=2000):
        c = self._session().client("bedrock-runtime", region_name=self.region,
                                   **({"endpoint_url": self.endpoint_url} if self.endpoint_url else {}))
        r = c.converse(modelId=self.model, system=[{"text": system}],
                       messages=[{"role": "user", "content": [{"text": user}]}],
                       inferenceConfig={"maxTokens": max_tokens, "temperature": 0.0})
        text = "".join(p.get("text", "") for p in r["output"]["message"]["content"])
        u = r.get("usage", {})
        return text, {"input_tokens": u.get("inputTokens", 0), "output_tokens": u.get("outputTokens", 0)}

    def describe(self):
        return {**super().describe(), "model": self.model, "region": self.region, "endpoint_url": self.endpoint_url}


class OpenAICompatProvider(LLMProvider):
    name = "openai"

    default_url = "http://127.0.0.1:8000/v1"
    default_model = "llama3.1"

    def __init__(self, base_url: str | None = None, model: str | None = None, api_key: str | None = None,
                 json_mode: bool = True, timeout: float = 300):
        self.base_url = (base_url or os.environ.get("STUDIO_LLM_BASE_URL", self.default_url)).rstrip("/")
        self.model = model or os.environ.get("STUDIO_LLM_MODEL", self.default_model)
        self.api_key = api_key or os.environ.get("STUDIO_LLM_API_KEY", "none")
        self.json_mode, self.timeout = json_mode, float(timeout)
        self._ok: tuple[float, bool] | None = None

    def models(self) -> list[str]:
        import httpx
        r = httpx.get(self.base_url + "/models", timeout=5, headers={"Authorization": f"Bearer {self.api_key}"})
        r.raise_for_status()
        return sorted(m.get("id") for m in r.json().get("data", []) if m.get("id"))

    def available(self) -> bool:
        if self._ok and time.monotonic() - self._ok[0] < 30:   # the header asks often; ask the server at most every 30 s
            return self._ok[1]
        try:
            import httpx
            ok = httpx.get(self.base_url + "/models", timeout=2,
                           headers={"Authorization": f"Bearer {self.api_key}"}).status_code < 500
        except Exception:
            ok = False
        self._ok = (time.monotonic(), ok)
        return ok

    def complete(self, system, user, max_tokens=2000):
        import httpx
        thinking = re.search(r"qwen3|deepseek-r1|qwq", self.model or "", re.I)
        if thinking and "qwen3" in self.model.lower():
            user = user + "\n/no_think"                  # Qwen 3: answer directly, no thinking pass
        body = {"model": self.model, "temperature": 0,
                "max_tokens": max_tokens * 3 if thinking else max_tokens,   # room left if it thinks anyway
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        r = httpx.post(self.base_url + "/chat/completions", timeout=self.timeout,
                       headers={"Authorization": f"Bearer {self.api_key}"}, json=body)
        if r.status_code == 400 and self.json_mode and "response_format" in r.text:
            body.pop("response_format")                  # servers without JSON mode
            r = httpx.post(self.base_url + "/chat/completions", timeout=self.timeout,
                           headers={"Authorization": f"Bearer {self.api_key}"}, json=body)
        if r.status_code >= 400:
            raise LLMError(f"LLM endpoint {r.status_code}: {r.text[:300]}")
        d = r.json()
        u = d.get("usage", {})
        return d["choices"][0]["message"]["content"], {"input_tokens": u.get("prompt_tokens", 0),
                                                       "output_tokens": u.get("completion_tokens", 0)}

    def describe(self):
        return {**super().describe(), "model": self.model, "base_url": self.base_url}


class OllamaProvider(OpenAICompatProvider):
    """Ollama on this machine or the local network (``ollama serve``; ``ollama pull qwen2.5:7b``)."""
    name = "ollama"
    default_url = "http://127.0.0.1:11434/v1"
    default_model = "qwen2.5:7b"


PROVIDERS = {"none": NoneProvider, "anthropic": AnthropicProvider, "bedrock": BedrockProvider,
             "ollama": OllamaProvider, "openai": OpenAICompatProvider}
SETTING_KEYS = ("model", "region", "base_url", "api_key", "endpoint_url", "profile", "json_mode", "timeout")


def get_provider(cfg: dict | None = None) -> LLMProvider:
    cfg = cfg or {}
    name = (cfg.get("provider") or os.environ.get("STUDIO_LLM_PROVIDER") or "none").lower()
    cls = PROVIDERS.get(name, NoneProvider)
    import inspect
    accepted = set(inspect.signature(cls.__init__).parameters)
    kwargs = {k: v for k, v in cfg.items() if k in SETTING_KEYS and k in accepted and v not in (None, "")}
    if cfg.get("api_key_env") and "api_key" in accepted and not kwargs.get("api_key"):
        kwargs["api_key"] = os.environ.get(cfg["api_key_env"])
    prov = cls(**kwargs)
    prov.batch_size = int(cfg.get("batch_size") or (8 if name == "ollama" else 15))   # small local models: small batches
    return prov


REFINE_SYSTEM = """You edit aircraft maintenance procedure steps written in ASD-STE100 Simplified Technical English.
Rules: imperative, one instruction per sentence, max 20 words per sentence, approved STE verbs only,
no modal verbs, active voice. Keep every item number in parentheses, every quantity and every torque
value exactly as given. Do not add facts, tools, parts or values. If a step is already correct, return it unchanged.
Answer with JSON only: {"steps": [{"id": "...", "text": "..."}]}"""


def _json_from(text: str) -> dict:
    """The JSON object in a model's answer, after removing thinking blocks and code fences."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise LLMError("the answer contains no JSON object")
    return json.loads(text[start:end + 1])


def refine_steps(provider: LLMProvider, steps: list[dict], context: str, batch_size: int = 12) -> tuple[dict[str, str], dict]:
    """Ask the provider for improved wording, a batch of steps at a time. Returns ({step_id: text}, usage).
    Never raises: a batch that fails keeps its template text."""
    if not provider.available() or not steps:
        return {}, {}
    out: dict[str, str] = {}
    usage = {"input_tokens": 0, "output_tokens": 0, "batches": 0, "failed_batches": 0, "seconds": 0.0}
    errors = []
    for i in range(0, len(steps), max(1, batch_size)):
        batch = steps[i:i + batch_size]
        user = context + "\n\nSteps:\n" + json.dumps([{"id": s["id"], "text": s["text"]} for s in batch], indent=1)
        t0 = time.monotonic()
        try:
            text, u = provider.complete(REFINE_SYSTEM, user)
            data = _json_from(text)
            ids = {s["id"] for s in batch}
            out.update({s["id"]: s["text"] for s in data.get("steps", []) if s.get("id") in ids and s.get("text")})
            usage["input_tokens"] += u.get("input_tokens", 0)
            usage["output_tokens"] += u.get("output_tokens", 0)
        except Exception as e:  # provider down, bad JSON: these steps keep the template text
            usage["failed_batches"] += 1
            errors.append(str(e)[:200])
            log.warning("LLM refinement of a batch skipped: %s", e)
        usage["batches"] += 1
        usage["seconds"] = round(usage["seconds"] + time.monotonic() - t0, 1)
    if errors:
        usage["error"] = errors[0]
    return out, usage


def test_provider(provider: LLMProvider) -> dict:
    """Send two sample steps through the real refinement path; report what came back and how long it took."""
    sample = [{"id": "t1", "text": "Carefully remove the six screws (220) and the six washers (230) from the plugs (210)."},
              {"id": "t2", "text": "Torque the six screws (220) to 10 to 11 N.m (88.5 to 97.4 lbf.in)."}]
    if not provider.available():
        return {"ok": False, "message": f"{provider.name}: not reachable or not configured", **provider.describe()}
    t0 = time.monotonic()
    try:
        text, usage = provider.complete(REFINE_SYSTEM, "Component: hydraulic valve.\n\nSteps:\n" + json.dumps(sample, indent=1))
        data = _json_from(text)
        return {"ok": True, "seconds": round(time.monotonic() - t0, 1), "usage": usage, "sample": sample,
                "answer": data.get("steps", []), **provider.describe()}
    except Exception as e:
        return {"ok": False, "seconds": round(time.monotonic() - t0, 1), "message": str(e)[:400], **provider.describe()}
