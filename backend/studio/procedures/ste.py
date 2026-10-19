"""ASD-STE100 checks and fact checks for generated procedure text.

Built-in rules (procedural writing) — each finding has a rule id, severity and a message:

* ``STE-4.1``  a procedural sentence has at most 20 words
* ``STE-8.1``  no semicolons
* ``STE-9.1``  an instruction starts with an approved verb in the imperative
* ``STE-MOD``  no modal verbs (should, could, may, might, would, shall)
* ``STE-ING``  no "-ing" verb forms (technical names excepted)
* ``STE-PAS``  no passive voice in instructions
* ``FACT-NUM`` every number in the text exists in the operation it came from (no invented values)
* ``FACT-ITM`` every item number in brackets exists in the IPL

Customer dictionaries plug in: ``STEChecker(extra_verbs=..., technical_names=...)``
(the same lists ASTHRA / TechPub keep per customer).
"""
from __future__ import annotations

import re

APPROVED_VERBS = {
    "adjust", "align", "apply", "attach", "bond", "cap", "clean", "close", "compress", "connect", "cover", "cut",
    "discard", "disconnect", "do", "drain", "dry", "engage", "examine", "fill", "hold", "identify", "install",
    "keep", "let", "loosen", "lubricate", "make", "measure", "monitor", "move", "open", "prepare", "pull", "push",
    "put", "record", "refer", "release", "remove", "replace", "safety", "set", "tighten", "torque", "turn", "use",
    "wipe", "after", "before", "if", "when", "for", "with", "note", "warning", "caution",
}
ING_OK = {"packing", "packings", "ring", "rings", "bushing", "bushings", "fitting", "fittings", "spring", "springs",
          "housing", "housings", "bearing", "bearings", "coating", "anodising", "anodizing", "o-ring", "back-up",
          "during", "thing", "things", "string", "following", "opening", "openings", "marking", "markings",
          "filling", "anything", "nothing", "something", "everything", "drawing", "drawings", "testing",
          # technical modifiers in part names ("retaining ring", "locking washer", "closing spacer")
          "retaining", "locking", "sealing", "mounting", "adjusting", "closing", "machining", "centering",
          "centring", "guiding", "damping", "backing", "bearing", "padding", "lining", "casting", "forging",
          "mooring", "tubing", "piping", "wiring", "shielding", "spacing", "fixing", "clamping", "coupling",
          "couplings", "landing", "steering", "folding", "reclining", "swivelling", "tracking", "stiffening"}


def technical_names_from(ipl: dict | None, names: dict | None = None) -> set[str]:
    """-ing words that are part of a part's name in this IPL ('recopy lever machining'): not verb forms."""
    out: set[str] = set()
    texts = [r.get("nomenclature") or "" for r in (ipl or {}).get("rows", [])] + list((names or {}).values())
    for t in texts:
        for w in re.findall(r"[A-Za-z][A-Za-z-]*ing\b", str(t)):
            out.add(w.lower())
    return out
MODALS = {"should", "could", "may", "might", "would", "shall", "must"}
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


class STEChecker:
    def __init__(self, extra_verbs: set[str] | None = None, technical_names: set[str] | None = None,
                 max_words: int = 20, max_words_descriptive: int = 25):
        self.verbs = APPROVED_VERBS | {v.lower() for v in (extra_verbs or set())}
        self.names = {n.lower() for n in (technical_names or set())}
        self.max_words = max_words
        self.max_words_descriptive = max_words_descriptive

    def check(self, text: str, kind: str = "step") -> list[dict]:
        issues = []
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
        for s in sentences:
            core = re.sub(r"\([^)]*\)", "", s)                       # item refs and specs do not count as words
            words = re.findall(r"[A-Za-z][A-Za-z\-']*", core)
            if kind == "step" and len(words) > self.max_words:
                issues.append({"rule": "STE-4.1", "severity": "error",
                               "message": f"{len(words)} words; a procedural sentence has at most {self.max_words}."})
            elif kind != "step" and len(words) > self.max_words_descriptive:
                issues.append({"rule": "STE-4.2", "severity": "error",
                               "message": f"{len(words)} words; a descriptive sentence has at most {self.max_words_descriptive}."})
            if ";" in s:
                issues.append({"rule": "STE-8.1", "severity": "error", "message": "Do not use semicolons."})
            if kind == "step" and words:
                first = words[0].lower()
                if first not in self.verbs and not s.lower().startswith(("do not", "make sure")):
                    issues.append({"rule": "STE-9.1", "severity": "warning",
                                   "message": f"Start an instruction with an approved verb (found '{words[0]}')."})
            for w in words:
                lw = w.lower()
                if lw in MODALS:
                    issues.append({"rule": "STE-MOD", "severity": "error", "message": f"Do not use '{w}'."})
                if lw.endswith("ing") and len(lw) > 4 and lw not in ING_OK and lw not in self.names:
                    issues.append({"rule": "STE-ING", "severity": "warning",
                                   "message": f"Avoid the -ing form '{w}' unless it is a technical name."})
            if kind == "step" and re.search(r"\b(is|are|be|been)\s+\w+ed\b", core, re.I):
                issues.append({"rule": "STE-PAS", "severity": "warning", "message": "Use the active voice in instructions."})
        return issues


def _numbers(text: str) -> list[str]:
    t = re.sub(r"\b[A-Z]{2,}[A-Z0-9]*-[A-Z0-9\-]+\b", " ", text)       # spec codes like PCS-7200, IFC30-145-07
    t = re.sub(r"\b[A-Z]+\d+[A-Z0-9\-]*\b", " ", t)
    return re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?[A-Z]?", t)


def allowed_numbers(op: dict) -> set[str]:
    nums: set[str] = set()

    def add(v):
        if v is None:
            return
        if isinstance(v, (int, float)):
            s = f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)
            nums.add(s)
        else:
            nums.add(str(v))
    for key in ("items", "with", "on_items"):
        for it in op.get(key) or []:
            add(it.get("item"))
            add(it.get("count"))
            add(it.get("qty"))
    for key in ("from", "on"):
        if op.get(key):
            add(op[key].get("item"))
    if op.get("torque"):
        for k in ("min_nm", "max_nm", "min_lbfin", "max_lbfin", "nominal_nm"):
            add(float(op["torque"][k]))
    for key in ("supplies", "references"):
        for sp in op.get(key) or []:
            for n in re.findall(r"\d+(?:\.\d+)?", f"{sp.get('name', '')} {sp.get('description', '')} {sp.get('spec', '')}"):
                nums.add(n)
    return nums


def fact_check(text: str, op: dict, ipl_items: set[str]) -> list[dict]:
    issues = []
    allowed = allowed_numbers(op)
    for n in _numbers(text):
        if n not in allowed and n.rstrip("0").rstrip(".") not in allowed:
            issues.append({"rule": "FACT-NUM", "severity": "error",
                           "message": f"'{n}' is not a value of this operation (item, quantity or torque)."})
    refs = [x.strip() for grp in re.findall(r"\((-?\d+[A-Z]?(?:,\s*-?\d+[A-Z]?)*)\)", text) for x in grp.split(",")]
    for it in refs:
        if it not in ipl_items:
            issues.append({"rule": "FACT-ITM", "severity": "error", "message": f"Item ({it}) is not in the IPL."})
    for w, v in NUMBER_WORDS.items():
        if re.search(rf"\b{w}\b", text, re.I) and str(v) not in allowed:
            issues.append({"rule": "FACT-NUM", "severity": "error",
                           "message": f"Quantity '{w}' does not match this operation."})
    return issues
