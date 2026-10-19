"""Settings changed in the app (stored in ``<data_dir>/settings.json``): the language model connection."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .llm.providers import PROVIDERS, get_provider, test_provider

LLM_FIELDS = {"provider", "model", "base_url", "api_key", "api_key_env", "region", "endpoint_url", "profile",
              "json_mode", "timeout", "batch_size"}


def _masked(s: dict) -> dict:
    out = {k: v for k, v in s.items() if k in LLM_FIELDS}
    if out.get("api_key"):
        out["api_key"] = "••••" + str(out["api_key"])[-4:]
        out["api_key_set"] = True
    return out


def mount_settings(app: FastAPI, cfg) -> None:
    @app.get("/api/settings/llm")
    def get_llm():
        cur = cfg.llm_settings()
        return {"settings": _masked(cur), "source": "app" if (cfg.app_settings().get("llm") or {}).get("provider") else "studio.yaml",
                "providers": list(PROVIDERS), "status": get_provider(cur).describe()}

    def _merge(body: dict) -> dict:
        body = {k: v for k, v in (body or {}).items() if k in LLM_FIELDS}
        if body.get("provider") and body["provider"] not in PROVIDERS:
            raise HTTPException(400, f"unknown provider {body['provider']}; use one of {', '.join(PROVIDERS)}")
        cur = cfg.llm_settings()
        if str(body.get("api_key", "")).startswith("••••"):          # unchanged masked key
            body["api_key"] = cur.get("api_key")
        return body

    @app.put("/api/settings/llm")
    def put_llm(body: dict):
        s = _merge(body)
        cfg.save_app_settings({"llm": s})
        return {"settings": _masked(s), "status": get_provider(s).describe()}

    @app.post("/api/settings/llm/test")
    def test_llm(body: dict | None = None):
        """Try the settings in the body (or the saved ones) with two sample steps, without saving."""
        s = _merge(body) if body else cfg.llm_settings()
        return test_provider(get_provider(s))

    @app.post("/api/settings/llm/models")
    def list_models(body: dict | None = None):
        s = _merge(body) if body else cfg.llm_settings()
        p = get_provider(s)
        if not hasattr(p, "models"):
            return {"models": []}
        try:
            return {"models": p.models()}
        except Exception as e:
            raise HTTPException(502, f"could not list models at {getattr(p, 'base_url', '')}: {e}")
