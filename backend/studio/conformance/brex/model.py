"""Reading a BREX data module: identity, the BREX it builds on, and its rules."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

XSI = "{http://www.w3.org/2001/XMLSchema-instance}"


def _local(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def _first(el, name):
    for d in el.iter():
        if _local(d) == name:
            return d
    return None


def dmc_of(code) -> str:
    """DMC string from a dmCode element: MIC-SDC-SYS-SUBSUBSUB-ASSY-DISDISV-INFOINFOV-ILC."""
    g = lambda k: code.get(k, "")
    return (f"{g('modelIdentCode')}-{g('systemDiffCode')}-{g('systemCode')}-{g('subSystemCode')}{g('subSubSystemCode')}-"
            f"{g('assyCode')}-{g('disassyCode')}{g('disassyCodeVariant')}-{g('infoCode')}{g('infoCodeVariant')}-{g('itemLocationCode')}")


def schema_issue(schema_location: str | None) -> str | None:
    """'4.1' from .../S1000D_4-1/xml_schema_flat/brex.xsd, '6' from .../S1000D_6/..."""
    m = re.search(r"S1000D_(\d+)(?:-(\d+))?", schema_location or "")
    return (m.group(1) + (f".{m.group(2)}" if m.group(2) else "")) if m else None


@dataclass
class Rule:
    index: int
    rule_id: str                  # e.g. BREX-CMMST-00013, or Req_dmRef_01_@caveat_01_A, or #12
    context: str | None           # schema file name the rule is limited to (e.g. "proced.xsd"), None = all
    path: str
    flag: str                     # "0" prohibited | "1" required | "2" allowed (values)
    use: str
    values: list[tuple[str, str, str]] = field(default_factory=list)   # (form, allowed, meaning)


@dataclass
class SnsNode:
    code: str
    title: str
    children: dict[str, "SnsNode"] = field(default_factory=dict)


@dataclass
class Brex:
    dmc: str
    issue: str
    title: str
    schema_issue: str | None
    parent_dmc: str | None        # the BREX this one builds on (None when it refers to itself or to nothing)
    rules: list[Rule]
    sns: dict[str, SnsNode]       # system code -> node (empty: no SNS rules)
    notations: dict[str, bool]    # notation name -> allowed
    text_rules: list[str]         # rules that are prose only (nonContextRules)

    @property
    def label(self) -> str:
        return f"{self.dmc} issue {self.issue}"


def _rule_id(use: str, index: int) -> str:
    m = re.match(r"\s*(BREX-[A-Z0-9]+-\d+(?:/BREX-[A-Z0-9]+-\d+)*)", use)
    if m:
        return m.group(1)
    m = re.search(r"\[[^\]]*?\b(Req_[^\],\s]+)\]", use)
    if m:
        return m.group(1)
    m = re.match(r"\s*([A-Z][A-Z0-9]*(?:[-_.][A-Z0-9]+)+)\s*(\([^)]*\))?\s*:", use)   # "SLS-002: ..." project style
    if m:
        return m.group(1)
    return f"rule {index}"


def _sns_tree(parent_el, level_names: list[str]) -> dict[str, SnsNode]:
    if not level_names:
        return {}
    name, rest = level_names[0], level_names[1:]
    out: dict[str, SnsNode] = {}
    for el in parent_el:
        if _local(el) != name:
            continue
        code = _text(next((c for c in el if _local(c) == "snsCode"), None))
        title = _text(next((c for c in el if _local(c) == "snsTitle"), None))
        out[code] = SnsNode(code, title, _sns_tree(el, rest))
    return out


def parse_brex(data: bytes) -> Brex:
    """Raises ValueError if the file is not a BREX data module."""
    p = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=True)
    from ..identify.service import fix_declared_encoding
    try:
        root = etree.fromstring(fix_declared_encoding(data), p)
    except etree.XMLSyntaxError as e:
        raise ValueError(f"not well-formed XML: {e}") from None
    content = _first(root, "content")
    brex_el = next((c for c in content if _local(c) == "brex"), None) if content is not None else None
    if _local(root) != "dmodule" or brex_el is None:
        raise ValueError("this is not a BREX data module (no <content><brex>)")
    code = _first(_first(root, "dmIdent"), "dmCode")
    dmc = dmc_of(code)
    iss = _first(root, "issueInfo")
    issue = f"{iss.get('issueNumber', '')}-{iss.get('inWork', '')}" if iss is not None else ""
    title = " - ".join(x for x in (_text(_first(root, "techName")), _text(_first(root, "infoName"))) if x)
    parent = None
    status = _first(root, "dmStatus")
    ref = _first(status if status is not None else root, "brexDmRef")
    if ref is not None and _first(ref, "dmCode") is not None:
        parent = dmc_of(_first(ref, "dmCode"))
        if parent == dmc:
            parent = None
    rules: list[Rule] = []
    for ctx in (x for x in brex_el.iter() if _local(x) == "contextRules"):
        context = (ctx.get("rulesContext") or "").replace("\\", "/").split("/")[-1] or None
        for r in (x for x in ctx.iter() if _local(x) == "structureObjectRule"):
            op = next((c for c in r if _local(c) == "objectPath"), None)
            if op is None or not (op.text or "").strip():
                continue
            use = _text(next((c for c in r if _local(c) == "objectUse"), None))
            vals = [(v.get("valueForm") or "single", v.get("valueAllowed") or "", _text(v))
                    for v in r.iter() if _local(v) == "objectValue"]
            i = len(rules) + 1
            rules.append(Rule(i, _rule_id(use, i), context, op.text.strip(), op.get("allowedObjectFlag", "0"), use, vals))
    sns_el = _first(brex_el, "snsRules")
    sns = {}
    if sns_el is not None:
        descr = _first(sns_el, "snsDescr")
        sns = _sns_tree(descr if descr is not None else sns_el, ["snsSystem", "snsSubSystem", "snsSubSubSystem", "snsAssy"])
    notations = {n.get("notationName", ""): n.get("allowedNotationFlag", "1") != "0"
                 for n in brex_el.iter() if _local(n) == "notationRule" and n.get("notationName")}
    text_rules = [_text(x) for x in brex_el.iter() if _local(x) == "nonContextRule" and _text(x)]
    return Brex(dmc, issue, title, schema_issue(root.get(f"{XSI}noNamespaceSchemaLocation")), parent, rules, sns,
                notations, text_rules)
