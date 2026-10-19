"""ASTHRA connector: publish generated documents into ASTHRA for validation, revision control and editing.

Uses ASTHRA's integration API (``/api/integrations/studio/*``, added to ASTHRA alongside
this studio). ASTHRA binds to loopback by default; to reach it from another host start it
with ``ASTHRA_ALLOWED_HOSTS=<name>`` and protect the integration with
``ASTHRA_INTEGRATION_TOKEN`` (sent here as ``x-asthra-token``).
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from .base import Bundle, Connector


class AsthraConnector(Connector):
    name = "asthra"

    def __init__(self, url: str = "http://127.0.0.1:8765", token: str | None = None, ui_url: str | None = None,
                 timeout: float = 120.0):
        self.url = url.rstrip("/")
        self.ui_url = (ui_url or self.url).rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict:
        h = {"x-asthra": "1"}
        if self.token:
            h["x-asthra-token"] = self.token
        return h

    def _client(self):
        import httpx
        return httpx.Client(base_url=self.url, headers=self._headers(), timeout=self.timeout)

    def status(self) -> dict:
        try:
            with self._client() as c:
                r = c.get("/api/integrations/studio/status")
                if r.status_code == 404:
                    return {"name": self.name, "available": False, "url": self.url,
                            "error": "This ASTHRA does not have the studio integration (update ASTHRA)."}
                r.raise_for_status()
                return {"name": self.name, "available": True, "url": self.url, "ui_url": self.ui_url, **r.json()}
        except Exception as e:
            return {"name": self.name, "available": False, "url": self.url, "error": str(e)}

    def install_package(self, directory: str | Path) -> dict:
        """Zip a schema package directory and install it in ASTHRA."""
        directory = Path(directory)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in directory.rglob("*"):
                if p.is_file():
                    z.write(p, p.relative_to(directory.parent))
        with self._client() as c:
            r = c.post("/api/integrations/studio/packages", files={"file": (directory.name + ".zip", buf.getvalue())})
            if r.status_code >= 400:
                return {"installed": False, "error": r.json().get("detail", r.text)}
            return {"installed": True, **r.json()}

    def publish(self, bundle: Bundle) -> dict:
        manifest = {"project": bundle.project, "source": bundle.run, "message": bundle.message,
                    "documents": [{"file": f.name, "key": f.key} for f in bundle.documents()]}
        kn = {f.target: f.name for f in bundle.files if f.kind == "knowledge"}
        if kn:
            manifest["knowledge"] = {"reconciliation": kn.get("reconciliation"), "model": kn.get("model")}
        files = [("files", (f.name, f.data, "application/octet-stream")) for f in bundle.files]
        with self._client() as c:
            r = c.post("/api/integrations/studio/publish", data={"manifest": json.dumps(manifest)}, files=files)
            if r.status_code >= 400:
                detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text
                raise RuntimeError(f"ASTHRA refused the publication ({r.status_code}): {detail}")
            res = r.json()
        pid = res["project"]["id"]
        for d in res["documents"]:
            if d.get("document_id"):
                d["open_url"] = f"{self.ui_url}/?project={pid}&doc={d['document_id']}"
        res["connector"] = self.name
        res["open_url"] = f"{self.ui_url}/?project={pid}"
        return res
