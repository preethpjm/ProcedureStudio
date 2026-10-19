"""Classify a drawing note into an operation the procedure engine knows.

Rules are data: ordered (operation, keywords, target roles, phase). The first rule whose
keywords all appear wins. Projects extend ``NOTE_RULES`` in ``studio.yaml``.

phase: ``install`` (done while installing the target parts), ``after_install``
(after the target parts are installed), ``after_test``, ``final`` (closing requirement),
``general`` (preliminary requirement / note for the whole task).
"""
from __future__ import annotations

NOTE_RULES: list[dict] = [
    {"op": "apply_sealant", "all": ["SEALANT"], "any": ["AFTER TEST"], "targets": ["press_plug"], "phase": "after_test",
     "label": "Fill the Lee plug implantation with sealant after the test"},
    {"op": "apply_conductive_compound", "all": ["SCREW", "WASHER"], "any": ["CONDUCTIVE"], "targets": ["fastener", "washer"],
     "phase": "install", "label": "Install the screws and washers with conductive compound"},
    {"op": "apply_sealant", "all": ["SEALANT"], "any": ["SCREW HEAD", "WASHER"], "targets": ["fastener", "washer"],
     "phase": "after_install", "label": "Apply sealant on the screw heads and washers"},
    {"op": "lockwire", "all": [], "any": ["LOCK WIRE", "SAFETY CABLE", "LOCKWIRE"], "targets": ["fastener", "closure", "fitting"],
     "phase": "after_install", "label": "Safety with lock wire or safety cable"},
    {"op": "apply_compound", "all": ["COMPOUND"], "any": ["NOT IN CONTACT", "ALL PARTS"], "targets": [],
     "phase": "general", "label": "Apply corrosion-preventive compound to parts not in contact with the operating fluid"},
    {"op": "install_fittings", "all": ["UNION"], "any": ["INSTALLATION"], "targets": ["fitting"], "phase": "install",
     "label": "Install the hydraulic unions per the specification"},
    {"op": "bond_label", "all": [], "any": ["LABEL", "PLATE"], "targets": ["plate"], "phase": "install",
     "label": "Bond the identification label"},
    {"op": "test", "all": ["TEST"], "any": ["ATS", "ACCORDING"], "targets": [], "phase": "final",
     "label": "Do the acceptance test"},
]


def classify_note(text: str) -> dict:
    up = " " + " ".join(text.upper().split()) + " "
    for r in NOTE_RULES:
        if all(k in up for k in r["all"]) and (not r["any"] or any(k in up for k in r["any"])):
            return {"op": r["op"], "targets": r["targets"], "phase": r["phase"], "label": r["label"]}
    return {"op": "note", "targets": [], "phase": "general", "label": "General note"}
