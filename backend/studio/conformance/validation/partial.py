"""Finding every schema error, as a streaming validator (Xerces) does.

libxml2 stops checking the rest of an element's content — and everything inside it — at the first
element it does not expect there; and it cannot validate a document that is not well-formed.
Xerces does both: it reports errors in the skipped parts, and the errors before the point where a
broken file breaks off. Two helpers close the gap:

  continue_after_skips   validate what libxml2 skipped: the contents of the unexpected element (laxly,
                         through global declarations — children of an undeclared element are checked on
                         their own) and of each later sibling. As in Xerces, the parent's order is
                         judged once (the first error); the siblings' contents are still checked.
                         Each part is validated as a fragment that keeps the document's DOCTYPE (for
                         ENTITY checks); errors are mapped back to the original elements and lines.
                         ID references are left to the whole-document check.
  partial_check          a document that is not well-formed: recovery parse, validate, and keep only
                         errors before the break — never "incomplete" errors caused by the file ending.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass

from lxml import etree

NOT_EXPECTED = "This element is not expected"
NO_GLOBAL = "No matching global declaration available for the validation root"
IDREF_NOISE = re.compile(r"xs:IDREFS?'|keyref|key-sequence|No match found for key", re.I)


@dataclass
class Entry:
    """Shaped like an lxml error-log entry, with the original element attached."""
    line: int | None
    column: int | None
    message: str
    type_name: str
    path: str | None
    node: object = None


def _decode(data: bytes) -> str:
    """The document text in its own encoding (S1000D files are often UTF-16 with a byte-order mark)."""
    from ..identify.service import sniff
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode(sniff(data).encoding or "utf-8", errors="replace")


def _doctype_text(data: bytes) -> str:
    text = _decode(data)
    m = re.search(r"<!DOCTYPE", text)
    if not m:
        return ""
    i, depth, q = m.end(), 0, ""
    while i < len(text):
        c = text[i]
        if q:
            if c == q:
                q = ""
        elif c in "\"'":
            q = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        elif c == ">" and depth <= 0:
            return text[m.start():i + 1]
        i += 1
    return ""


def _parser():
    from ..security.xml_safe import NoExternalResolver
    p = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, remove_comments=False)
    p.resolvers.add(NoExternalResolver())
    return p


def _validate_fragment(schema, frag_el, originals: list, doctype: str, tree) -> list[Entry] | None:
    """Validate an element copy as its own document. originals: the original elements in document
    order, matching frag_el.iter(). None if the element has no global declaration."""
    text = (doctype + "\n" if doctype else "") + etree.tostring(frag_el, encoding="unicode", with_tail=False)
    try:
        doc = etree.fromstring(text.encode("utf-8"), _parser()).getroottree()
    except etree.XMLSyntaxError:
        return []
    schema.validate(doc)
    log = list(schema.error_log)
    if any(NO_GLOBAL in e.message for e in log):
        return None
    frag_elems = [e for e in doc.getroot().iter() if isinstance(e.tag, str)]
    index = {id(el): i for i, el in enumerate(frag_elems)}
    out = []
    for e in log:
        if IDREF_NOISE.search(e.message):
            continue
        node = None
        try:
            hit = doc.xpath(e.path) if e.path else []
            if hit and isinstance(hit[0], etree._Element) and id(hit[0]) in index:
                i = index[id(hit[0])]
                node = originals[i] if i < len(originals) else None
        except etree.XPathError:
            node = None
        line = node.sourceline if node is not None else None
        out.append(Entry(line, None, e.message, e.type_name, tree.getpath(node) if node is not None else None, node))
    return out


def _elements(el, skip=None) -> list:
    out = []
    for d in el.iter():
        if not isinstance(d.tag, str):
            continue
        if skip is not None and (d is skip or skip in d.iterancestors()):
            continue
        out.append(d)
    return out


def continue_after_skips(schema, tree, data: bytes, entries, limit: int = 200) -> list[Entry]:
    if hasattr(schema, "schema"):          # the xmlschema engine continues by itself
        return []
    doctype = _doctype_text(data)
    seen = {(e.line, e.message) for e in entries}
    out: list[Entry] = []
    queue = list(entries)
    budget = limit

    def node_of(e):
        n = getattr(e, "node", None)
        if n is None and getattr(e, "path", None):
            try:
                hit = tree.xpath(e.path)
                n = hit[0] if hit and isinstance(hit[0], etree._Element) else None
            except etree.XPathError:
                n = None
        return n

    def lax(el):
        """Validate el, or (if it has no global declaration) each of its children, recursively."""
        nonlocal budget
        if budget <= 0:
            return []
        budget -= 1
        res = _validate_fragment(schema, copy.deepcopy(el), _elements(el), doctype, tree)
        if res is not None:
            return res
        found = []
        for c in el:
            if isinstance(c.tag, str):
                found += lax(c)
        return found

    handled = set()
    while queue and budget > 0:
        e = queue.pop(0)
        if NOT_EXPECTED not in e.message:
            continue
        c = node_of(e)
        if c is None or id(c) in handled:
            continue
        handled.add(id(c))
        p = c.getparent()
        # like Xerces: one order error per parent; then the contents of the unexpected element and of
        # each later sibling are still checked (their order within the parent is not re-judged)
        new = lax(c)
        if p is not None:
            for sib in c.itersiblings():
                if isinstance(sib.tag, str) and budget > 0:
                    new += lax(sib)
        for n in new:
            k = (n.line, n.message)
            if k in seen:
                continue
            seen.add(k)
            out.append(n)
            if NOT_EXPECTED in n.message:
                queue.append(n)                            # it skipped again further down: continue there too
    return out


def open_element_lines(data: bytes, before_line: int) -> set[int]:
    """Start-tag lines of the elements still open at the start of before_line."""
    text = _decode(data).replace("\r\n", "\n")
    stop = sum(len(l) for l in text.splitlines(keepends=True)[:max(0, before_line - 1)]) if before_line else len(text)
    tok = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<!DOCTYPE(?:[^\[>]|\[.*?\])*>|</\s*([^\s>]+)\s*>|<([^\s/>!?]+)(?:[^>\"']|\"[^\"]*\"|'[^']*')*?(/?)>", re.S)
    stack = []
    for m in tok.finditer(text, 0, stop):
        if m.group(1):
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == m.group(1):
                    del stack[i:]
                    break
        elif m.group(2) and not m.group(3):
            stack.append((m.group(2), text.count("\n", 0, m.start()) + 1))
    return {line for _, line in stack}


def partial_check(schema, data: bytes, break_line: int | None) -> tuple[list[Entry], object]:
    """Validate a document that is not well-formed up to the point where it breaks."""
    from ..security.xml_safe import NoExternalResolver
    p = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    p.resolvers.add(NoExternalResolver())
    from ..identify.service import fix_declared_encoding
    try:
        root = etree.fromstring(fix_declared_encoding(data), p)
    except etree.XMLSyntaxError:
        return [], None
    if root is None:
        return [], None
    tree = root.getroottree()
    schema.validate(tree)
    open_lines = open_element_lines(data, break_line or 0)
    first = list(schema.error_log)
    keep: list[Entry] = []
    for e in first:
        if break_line and e.line and e.line >= break_line:
            continue
        if "Missing child element" in e.message and e.line in open_lines:
            continue                                       # incomplete only because the file ends early
        keep.append(Entry(e.line, e.column, e.message, e.type_name, e.path, None))
    more = continue_after_skips(schema, tree, data, keep)
    keep += [m for m in more if not (break_line and m.line and m.line >= break_line)
             and not ("Missing child element" in m.message and m.line in open_lines)]
    return keep, tree
