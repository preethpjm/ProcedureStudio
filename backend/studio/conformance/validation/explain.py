"""Turn raw libxml2 schema messages into plain explanations, a location hint
(attribute / value) and, only where the correction is certain, a fix.

A fix is offered only if its result is known to satisfy the facet that failed:
  * enumeration: the replacement is itself a member of the allowed set;
  * decimal written with a comma: the replacement parses as xs:decimal;
  * pattern: the replacement matches the schema's own pattern.
Nothing is applied automatically; the user clicks the fix, and the document is
revalidated afterwards. Engineering values are never converted or guessed.
"""
from __future__ import annotations

import difflib
import re
from decimal import Decimal, InvalidOperation

from .model import Fix

_HEAD = re.compile(r"^Element '(?P<el>[^']+)'(?:, attribute '(?P<attr>[^']+)')?: ")
_ENUM = re.compile(r"\[facet 'enumeration'\] The value '(?P<v>.*?)' is not an element of the set \{(?P<set>.*)\}\.?$", re.S)
_TYPE = re.compile(r"'(?P<v>.*?)' is not a valid value of the (?:atomic|list|union) type '(?P<t>[^']+)'")
_PATTERN = re.compile(r"\[facet 'pattern'\] The value '(?P<v>.*?)' is not accepted by the pattern '(?P<p>.*)'\.?$", re.S)
_NOT_EXPECTED = re.compile(r"This element is not expected\.(?: Expected is(?: one of)? \( (?P<exp>.*?) \))?")
_MISSING_CHILD = re.compile(r"Missing child element\(s\)\. Expected is(?: one of)? \( (?P<exp>.*?) \)")
_REQ_ATTR = re.compile(r"The attribute '(?P<a>[^']+)' is required but missing")
_NOT_ALLOWED_ATTR = re.compile(r"The attribute '(?P<a>[^']+)' is not allowed")
_RULE = re.compile(r"The schema rule \[(?P<test>.*?)\] is not met\.(?: (?P<doc>.*))?$", re.S)
_IDREF = re.compile(r"Reference '(?P<v>[^']+)' \(attribute (?P<a>[\w:.-]+)\)")

_TYPE_HELP = {
    "xs:decimal": "a plain decimal number such as 0.079 (use a point, no comma, no fraction, no unit)",
    "xs:integer": "a whole number", "xs:nonNegativeInteger": "a whole number, 0 or more",
    "xs:positiveInteger": "a whole number greater than 0", "xs:ID": "a unique identifier (letters, digits, - _ .; must not start with a digit)",
    "xs:IDREF": "the id of an element in this document", "xs:date": "a date as YYYY-MM-DD",
    "xs:ENTITY": "the name of an unparsed entity declared in the DOCTYPE",
}


def _local(q: str) -> str:
    return q.split("}")[-1].split(":")[-1]


def _names(exp: str) -> list[str]:
    return [_local(x.strip()) for x in exp.split(",") if x.strip()]


def explain(raw: str) -> tuple[str, str | None, str | None, str | None, Fix | None]:
    """-> (message, suggestion, attribute, value, fix)"""
    m = _HEAD.match(raw)
    el = _local(m.group("el")) if m else None
    attr = m.group("attr") if m else None
    body = raw[m.end():] if m else raw
    target = f"attribute {attr} on <{el}>" if attr else (f"<{el}>" if el else "this element")

    if (r := _RULE.search(body)):
        doc = (r.group("doc") or "").strip()
        test = r.group("test")
        msg = f"<{el}> breaks a rule written into the schema: " + (doc if doc else f"the condition {test} is false") + ("" if doc.endswith(".") or not doc else ".")
        return (msg if msg.endswith(".") else msg + "."), f"Schema rule (XSD assertion): {test}", None, None, None

    if (e := _ENUM.search(body)):
        v = e.group("v")
        allowed = [x.strip()[1:-1] for x in re.findall(r"'(?:[^']|'')*'", e.group("set"))]
        ci = [a for a in allowed if a.lower() == v.lower()]
        close = ci or difflib.get_close_matches(v, allowed, n=3, cutoff=0.75)
        if close:
            sugg = "Did you mean " + " or ".join(f"'{c}'" for c in close) + "?"
        elif len(allowed) <= 12:
            sugg = "Allowed values: " + ", ".join(f"'{a}'" for a in allowed) + "."
        else:
            sugg = None
        count = f" It must be one of {len(allowed)} allowed values." if len(allowed) > 12 else ""
        fix = Fix(label=f"Replace with '{close[0]}'", kind="attr" if attr else "text", attribute=attr, value=close[0]) \
            if len(close) == 1 or len(ci) == 1 else None
        return f"'{v}' is not an allowed value for {target}.{count}", sugg, attr, v, fix

    if (e := _PATTERN.search(body)):
        v, pat = e.group("v"), e.group("p")
        fix = None
        cand = None
        if v.isdigit():
            for width in range(len(v) + 1, len(v) + 4):
                c = v.zfill(width)
                try:
                    if re.fullmatch(pat, c):
                        cand = c
                        break
                except re.error:
                    break
        if cand:
            fix = Fix(label=f"Replace with '{cand}'", kind="attr" if attr else "text", attribute=attr, value=cand)
        return (f"'{v}' has the wrong format for {target}.", f"It must match the pattern {pat}." +
                (f" For example '{cand}'." if cand else ""), attr, v, fix)

    if (e := _TYPE.search(body)) and e.group("t") in ("xs:ENTITY", "xs:ENTITIES"):
        v = e.group("v")
        return (f"{target} refers to '{v}', which is not declared as an entity in the document's DOCTYPE.",
                f"Declare it in the DOCTYPE, e.g. <!ENTITY {v} SYSTEM \"{v}.CGM\" NDATA cgm> (with the matching "
                "<!NOTATION>), or correct the reference. In S1000D this is usually an ICN of a graphic.",
                attr, v, None)

    if (e := _TYPE.search(body)):
        v, t = e.group("v"), e.group("t")
        help_ = _TYPE_HELP.get(t, f"a value of type {t}")
        fix = None
        sugg = f"It must be {help_}."
        if t == "xs:decimal" and re.fullmatch(r"-?\d+,\d+", v.strip()):
            c = v.strip().replace(",", ".")
            try:
                Decimal(c)
                fix = Fix(label=f"Replace with '{c}'", kind="attr" if attr else "text", attribute=attr, value=c)
                sugg = "Use a decimal point instead of a comma."
            except InvalidOperation:
                pass
        elif t == "xs:decimal" and re.fullmatch(r"\s*\d+\s*/\s*\d+\s*", v):
            n, d = (int(x) for x in v.split("/"))
            if d:
                sugg = (f"Write the fraction as a decimal (e.g. {n}/{d} = {n / d:.4f}) at the precision your "
                        "rules require. Procedure Studio does not choose the precision for you.")
        elif t == "xs:ENTITY":
            sugg = (f"Declare it in the DOCTYPE, e.g. <!NOTATION cgm SYSTEM \"cgm\"> and "
                    f"<!ENTITY {v} SYSTEM \"{v}.cgm\" NDATA cgm> (use the real file format).")
        return f"'{v}' is not valid for {target}.", sugg, attr, v, fix

    if (e := _NOT_EXPECTED.search(body)):
        exp = _names(e.group("exp") or "")
        sugg = ("At this position the schema allows: " + ", ".join(f"<{x}>" for x in exp) + ".") if exp else \
            "No further elements are allowed here."
        return f"<{el}> is not allowed at this position.", sugg + " Move or remove it.", None, None, None

    if (e := _MISSING_CHILD.search(body)):
        exp = _names(e.group("exp"))
        return (f"<{el}> is incomplete: a required child element is missing.",
                "Add " + " or ".join(f"<{x}>" for x in exp) + ".", None, None, None)

    if (e := _REQ_ATTR.search(body)):
        a = e.group("a")
        return f"<{el}> is missing the required attribute {a}.", f"Add {a}=\"…\" to <{el}>.", a, None, None

    if (e := _NOT_ALLOWED_ATTR.search(body)):
        a = e.group("a")
        return f"Attribute {a} is not allowed on <{el}>.", f"Remove {a}, or check its spelling.", a, None, None

    if (e := _IDREF.search(raw)):
        return raw, None, e.group("a"), e.group("v"), None

    return raw, None, attr, None, None
