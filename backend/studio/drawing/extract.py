"""Facts from an engineering drawing PDF: torques, callouts and process notes.

Vector PDFs exported from CATIA keep their text, so no OCR or LLM is needed for the facts
that matter most to procedures:

* **Torques** — ``C.S. : 10,5N.m±0,5`` / ``T.T. : 8,5N.m±0,5`` / ``C.S./T.T.: 24±3,7N.m``
  (C.S. = couple de serrage, T.T. = tightening torque; both forms are read, French decimal
  commas included). A torque is attached to the part in the callout stack printed just
  above it (``(x3) C77004-10`` / ``(x3) NAS1149C0432R``), preferring the fastener of the
  stack. A torque with no readable callout is kept as *unassigned*, with candidates.
* **Callouts** — part numbers printed on the sheet, with ``(xN)`` quantities.
* **Notes** — numbered/dashed general notes that cite a process specification
  (IFC…, NCT…, PCS-…, DRSE…). French and English halves of bilingual notes are split, and
  the English text is kept. Each note is classified by ``notes.classify_note`` into an
  operation the procedure engine knows (compound, sealant, lock wire, test, …).

Every fact carries page, position and the raw text, so the UI can show the evidence.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .notes import classify_note

_NUM = r"\d+(?:[.,]\d+)?"
TORQUE_RX = re.compile(
    rf"(?P<nom>{_NUM})\s*(?:±\s*(?P<tol1>{_NUM}))?\s*(?:(?<=\d)\s+(?P<tolsp>{_NUM}))?\s*N\.?\s?m(?:\s*(?:±\s*)?(?P<tol2>{_NUM}))?",
    re.I)
TORQUE_KEY = re.compile(r"\b(C\.S\.|T\.T\.|COUPLE|TORQUE)", re.I)
QTY_RX = re.compile(r"\(\s*x\s*(\d+)\s*\)", re.I)
SPEC_RX = re.compile(r"\b(IFC\d{2}-\d{3}-\d{2}|NCT\d{2}-\d{3}-\d{2}|PCS-?\d{3,5}|DRSE\d{4,}|ATS)\b")


def _f(s: str | None) -> float | None:
    return float(s.replace(",", ".")) if s else None


def _norm(pn: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (pn or "").upper())


@dataclass
class Run:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    page: int

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Torque:
    nominal_nm: float
    tolerance_nm: float | None
    min_nm: float | None
    max_nm: float | None
    text: str
    page: int
    bbox: list[float]
    target_pn: str | None = None
    stack_pns: list[str] = field(default_factory=list)
    qty_hint: int | None = None
    confidence: float = 0.0
    method: str = ""
    candidates: list[str] = field(default_factory=list)


@dataclass
class Callout:
    pn: str
    qty: int | None
    page: int
    bbox: list[float]
    text: str


def _runs(page, pno: int, gap: float = 22.0) -> list[Run]:
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    words.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
    runs: list[Run] = []
    for w in words:
        if runs:
            r = runs[-1]
            if abs(w["top"] - r.top) <= 3 and 0 <= w["x0"] - r.x1 <= gap:
                r.text += " " + w["text"]
                r.x1, r.bottom = w["x1"], max(r.bottom, w["bottom"])
                continue
        runs.append(Run(w["text"], w["x0"], w["x1"], w["top"], w["bottom"], pno))
    return runs


def _parse_torque(text: str) -> tuple[float, float | None] | None:
    m = TORQUE_RX.search(text)
    if not m:
        return None
    nom = _f(m.group("nom"))
    tol = _f(m.group("tol1") or m.group("tol2") or m.group("tolsp"))
    if nom is None or nom <= 0 or nom > 5000:
        return None
    return nom, tol


def extract_drawing(pdf_path: str | Path, known_pns: list[str] | None = None,
                    roles_by_pn: dict[str, str] | None = None) -> dict:
    """known_pns: MBOM part numbers (any form). roles_by_pn: normalised PN -> role."""
    import pdfplumber
    known = {_norm(p): p.lstrip(".") for p in (known_pns or []) if len(_norm(p)) >= 4}
    roles_by_pn = roles_by_pn or {}
    torques: list[Torque] = []
    callouts: list[Callout] = []
    notes: list[dict] = []
    pages_text = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        meta = {"pages": len(pdf.pages), "size_pt": [pdf.pages[0].width, pdf.pages[0].height] if pdf.pages else None}
        for pno, page in enumerate(pdf.pages, start=1):
            runs = _runs(page, pno)
            # callouts: runs containing a known part number
            for r in runs:
                for tok in re.findall(r"[A-Z0-9][A-Z0-9\-/]{3,}", r.text.upper()):
                    k = _norm(tok)
                    if k in known:
                        q = QTY_RX.search(r.text)
                        callouts.append(Callout(known[k], int(q.group(1)) if q else None, pno,
                                                [r.x0, r.top, r.x1, r.bottom], r.text))
            # torques: a key run (C.S. / T.T.) plus the value on the same line
            used = set()
            for i, r in enumerate(runs):
                if i in used or not TORQUE_KEY.search(r.text):
                    continue
                line = [r] + [s for j, s in enumerate(runs) if j != i and abs(s.top - r.top) <= 3
                              and 0 <= s.x0 - r.x1 <= 140]
                text = " ".join(s.text for s in sorted(line, key=lambda s: s.x0))
                tv = _parse_torque(text)
                if not tv:
                    continue
                for j, s in enumerate(runs):
                    if s in line:
                        used.add(j)
                x0, x1 = min(s.x0 for s in line), max(s.x1 for s in line)
                top, bottom = min(s.top for s in line), max(s.bottom for s in line)
                nom, tol = tv
                torques.append(Torque(nom, tol, round(nom - tol, 3) if tol else None,
                                      round(nom + tol, 3) if tol else None, text, pno, [x0, top, x1, bottom]))
            pages_text.append(page.extract_text() or "")

        # merge C.S. and T.T. lines of the same block (same value, stacked within 25 pt)
        merged: list[Torque] = []
        for t in sorted(torques, key=lambda t: (t.page, t.bbox[0], t.bbox[1])):
            m = next((u for u in merged if u.page == t.page and u.nominal_nm == t.nominal_nm
                      and abs(u.bbox[0] - t.bbox[0]) < 40 and 0 <= t.bbox[1] - u.bbox[3] < 25), None)
            if m:
                m.text += " / " + t.text
                m.bbox[3] = t.bbox[3]
                m.tolerance_nm = m.tolerance_nm or t.tolerance_nm
            else:
                merged.append(t)
        torques = merged

        # attach each torque to the callout stack directly above it
        role_rank = {"fastener": 0, "nut": 0, "fitting": 1, "closure": 2, "cap": 3}
        for t in torques:
            tx, ty = (t.bbox[0] + t.bbox[2]) / 2, t.bbox[1]
            above = [c for c in callouts if c.page == t.page and 0 < ty - c.bbox[3] < 95
                     and abs((c.bbox[0] + c.bbox[2]) / 2 - tx) < 140]
            above.sort(key=lambda c: ty - c.bbox[3])
            stack, last_top = [], ty
            for c in above:                       # contiguous lines only (stop at a gap)
                if last_top - c.bbox[3] > 30 and stack:
                    break
                stack.append(c)
                last_top = c.bbox[1]
            if stack:
                t.stack_pns = [c.pn for c in stack]
                if not any(roles_by_pn.get(_norm(c.pn)) in role_rank for c in stack):
                    t.confidence = 0.4
                best = min(stack, key=lambda c: (role_rank.get(roles_by_pn.get(_norm(c.pn), ""), 9),
                                                 ty - c.bbox[3]))
                t.target_pn, t.qty_hint = best.pn, best.qty
                t.method = "callout stack above the torque"
                t.confidence = 0.9 if roles_by_pn.get(_norm(best.pn)) in role_rank else 0.7
            else:
                near = sorted((c for c in callouts if c.page == t.page),
                              key=lambda c: ((c.bbox[0] + c.bbox[2]) / 2 - tx) ** 2 + (c.bbox[1] - ty) ** 2)
                if near and ((near[0].bbox[0] - tx) ** 2 + (near[0].bbox[1] - ty) ** 2) ** 0.5 < 160:
                    t.target_pn, t.method, t.confidence = near[0].pn, "nearest callout", 0.5
                else:
                    t.method = "unassigned: no readable callout near the torque"
            # a bare "(xN)" just above an unassigned torque is a quantity hint
            if not t.qty_hint:
                pass

    # notes that cite a specification (English half kept)
    full = "\n".join(pages_text)
    seen = set()
    for raw in re.split(r"\n", full):
        for seg in re.split(r"\s-\s|^-\s", raw):
            seg = seg.strip(" -")
            if not seg or not SPEC_RX.search(seg) and "TEST" not in seg.upper():
                continue
            up = seg.upper()
            french = sum(w in up for w in (" SUIVANT", " DES ", " LES ", " SUR ", " DU ", " AU ", " ET ", "ESSAI", " DE "))
            english = sum(w in up for w in ("ACCORDING", " THE ", " WITH ", " ON ", " AND ", "TEST", "AFTER", "INSTALLATION"))
            if french > english:
                continue
            key = up[:60]
            if key in seen:
                continue
            seen.add(key)
            notes.append({"text": seg, "specs": sorted(set(SPEC_RX.findall(seg))), **classify_note(seg)})

    return {
        "format": "studio.drawing-facts/1",
        "source": Path(pdf_path).name,
        "meta": meta,
        "torques": [asdict(t) for t in torques],
        "callouts": [asdict(c) for c in callouts],
        "notes": notes,
    }


def suggest_unassigned(facts: dict, fasteners_by_qty: dict[int, list[str]], assigned: set[str]) -> None:
    """For torques with no callout, suggest fastener groups whose quantity matches a nearby (xN)."""
    for t in facts["torques"]:
        if t["target_pn"]:
            continue
        cands = []
        for q, pns in fasteners_by_qty.items():
            if t.get("qty_hint") in (None, q):
                cands += [p for p in pns if p not in assigned]
        t["candidates"] = sorted(set(cands))
