"""Minimal ISO 10303-21 (STEP Part 21) reader for the product structure.

Open CASCADE gives us geometry and the assembly tree, but in CATIA exports the XCAF
label names are the *instance* names of NEXT_ASSEMBLY_USAGE_OCCURRENCE (often stale,
e.g. ``F6137-C72643-3.1``) and the product *name*, never the PRODUCT id that the MBOM
uses (``F6137-C75970``). This module reads the PRODUCT / PRODUCT_DEFINITION /
NAUO entities straight from the file so every occurrence can be resolved to its real
part number, name and description.

Only the handful of entity types needed are parsed; everything else is skipped
without being tokenised, so a multi-hundred-MB file is read in one pass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_WANTED = (
    "PRODUCT(",
    "PRODUCT_DEFINITION_FORMATION",
    "PRODUCT_DEFINITION(",
    "NEXT_ASSEMBLY_USAGE_OCCURRENCE(",
    "PRODUCT_DEFINITION_WITH_ASSOCIATED_DOCUMENTS(",
)
_ENTITY = re.compile(r"#(\d+)\s*=\s*([A-Z0-9_]+)\s*\((.*)\)\s*$", re.S)


@dataclass
class StepProduct:
    entity: int
    id: str
    name: str
    description: str


@dataclass
class StepUsage:
    entity: int
    id: str
    name: str
    parent_pd: int
    child_pd: int
    reference_designator: str = ""


@dataclass
class StepHeader:
    file_name: str = ""
    time_stamp: str = ""
    originating_system: str = ""
    preprocessor: str = ""
    schema: str = ""
    description: list[str] = field(default_factory=list)


@dataclass
class StepStructure:
    header: StepHeader
    products: dict[int, StepProduct]               # PRODUCT entity -> product
    pd_to_product: dict[int, int]                  # PRODUCT_DEFINITION -> PRODUCT entity
    usages: list[StepUsage]

    def product_of_pd(self, pd: int) -> StepProduct | None:
        p = self.pd_to_product.get(pd)
        return self.products.get(p) if p is not None else None

    def children_of(self, parent_pd: int) -> list[StepUsage]:
        return [u for u in self.usages if u.parent_pd == parent_pd]

    def root_pds(self) -> list[int]:
        children = {u.child_pd for u in self.usages}
        parents = {u.parent_pd for u in self.usages}
        roots = [pd for pd in self.pd_to_product if pd not in children and (pd in parents or not self.usages)]
        return roots or list(self.pd_to_product)[:1]


def _split_args(s: str) -> list[str]:
    """Top-level comma split of a STEP argument list, respecting strings and parentheses."""
    out, depth, buf, i, n = [], 0, [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "'":
            j = i + 1
            while j < n:
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            buf.append(s[i:j + 1])
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
        i += 1
    if buf or out:
        out.append("".join(buf).strip())
    return out


_ESC = re.compile(r"\\X2\\([0-9A-F]+)\\X0\\|\\X\\([0-9A-F]{2})|\\S\\(.)")


def _decode_escapes(s: str) -> str:
    def rep(m):
        if m.group(1):
            h = m.group(1)
            return "".join(chr(int(h[k:k + 4], 16)) for k in range(0, len(h), 4))
        if m.group(2):
            return bytes([int(m.group(2), 16)]).decode("latin-1")
        return chr(ord(m.group(3)) + 128)
    return _ESC.sub(rep, s)


def _str(tok: str) -> str:
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == "'" and tok[-1] == "'":
        return _decode_escapes(tok[1:-1].replace("''", "'")).strip()
    return ""


def _ref(tok: str) -> int | None:
    tok = tok.strip()
    return int(tok[1:]) if tok.startswith("#") and tok[1:].isdigit() else None


def _iter_statements(text: str):
    """Yield DATA-section statements (without the trailing ';'), string-aware."""
    start = text.find("DATA;")
    i = start + 5 if start >= 0 else 0
    n = len(text)
    buf_start = i
    in_str = False
    while i < n:
        c = text[i]
        if c == "'":
            in_str = not in_str
        elif c == ";" and not in_str:
            yield text[buf_start:i].strip()
            buf_start = i + 1
        i += 1


def _header(text: str) -> StepHeader:
    h = StepHeader()
    head = text[: text.find("DATA;")] if "DATA;" in text else text[:5000]
    m = re.search(r"FILE_NAME\s*\((.*?)\)\s*;", head, re.S)
    if m:
        a = _split_args(m.group(1))
        if len(a) >= 7:
            h.file_name, h.time_stamp = _str(a[0]), _str(a[1])
            h.preprocessor, h.originating_system = _str(a[4]), _str(a[5])
    m = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']*)'", head)
    if m:
        h.schema = m.group(1).strip()
    m = re.search(r"FILE_DESCRIPTION\s*\(\s*\((.*?)\)", head, re.S)
    if m:
        h.description = [_str(t) for t in _split_args(m.group(1))]
    return h


def read_step_structure(path: str | Path) -> StepStructure:
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8", errors="replace") if b"\x00" not in raw[:100] else raw.decode("latin-1")
    header = _header(text)
    products: dict[int, StepProduct] = {}
    pdf_to_product: dict[int, int] = {}
    pd_to_pdf: dict[int, int] = {}
    usages: list[StepUsage] = []
    for stmt in _iter_statements(text):
        if not stmt.startswith("#") or not any(w in stmt[:80] for w in _WANTED):
            continue
        m = _ENTITY.match(stmt)
        if not m:
            continue
        eid, kind, body = int(m.group(1)), m.group(2), m.group(3)
        args = _split_args(body)
        if kind == "PRODUCT" and len(args) >= 3:
            products[eid] = StepProduct(eid, _str(args[0]), _str(args[1]), _str(args[2]))
        elif kind.startswith("PRODUCT_DEFINITION_FORMATION") and len(args) >= 3:
            r = _ref(args[2])
            if r is not None:
                pdf_to_product[eid] = r
        elif kind in ("PRODUCT_DEFINITION", "PRODUCT_DEFINITION_WITH_ASSOCIATED_DOCUMENTS") and len(args) >= 3:
            r = _ref(args[2])
            if r is not None:
                pd_to_pdf[eid] = r
        elif kind == "NEXT_ASSEMBLY_USAGE_OCCURRENCE" and len(args) >= 5:
            p, c = _ref(args[3]), _ref(args[4])
            if p is not None and c is not None:
                usages.append(StepUsage(eid, _str(args[0]), _str(args[1]), p, c,
                                        _str(args[5]) if len(args) > 5 else ""))
    pd_to_product = {pd: pdf_to_product[pdf] for pd, pdf in pd_to_pdf.items() if pdf in pdf_to_product}
    return StepStructure(header, products, pd_to_product, usages)
