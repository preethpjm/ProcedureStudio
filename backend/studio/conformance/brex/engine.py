"""Checking a document against BREX rules.

Rule semantics (S1000D allowedObjectFlag):
  0  prohibited  — whatever the path finds is a violation (with object values: only those values are prohibited)
  1  required    — the path must find something
  2  allowed     — whatever the path finds must have one of the listed values (no values: no restriction)
Paths are XPath. Most run on lxml (fast) with the XPath 2.0 functions BREX files use added per
expression; anything lxml cannot evaluate falls back to elementpath (full XPath 2.0). A rule that
neither can evaluate is reported as not checked — never as passed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from .model import Brex, Rule, _first, _local, dmc_of


# ------------------------------------------------------------------ XPath 2.0 functions for lxml
def _s(v) -> str:
    if isinstance(v, list):
        v = v[0] if v else ""
    if isinstance(v, etree._Element):
        return "".join(v.itertext())
    return "" if v is None else str(v)


def _seq(v) -> list[str]:
    if isinstance(v, list):
        return [_s(x) for x in v]
    return [] if v is None else [_s(v)]


def _flags(f) -> int:
    f = _s(f)
    return (re.I if "i" in f else 0) | (re.S if "s" in f else 0) | (re.M if "m" in f else 0) | (re.X if "x" in f else 0)


def _class_end(p: str, i: int) -> tuple[str, str | None, int]:
    """p[i] == '['. -> (positive class body, subtracted class or None, index after the closing ']')."""
    j = i + 1
    body = ""
    if j < len(p) and p[j] == "^":
        body, j = "^", j + 1
    if j < len(p) and p[j] == "]":                     # a ']' right at the start is literal
        body, j = body + "\\]", j + 1
    while j < len(p):
        c = p[j]
        if c == "\\" and j + 1 < len(p):
            body += p[j:j + 2]
            j += 2
            continue
        if c == "-" and j + 1 < len(p) and p[j + 1] == "[":      # XML Schema class subtraction
            sub, j = _xsd_to_py_class(p, j + 1)
            if j < len(p) and p[j] == "]":
                j += 1
            return body, sub, j
        if c == "]":
            return body, None, j + 1
        body += c
        j += 1
    return body, None, j


def _xsd_to_py_class(p: str, i: int) -> tuple[str, int]:
    body, sub, j = _class_end(p, i)
    cls = f"[{body}]"
    return (f"(?:(?!{sub}){cls})" if sub else cls), j


def _rx(p: str) -> str:
    """An XML Schema / XPath regular expression in Python's syntax: class subtraction
    ([A-Z-[O]] = A to Z except O) and the XML name escapes \\i and \\c."""
    p = p.replace(r"\i", r"[A-Za-z_:]").replace(r"\c", r"[-\w.:]")
    if "-[" not in p:
        return p
    out, i = "", 0
    while i < len(p):
        c = p[i]
        if c == "\\" and i + 1 < len(p):
            out += p[i:i + 2]
            i += 2
        elif c == "[":
            cls, i = _xsd_to_py_class(p, i)
            out += cls
        else:
            out += c
            i += 1
    return out


XPATH2 = {
    "matches": lambda ctx, s, p, f="": re.search(_rx(_s(p)), _s(s), _flags(f)) is not None,
    "tokenize": lambda ctx, s, p, f="": [t for t in re.split(_rx(_s(p)), _s(s), flags=_flags(f))],
    "replace": lambda ctx, s, p, r, f="": re.sub(_rx(_s(p)), re.sub(r"\$(\d)", r"\\\1", _s(r)), _s(s), flags=_flags(f)),
    "lower-case": lambda ctx, s: _s(s).lower(),
    "upper-case": lambda ctx, s: _s(s).upper(),
    "ends-with": lambda ctx, s, t: _s(s).endswith(_s(t)),
    "string-join": lambda ctx, seq, sep="": _s(sep).join(_seq(seq)),
    "distinct-values": lambda ctx, seq: list(dict.fromkeys(_seq(seq))),
    "compare": lambda ctx, a, b: float((_s(a) > _s(b)) - (_s(a) < _s(b))),
    "exists": lambda ctx, seq: bool(seq) if isinstance(seq, list) else seq is not None,
    "empty": lambda ctx, seq: (not seq) if isinstance(seq, list) else seq is None,
    "abs": lambda ctx, n: abs(float(_s(n) or 0)),
}
_EXT = {(None, k): v for k, v in XPATH2.items()}
# prefixes BREX paths use without declaring them (S1000D conventions)
NS = {"xlink": "http://www.w3.org/1999/xlink", "xsi": "http://www.w3.org/2001/XMLSchema-instance",
      "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#", "dc": "http://www.purl.org/dc/elements/1.1/"}


@dataclass
class Compiled:
    rule: Rule
    lx: object = None             # lxml XPath, or None
    ep: object = None             # elementpath selector, or None
    error: str | None = None


def compile_rule(rule: Rule) -> Compiled:
    c = Compiled(rule)
    try:
        c.lx = etree.XPath(rule.path, extensions=_EXT, namespaces=NS)
    except etree.XPathSyntaxError:
        c.lx = None
    if c.lx is None:
        c.ep = _ep(rule.path)
        if c.ep is None:
            c.error = "the rule's path is not valid XPath"
    return c


def _ep(path: str):
    try:
        import elementpath
        from elementpath import XPath2Parser
        return elementpath.Selector(path, parser=XPath2Parser, namespaces=NS)
    except Exception:              # noqa: BLE001 - an unusable path is reported, not raised
        return None


def _evaluate(c: Compiled, root):
    if c.lx is not None:
        try:
            return c.lx(root)
        except (etree.XPathEvalError, etree.XPathResultError, TypeError, ValueError, re.error):
            if c.ep is None:
                c.ep = _ep(c.rule.path)
    if c.ep is not None:
        return c.ep.select(root)
    raise ValueError(c.error or "cannot evaluate")


def _items(res) -> list:
    """Normalise an XPath result: the things found (empty list = nothing / false)."""
    if isinstance(res, list):
        return [x for x in res if x is not None]
    if isinstance(res, bool):
        return [True] if res else []
    if isinstance(res, (int, float)):
        return [res] if res else []
    if isinstance(res, str):
        return [res] if res else []
    return [res] if res is not None else []


def _value(x) -> str:
    if isinstance(x, etree._Element):
        return "".join(x.itertext()).strip()
    if hasattr(x, "value") and not isinstance(x, str):        # elementpath attribute node
        return str(x.value)
    return str(x)


def _node_line(x, root) -> tuple[int | None, object]:
    """(line, element) of a found item: elements, attributes and text results map to their element."""
    if isinstance(x, etree._Element):
        return x.sourceline, x
    gp = getattr(x, "getparent", None)
    if callable(gp):
        el = gp()
        if isinstance(el, etree._Element):
            return el.sourceline, el
    el = getattr(x, "parent", None) or getattr(x, "elem", None)
    if isinstance(el, etree._Element):
        return el.sourceline, el
    return None, None


def _value_ok(v: str, values) -> bool:
    for form, allowed, _ in values:
        if form == "single" and v == allowed:
            return True
        if form == "range" and "~" in allowed:
            lo, hi = (x.strip() for x in allowed.split("~", 1))
            try:
                if float(lo) <= float(v) <= float(hi):
                    return True
            except ValueError:
                if lo <= v <= hi:
                    return True
        if form == "pattern":
            try:
                if re.fullmatch(_rx(allowed), v):
                    return True
            except re.error:
                pass
    return False


@dataclass
class Finding:
    rule: Rule
    brex: Brex
    line: int | None
    element: object
    detail: str = ""


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)
    checked: int = 0
    not_checked: list[tuple[Rule, str]] = field(default_factory=list)


class CompiledBrex:
    def __init__(self, brex: Brex):
        self.brex = brex
        self.rules = [compile_rule(r) for r in brex.rules]

    def check(self, root, dm_type: str | None, result: Result | None = None) -> Result:
        res = result or Result()
        for c in self.rules:
            r = c.rule
            if r.context and dm_type and r.context != dm_type:
                continue
            try:
                found = _items(_evaluate(c, root))
            except Exception as e:  # noqa: BLE001 - not checked, reported
                res.not_checked.append((r, str(e)[:160]))
                continue
            res.checked += 1
            if r.flag == "1":
                if not found:
                    res.findings.append(Finding(r, self.brex, None, None, "required, but not found"))
                continue
            if r.flag == "2":
                if not r.values:
                    continue
                for x in found:
                    v = _value(x)
                    if not _value_ok(v, r.values):
                        line, el = _node_line(x, root)
                        allowed = ", ".join(f"'{a}'" for _, a, _ in r.values[:8])
                        res.findings.append(Finding(r, self.brex, line, el, f"value '{v}'; allowed: {allowed}"))
                continue
            # flag 0 (and anything unknown): prohibited
            for x in found:
                if r.values and not _value_ok(_value(x), r.values):
                    continue                                       # only the listed values are prohibited
                line, el = _node_line(x, root)
                res.findings.append(Finding(r, self.brex, line, el,
                                            f"value '{_value(x)}'" if r.values else ""))
        self._check_sns(root, res)
        return res

    def _check_sns(self, root, res: Result) -> None:
        sns = self.brex.sns
        if not sns:
            return
        ident = _first(root, "dmIdent")
        code = _first(ident, "dmCode") if ident is not None else None
        if code is None:
            return
        levels = [("system", code.get("systemCode", "")), ("subsystem", code.get("subSystemCode", "")),
                  ("sub-subsystem", code.get("subSubSystemCode", "")), ("assembly", code.get("assyCode", ""))]
        node_children, path = sns, []
        for name, val in levels:
            if not node_children or not val:
                break                                          # an absent code is a schema error, reported there
            if val not in node_children:
                rule = Rule(0, "BREX-SNS", None, "dmCode", "2",
                            f"The {name} code '{val}' of this data module code is not defined in the BREX's SNS "
                            f"({'-'.join(path) or 'top level'}: allowed {', '.join(sorted(node_children)[:12])}"
                            f"{' …' if len(node_children) > 12 else ''}).")
                res.findings.append(Finding(rule, self.brex, code.sourceline, code))
                return
            path.append(val)
            node_children = node_children[val].children
        res.checked += 1


def document_brex_ref(root) -> str | None:
    """The DMC the document names in its brexDmRef (S1000D dmStatus/brexDmRef)."""
    status = _first(root, "dmStatus")
    if status is None:
        status = _first(root, "pmStatus")
    ref = _first(status, "brexDmRef") if status is not None else None
    code = _first(ref, "dmCode") if ref is not None else None
    return dmc_of(code) if code is not None else None
