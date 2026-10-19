"""Diagnostic precision (Phase 1): turn validator messages into what an engineer needs.

  * Duplicate IDs: libxml2 reports a repeated xs:ID as "not a valid value of the atomic type
    'xs:ID'", the same message as a malformed ID. Studio checks for the earlier occurrence
    and reports both places.
  * "Not expected" is split three ways using the schema's content model:
      - a required element is MISSING before this one (inserting one element makes it valid;
        if exactly one element does, an Insert fix is offered),
      - the element is in the WRONG POSITION (the parent allows it, just not here),
      - the element is NOT ALLOWED in this parent at all.
  * Every diagnostic gets a stable category (used by the benchmark and the UI), and errors
    inside an element that is itself misplaced are linked to that cause.
"""
from __future__ import annotations

import re

from lxml import etree

from .model import Diagnostic, Fix, Severity, ValidationReport

MAX_REPEAT = 12


# ------------------------------------------------------------------ content-model NFA (same semantics as the editor)
class _Nfa:
    def __init__(self, particle: dict):
        self.eps: list[list[int]] = []
        self.edges: list[tuple[int, str, int]] = []
        self.any: list[tuple[int, int]] = []
        self.start, self.end = self._build(particle)

    def _node(self) -> int:
        self.eps.append([])
        return len(self.eps) - 1

    def _build(self, p: dict) -> tuple[int, int]:
        def once() -> tuple[int, int]:
            s, e = self._node(), self._node()
            k = p["k"]
            if k == "el":
                self.edges.append((s, p["n"], e))
            elif k == "any":
                self.any.append((s, e))
            elif k == "seq":
                cur = s
                for it in p["items"]:
                    a, b = self._build(it)
                    self.eps[cur].append(a)
                    cur = b
                self.eps[cur].append(e)
            elif k == "choice":
                if not p["items"]:
                    self.eps[s].append(e)
                for it in p["items"]:
                    a, b = self._build(it)
                    self.eps[s].append(a)
                    self.eps[b].append(e)
            else:                                        # all: any order, each at most once (approximation)
                hub = self._node()
                self.eps[s].append(hub)
                self.eps[hub].append(e)
                for it in p["items"]:
                    a, b = self._build({**it, "min": min(it["min"], 1)})
                    self.eps[hub].append(a)
                    self.eps[b].append(hub)
            return s, e
        lo = min(p["min"], MAX_REPEAT)
        hi = None if p["max"] is None or p["max"] > MAX_REPEAT else p["max"]
        s, e = self._node(), self._node()
        cur = s
        for _ in range(lo):
            a, b = once()
            self.eps[cur].append(a)
            cur = b
        if hi is None:
            a, b = once()
            self.eps[cur].append(a)
            self.eps[b].append(a)
            self.eps[b].append(e)
            self.eps[cur].append(e)
        else:
            for _ in range(lo, hi):
                a, b = once()
                self.eps[cur].append(a)
                self.eps[cur].append(e)
                cur = b
            self.eps[cur].append(e)
        return s, e

    def _closure(self, states: set[int]) -> set[int]:
        stack = list(states)
        while stack:
            x = stack.pop()
            for y in self.eps[x]:
                if y not in states:
                    states.add(y)
                    stack.append(y)
        return states

    def run(self, names: list[str]) -> set[int]:
        cur = self._closure({self.start})
        for n in names:
            nxt = {b for a, name, b in self.edges if a in cur and name == n} | {b for a, b in self.any if a in cur}
            if not nxt:
                return set()
            cur = self._closure(nxt)
        return cur

    def prefix_ok(self, names: list[str]) -> bool:
        return bool(self.run(names))

    def complete(self, names: list[str]) -> bool:
        return self.end in self.run(names)


def _names_in(p: dict | None, out: set[str] | None = None) -> set[str]:
    out = set() if out is None else out
    if not p:
        return out
    if p["k"] == "el":
        out.add(p["n"])
    for it in p.get("items", []):
        _names_in(it, out)
    return out


class ModelHelper:
    """Lazy access to the document type's schema model and per-element NFAs."""
    def __init__(self, registry, key: str | None, dt_id: str | None):
        self.registry, self.key, self.dt_id = registry, key, dt_id
        self._model: dict | None = None
        self._failed = False
        self._nfas: dict[str, _Nfa] = {}

    def model(self) -> dict | None:
        if self._model is None and not self._failed and self.key and self.dt_id:
            try:
                self._model = self.registry.schema_model(self.key, self.dt_id)
            except Exception:                           # noqa: BLE001 - diagnostics degrade gracefully
                self._failed = True
        return self._model

    def element(self, name: str) -> dict | None:
        m = self.model()
        return m["elements"].get(name) if m else None

    def nfa(self, name: str) -> _Nfa | None:
        d = self.element(name)
        if not d or not d.get("content"):
            return None
        if name not in self._nfas:
            self._nfas[name] = _Nfa(d["content"])
        return self._nfas[name]


def _local(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _kids(el) -> list:
    return [c for c in el if isinstance(c.tag, str)]


# ------------------------------------------------------------------ refinements
def refine_not_expected(node, expected: list[str], helper: ModelHelper):
    """-> (message, suggestion, fix, category) or None when the model cannot decide."""
    parent = node.getparent()
    if parent is None:
        return None
    pname, off = _local(parent), _local(node)
    nfa = helper.nfa(pname)
    if nfa is None:                                  # content model not usable here: still name the parent
        exp = (" At this position the schema allows: " + ", ".join(f"<{x}>" for x in expected) + ".") if expected else ""
        return (f"<{off}> is not allowed at this position inside <{pname}>.",
                f"Remove it or move it to where it belongs.{exp}", None, "not-allowed")
    kids = _kids(parent)
    i = kids.index(node)
    prefix = [_local(k) for k in kids[:i]]
    fixers = [c for c in expected if nfa.prefix_ok(prefix + [c, off])]
    later = {_local(k): k for k in reversed(kids[i + 1:])}
    present = [c for c in fixers if c in later]
    if present:
        # the element the schema wants here exists, just further down: an ordering problem, not a missing one
        c = present[0]
        return (f"<{off}> comes too early: <{c}> (line {later[c].sourceline}) must come before it.",
                f"Move <{c}> to just before <{off}> inside <{pname}>.", None, "wrong-position")
    if fixers:
        if len(fixers) == 1:
            c = fixers[0]
            return (f"A required <{c}> is missing before <{off}>.",
                    f"Add <{c}> just before <{off}> inside <{pname}>.",
                    Fix(label=f"Insert <{c}>", kind="insert", element=c, position="before", value=""),
                    "missing-element")
        opts = ", ".join(f"<{c}>" for c in fixers)
        return (f"<{pname}> needs one of {opts} before <{off}>.",
                f"Add one of them just before <{off}>.", None, "missing-element")
    allowed_here = _names_in(helper.element(pname).get("content")) if helper.element(pname) else set()
    if off in allowed_here:
        exp = ", ".join(f"<{x}>" for x in expected) if expected else "nothing more"
        return (f"<{off}> is in the wrong position inside <{pname}>.",
                f"At this position the schema expects {exp}. Move <{off}> to its correct place.", None,
                "wrong-position")
    exp = (" At this position the schema allows: " + ", ".join(f"<{x}>" for x in expected) + ".") if expected else ""
    return (f"<{off}> is not allowed inside <{pname}>.", f"Remove it or move it to where it belongs.{exp}", None,
            "not-allowed")


def refine_missing_child(node, expected: list[str], helper: ModelHelper):
    pname = _local(node)
    nfa = helper.nfa(pname)
    if nfa is None:
        return None
    names = [_local(k) for k in _kids(node)]
    fixers = [c for c in expected if nfa.complete(names + [c]) or nfa.prefix_ok(names + [c])]
    if len(fixers) == 1:
        c = fixers[0]
        return (f"<{pname}> is incomplete: the required <{c}> is missing.", f"Add <{c}> at the end of <{pname}>.",
                Fix(label=f"Insert <{c}>", kind="insert", element=c, position="end", value=""), "missing-element")
    if fixers:
        opts = ", ".join(f"<{c}>" for c in fixers)
        return (f"<{pname}> is incomplete: it needs one of {opts}.", "Add one of them at the end.", None,
                "missing-element")
    return None


def duplicate_of(node, attr: str, value: str):
    """The first element before node carrying the same ID value, or None."""
    for el in node.getroottree().iter():
        if el is node:
            return None
        if isinstance(el.tag, str) and (el.get(attr) == value or (attr != "id" and el.get("id") == value)):
            return el
    return None


# ------------------------------------------------------------------ categories and grouping
_CATEGORY_RULES = [
    ("duplicate-id", re.compile(r"^Duplicate ID")),
    ("schema-rule", re.compile(r"breaks a rule written into the schema")),
    ("missing-element", re.compile(r"is missing before|needs one of .* before|is incomplete|required child|is missing\b.*element")),
    ("wrong-position", re.compile(r"in the wrong position")),
    ("not-allowed", re.compile(r"is not allowed (?:at this position|inside|here)|is not defined")),
    ("missing-attribute", re.compile(r"missing the required attribute|required attribute \S+ is missing")),
    ("attribute-not-allowed", re.compile(r"^Attribute \S+ is not allowed")),
    ("broken-reference", re.compile(r"does not match any ID|IDREF|no such ID")),
    ("value-invalid", re.compile(r"is not an allowed value|has the wrong format|is not valid for")),
    ("not-well-formed", re.compile(r"not well-formed|Opening and ending tag|Premature end|parser", re.I)),
]


def categorize(d: Diagnostic) -> str:
    if d.category:
        return d.category
    if d.stage == 2 and d.severity in (Severity.ERROR, Severity.FATAL):
        return "not-well-formed"
    for cat, rx in _CATEGORY_RULES:
        if rx.search(d.message or ""):
            return cat
    return "other"


def finalize(rep: ValidationReport) -> None:
    """A category for every diagnostic. (Errors inside a misplaced element are not marked as
    consequences: every part is checked against its own declaration, so they are real errors.)"""
    for d in rep.diagnostics:
        d.category = categorize(d)


def document_duplicate_ids(tree, helper: ModelHelper, reported: set[tuple[str, int | None]], file: str, ref: str):
    """Duplicate IDs anywhere in the document, including inside parts the validator skipped
    after an earlier error (libxml2 stops checking an element's content at its first misfit)."""
    from .pipeline import readable_path
    m = helper.model()
    if not m or tree is None:
        return []
    id_attrs = {n: [a["name"] for a in d.get("attrs", []) if a.get("kind") == "id"] for n, d in m["elements"].items()}
    first: dict[str, object] = {}
    out = []
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        for a in id_attrs.get(_local(el), ()):
            v = el.get(a)
            if v is None:
                continue
            if v not in first:
                first[v] = el
                continue
            if (v, el.sourceline) in reported:
                continue
            f = first[v]
            out.append(Diagnostic(
                stage=3, severity=Severity.ERROR, rule_id="VAL-DUPLICATE-ID", category="duplicate-id",
                message=(f"Duplicate ID '{v}': already used at line {f.sourceline} on <{_local(f)}>; "
                         f"used again here on <{_local(el)}>."),
                suggestion="IDs must be unique within the document. Give one of them a new ID and update any references to it.",
                attribute=a, value=v, source_file=file, element_path=readable_path(el), line=el.sourceline, reference=ref))
    return out


# ------------------------------------------------------------------ XML syntax errors in plain language
_WF = [
    (re.compile(r"Opening and ending tag mismatch: (\S+) line (\d+) and (\S+)"),
     lambda m: (f"The end tag </{m[3]}> does not match the open element <{m[1]}> (opened on line {m[2]}).",
                f"Change it to </{m[1]}>, or check for a missing or misspelled tag between line {m[2]} and here.")),
    (re.compile(r"Premature end of data in tag (\S+) line (\d+)"),
     lambda m: (f"The file ends while <{m[1]}> (line {m[2]}) is still open.",
                f"Add the missing </{m[1]}>, or check whether the file was cut off.")),
    (re.compile(r"EndTag: '</' not found|Extra content at the end of the document"),
     lambda m: ("There is content after the end of the root element, or an end tag is missing.",
                "An XML document has exactly one root element; check the last lines of the file.")),
    (re.compile(r"Specification mandates value for attribute (\S+)"),
     lambda m: (f"Attribute {m[1]} has no value.", f'Write it as {m[1]}="…" (XML requires quoted values).')),
    (re.compile(r"AttValue: \\?\\?\" or ' expected|attributes construct error"),
     lambda m: ("An attribute value is not in quotes, or an attribute is malformed.",
                'Write every attribute as name="value".')),
    (re.compile(r"Attribute (\S+) redefined"),
     lambda m: (f"Attribute {m[1]} appears twice on the same element.", "Keep only one of them.")),
    (re.compile(r"xmlParseEntityRef: no name|EntityRef: expecting ';'"),
     lambda m: ("A '&' is not followed by an entity or character reference.",
                "Write a literal ampersand as &amp;amp;.")),
    (re.compile(r"StartTag: invalid element name|Couldn't find end of Start Tag (\S+)"),
     lambda m: ("A start tag is malformed.", "Check the '<' and '>' around this position; a literal '<' must be written as &amp;lt;.")),
    (re.compile(r"Input is not proper UTF-8|Char 0x[0-9A-F]+ out of allowed range|PCDATA invalid Char value"),
     lambda m: ("The file contains a character that is not allowed in XML, or its encoding is declared wrongly.",
                "Check the encoding declaration and remove control characters.")),
    (re.compile(r"Document is empty|Start tag expected"),
     lambda m: ("The file does not contain an XML element.", "Check that this is the right file.")),
]


def explain_well_formedness(raw: str) -> tuple[str, str | None]:
    for rx, fn in _WF:
        m = rx.search(raw or "")
        if m:
            return fn(m)
    return raw, "The file is not well-formed XML; fix this before schema validation can run."
