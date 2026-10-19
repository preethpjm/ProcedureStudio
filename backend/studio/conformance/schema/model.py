"""One schema model for every schema language (spec §6, "deterministic schema intelligence").

    {"root": "dmodule",
     "elements": {
        "procedure": {"content": <particle> | None, "mixed": bool, "empty": bool, "any": bool,
                      "text": bool,                      # text only, no child elements
                      "attrs": [{"name", "required", "kind", "values", "default", "fixed"}],
                      "inclusions": [...], "exclusions": [...]},   # SGML exceptions
        ...}}
    particle := {"k": "el", "n": name, "min": int, "max": int | None}
              | {"k": "seq" | "choice" | "all", "items": [particle...], "min": int, "max": int | None}
              | {"k": "any", "min": int, "max": int | None}
    attribute kind: "text" | "id" | "idref" | "idrefs" | "enum" | "token" | "number" | "entity"

The editor answers "what may go here", "what does a new element need" and "can this be
deleted/moved" from this model alone; nothing is inferred by AI or hard-coded per standard.
"""
from __future__ import annotations

import re
from pathlib import Path

XLINK = "http://www.w3.org/1999/xlink"


# ------------------------------------------------------------------ XSD (xmlschema)
def _local(qname: str | None) -> str:
    if not qname:
        return ""
    if qname.startswith("{"):
        ns, local = qname[1:].split("}", 1)
        return f"xlink:{local}" if ns == XLINK else local
    return qname


def _type_chain(t):
    seen = 0
    while t is not None and seen < 20:
        yield t
        t = getattr(t, "base_type", None)
        seen += 1


def _xsd_attr(name: str, a) -> dict:
    t = a.type
    names = [_local(getattr(x, "name", None) or "") for x in _type_chain(t)]
    enum = list(getattr(t, "enumeration", None) or [])
    kind = "text"
    if enum:
        kind = "enum"
    elif any(n == "ID" for n in names):
        kind = "id"
    elif any(n == "IDREFS" for n in names):
        kind = "idrefs"
    elif any(n == "IDREF" for n in names):
        kind = "idref"
    elif any(n in ("decimal", "integer", "int", "positiveInteger", "nonNegativeInteger") for n in names):
        kind = "number"
    elif any(n in ("NMTOKEN", "NMTOKENS", "NCName", "Name", "token") for n in names):
        kind = "token"
    elif any(n == "ENTITY" for n in names):
        kind = "entity"
    return {"name": _local(name), "required": a.use == "required", "kind": kind,
            "values": [str(v) for v in enum], "default": a.default, "fixed": a.fixed}


def xsd_model(schema, root: str) -> dict:
    import xmlschema
    elements: dict[str, dict] = {}

    def particle(p) -> dict | None:
        if isinstance(p, xmlschema.validators.XsdElement):
            ref = getattr(p, "ref", None) or p
            if getattr(ref, "abstract", False):
                subs = [e for e in schema.substitution_groups.get(ref.name, []) if not getattr(e, "abstract", False)]
                items = []
                for e in subs:
                    visit(e)
                    items.append({"k": "el", "n": _local(e.name), "min": 1, "max": 1})
                return {"k": "choice", "items": items, "min": p.min_occurs, "max": p.max_occurs}
            visit(p)
            return {"k": "el", "n": _local(p.name), "min": p.min_occurs, "max": p.max_occurs}
        if isinstance(p, xmlschema.validators.XsdGroup):
            model = {"sequence": "seq", "choice": "choice", "all": "all"}.get(p.model, "seq")
            items = [x for x in (particle(i) for i in p) if x is not None]
            return {"k": model, "items": items, "min": p.min_occurs, "max": p.max_occurs}
        if isinstance(p, xmlschema.validators.XsdAnyElement):
            return {"k": "any", "min": p.min_occurs, "max": p.max_occurs}
        return None

    def visit(el) -> None:
        name = _local(el.name)
        if name in elements:
            return
        t = el.type
        entry = {"content": None, "mixed": False, "empty": False, "any": False, "text": False,
                 "attrs": [], "inclusions": [], "exclusions": []}
        elements[name] = entry                           # before recursing (recursive models)
        try:
            entry["attrs"] = [_xsd_attr(n, a) for n, a in el.attributes.items()
                              if n and not str(n).startswith("{http://www.w3.org/2001/XMLSchema-instance}")]
        except Exception:                                # noqa: BLE001 - exotic attribute wildcards
            entry["attrs"] = []
        if t.is_simple() or t.has_simple_content():
            entry["text"] = True
            return
        entry["mixed"] = bool(t.has_mixed_content())
        entry["empty"] = bool(t.is_empty())
        content = getattr(t, "content", None)
        if content is not None and hasattr(content, "model"):
            entry["content"] = particle(content)

    for e in schema.elements.values():
        visit(e)
    return {"root": root, "elements": elements}


# ------------------------------------------------------------------ XML DTD (lxml)
_OCCUR = {"once": (1, 1), "opt": (0, 1), "mult": (0, None), "plus": (1, None)}


def _dtd_particle(decl) -> dict | None:
    if decl is None:
        return None
    lo, hi = _OCCUR.get(decl.occur, (1, 1))
    if decl.type == "pcdata":
        return None
    if decl.type == "element":
        return {"k": "el", "n": decl.name, "min": lo, "max": hi}
    kind = "seq" if decl.type == "seq" else "choice"
    items = []

    def flat(d):
        if d is None:
            return
        if d.type == decl.type and d.occur == "once" and d is not decl:
            flat(d.left); flat(d.right)
        else:
            p = _dtd_particle(d)
            if p:
                items.append(p)
    flat(decl.left); flat(decl.right)
    return {"k": kind, "items": items, "min": lo, "max": hi}


def dtd_model(dtd, root: str) -> dict:
    elements = {}
    for el in dtd.iterelements():
        attrs = []
        for a in el.iterattributes():
            kind = {"id": "id", "idref": "idref", "idrefs": "idrefs", "enumeration": "enum", "nmtoken": "token",
                    "nmtokens": "token", "entity": "entity", "entities": "entity"}.get(a.type, "text")
            attrs.append({"name": a.name if not a.prefix else f"{a.prefix}:{a.name}", "required": a.default == "required",
                          "kind": kind, "values": list(a.values()) if a.type == "enumeration" else [],
                          "default": a.default_value, "fixed": a.default_value if a.default == "fixed" else None})
        entry = {"content": None, "mixed": el.type == "mixed", "empty": el.type == "empty", "any": el.type == "any",
                 "text": False, "attrs": attrs, "inclusions": [], "exclusions": []}
        if el.type in ("element", "mixed"):
            p = _dtd_particle(el.content)
            if el.type == "mixed":
                names = []
                def names_of(d):
                    if d is None:
                        return
                    if d.type == "element":
                        names.append(d.name)
                    names_of(d.left); names_of(d.right)
                names_of(el.content)
                if names:
                    entry["content"] = {"k": "choice", "items": [{"k": "el", "n": n, "min": 1, "max": 1} for n in names],
                                        "min": 0, "max": None}
                else:
                    entry["text"] = True
            else:
                entry["content"] = p
        elements[el.name] = entry
    return {"root": root, "elements": elements}


# ------------------------------------------------------------------ SGML DTD (own parser)
def _strip_comments(text: str) -> str:
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    return text


def _expand_pe(text: str, base: Path, catalog: dict[str, str], depth: int = 0) -> str:
    """Declare and expand parameter entities (internal values and external files from the package)."""
    if depth > 8:
        return text
    pes: dict[str, str] = {}
    out, pos = [], 0
    decl = re.compile(r"""<!ENTITY\s+%\s+([\w.:-]+)\s+(?:"([^"]*)"|'([^']*)'|(PUBLIC|SYSTEM)\s+("[^"]*"|'[^']*')(?:\s+("[^"]*"|'[^']*'))?)[^>]*>""", re.I | re.S)
    ref = re.compile(r"%([A-Za-z][\w.:-]*);?")
    while pos < len(text):
        m = decl.search(text, pos)
        chunk_end = m.start() if m else len(text)
        chunk = text[pos:chunk_end]
        chunk = ref.sub(lambda r: pes.get(r.group(1), r.group(0)), chunk)
        out.append(chunk)
        if not m:
            break
        name = m.group(1)
        if m.group(2) is not None or m.group(3) is not None:
            val = m.group(2) if m.group(2) is not None else m.group(3)
            pes.setdefault(name, ref.sub(lambda r: pes.get(r.group(1), r.group(0)), val))
        else:
            ids = [x[1:-1] for x in (m.group(5), m.group(6)) if x]
            target = None
            for i in ids:
                rel = catalog.get(i) or catalog.get(Path(i).name)
                if rel and (base / rel).is_file():
                    target = base / rel
                    break
            if target is not None:
                sub = _strip_comments(target.read_text(encoding="utf-8", errors="ignore"))
                pes.setdefault(name, _expand_pe(sub, target.parent, catalog, depth + 1))
            else:
                pes.setdefault(name, "")
        pos = m.end()
    return "".join(out)


def _names(group: str) -> list[str]:
    return [n.lower() for n in re.findall(r"[A-Za-z][\w.:-]*", group)]


class _GroupParser:
    def __init__(self, text: str):
        self.toks = re.findall(r"#PCDATA|[A-Za-z][\w.:-]*|[()|,&?*+]", text, re.I)
        self.i = 0
        self.mixed = False

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def occ(self, p):
        t = self.peek()
        if t in ("?", "*", "+"):
            self.i += 1
            p["min"], p["max"] = {"?": (0, 1), "*": (0, None), "+": (1, None)}[t]
        return p

    def primary(self):
        t = self.peek()
        if t == "(":
            self.i += 1
            items, conn = [], None
            while self.peek() not in (")", None):
                if self.peek() in ("|", ",", "&"):
                    conn = conn or self.toks[self.i]
                    self.i += 1
                    continue
                it = self.primary()
                if it:
                    items.append(it)
            self.i += 1
            kind = {"|": "choice", ",": "seq", "&": "all", None: "seq"}[conn]
            return self.occ({"k": kind, "items": items, "min": 1, "max": 1})
        self.i += 1
        if t and t.upper() == "#PCDATA":
            self.mixed = True
            return None
        return self.occ({"k": "el", "n": t.lower(), "min": 1, "max": 1})


def sgml_dtd_model(dtd_file: Path, root: str, catalog: dict[str, str]) -> dict:
    text = _strip_comments(dtd_file.read_text(encoding="utf-8", errors="ignore"))
    text = _expand_pe(text, dtd_file.parent, catalog)
    text = re.sub(r"--.*?--", " ", text, flags=re.S)           # comments inside declarations
    elements: dict[str, dict] = {}
    for m in re.finditer(r"<!ELEMENT\s+(\([^)]*\)|[\w.:-]+)\s+(?:([-Oo])\s+([-Oo])\s+)?(.*?)>", text, re.S | re.I):
        names, body = _names(m.group(1)), m.group(4).strip()
        inc = re.findall(r"\+\(([^)]*)\)", body)
        exc = re.findall(r"-\(([^)]*)\)", body)
        body = re.sub(r"[+-]\([^)]*\)", " ", body).strip()
        entry = {"content": None, "mixed": False, "empty": False, "any": False, "text": False, "attrs": [],
                 "inclusions": [n for g in inc for n in _names(g)], "exclusions": [n for g in exc for n in _names(g)],
                 "end_tag_omissible": (m.group(3) or "-").upper() == "O"}
        kw = body.upper()
        if kw.startswith("EMPTY"):
            entry["empty"] = True
        elif kw.startswith(("CDATA", "RCDATA")):
            entry["text"] = True
        elif kw.startswith("ANY"):
            entry["any"] = True
        else:
            gp = _GroupParser(body)
            p = gp.primary()
            if gp.mixed:
                names_in = []
                def collect(x):
                    if not x:
                        return
                    if x["k"] == "el":
                        names_in.append(x["n"])
                    for c in x.get("items", []):
                        collect(c)
                collect(p)
                if names_in:
                    entry["mixed"] = True
                    entry["content"] = {"k": "choice", "items": [{"k": "el", "n": n, "min": 1, "max": 1} for n in names_in],
                                        "min": 0, "max": None}
                else:
                    entry["text"] = True
            else:
                entry["content"] = p
        for n in names:
            elements[n] = dict(entry)
    for m in re.finditer(r"<!ATTLIST\s+(\([^)]*\)|[\w.:-]+)\s+(.*?)>", text, re.S | re.I):
        targets = _names(m.group(1))
        toks = re.findall(r'"[^"]*"|\'[^\']*\'|\([^)]*\)|#?[\w.:-]+', m.group(2))
        attrs, i = [], 0
        while i + 2 <= len(toks):
            name, dv = toks[i].lower(), toks[i + 1]
            i += 2
            default_tok = toks[i] if i < len(toks) else "#IMPLIED"
            i += 1
            fixed = None
            if default_tok.upper() == "#FIXED" and i < len(toks):
                fixed = toks[i].strip("\"'"); i += 1
            values = []
            if dv.startswith("("):
                kind, values = "enum", [v.lower() for v in re.findall(r"[\w.:-]+", dv)]
            else:
                kind = {"ID": "id", "IDREF": "idref", "IDREFS": "idrefs", "NUMBER": "number", "NUMBERS": "number",
                        "NAME": "token", "NAMES": "token", "NMTOKEN": "token", "NMTOKENS": "token", "NUTOKEN": "token",
                        "ENTITY": "entity", "ENTITIES": "entity"}.get(dv.upper(), "text")
            req = default_tok.upper() == "#REQUIRED"
            default = None if default_tok.startswith("#") else default_tok.strip("\"'")
            attrs.append({"name": name, "required": req, "kind": kind, "values": values, "default": default, "fixed": fixed})
        for t in targets:
            if t in elements:
                elements[t]["attrs"] = elements[t]["attrs"] + attrs
            else:
                elements[t] = {"content": None, "mixed": False, "empty": False, "any": False, "text": False,
                               "attrs": attrs, "inclusions": [], "exclusions": []}
    return {"root": root.lower(), "elements": elements}
