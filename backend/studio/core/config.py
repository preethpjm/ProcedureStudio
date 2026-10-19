"""Studio configuration: one YAML file (or environment variables) selects every adapter.

```yaml
# studio.yaml
data_dir: ~/.techpub-studio
converter:
  kind: occ                  # occ (Open CASCADE, any OS) | exe (legacy cad-step-to-glb.exe)
  quality: balanced          # fast | balanced | precise
  exe_path: engine/cad-step-to-glb.exe
llm:
  provider: none             # none | anthropic | bedrock | openai
  model: eu.anthropic.claude-sonnet-4-5-20250929-v1:0
connectors:
  - kind: folder
    path: ./published
defaults:                    # project defaults (overridable per project)
  manuf_prefix: null         # auto-detected from the STEP part numbers
  publication: {model_ident_code: TECHPUB, enterprise_code: "", ata_chapter: "32"}
roles:
  extra_rules: []            # [[role, pn|name, regex], ...] checked before the built-in rules
```
Environment overrides: ``STUDIO_DATA``, ``STUDIO_CONVERTER``, ``STUDIO_LLM_PROVIDER``.
Schema packages, BREX, rule sets and stylesheets live under ``<data_dir>/conformance`` and are managed in the app.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path


def _expand(v):
    if isinstance(v, str):
        return re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: os.environ.get(m.group(1), ""), v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


_CONF_LOCK = threading.Lock()


@dataclass
class StudioConfig:
    data_dir: Path
    converter: dict = field(default_factory=lambda: {"kind": "occ", "quality": "balanced"})
    llm: dict = field(default_factory=lambda: {"provider": "none"})
    connectors: list[dict] = field(default_factory=list)
    defaults: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)
    packages_dir: Path | None = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> "StudioConfig":
        raw: dict = {}
        p = Path(path or os.environ.get("STUDIO_CONFIG", "studio.yaml"))
        if p.exists():
            import yaml
            raw = _expand(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
        data_dir = Path(os.path.expanduser(os.environ.get("STUDIO_DATA") or raw.get("data_dir") or "~/.techpub-studio"))
        cfg = cls(data_dir=data_dir.resolve(), converter={"kind": "occ", "quality": "balanced", **(raw.get("converter") or {})},
                  llm={"provider": "none", **(raw.get("llm") or {})}, connectors=list(raw.get("connectors") or []),
                  defaults=raw.get("defaults") or {}, roles=raw.get("roles") or {})
        if os.environ.get("STUDIO_CONVERTER"):
            cfg.converter["kind"] = os.environ["STUDIO_CONVERTER"]
        if os.environ.get("STUDIO_LLM_PROVIDER"):
            cfg.llm["provider"] = os.environ["STUDIO_LLM_PROVIDER"]
        legacy = [c for c in cfg.connectors if c.get("kind") == "asthra" and not c.get("class")]
        if legacy:                       # validation is built in now; an old ASTHRA entry is not a destination
            import logging
            logging.getLogger("studio.config").warning(
                "studio.yaml lists the 'asthra' connector; Procedure Studio checks documents itself now, so it is "
                "ignored. Remove it from connectors:.")
            cfg.connectors = [c for c in cfg.connectors if c not in legacy]
        if not any(c.get("kind") == "folder" for c in cfg.connectors):
            cfg.connectors.append({"kind": "folder", "path": str(cfg.data_dir / "published")})
        here = Path(__file__).resolve().parents[3] / "packages"
        cfg.packages_dir = here if here.is_dir() else None
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("STUDIO_DATA", str(cfg.data_dir))
        return cfg

    def connector(self, kind: str):
        """Build the connector configured under ``kind``.

        The built-in kind is ``folder``. Any other connector plugs in from configuration alone:
        ``{kind: plm, class: "mycompany.studio_plm:PlmConnector", url: ...}`` imports the class and passes the
        remaining keys as keyword arguments. It must subclass ``studio.connectors.base.Connector``.
        """
        from ..connectors.base import Connector, FolderConnector
        for c in self.connectors:
            if c.get("kind") != kind:
                continue
            if c.get("class"):
                import importlib
                mod, _, name = c["class"].partition(":")
                cls = getattr(importlib.import_module(mod), name)
                if not (isinstance(cls, type) and issubclass(cls, Connector)):
                    raise TypeError(f"{c['class']} is not a studio Connector")
                conn = cls(**{k: v for k, v in c.items() if k not in ("kind", "class")})
                conn.name = kind
                return conn
            if kind == "folder":
                return FolderConnector(c.get("path", str(self.data_dir / "published")))
            raise KeyError(f"connector '{kind}' has no built-in implementation; give it a 'class'")
        raise KeyError(f"connector '{kind}' is not configured")

    # ------------------------------------------------------------------ settings changed in the app
    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    def app_settings(self) -> dict:
        import json
        try:
            return json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_app_settings(self, patch: dict) -> dict:
        import json
        s = {**self.app_settings(), **patch}
        tmp = self.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
        os.replace(tmp, self.settings_file)
        try:
            os.chmod(self.settings_file, 0o600)          # may hold an API key
        except OSError:
            pass
        return s

    def llm_settings(self) -> dict:
        """studio.yaml / environment, overridden by what was saved in the app (Settings → Language model)."""
        saved = self.app_settings().get("llm") or {}
        return {**self.llm, **saved} if saved.get("provider") else dict(self.llm)

    def conformance(self):
        """The schema registry, BREX, rule sets and stylesheets (one per data folder)."""
        with _CONF_LOCK:
            if getattr(self, "_conformance", None) is None:
                from ..conformance.service import Conformance
                self._conformance = Conformance(self.data_dir / "conformance")
            return self._conformance

    def describe(self) -> dict:
        from ..llm.providers import get_provider
        return {"data_dir": str(self.data_dir), "converter": self.converter,
                "llm": get_provider(self.llm_settings()).describe(),
                "connectors": [{k: v for k, v in c.items() if k != "token"} | {"token": bool(c.get("token"))}
                               for c in self.connectors]}
