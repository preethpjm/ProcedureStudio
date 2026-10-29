"""Part roles: what kind of thing a part is, for IPL rules, sequencing and wording.

A role drives three things:

* the IPL (attaching parts are listed after the part they attach; permanent items are
  listed but never removed in disassembly);
* the sequence planner (permanent parts ride with their carrier; seals ride with the
  part they sit on);
* the procedure wording ("Remove and discard the packings", "Do not remove the inserts").

Rules are data (ordered regular expressions over part number and nomenclature, then
geometry). Projects can extend them in ``studio.yaml`` (``roles.extra_rules``) without
code changes. The first matching rule wins; every result records which rule decided it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ROLES = {
    "fastener":   {"label": "Fastener", "attaching": True,  "permanent": False, "consumable_on_removal": False},
    "washer":     {"label": "Washer",   "attaching": True,  "permanent": False, "consumable_on_removal": False},
    "nut":        {"label": "Nut",      "attaching": True,  "permanent": False, "consumable_on_removal": False},
    "lockwire":   {"label": "Lock wire / safety cable", "attaching": True, "permanent": False, "consumable_on_removal": True},
    "seal":       {"label": "Seal / packing", "attaching": False, "permanent": False, "consumable_on_removal": True},
    "backup_ring": {"label": "Back-up ring", "attaching": False, "permanent": False, "consumable_on_removal": True},
    "insert":     {"label": "Thread insert", "attaching": False, "permanent": True,  "consumable_on_removal": False},
    "press_plug": {"label": "Press-fit plug (Lee plug)", "attaching": False, "permanent": True, "consumable_on_removal": False},
    "dowel":      {"label": "Dowel / locating pin (stays in its bore)", "attaching": False, "permanent": True, "consumable_on_removal": False},
    "plate":      {"label": "Bonded plate / label", "attaching": False, "permanent": True, "consumable_on_removal": False},
    "fitting":    {"label": "Fitting / union", "attaching": False, "permanent": False, "consumable_on_removal": False},
    "cap":        {"label": "Protective / storage cap", "attaching": False, "permanent": False, "consumable_on_removal": False},
    "spring":     {"label": "Spring", "attaching": False, "permanent": False, "consumable_on_removal": False},
    "bush":       {"label": "Bush / bushing", "attaching": False, "permanent": False, "consumable_on_removal": False},
    "closure":    {"label": "Threaded plug / cover", "attaching": False, "permanent": False, "consumable_on_removal": False},
    "moving":     {"label": "Moving part (piston, valve, spool)", "attaching": False, "permanent": False, "consumable_on_removal": False},
    "housing":    {"label": "Body / housing", "attaching": False, "permanent": False, "consumable_on_removal": False},
    "lru":        {"label": "Sub-assembly / LRU", "attaching": False, "permanent": False, "consumable_on_removal": False},
    "marker":     {"label": "Reference marker (no physical part)", "attaching": False, "permanent": True, "consumable_on_removal": False},
    "process":    {"label": "Process / material specification", "attaching": False, "permanent": True, "consumable_on_removal": False},
    "part":       {"label": "Part", "attaching": False, "permanent": False, "consumable_on_removal": False},
}

# (role, field, pattern) — field: "pn" (normalised part number) or "name" (nomenclature, upper case)
DEFAULT_RULES: list[tuple[str, str, str]] = [
    ("process", "pn", r"^(NCT|IFC|PCS-|DRSE)"),
    ("marker", "name", r"\b(CENTER|CENTRE) OF GRAVITY\b|\bCDG\b|\bSKELETON\b|\bENVELOPE\b"),
    ("marker", "pn", r"-CDG$"),
    ("lockwire", "name", r"\bLOCK ?WIRE\b|\bSAFETY (CABLE|WIRE)\b|\bFIL FREIN\b|\bCABLE\b|\bFERRULE\b"),
    ("backup_ring", "name", r"\bBACK[- ]?UP RING\b|\bANTI[- ]?EXTRUSION\b|\bCONTRE[- ]?BAGUE\b"),
    ("seal", "name", r"\bO[- ]?RING\b|\bPACKING\b|\bSEAL\b|\bJOINT\b|\bGASKET\b"),
    ("seal", "pn", r"^(M83461|M83248|M25988|MS28775|MS29513|NAS1611|NAS1612|AS568)"),
    ("backup_ring", "pn", r"^(M8791|MS28774|MS28782|NSA8671)"),
    ("insert", "name", r"\bINSERT\b|\bHELICOIL\b|\bHELI-?COIL\b|\bKEENSERT\b"),
    ("insert", "pn", r"^(MS21209|NAS1130|MS124\d{3}|2TLF|\d{11}$)"),
    ("press_plug", "name", r"\bLEE PLUG\b|\bEXPANDER\b|\bBETA PLUG\b"),
    ("press_plug", "pn", r"^PLG[A-Z]?\d"),
    ("dowel", "name", r"\bDOWEL\b|\bLOCATING PIN\b|\bPIED DE CENTRAGE\b"),
    ("plate", "name", r"\bPLATE\b.*\b(IDENT|AMEND|NAME|DATA)|\b(IDENT|AMEND|NAME|DATA)\w*\s+PLATE\b|\bLABEL\b|\bPLAQU"),
    ("cap", "name", r"\bPROTECTIVE CAP\b|\bSTORAGE PLUG\b|\bDUST CAP\b|\bBOUCHON\b"),
    ("cap", "pn", r"^(MS21914|MS21913)"),
    ("washer", "name", r"\bWASHER\b|\bRONDELLE\b"),
    ("washer", "pn", r"^(NAS1149|AN960|MS15795|NAS620)"),
    ("nut", "name", r"\bNUT\b|\bECROU\b"),
    ("fastener", "name", r"\bSCREW\b|\bBOLT\b|\bVIS\b|\bSTUD\b|\bRIVET\b"),
    ("fastener", "pn", r"^(NAS13\d\d|NAS6\d\d|MS2\d{4}|AN[34]\d|C77\d{3})"),
    ("fitting", "name", r"\bUNION\b|\bFITTING\b|\bADAPTER\b|\bNIPPLE\b|\bROSAN\b|\bRACCORD\b"),
    ("fitting", "pn", r"^AS4099"),
    ("spring", "name", r"\bSPRING\b|\bRESSORT\b"),
    ("closure", "name", r"\bPLUG\b|\bCOVER\b|\bBLANKING\b"),
    ("bush", "name", r"\bBUSH(ING)?\b|\bBAGUE\b|\bSLEEVE\b"),
    ("lru", "name", r"\bSOLENOID\b|\bACTUATOR\b|\bMOTOR\b|\bSENSOR\b|\bTRANSDUCER\b|\bSWITCH\b|\bSERVO"),
    ("moving", "name", r"\bPISTON\b|\bVALVE\b|\bSPOOL\b|\bPOPPET\b|\bPLUNGER\b|\bSLIDE\b"),
    ("housing", "name", r"\bBODY\b|\bHOUSING\b|\bMANIFOLD\b|\bCASE\b|\bCARTER\b|\bCORPS\b"),
]


@dataclass
class RoleDecision:
    role: str
    rule: str
    confidence: float

    def as_dict(self) -> dict:
        return {"role": self.role, "rule": self.rule, "confidence": self.confidence, **ROLES[self.role]}


def normalise_pn(pn: str, manuf_prefix: str | None = None) -> str:
    p = (pn or "").strip().upper().lstrip(".")
    if manuf_prefix and p.startswith(manuf_prefix.upper() + "-"):
        p = p[len(manuf_prefix) + 1:]
    return p


def _thin_ring(geometry: dict | None) -> bool:
    """Round and flat: axial length under 15 % of the diameter (a back-up ring, not a bush or a sleeve)."""
    b = (geometry or {}).get("bbox")
    if not b:
        return False
    dims = sorted([b[3] - b[0], b[4] - b[1], b[5] - b[2]], reverse=True)
    return dims[0] > 0 and dims[1] / dims[0] > 0.95 and dims[2] / dims[0] < 0.15


class RoleClassifier:
    def __init__(self, extra_rules: list[tuple[str, str, str]] | None = None):
        rules = list(extra_rules or []) + DEFAULT_RULES
        self.rules = [(role, fld, re.compile(pat, re.I)) for role, fld, pat in rules if role in ROLES]

    def classify(self, part_number: str, names: list[str], geometry: dict | None = None,
                 is_assembly: bool = False, manuf_prefix: str | None = None) -> RoleDecision:
        pn = normalise_pn(part_number, manuf_prefix)
        texts = [n.upper() for n in names if n]
        for role, fld, rx in self.rules:
            if fld == "pn" and rx.search(pn):
                return RoleDecision(role, f"part number /{rx.pattern}/", 0.9)
            if fld == "name" and any(rx.search(t) for t in texts):
                if role == "bush" and _thin_ring(geometry):
                    # "BAGUE" / "RING" that is a flat ring, not a sleeve: a back-up ring next to a packing
                    return RoleDecision("backup_ring", f"nomenclature /{rx.pattern}/ + geometry: thin flat ring", 0.7)
                return RoleDecision(role, f"nomenclature /{rx.pattern}/", 0.85)
        if geometry is not None and not geometry.get("bbox"):
            return RoleDecision("marker", "no geometry", 0.7)
        if is_assembly:
            return RoleDecision("lru", "STEP sub-assembly", 0.6)
        if geometry and geometry.get("bbox"):
            b = geometry["bbox"]
            dims = sorted([b[3] - b[0], b[4] - b[1], b[5] - b[2]], reverse=True)
            cyl = geometry.get("cylinders") or []
            if dims[0] / max(dims[2], 1e-6) > 2.5 and cyl and max(c["faces"] for c in cyl) >= 2 and dims[1] < 15:
                return RoleDecision("fastener", "geometry: slender, cylindrical", 0.4)
        return RoleDecision("part", "default", 0.3)
