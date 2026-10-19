"""DTD validation for XML documents (ATA iSpec 2200 XML, OEM DTDs).

The DTD and every entity file it pulls in are loaded only from the installed
package (plus its dependency packages), through the package catalog. The document
is always validated against the doc type's own entry DTD: its DOCTYPE external
identifier is redirected to that DTD without changing line numbers, and a DOCTYPE
is inserted on the root's line if the document has none. Entity expansion is
bounded by libxml2's amplification limit; anything outside the package is refused.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

from lxml import etree

from ..security.xml_safe import BlockedResolution, ConfinedResolver
from .model import Fix

ENTRY = "asthra-dtd:entry"
_EXTID = re.compile(r"""\s+(?:PUBLIC\s+("[^"]*"|'[^']*')(?:\s+("[^"]*"|'[^']*'))?|SYSTEM\s+("[^"]*"|'[^']*'))""")


def validating_parser(roots: list[Path], catalog: dict[str, str]) -> tuple[etree.XMLParser, ConfinedResolver]:
    p = etree.XMLParser(load_dtd=True, dtd_validation=True, resolve_entities=True, no_network=True,
                        huge_tree=False, recover=True)
    r = ConfinedResolver(roots, catalog)
    p.resolvers.add(r)
    return p, r


def _root_start(text: str) -> int:
    """Offset of the root element's '<' (skips declaration, comments, PIs, DOCTYPE)."""
    i, n = 0, len(text)
    while i < n:
        j = text.find("<", i)
        if j < 0:
            return -1
        if text.startswith("<?", j):
            i = text.find("?>", j) + 2
        elif text.startswith("<!--", j):
            i = text.find("-->", j) + 3
        elif text.startswith("<!", j):
            depth, k, q = 0, j + 2, ""
            while k < n:
                c = text[k]
                if q:
                    q = "" if c == q else q
                elif c in "\"'":
                    q = c
                elif c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                elif c == ">" and depth <= 0:
                    break
                k += 1
            i = k + 1
        else:
            return j
        if i <= 0:
            return -1
    return -1


def redirect_doctype(text: str, root_qname: str) -> str:
    """Point the document at the package's entry DTD, keeping every line number."""
    text = re.sub(r"""^(\ufeff?<\?xml[^>]*?)\s+encoding\s*=\s*["'][^"']*["']""", r"\1", text, count=1)
    text = text.lstrip("\ufeff")
    m = re.search(r"<!DOCTYPE\s+([^\s\[>]+)", text)
    rs = _root_start(text)
    if m and (rs < 0 or m.start() < rs):
        after = m.end()
        ext = _EXTID.match(text, after)
        new = f' SYSTEM "{ENTRY}"'
        if ext:
            new += "\n" * text.count("\n", ext.start(), ext.end())
            return text[:ext.start()] + new + text[ext.end():]
        return text[:after] + new + text[after:]
    if rs < 0:
        return text
    return text[:rs] + f'<!DOCTYPE {root_qname} SYSTEM "{ENTRY}">' + text[rs:]


def _values(dtd, el: str, attr: str) -> list[str]:
    if dtd is None:
        return []
    for e in dtd.iterelements():
        if e.name == el:
            for a in e.iterattributes():
                if a.name == attr:
                    return list(a.values())
    return []


def explain_dtd(raw: str, dtd) -> tuple[str, str | None, str | None, str | None, Fix | None, str | None]:
    """-> (message, suggestion, attribute, value, fix, element name)"""
    if (m := re.match(r"Element (\S+) content does not follow the DTD, expecting \((.*)\), got \((.*)\)", raw)):
        el, exp, got = m.groups()
        return (f"<{el}> contains elements in an order or combination the DTD does not allow.",
                f"Expected: {exp.strip()}. Found: {got.strip() or 'nothing'}.", None, None, None, el)
    if (m := re.match(r"No declaration for element (\S+)", raw)):
        return f"<{m[1]}> is not defined in this DTD.", "Check the spelling, or remove it.", None, None, None, m[1]
    if (m := re.match(r"Element (\S+) does not carry attribute (\S+)", raw)):
        return (f"<{m[1]}> is missing the required attribute {m[2]}.", f'Add {m[2]}="…" to <{m[1]}>.',
                m[2], None, None, m[1])
    if (m := re.match(r"No declaration for attribute (\S+) of element (\S+)", raw)):
        return (f"Attribute {m[1]} is not allowed on <{m[2]}>.", f"Remove {m[1]}, or check its spelling.",
                m[1], None, None, m[2])
    if (m := re.match(r'Value "(.*)" for attribute (\S+) of (\S+) is not among the enumerated set', raw)):
        v, a, el = m.groups()
        allowed = _values(dtd, el, a)
        ci = [x for x in allowed if x.lower() == v.lower()]
        close = ci or difflib.get_close_matches(v, allowed, n=3, cutoff=0.75)
        sugg = ("Did you mean " + " or ".join(f"'{c}'" for c in close) + "?") if close else \
            (("Allowed values: " + ", ".join(f"'{x}'" for x in allowed) + ".") if allowed and len(allowed) <= 12 else None)
        fix = Fix(label=f"Replace with '{close[0]}'", kind="attr", attribute=a, value=close[0]) if len(close) == 1 else None
        return f"'{v}' is not an allowed value for attribute {a} on <{el}>.", sugg, a, v, fix, el
    if (m := re.match(r"Entity '(\S+)' not defined", raw)):
        return (f"&{m[1]}; is not declared in the DTD or the installed entity sets.",
                "Check the entity name, or install the entity files your DTD package needs.", None, m[1], None, None)
    if (m := re.match(r'IDREFS? attribute (\S+) references an unknown ID "(.*)"', raw)):
        return (f"Reference '{m[2]}' (attribute {m[1]}) does not match any ID in this document.",
                "Correct the reference or add the referenced element.", m[1], m[2], None, None)
    if (m := re.match(r"Syntax of value for attribute (\S+) of (\S+) is not valid", raw)):
        return (f"The value of {m[1]} on <{m[2]}> has the wrong format for its declared type.", None, m[1], None, None, m[2])
    if (m := re.match(r"Element (\S+) was declared EMPTY this one has content", raw)):
        return f"<{m[1]}> must be empty.", "Remove its content.", None, None, None, m[1]
    if (m := re.match(r"root and DTD name do not match '(\S+)' and '(\S+)'", raw)):
        return (f"The root element <{m[1]}> is not the one this DTD is for (<{m[2]}>).",
                "Choose the matching document type, or check the file.", None, None, None, m[1])
    if (m := re.match(r"ID (\S+) already defined", raw)):
        return f"ID '{m[1]}' is used more than once.", "Every id must be unique in the document.", None, m[1], None, None
    return raw, None, None, None, None, None


def validate_dtd(text: str, root_qname: str, roots: list[Path], catalog: dict[str, str], entry: str):
    """-> (tree or None, [libxml2 log entries], dtd object or None, blocked message or None)"""
    cat = dict(catalog)
    cat[ENTRY] = entry
    parser, resolver = validating_parser(roots, cat)
    src = redirect_doctype(text, root_qname)
    try:
        tree = etree.fromstring(src.encode("utf-8"), parser).getroottree()
    except BlockedResolution as e:
        return None, [], None, str(e)
    except etree.XMLSyntaxError:
        return None, list(parser.error_log), None, None
    entries = [e for e in parser.error_log if e.level_name in ("ERROR", "FATAL")]
    return tree, entries, tree.docinfo.externalDTD, None


def entity_texts(dtd) -> dict[str, str]:
    """Replacement text of internal entities (e.g. mdash -> '—'), for display only."""
    import html
    out: dict[str, str] = {}
    if dtd is None:
        return out
    for e in dtd.iterentities():
        if e.content is not None and not e.system_url:
            out[e.name] = html.unescape(e.content)
    return out
