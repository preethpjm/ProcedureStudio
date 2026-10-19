"""XSD validation through the xmlschema library, for schemas libxml2 cannot compile.

libxml2 implements XSD 1.0 only. The S-Series 2021 block release schemas (S2000M 7.0, S3000L 2.0 …)
use XSD 1.1 features such as xs:assert, which libxml2 rejects ("Element complexType: The content is
not valid"). xmlschema supports XSD 1.0 and 1.1. Its structured errors are translated into the same
message forms libxml2 produces, so explanations, precise diagnostics and fixes work unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from lxml import etree

XSD11_MARKERS = (b":assert", b":openContent", b":alternative", b":override", b"defaultAttributes",
                 b"XMLSchema-versioning", b":defaultOpenContent")


def looks_like_xsd11(paths) -> bool:
    for p in paths:
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if any(m in data for m in XSD11_MARKERS):
            return True
    return False


@dataclass
class Entry:
    """Shaped like an lxml error-log entry."""
    line: int | None
    column: int | None
    message: str
    type_name: str
    path: str | None
    node: object = None          # the element itself (namespaced paths cannot be looked up without prefixes)


def _local(tag) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else str(tag)


def _names(expected) -> list[str]:
    if expected is None:
        return []
    items = expected if isinstance(expected, (list, tuple)) else [expected]
    out = []
    for x in items:
        n = getattr(x, "name", None) or (x if isinstance(x, str) else None)
        if n:
            out.append(_local(n))
    return out


def _doc_text(validator) -> str:
    ann = getattr(validator, "annotation", None)
    docs = getattr(ann, "documentation", None) or []
    texts = [("".join(d.itertext()) if hasattr(d, "itertext") else str(d)).strip() for d in docs]
    return " ".join(t for t in texts if t)


def translate(err, tree) -> Entry | None:
    elem = getattr(err, "elem", None)
    is_node = isinstance(elem, etree._Element)
    tag = _local(elem.tag) if is_node else "?"
    line = elem.sourceline if is_node else None
    path = tree.getpath(elem) if is_node else None
    reason = (err.reason or "").strip()
    vname = type(getattr(err, "validator", None)).__name__

    if "IDREF" in reason and "not found" in reason:
        return None                                   # reported by the ID/IDREF check, with its target
    if type(err).__name__ == "XMLSchemaChildrenValidationError":
        exp = _names(getattr(err, "expected", None))
        exp_s = f" Expected is ( {', '.join(exp)} )." if exp else ""
        invalid = getattr(err, "invalid_tag", None)
        if invalid is not None and is_node:
            kids = [c for c in elem if isinstance(c.tag, str)]
            i = getattr(err, "index", None)
            child = kids[i] if isinstance(i, int) and 0 <= i < len(kids) else None
            return Entry(child.sourceline if child is not None else line, None,
                         f"Element '{_local(invalid)}': This element is not expected.{exp_s}",
                         "SCHEMAV_ELEMENT_CONTENT", tree.getpath(child) if child is not None else path,
                         child if child is not None else elem)
        return Entry(line, None, f"Element '{tag}': Missing child element(s).{exp_s}", "SCHEMAV_ELEMENT_CONTENT", path)
    if vname == "XsdAssert":
        if reason != "assertion test is false":
            return None                               # could not evaluate: follows from an invalid value reported separately
        test = getattr(err.validator, "path", "") or ""
        doc = _doc_text(err.validator)
        return Entry(line, None, f"Element '{tag}': The schema rule [{test}] is not met.{(' ' + doc) if doc else ''}",
                     "SCHEMAV_CVC_ASSERTION", path)
    if (m := re.match(r"missing required attribute '([^']+)'", reason)):
        return Entry(line, None, f"Element '{tag}': The attribute '{_local(m[1])}' is required but missing.",
                     "SCHEMAV_CVC_COMPLEX_TYPE_4", path)
    if (m := re.match(r"'([^']+)' attribute not allowed", reason)):
        a = _local(m[1])
        return Entry(line, None, f"Element '{tag}', attribute '{a}': The attribute '{a}' is not allowed.",
                     "SCHEMAV_CVC_COMPLEX_TYPE_3_2_1", path)
    head = f"Element '{tag}'"
    rest = reason
    if (m := re.match(r"attribute ([^=\s]+)=(['\"])(.*?)\2: (.*)$", reason, re.S)):
        head += f", attribute '{_local(m[1])}'"
        value, rest = m[3], m[4]
    else:
        value = elem.text.strip() if is_node and elem.text else ""
    if (m := re.match(r"value must be one of \[(.*)\]", rest, re.S)):
        vals = ", ".join(f"'{v}'" for v in re.findall(r"'([^']*)'", m[1]))
        return Entry(line, None, f"{head}: [facet 'enumeration'] The value '{value}' is not an element of the set {{{vals}}}.",
                     "SCHEMAV_CVC_ENUMERATION_VALID", path)
    if (m := re.match(r"value doesn't match any pattern of \[(.*)\]", rest, re.S)):
        pats = re.findall(r"'([^']*)'", m[1])
        return Entry(line, None, f"{head}: [facet 'pattern'] The value '{value}' is not accepted by the pattern '{pats[0] if pats else ''}'.",
                     "SCHEMAV_CVC_PATTERN_VALID", path)
    if (m := re.match(r"duplicated xs:ID value '([^']*)'", rest)):
        return Entry(line, None, f"{head}: '{m[1]}' is not a valid value of the atomic type 'xs:ID'.",
                     "SCHEMAV_CVC_DATATYPE_VALID_1_2_1", path)
    if (m := re.match(r"invalid value '(.*)' for (xs:\S+)", rest, re.S)):
        return Entry(line, None, f"{head}: '{m[1]}' is not a valid value of the atomic type '{m[2]}'.",
                     "SCHEMAV_CVC_DATATYPE_VALID_1_2_1", path)
    return Entry(line, None, f"{head}: {rest}", "SCHEMAV_XMLSCHEMA", path)


class XmlschemaValidator:
    """Drop-in for lxml.etree.XMLSchema in the validation pipeline (validate + error_log)."""

    def __init__(self, schema, version: str):
        self.schema = schema
        self.version = version
        self.engine = f"xmlschema (XSD {version})"
        self.error_log: list[Entry] = []

    def validate(self, tree) -> bool:
        entries = []
        for err in self.schema.iter_errors(tree):
            e = translate(err, tree)
            if e is not None:
                if e.node is None and isinstance(getattr(err, "elem", None), etree._Element):
                    e.node = err.elem
                entries.append(e)
        self.error_log = entries
        return not entries
