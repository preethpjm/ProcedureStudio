"""Reading a parts list (MBOM / APL / EBOM) from Excel, CSV or PDF, with the ambiguity made visible.

``inspect(path)`` finds every table that could be the parts list (each sheet; each table on each PDF page),
its header row, and a proposed column -> field mapping with how sure it is:

* ``exact``    the header is a known name for the field ("Part Number", "Désignation EN", "Qty.")
* ``guessed``  the header resembles a known name, or the column's values look like the field (part numbers,
               small whole numbers, "*"/"**" levels)
* ``missing``  nothing found

Required fields are part number, description and quantity. When one of them is guessed or missing, or two
tables score alike, the import is marked ``needs_review`` and the writer confirms it on Match 3D to parts list
-> Parts list import. The confirmed mapping is kept per product and per file layout, so the next issue of the
same export loads without asking.

``load(path, mapping)`` reads the records with a confirmed (or proposed) mapping, in the format the
reconciler expects.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path

from ..reconcile.column_mapping import DEFAULT_COLUMN_MAP, normalize_header_text
from ..reconcile.mbom_loader import _clean_cell

FIELDS = {
    "part_number": "Part number", "description": "Description", "qty": "Quantity", "level": "Level / indenture",
    "manuf_code": "CAGE / manufacturer", "unit_measure": "Unit", "find_number": "Find / item number",
    "seq_number": "Sequence number", "equiv": "Equivalence (J/K alternates)", "amdt": "Amendment",
    "comments": "Remarks", "material_spec": "Material", "class": "Class", "status": "Status",
    "cad_reference": "3D / CAD reference", "authority": "Authority",
}
REQUIRED = ("part_number", "description", "qty")
KEYWORDS = {                                     # words that suggest a field when no known header matches
    "part_number": ["part", "pn", "p/n", "réf", "reference", "ref", "pnr", "partno", "numéro", "numero", "pièce"],
    "description": ["desc", "nomen", "désign", "design", "name", "libell", "title"],
    "qty": ["qty", "qté", "quant", "upa", "nbr", "nb", "count", "nombre"],
    "level": ["lev", "niv", "indent", "depth"],
    "manuf_code": ["cage", "mfr", "manuf", "fabric", "vendor", "supplier", "fournisseur"],
    "unit_measure": ["uom", "unit", "unité"],
    "find_number": ["find", "item", "pos", "repère", "repere"],
    "seq_number": ["seq"],
    "comments": ["remark", "comment", "note", "obs"],
}


def _header_score(text: str) -> tuple[dict, dict]:
    """-> ({field: 'exact'|'guessed'}, {field: score}) for one header cell."""
    norm = normalize_header_text(text)
    if not norm:
        return {}, {}
    hit, score = {}, {}
    for f, variants in DEFAULT_COLUMN_MAP.items():
        vs = {normalize_header_text(v) for v in variants}
        if norm in vs:
            hit[f], score[f] = "exact", 1.0
            continue
        r = max(difflib.SequenceMatcher(None, norm, v).ratio() for v in vs)
        kw = any(k.replace(" ", "") in norm for k in KEYWORDS.get(f, []))
        s = max(r, 0.72 if kw else 0)
        if s >= 0.72:
            hit[f], score[f] = "guessed", round(s, 2)
    return hit, score


_PN = re.compile(r"^\.*[A-Z0-9][A-Z0-9\-./]{3,}$", re.I)


def _value_score(values: list[str]) -> dict:
    """What the values of a column look like (used when headers are unknown or ambiguous)."""
    vals = [v for v in values if v][:60]
    if len(vals) < 3:
        return {}
    n = len(vals)
    pn = sum(1 for v in vals if _PN.match(v) and re.search(r"\d", v)) / n
    num = sum(1 for v in vals if re.fullmatch(r"\d+([.,]\d+)?", v)) / n
    small_int = sum(1 for v in vals if re.fullmatch(r"\d{1,3}([.,]0+)?", v)) / n
    stars = sum(1 for v in vals if re.fullmatch(r"\*+|\d", v)) / n
    words = sum(1 for v in vals if len(v.split()) >= 2 and re.search(r"[A-Za-z]{3}", v)) / n
    cage = sum(1 for v in vals if re.fullmatch(r"[A-Z0-9]{5}", v, re.I) and re.search(r"\d", v)) / n
    distinct = len(set(vals)) / n
    out = {}
    if pn > 0.7 and distinct > 0.5:
        out["part_number"] = pn
    if words > 0.6:
        out["description"] = words
    ints = [int(float(v.replace(",", "."))) for v in vals if re.fullmatch(r"\d+([.,]0+)?", v)]
    rising = len(ints) >= 3 and all(b > a for a, b in zip(ints, ints[1:]))
    if rising and distinct > 0.95:                  # 10, 20, 30…: item / find numbers, not quantities
        out["find_number"] = 0.85
    elif small_int > 0.8 and num > 0.8:
        out["qty"] = small_int * 0.9
    if stars > 0.9 and distinct < 0.3:
        out["level"] = stars
    if cage > 0.8 and distinct < 0.5:
        out["manuf_code"] = cage
    return out


def _tables(path: Path) -> list[dict]:
    """Every candidate table: {id, label, rows}."""
    suffix = path.suffix.lower()
    out = []
    if suffix == ".csv":
        import pandas as pd
        out.append({"id": "csv", "label": path.name, "rows": pd.read_csv(path, header=None, dtype=str).values.tolist()})
    elif suffix in (".xlsx", ".xls", ".xlsm"):
        import pandas as pd
        for name, df in pd.read_excel(path, sheet_name=None, header=None, dtype=str).items():
            out.append({"id": f"sheet:{name}", "label": f"Sheet “{name}”", "rows": df.values.tolist()})
    elif suffix == ".pdf":
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            for pno, page in enumerate(pdf.pages, start=1):
                for ti, t in enumerate(page.extract_tables() or [], start=1):
                    out.append({"id": f"pdf:{pno}:{ti}", "label": f"Page {pno}, table {ti}", "rows": t, "page": pno})
    else:
        raise ValueError(f"a parts list is .xlsx, .xls, .csv or .pdf (got {suffix})")
    for t in out:
        t["rows"] = [[_clean_cell(c) for c in r] for r in t["rows"]]
    return out


def _propose(rows: list[list[str]]) -> dict | None:
    """Best header row in the first 25 rows, and its mapping. Each (field, column) pair is scored from the
    header text and from what the values below look like; columns are then assigned greedily, one field per
    column and one column per field, so a header that only sounds right cannot beat a column whose values are
    clearly part numbers."""
    best = None
    for i, row in enumerate(rows[:25]):
        if sum(1 for c in row if c) < 2:
            continue
        body = rows[i + 1:i + 61]
        width = max([len(row)] + [len(r) for r in body])
        cand = []                                   # (score, field, column, how, why)
        for ci in range(width):
            head = row[ci] if ci < len(row) else ""
            hit, sc = _header_score(head)
            vals = _value_score([r[ci] if ci < len(r) else "" for r in body])
            for f in set(hit) | set(vals):
                h, v = sc.get(f, 0.0), vals.get(f, 0.0)
                if hit.get(f) == "exact":
                    cand.append((2.0 + v, f, ci, "exact", None))
                    continue
                score = h + 0.9 * v
                if f in ("part_number", "description", "qty") and h and not v and body:
                    score -= 0.4                    # the header sounds right but the values do not fit
                if score >= 0.7:
                    why = "header and values" if h and v else "values look like this field" if v else None
                    cand.append((score, f, ci, "guessed", why))
        mapping, used, total = {}, set(), 0.0
        for score, f, ci, how, why in sorted(cand, key=lambda c: -c[0]):
            if f in mapping or ci in used:
                continue
            mapping[f] = {"column": ci, "header": row[ci] if ci < len(row) else "", "how": how, **({"why": why} if why else {})}
            used.add(ci)
            total += score
        exact = sum(1 for m in mapping.values() if m["how"] == "exact")
        score = exact * 2 + len(mapping) + total * 0.2 + (3 if "part_number" in mapping else -10)
        if best is None or score > best["score"]:
            best = {"header_row": i, "mapping": mapping, "score": round(score, 2)}
    return best


def signature(headers: list[str]) -> str:
    """Identifies a file layout (the header texts), so a confirmed mapping is reused for the next issue."""
    return hashlib.sha1("|".join(normalize_header_text(h) for h in headers).encode()).hexdigest()[:12]


def inspect(path: str | Path) -> dict:
    path = Path(path)
    tables = []
    for t in _tables(path):
        p = _propose(t["rows"])
        if not p or "part_number" not in p["mapping"]:
            continue
        hdr = t["rows"][p["header_row"]]
        body = [r for r in t["rows"][p["header_row"] + 1:] if any(r)]
        pn_col = p["mapping"]["part_number"]["column"]
        data_rows = sum(1 for r in body if pn_col < len(r) and r[pn_col])
        tables.append({"id": t["id"], "label": t["label"], "header_row": p["header_row"], "headers": hdr,
                       "signature": signature(hdr), "mapping": p["mapping"], "score": p["score"], "rows": data_rows,
                       "sample": body[:8], "page": t.get("page")})
    if not tables:
        return {"file": path.name, "tables": [], "chosen": None, "needs_review": True,
                "problems": ["No table with a part-number column was found. A scanned (image) PDF needs OCR first; "
                             "for a spreadsheet, check that the parts list has a header row."]}
    tables.sort(key=lambda t: (-t["score"], -t["rows"]))
    chosen = tables[0]
    pdf = path.suffix.lower() == ".pdf"
    def heads(t):
        return {f: normalize_header_text(m["header"]) for f, m in t["mapping"].items() if m.get("header")}

    def continues(t, c):                            # same part-number header and most mapped headers in common
        a, b = heads(t), heads(c)
        return a.get("part_number") == b.get("part_number") and len(set(a.items()) & set(b.items())) >= min(3, len(b))

    if pdf:                                         # a PDF parts list continues over pages, sometimes re-laid out
        same = [t for t in tables if continues(t, chosen)]
        chosen = {**chosen, "id": "pdf:*", "label": f"{len(same)} table(s) with this layout on pages "
                  + ", ".join(sorted({str(t['page']) for t in same}, key=int)),
                  "rows": sum(t["rows"] for t in same), "parts": [t["id"] for t in same]}
    problems = []
    for f in REQUIRED:
        m = chosen["mapping"].get(f)
        if not m:
            problems.append(f"No column was found for {FIELDS[f].lower()}.")
        elif m["how"] != "exact":
            problems.append(f"{FIELDS[f]} was guessed from “{m['header'] or 'values'}”"
                            + (f" ({m['why']})" if m.get("why") else "") + ".")
    others = [t for t in tables if not (continues(t, chosen) if pdf else t["id"] == chosen["id"])]
    if others and others[0]["score"] >= chosen["score"] - 1:
        problems.append(f"{others[0]['label']} could also be the parts list.")
    if "level" not in chosen["mapping"]:
        problems.append("No level column: the structure is taken from leading dots in the part numbers, if any.")
    return {"file": path.name, "tables": tables, "chosen": chosen, "problems": problems,
            "needs_review": any(not chosen["mapping"].get(f) or chosen["mapping"][f]["how"] != "exact" for f in REQUIRED)
                            or bool(others and others[0]["score"] >= chosen["score"] - 1),
            "layouts": sorted({t["signature"] for t in tables if t["id"] in chosen.get("parts", [chosen["id"]])})}


def load(path: str | Path, mapping: dict) -> list[dict]:
    """mapping = {"table": id | "pdf:*", "signature": ..., "columns": {field: header text}}. Headers are matched by
    text, so the same mapping works on every page of a PDF even when column positions shift."""
    path = Path(path)
    cols = {f: normalize_header_text(h) for f, h in (mapping.get("columns") or {}).items() if h is not None}
    if "part_number" not in cols:
        raise ValueError("the parts list mapping has no part-number column")
    want_table = mapping.get("table")
    records = []
    for t in _tables(path):
        if want_table and want_table != "pdf:*" and t["id"] != want_table:
            continue
        rows = t["rows"]
        hdr_i = None
        for i, row in enumerate(rows[:25]):
            norm = [normalize_header_text(c) for c in row]
            if cols["part_number"] in norm and sum(1 for h in cols.values() if h in norm) >= min(2, len(cols)):
                hdr_i = i
                break
        if hdr_i is None:
            continue
        norm = [normalize_header_text(c) for c in rows[hdr_i]]
        idx = {f: norm.index(h) for f, h in cols.items() if h in norm}
        for row in rows[hdr_i + 1:]:
            rec = {f: (row[i] if i < len(row) else "") for f, i in idx.items()}
            rec = {k: v for k, v in rec.items() if v not in (None, "")}
            if rec.get("part_number") and normalize_header_text(rec["part_number"]) != cols["part_number"]:
                records.append(rec)
        if want_table and want_table != "pdf:*":
            break
    if not records:
        raise ValueError("no parts were read with this mapping")
    return records


def mapping_from_proposal(chosen: dict) -> dict:
    return {"table": chosen["id"], "signature": chosen["signature"],
            "columns": {f: m["header"] for f, m in chosen["mapping"].items() if m.get("header")}}
