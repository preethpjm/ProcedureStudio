"""Projects and runs on disk: plain JSON and files, readable without the app.

```
<data_dir>/projects/<project_id>/project.json
<data_dir>/projects/<project_id>/runs/<run_id>/
    run.json                 status of every stage, timings, input hashes, base run, sign-off
    inputs/                  the STEP, MBOM and drawing used (copied, hashed)
    <model>.glb  product_model.json  meshes.npz
    contacts.json  roles.json  sequence.json  reconciliation.json  drawing.json
    ipl.json  process.json  procedure_assembly.json  procedure_disassembly.json
    changeset.json  procedure_modification.json      (revision runs)
    edits.json               every writer edit (IPL cells, sequence moves, step text) with time
    publish/<n>.json         each publication result
```
A signed-off run is a *baseline*: it is never modified again (edits are refused), and
later runs compare against it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _next_letter(v: str) -> str:
    """Variant letters A, B, … Z, then AA (S1000D disassembly code variant)."""
    v = (v or "A").upper()
    return v[:-1] + chr(ord(v[-1]) + 1) if v[-1] != "Z" else v + "A"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def write_json(path: Path, obj) -> None:
    """Write atomically. On Windows a file cannot be replaced while another thread has it open for reading
    (the UI polls run.json during a run), so the replace is retried briefly instead of failing the run."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")   # one temp file per writer
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 49:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.02 * (attempt + 1) if attempt < 10 else 0.2)


def read_json(path: Path, default=None):
    for attempt in range(50):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except PermissionError:          # Windows: the file is being replaced at this instant
            if attempt == 49:
                raise
            time.sleep(0.02 * (attempt + 1) if attempt < 10 else 0.2)
        except json.JSONDecodeError:     # caught mid-write by a non-atomic writer: read again
            if attempt == 49:
                raise
            time.sleep(0.05)


class StoreError(Exception):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = Path(root) / "projects"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ projects
    def _pdir(self, pid: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", pid or ""):
            raise StoreError("invalid project id")
        return self.root / pid

    def create_project(self, name: str, settings: dict | None = None, parent: str | None = None) -> dict:
        """A product. With ``parent`` it is a *variant* of an existing product: it starts from that product's
        signed-off baseline (files, settings, procedure text) and records only what differs."""
        pid = uuid.uuid4().hex[:12]
        settings = dict(settings or {})
        link = None
        if parent:
            pp = self.project(parent)
            if not pp.get("baseline"):
                raise StoreError(f"{pp['name']} has no signed-off baseline yet; sign off a run of it first")
            base = self.run(pp["baseline"], parent)
            inherited = {k: v for k, v in (pp.get("settings") or {}).items() if k not in ("root_part_number", "expected_variant")}
            pub = dict(inherited.get("publication") or {})
            for k in ("applicability", "cmm_doc_number"):           # these name the product itself
                pub.pop(k, None)
            pub["disassy_code_variant"] = _next_letter(pub.get("disassy_code_variant") or "A")
            pub["issue_number"], pub["in_work"] = "001", "00"
            inherited["publication"] = pub
            settings = {**inherited, **settings}
            link = {"project": parent, "run": pp["baseline"], "name": pp["name"], "label": base.get("label")}
        d = self._pdir(pid)
        (d / "runs").mkdir(parents=True)
        p = {"id": pid, "name": name.strip() or "Untitled product", "created": now(), "settings": settings,
             "runs": [], "baseline": None, "kind": "variant" if link else "product", "parent": link,
             "family": (self.project(parent).get("family") or parent) if parent else pid}
        write_json(d / "project.json", p)
        return p

    def projects(self) -> list[dict]:
        out = []
        for d in sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            p = read_json(d / "project.json")
            if p:
                last = p["runs"][-1] if p["runs"] else None
                p["last_run"] = self.run(pid=p["id"], rid=last) if last else None
                out.append(p)
        return out

    def project(self, pid: str) -> dict:
        p = read_json(self._pdir(pid) / "project.json")
        if not p:
            raise StoreError("no such project")
        return p

    def save_project(self, p: dict) -> None:
        with self._lock:
            write_json(self._pdir(p["id"]) / "project.json", p)

    def update_settings(self, pid: str, settings: dict) -> dict:
        with self._lock:
            p = self.project(pid)
            for k, v in settings.items():
                if isinstance(v, dict) and isinstance(p["settings"].get(k), dict):
                    p["settings"][k] = {**p["settings"][k], **v}
                else:
                    p["settings"][k] = v
            self.save_project(p)
            return p

    def delete_project(self, pid: str) -> None:
        shutil.rmtree(self._pdir(pid), ignore_errors=True)

    # ------------------------------------------------------------------ runs
    def run_dir(self, pid: str, rid: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{10}", rid or ""):
            raise StoreError("invalid run id")
        return self._pdir(pid) / "runs" / rid

    def find_run(self, rid: str) -> tuple[str, Path]:
        for d in self.root.glob(f"*/runs/{rid}"):
            return d.parent.parent.name, d
        raise StoreError("no such run")

    def create_run(self, pid: str, inputs: dict[str, Path], base_run: str | None = None, label: str = "",
                   change: dict | None = None, publication: dict | None = None) -> dict:
        with self._lock:
            p = self.project(pid)
            rid = uuid.uuid4().hex[:10]
            d = self.run_dir(pid, rid)
            (d / "inputs").mkdir(parents=True)
            files = {}
            for kind, src in inputs.items():
                if not src:
                    continue
                dst = d / "inputs" / Path(src).name
                shutil.copy2(src, dst)
                files[kind] = {"file": dst.name, "sha256": sha256(dst), "bytes": dst.stat().st_size}
            r = {"id": rid, "project": pid, "created": now(), "label": label or f"Run {len(p['runs']) + 1}",
                 "inputs": files, "base_run": base_run, "change": change or {"kind": "initial"},
                 "publication": publication or {}, "status": "queued", "stages": {}, "signed_off": None,
                 "error": None, "progress": {"stage": None, "fraction": 0.0, "message": ""}}
            write_json(d / "run.json", r)
            p["runs"].append(rid)
            self.save_project(p)
            return r

    def run(self, rid: str, pid: str | None = None) -> dict:
        if pid is None:
            pid, d = self.find_run(rid)
        else:
            d = self.run_dir(pid, rid)
        r = read_json(d / "run.json")
        if not r:
            raise StoreError("no such run")
        return r

    def save_run(self, r: dict) -> None:
        with self._lock:
            write_json(self.run_dir(r["project"], r["id"]) / "run.json", r)

    def artifact(self, rid: str, name: str, default=None):
        _, d = self.find_run(rid)
        if not re.fullmatch(r"[A-Za-z0-9_\-.]+", name):
            raise StoreError("invalid artifact name")
        return read_json(d / name, default)

    def save_artifact(self, rid: str, name: str, obj) -> None:
        _, d = self.find_run(rid)
        write_json(d / name, obj)

    def log_edit(self, rid: str, edit: dict) -> None:
        _, d = self.find_run(rid)
        edits = read_json(d / "edits.json", [])
        edits.append({"at": now(), **edit})
        write_json(d / "edits.json", edits)

    def sign_off(self, rid: str, by: str = "writer") -> dict:
        with self._lock:
            r = self.run(rid)
            if r["status"] != "done":
                raise StoreError("only a finished run can be signed off")
            r["signed_off"] = {"at": now(), "by": by}
            self.save_run(r)
            p = self.project(r["project"])
            p["baseline"] = rid
            self.save_project(p)
            return r

    def guard_editable(self, rid: str) -> dict:
        r = self.run(rid)
        if r.get("signed_off"):
            raise StoreError("this run is a signed-off baseline; start a new run to change it")
        return r
