"""Installed BREX data modules: stored in the data folder, found by DMC, chained, compiled once."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .engine import CompiledBrex
from .model import Brex, parse_brex




def _issue_key(issue: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[-.]", issue or "0"))


class BrexLibrary:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_file = self.root / "index.json"
        self.alias_file = self.root / "substitutes.json"
        self._compiled: dict[str, tuple[float, CompiledBrex]] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- index
    def list(self) -> list[dict]:
        if not self.index_file.exists():
            return []
        try:
            return json.loads(self.index_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

    def _save(self, entries: list[dict]) -> None:
        self.index_file.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def install(self, data: bytes, source_name: str = "") -> dict:
        """Add (or replace the same DMC and issue of) a BREX data module. Raises ValueError."""
        b = parse_brex(data)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{b.dmc}_{b.issue}") + ".xml"
        with self._lock:
            (self.root / safe).write_bytes(data)
            entries = [e for e in self.list() if not (e["dmc"] == b.dmc and e["issue"] == b.issue)]
            entry = {"dmc": b.dmc, "issue": b.issue, "title": b.title, "schema_issue": b.schema_issue,
                     "parent_dmc": b.parent_dmc, "rules": len(b.rules), "sns_systems": len(b.sns),
                     "file": safe, "source_name": source_name,
                     "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            entries.append(entry)
            self._save(sorted(entries, key=lambda e: (e["dmc"], _issue_key(e["issue"]))))
            self._compiled.pop(safe, None)
        return entry

    def remove(self, dmc: str, issue: str) -> bool:
        with self._lock:
            entries = self.list()
            keep = [e for e in entries if not (e["dmc"] == dmc and e["issue"] == issue)]
            for e in entries:
                if e not in keep:
                    (self.root / e["file"]).unlink(missing_ok=True)
                    self._compiled.pop(e["file"], None)
            self._save(keep)
            return len(keep) != len(entries)

    def find(self, dmc: str) -> dict | None:
        """The installed BREX with this DMC (the highest issue if several are installed)."""
        hits = [e for e in self.list() if e["dmc"].upper() == dmc.upper()]
        return max(hits, key=lambda e: _issue_key(e["issue"])) if hits else None

    def default_for(self, s1000d_issue: str) -> dict | None:
        """The S1000D default BREX for an issue (modelIdentCode S1000D, written for that issue)."""
        hits = [e for e in self.list() if e["dmc"].upper().startswith("S1000D-") and e.get("schema_issue") == s1000d_issue]
        return max(hits, key=lambda e: _issue_key(e["issue"])) if hits else None

    # ---------------------------------------------------------------- substitutes
    def substitutes(self) -> dict[str, str]:
        """named DMC -> installed DMC used in its place (chosen by the user)."""
        if not self.alias_file.exists():
            return {}
        try:
            return json.loads(self.alias_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def set_substitute(self, named: str, use: str) -> None:
        if self.find(use) is None:
            raise ValueError(f"DMC-{use} is not installed")
        if named.upper() == use.upper():
            raise ValueError("a BREX cannot stand in for itself")
        with self._lock:
            subs = self.substitutes()
            subs[named] = use
            self.alias_file.write_text(json.dumps(subs, indent=2), encoding="utf-8")

    def remove_substitute(self, named: str) -> bool:
        with self._lock:
            subs = self.substitutes()
            hit = subs.pop(named, None) is not None
            self.alias_file.write_text(json.dumps(subs, indent=2), encoding="utf-8")
            return hit

    def chain(self, dmc: str) -> tuple[list[dict], str | None]:
        """The BREX named by a document and those it builds on. -> (installed chain, first missing DMC).
        A missing BREX with a substitute is replaced by it; such entries carry "substitute_for"."""
        out, seen, cur = [], set(), dmc
        subs = {k.upper(): v for k, v in self.substitutes().items()}
        while cur and cur.upper() not in seen:
            seen.add(cur.upper())
            e = self.find(cur)
            if e is None and cur.upper() in subs:
                e = self.find(subs[cur.upper()])
                if e is not None:
                    e = {**e, "substitute_for": cur}
                    seen.add(e["dmc"].upper())
            if e is None:
                return out, cur
            out.append(e)
            cur = e.get("parent_dmc")
        return out, None

    # ---------------------------------------------------------------- compiled rules
    def load(self, entry: dict) -> Brex:
        return self.compiled(entry).brex

    def compiled(self, entry: dict) -> CompiledBrex:
        p = self.root / entry["file"]
        mtime = p.stat().st_mtime
        with self._lock:
            hit = self._compiled.get(entry["file"])
            if hit and hit[0] == mtime:
                return hit[1]
        cb = CompiledBrex(parse_brex(p.read_bytes()))
        with self._lock:
            self._compiled[entry["file"]] = (mtime, cb)
        return cb
