"""Connector interface: where generated publications go.

A connector receives a *bundle* — the checked files plus what each one is — and returns a
per-document result. Validation happens before, in Studio (``studio.conformance``), so a connector
only delivers. Connectors are selected in ``studio.yaml`` (``connectors:`` list).

Built in:
* ``folder``  — write the bundle to a directory (shared drive, CMS hot folder, Git work tree).
Others (PLM, CMS, document control) plug in from configuration: see ``StudioConfig.connector``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BundleFile:
    name: str
    data: bytes
    key: str | None = None            # stable identity across runs, e.g. "S1000D-530"
    kind: str = "document"            # document | knowledge | report
    target: str | None = None         # s1000d_proced | s1000d_ipd | ata_cmm | techpub_ipl | ...


@dataclass
class Bundle:
    project: dict
    run: dict
    message: str
    files: list[BundleFile] = field(default_factory=list)

    def documents(self) -> list[BundleFile]:
        return [f for f in self.files if f.kind == "document"]


class Connector:
    name = "base"

    def status(self) -> dict:
        return {"name": self.name, "available": False}

    def publish(self, bundle: Bundle) -> dict:
        raise NotImplementedError


class FolderConnector(Connector):
    name = "folder"

    def __init__(self, path: str):
        self.path = Path(path)

    def status(self) -> dict:
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            ok = self.path.is_dir()
        except OSError:
            ok = False
        return {"name": self.name, "available": ok, "path": str(self.path),
                "capabilities": {"any": True}}

    def publish(self, bundle: Bundle) -> dict:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in bundle.project.get("name", "project"))
        out = self.path / safe / str(bundle.run.get("run_id", "run"))
        out.mkdir(parents=True, exist_ok=True)
        docs = []
        for f in bundle.files:
            (out / f.name).write_bytes(f.data)
            docs.append({"file": f.name, "key": f.key, "action": "written", "path": str(out / f.name)})
        (out / "manifest.json").write_text(json.dumps({"project": bundle.project, "run": bundle.run,
                                                       "files": [{"file": f.name, "key": f.key, "kind": f.kind}
                                                                 for f in bundle.files]}, indent=1))
        return {"connector": self.name, "documents": docs, "location": str(out)}
