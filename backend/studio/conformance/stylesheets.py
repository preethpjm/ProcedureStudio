"""XSLT stylesheets: how documents are shown, and optional transforms applied before validation.

Two kinds:

* ``view``       XML -> HTML, for reading a document in the Check screen (and for review exports).
                 Built-in views cover S1000D data modules and the ATA CMM; an installed view for the same
                 documents takes precedence over the built-in one.
* ``transform``  XML -> XML, applied to Studio's output before it is validated and published: house
                 conventions, a customer's flavour of the standard, added boilerplate. Transforms run in
                 ``order`` and only on the documents they apply to.

A stylesheet is a single ``.xsl``/``.xslt`` file, or a ``.zip`` with the main stylesheet and the files it
includes or imports. Stylesheets cannot reach the network or write files; includes are confined to the
stylesheet's own folder. XSLT 1.0 (libxslt).
"""
from __future__ import annotations

import io
import json
import re
import shutil
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

from .rules import Applies
from .security.paths import PathViolation, safe_extract_zip
from .security.xml_safe import ConfinedResolver, document_parser

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin" / "stylesheets"
BUILTINS = [
    {"id": "builtin-s1000d-view", "name": "S1000D reading view", "kind": "view", "file": "s1000d-view.xsl",
     "applies_to": {"standards": ["S1000D"], "doc_types": [], "roots": ["dmodule"]}},
    {"id": "builtin-ata-cmm-view", "name": "ATA CMM reading view", "kind": "view", "file": "ata-cmm-view.xsl",
     "applies_to": {"standards": ["ATA2200"], "doc_types": [], "roots": ["cmm"]}},
]
XSL = "{http://www.w3.org/1999/XSL/Transform}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-").lower() or "stylesheet"


ACCESS = etree.XSLTAccessControl(read_file=True, write_file=False, create_dir=False,
                                 read_network=False, write_network=False)


def compile_xslt(path: Path) -> etree.XSLT:
    parser = document_parser()
    parser.resolvers.add(ConfinedResolver([path.parent]))
    try:
        doc = etree.parse(str(path), parser)
    except etree.XMLSyntaxError as e:
        raise ValueError(f"the stylesheet is not well-formed XML: {e}") from e
    if doc.getroot().tag not in (f"{XSL}stylesheet", f"{XSL}transform"):
        raise ValueError("not an XSLT stylesheet (root must be xsl:stylesheet or xsl:transform)")
    try:
        return etree.XSLT(doc, access_control=ACCESS)
    except etree.XSLTParseError as e:
        raise ValueError(f"the stylesheet does not compile: {e}") from e


class StylesheetLibrary:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_file = self.root / "index.json"
        self._lock = threading.Lock()
        self._compiled: dict[str, tuple[float, etree.XSLT]] = {}

    # ------------------------------------------------------------------ index
    def _installed(self) -> list[dict]:
        try:
            return json.loads(self.index_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

    def _save(self, entries: list[dict]) -> None:
        self.index_file.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def _builtin_state(self) -> dict:
        p = self.root / "builtin-state.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def list(self) -> list[dict]:
        state = self._builtin_state()
        out = [{**b, "builtin": True, "enabled": state.get(b["id"], True), "order": 0} for b in BUILTINS]
        return out + [{**e, "builtin": False} for e in self._installed()]

    def get(self, sid: str) -> dict:
        e = next((x for x in self.list() if x["id"] == sid), None)
        if e is None:
            raise KeyError(f"no stylesheet {sid}")
        return e

    def _path(self, e: dict) -> Path:
        return BUILTIN_DIR / e["file"] if e.get("builtin") else self.root / e["id"] / e["file"]

    # ------------------------------------------------------------------ install / change
    def install(self, data: bytes, filename: str, kind: str, name: str | None = None,
                applies_to: dict | None = None, order: int = 100, main: str | None = None) -> dict:
        if kind not in ("view", "transform"):
            raise ValueError("kind must be 'view' or 'transform'")
        sid = _slug(Path(filename).stem)
        if any(b["id"] == sid for b in BUILTINS):
            sid += "-custom"
        dest = self.root / sid
        tmp = self.root / f".{sid}.new"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            if filename.lower().endswith(".zip"):
                zp = tmp / "upload.zip"
                zp.write_bytes(data)
                try:
                    safe_extract_zip(zp, tmp)
                except PathViolation as e:
                    raise ValueError(f"unsafe archive rejected: {e}") from e
                zp.unlink()
                cands = sorted(p for p in tmp.rglob("*") if p.suffix.lower() in (".xsl", ".xslt"))
                if main:
                    cands = [p for p in cands if p.relative_to(tmp).as_posix() == main]
                if not cands:
                    raise ValueError("no .xsl/.xslt stylesheet in the archive" + (f" named {main}" if main else ""))
                # the main stylesheet is the one no other stylesheet includes or imports
                included = {Path(h.get("href", "")).name for p in cands for h in
                            etree.parse(str(p), document_parser()).getroot().iter(f"{XSL}include", f"{XSL}import")}
                roots = [p for p in cands if p.name not in included] or cands
                file = roots[0].relative_to(tmp).as_posix()
            elif filename.lower().endswith((".xsl", ".xslt")):
                file = Path(filename).name
                (tmp / file).write_bytes(data)
            else:
                raise ValueError("a stylesheet is an .xsl/.xslt file, or a .zip with the stylesheet and its includes")
            compile_xslt(tmp / file)                      # must compile before it is accepted
            title = name or Path(file).stem.replace("-", " ").replace("_", " ")
            with self._lock:
                shutil.rmtree(dest, ignore_errors=True)
                tmp.rename(dest)
                entries = [e for e in self._installed() if e["id"] != sid]
                entry = {"id": sid, "name": title, "kind": kind, "file": file, "applies_to": Applies.of(applies_to).as_dict(),
                         "enabled": True, "order": int(order), "source_name": filename, "installed_at": _now()}
                entries.append(entry)
                self._save(sorted(entries, key=lambda e: (e["kind"], e["order"], e["id"])))
                self._compiled.pop(sid, None)
            return {**entry, "builtin": False}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def update(self, sid: str, enabled: bool | None = None, applies_to: dict | None = None,
               order: int | None = None, name: str | None = None) -> dict:
        with self._lock:
            if any(b["id"] == sid for b in BUILTINS):
                if enabled is not None:
                    st = self._builtin_state()
                    st[sid] = bool(enabled)
                    (self.root / "builtin-state.json").write_text(json.dumps(st), encoding="utf-8")
                return next(x for x in self.list() if x["id"] == sid)
            entries = self._installed()
            e = next((x for x in entries if x["id"] == sid), None)
            if e is None:
                raise KeyError(f"no stylesheet {sid}")
            if enabled is not None:
                e["enabled"] = bool(enabled)
            if applies_to is not None:
                e["applies_to"] = Applies.of(applies_to).as_dict()
            if order is not None:
                e["order"] = int(order)
            if name:
                e["name"] = name
            self._save(entries)
            return {**e, "builtin": False}

    def remove(self, sid: str) -> None:
        if any(b["id"] == sid for b in BUILTINS):
            raise ValueError("built-in stylesheets cannot be removed; disable them instead")
        with self._lock:
            entries = self._installed()
            if not any(e["id"] == sid for e in entries):
                raise KeyError(f"no stylesheet {sid}")
            shutil.rmtree(self.root / sid, ignore_errors=True)
            self._save([e for e in entries if e["id"] != sid])
            self._compiled.pop(sid, None)

    def source(self, sid: str) -> tuple[str, bytes]:
        e = self.get(sid)
        p = self._path(e)
        if e.get("builtin") or p.parent == self.root / sid and len(list(p.parent.rglob("*"))) == 1:
            return p.name, p.read_bytes()
        buf = io.BytesIO()                               # several files: give the folder back as a zip
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in (self.root / sid).rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(self.root / sid).as_posix())
        return f"{sid}.zip", buf.getvalue()

    # ------------------------------------------------------------------ use
    def _xslt(self, e: dict) -> etree.XSLT:
        p = self._path(e)
        mtime = p.stat().st_mtime
        hit = self._compiled.get(e["id"])
        if hit and hit[0] == mtime:
            return hit[1]
        x = compile_xslt(p)
        self._compiled[e["id"]] = (mtime, x)
        return x

    def applicable(self, kind: str, standard: str | None, doc_type: str | None, root: str | None) -> list[dict]:
        hits = [e for e in self.list() if e["kind"] == kind and e.get("enabled", True)
                and Applies.of(e.get("applies_to")).fits(standard, doc_type, root)]
        # installed before built-in, then by order
        return sorted(hits, key=lambda e: (e.get("builtin", False), e.get("order", 100), e["id"]))

    def view(self, tree, standard: str | None, doc_type: str | None, stylesheet: str | None = None) -> tuple[str, dict | None]:
        """HTML for a document. -> (html, stylesheet entry used or None)."""
        root = etree.QName(tree.getroot()).localname
        if stylesheet:
            e = self.get(stylesheet)
        else:
            cands = self.applicable("view", standard, doc_type, root)
            e = cands[0] if cands else None
        if e is None:
            return generic_view(tree), None
        try:
            res = self._xslt(e)(tree)
        except etree.XSLTApplyError as ex:
            raise ValueError(f"{e['name']} failed on this document: {ex}") from ex
        return etree.tostring(res, method="html", encoding="unicode") if res.getroot() is not None else str(res), e

    def transform(self, data: bytes, standard: str | None, doc_type: str | None, root: str | None) -> tuple[bytes, list[dict]]:
        """Apply every enabled transform that fits, in order. -> (new bytes, entries applied)."""
        used = self.applicable("transform", standard, doc_type, root)
        if not used:
            return data, []
        tree = etree.parse(io.BytesIO(data), document_parser())
        for e in used:
            try:
                tree = self._xslt(e)(tree)
            except etree.XSLTApplyError as ex:
                raise ValueError(f"transform {e['name']} failed: {ex}") from ex
            if tree.getroot() is None:
                raise ValueError(f"transform {e['name']} produced no XML document")
        out = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", pretty_print=True)
        return out, used


def generic_view(tree) -> str:
    """Fallback when no view fits: the document as an indented outline."""
    from html import escape
    lines = []

    def walk(el, depth):
        if not isinstance(el.tag, str):
            return
        attrs = " ".join(f'{etree.QName(k).localname}="{escape(v)}"' for k, v in el.attrib.items()
                         if not k.startswith("{http://www.w3.org/2001/XMLSchema-instance}"))
        text = (el.text or "").strip()
        lines.append(f'<div style="margin-left:{depth}em"><b>{escape(etree.QName(el).localname)}</b>'
                     f'{" <small>" + attrs + "</small>" if attrs else ""}{" — " + escape(text) if text else ""}</div>')
        for c in el:
            walk(c, depth + 1)
    walk(tree.getroot(), 0)
    return ('<html><head><meta charset="utf-8"><style>body{font:13px/1.5 system-ui,sans-serif;padding:1rem 1.5rem;color:#18212b}'
            'small{color:#6f7a86}</style></head><body><p><i>No view stylesheet fits this document; showing its structure.</i></p>'
            + "".join(lines) + "</body></html>")
