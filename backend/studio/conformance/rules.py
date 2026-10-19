"""Project rule sets: business rules beyond the schema, added by the publication team.

Two formats, both installed into the rule library (``<data>/conformance/rules``) and checked in the
business-rules stage next to the S1000D BREX:

* **Schematron** (ISO, ``.sch``): ``assert``/``report`` with ``@id`` (rule id) and ``@role``
  (``fatal | error | warning | info``). Run through lxml's ISO Schematron implementation.
* **Rule file** (``.yaml``/``.yml``/``.json``): a list of XPath rules, for teams that do not write
  Schematron. Each rule has an ``id``, a ``context`` XPath and one test:

  .. code-block:: yaml

     id: acme-house-rules
     name: ACME house rules
     applies_to: {standards: [S1000D], doc_types: [proced], roots: [dmodule]}   # all optional
     namespaces: {}                                   # prefix -> URI, for namespaced documents
     rules:
       - id: ACME-001
         severity: error                              # fatal | error | warning | info (default error)
         context: //proceduralStep
         assert: "normalize-space(para) != ''"        # must be true for every context node
         message: Every procedural step has text.
         suggestion: Write the instruction in the step's para.
       - id: ACME-002
         context: //para
         forbid: "contains(., ';')"                   # must be false
         message: Do not use semicolons.
       - id: ACME-003
         context: //dmCode
         attribute: modelIdentCode                    # check an attribute (or the text when omitted)
         allowed: [TECHPUB, SLSBSELV]                 # value list ...
       - id: ACME-004
         context: //quantityValue
         pattern: '^\\d+(\\.\\d+)?$'                  # ... or a regular expression (full match)
         message: Quantities are plain decimal numbers.

Every rule set can be enabled or disabled, and limited to standards, document types or root
elements. A rule that cannot be evaluated is reported as not checked, never as passed.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

from .security.xml_safe import document_parser
from .validation.model import Diagnostic, Severity

SEVERITIES = {s.value for s in Severity}
SVRL = "{http://purl.oclc.org/dsdl/svrl}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-").lower() or "rules"


@dataclass
class Applies:
    standards: list[str] = field(default_factory=list)
    doc_types: list[str] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, d: dict | None) -> "Applies":
        d = d or {}
        lst = lambda v: [v] if isinstance(v, str) else list(v or [])
        return cls([s.upper() for s in lst(d.get("standards") or d.get("standard"))],
                   lst(d.get("doc_types") or d.get("doc_type")), lst(d.get("roots") or d.get("root")))

    def fits(self, standard: str | None, doc_type: str | None, root: str | None) -> bool:
        return ((not self.standards or (standard or "").upper() in self.standards)
                and (not self.doc_types or doc_type in self.doc_types)
                and (not self.roots or root in self.roots))

    def as_dict(self) -> dict:
        return {"standards": self.standards, "doc_types": self.doc_types, "roots": self.roots}


@dataclass
class Outcome:
    diagnostics: list[Diagnostic] = field(default_factory=list)
    checked: int = 0
    not_checked: list[tuple[str, str]] = field(default_factory=list)     # (rule id, reason)


# ---------------------------------------------------------------------------------------- XPath rule files
def _line(node, root) -> tuple[int | None, etree._Element | None]:
    el = node.getparent() if isinstance(node, etree._ElementUnicodeResult) else node
    if not isinstance(el, etree._Element):
        el = root
    return el.sourceline, el


def parse_rule_file(data: bytes, name: str) -> dict:
    text = data.decode("utf-8-sig")
    if name.lower().endswith(".json"):
        doc = json.loads(text)
    else:
        import yaml
        doc = yaml.safe_load(text)
    if isinstance(doc, list):
        doc = {"rules": doc}
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list) or not doc["rules"]:
        raise ValueError("a rule file needs a non-empty 'rules' list")
    ns = doc.get("namespaces") or {}
    for i, r in enumerate(doc["rules"], start=1):
        if not isinstance(r, dict):
            raise ValueError(f"rule {i} is not a mapping")
        r.setdefault("id", f"RULE-{i:03d}")
        if not r.get("context"):
            raise ValueError(f"{r['id']}: 'context' (an XPath) is required")
        tests = [k for k in ("assert", "forbid", "allowed", "pattern") if k in r]
        if len(tests) != 1:
            raise ValueError(f"{r['id']}: give exactly one of assert, forbid, allowed, pattern")
        sev = str(r.get("severity", "error")).lower()
        if sev not in SEVERITIES:
            raise ValueError(f"{r['id']}: severity must be one of {', '.join(sorted(SEVERITIES))}")
        r["severity"] = sev
        try:                                              # compile now, so a bad rule fails at install
            etree.XPath(r["context"], namespaces=ns)
            for k in ("assert", "forbid"):
                if k in r:
                    etree.XPath(r[k], namespaces=ns)
            if "pattern" in r:
                re.compile(r["pattern"])
        except (etree.XPathSyntaxError, re.error) as e:
            raise ValueError(f"{r['id']}: {e}") from e
    return doc


def check_rule_file(doc: dict, tree, set_name: str, file: str) -> Outcome:
    out = Outcome()
    root = tree.getroot() if hasattr(tree, "getroot") else tree
    ns = doc.get("namespaces") or {}
    for r in doc["rules"]:
        try:
            nodes = etree.XPath(r["context"], namespaces=ns)(root)
        except etree.XPathError as e:
            out.not_checked.append((r["id"], str(e)))
            continue
        out.checked += 1
        nodes = nodes if isinstance(nodes, list) else []
        for n in nodes:
            ok, value = True, None
            try:
                if "assert" in r:
                    ok = bool(etree.XPath(r["assert"], namespaces=ns)(n))
                elif "forbid" in r:
                    ok = not bool(etree.XPath(r["forbid"], namespaces=ns)(n))
                else:
                    el = n if isinstance(n, etree._Element) else None
                    if r.get("attribute"):
                        if el is None or el.get(r["attribute"]) is None:
                            continue                      # absent attribute: a schema matter, not this rule's
                        value = el.get(r["attribute"])
                    else:
                        value = ("".join(el.itertext()) if el is not None else str(n)).strip()
                    if "allowed" in r:
                        ok = value in [str(v) for v in r["allowed"]]
                    else:
                        ok = re.fullmatch(r["pattern"], value) is not None
            except etree.XPathError as e:
                out.not_checked.append((r["id"], str(e)))
                break
            if ok:
                continue
            line, el = _line(n, root)
            from .validation.pipeline import readable_path
            msg = r.get("message") or f"Rule {r['id']} is not met."
            if value is not None:
                msg += f" Found '{value}'" + (f" in @{r['attribute']}." if r.get("attribute") else ".")
            out.diagnostics.append(Diagnostic(
                stage=4, severity=r["severity"], rule_id=str(r["id"]), category="business-rule", message=msg,
                suggestion=r.get("suggestion") or (f"Allowed: {', '.join(map(str, r['allowed']))}." if "allowed" in r else None),
                source_file=file, line=line, element_path=readable_path(el) if el is not None else None,
                attribute=r.get("attribute"), value=value, reference=f"Rule set {set_name}"))
    return out


# ---------------------------------------------------------------------------------------- Schematron
def compile_schematron(data: bytes):
    from lxml import isoschematron
    try:
        doc = etree.fromstring(data, document_parser())
    except etree.XMLSyntaxError as e:
        raise ValueError(f"the Schematron file is not well-formed: {e}") from e
    try:
        return isoschematron.Schematron(doc, store_report=True), doc
    except etree.SchematronParseError as e:
        raise ValueError(f"not a valid ISO Schematron schema: {e}") from e
    except etree.XSLTParseError as e:
        raise ValueError(f"the Schematron could not be compiled: {e}") from e


def check_schematron(compiled, sch_doc, tree, set_name: str, file: str) -> Outcome:
    from .validation.pipeline import readable_path
    out = Outcome()
    compiled.validate(tree)
    report = compiled.validation_report
    sch_ns = "{http://purl.oclc.org/dsdl/schematron}"
    roles = {a.get("id"): (a.get("role") or "error").lower() for a in sch_doc.iter(f"{sch_ns}assert", f"{sch_ns}report") if a.get("id")}
    out.checked = sum(1 for _ in sch_doc.iter(f"{sch_ns}assert", f"{sch_ns}report"))
    root = tree.getroot()
    for i, f in enumerate(report.iter(f"{SVRL}failed-assert", f"{SVRL}successful-report"), start=1):
        rid = f.get("id") or f"SCH-{i:03d}"
        role = (f.get("role") or roles.get(f.get("id")) or "error").lower()
        if role not in SEVERITIES:
            role = "warning" if role in ("warn", "caution") else "error"
        text = " ".join(" ".join("".join(t.itertext()).split()) for t in f.iter(f"{SVRL}text")) or f.get("test", "")
        el = None
        try:
            hit = root.getroottree().xpath(f.get("location") or "")
            el = hit[0] if hit and isinstance(hit[0], etree._Element) else None
        except etree.XPathError:
            pass
        out.diagnostics.append(Diagnostic(
            stage=4, severity=role, rule_id=rid, category="business-rule", message=text,
            suggestion=None, source_file=file, line=el.sourceline if el is not None else None,
            element_path=readable_path(el) if el is not None else f.get("location"),
            raw_message=f.get("test"), reference=f"Schematron {set_name}"))
    return out


# ---------------------------------------------------------------------------------------- library
class RuleLibrary:
    """Installed rule sets: ``index.json`` plus the files, one folder."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_file = self.root / "index.json"
        self._lock = threading.Lock()
        self._compiled: dict[str, tuple[float, object]] = {}

    def list(self) -> list[dict]:
        try:
            return json.loads(self.index_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

    def _save(self, entries: list[dict]) -> None:
        self.index_file.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def get(self, rid: str) -> dict:
        e = next((x for x in self.list() if x["id"] == rid), None)
        if e is None:
            raise KeyError(f"no rule set {rid}")
        return e

    def install(self, data: bytes, filename: str, name: str | None = None, applies_to: dict | None = None) -> dict:
        """Add or replace a rule set (same id replaces). Raises ValueError for a file that does not compile."""
        low = filename.lower()
        if low.endswith((".sch", ".schematron")):
            _, doc = compile_schematron(data)
            sch = "{http://purl.oclc.org/dsdl/schematron}"
            title = doc.findtext(f"{sch}title") or Path(filename).stem
            kind, count, rid = "schematron", sum(1 for _ in doc.iter(f"{sch}assert", f"{sch}report")), _slug(Path(filename).stem)
            ext, app = ".sch", Applies.of(applies_to)
        elif low.endswith((".yaml", ".yml", ".json")):
            doc = parse_rule_file(data, filename)
            title = doc.get("name") or Path(filename).stem
            kind, count, rid = "xpath", len(doc["rules"]), _slug(doc.get("id") or Path(filename).stem)
            ext, app = Path(low).suffix, Applies.of(applies_to or doc.get("applies_to"))
        else:
            raise ValueError("a rule set is a Schematron file (.sch) or a rule file (.yaml, .yml, .json)")
        with self._lock:
            entries = [e for e in self.list() if e["id"] != rid]
            old = next((e for e in self.list() if e["id"] == rid), None)
            file = rid + ext
            (self.root / file).write_bytes(data)
            entry = {"id": rid, "name": name or title, "kind": kind, "file": file, "rules": count,
                     "applies_to": app.as_dict(), "enabled": old["enabled"] if old else True,
                     "source_name": filename, "installed_at": _now()}
            entries.append(entry)
            self._save(sorted(entries, key=lambda e: e["id"]))
            self._compiled.pop(rid, None)
        return entry

    def update(self, rid: str, enabled: bool | None = None, applies_to: dict | None = None, name: str | None = None) -> dict:
        with self._lock:
            entries = self.list()
            e = next((x for x in entries if x["id"] == rid), None)
            if e is None:
                raise KeyError(f"no rule set {rid}")
            if enabled is not None:
                e["enabled"] = bool(enabled)
            if applies_to is not None:
                e["applies_to"] = Applies.of(applies_to).as_dict()
            if name:
                e["name"] = name
            self._save(entries)
            return e

    def remove(self, rid: str) -> None:
        with self._lock:
            entries = self.list()
            e = next((x for x in entries if x["id"] == rid), None)
            if e is None:
                raise KeyError(f"no rule set {rid}")
            (self.root / e["file"]).unlink(missing_ok=True)
            self._save([x for x in entries if x["id"] != rid])
            self._compiled.pop(rid, None)

    def source(self, rid: str) -> tuple[str, bytes]:
        e = self.get(rid)
        return e["file"], (self.root / e["file"]).read_bytes()

    def _load(self, e: dict):
        p = self.root / e["file"]
        mtime = p.stat().st_mtime
        hit = self._compiled.get(e["id"])
        if hit and hit[0] == mtime:
            return hit[1]
        data = p.read_bytes()
        obj = compile_schematron(data) if e["kind"] == "schematron" else parse_rule_file(data, e["file"])
        self._compiled[e["id"]] = (mtime, obj)
        return obj

    def applicable(self, standard: str | None, doc_type: str | None, root: str | None) -> list[dict]:
        return [e for e in self.list() if e.get("enabled", True)
                and Applies.of(e.get("applies_to")).fits(standard, doc_type, root)]

    def check(self, tree, standard: str | None, doc_type: str | None, file: str) -> tuple[Outcome, list[dict]]:
        root = etree.QName(tree.getroot()).localname
        total, used = Outcome(), self.applicable(standard, doc_type, root)
        for e in used:
            try:
                obj = self._load(e)
                o = check_schematron(*obj, tree, e["name"], file) if e["kind"] == "schematron" \
                    else check_rule_file(obj, tree, e["name"], file)
            except Exception as ex:                       # noqa: BLE001 - one broken set never hides the others
                total.not_checked.append((e["id"], f"rule set could not run: {ex}"))
                continue
            total.diagnostics += o.diagnostics
            total.checked += o.checked
            total.not_checked += o.not_checked
        return total, used
