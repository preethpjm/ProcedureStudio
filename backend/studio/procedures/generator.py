"""Procedures (assembly / disassembly) from the process graph: text, requirements, checks.

Output is one *neutral procedure model* per kind (``studio.procedure/1``) that every
publisher renders (S1000D, ATA iSpec 2200, the editor JSON). Every step keeps:
``op_ids`` (the operations it words), ``occ_ids`` (3D) and ``rows`` (IPL), the STE / fact
check findings, and where its text came from (``template`` / ``llm`` / ``writer``).
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..llm.providers import LLMProvider, NoneProvider, refine_steps
from .ste import STEChecker, fact_check, technical_names_from
from .text import clean_name, plural, ref, sentences_for, supply_ref
from ..process.graph import wrench_for

SAFETY_RULES = [
    {"id": "wrn-hydraulic", "kind": "warning", "match": ("text", r"SKYDROL|PHOSPHATE ESTER|HYDRAULIC FLUID"),
     "where": "preliminary",
     "text": "HYDRAULIC FLUID (PHOSPHATE ESTER) IS DANGEROUS. WEAR GLOVES AND GOGGLES. IF THE FLUID GETS ON YOUR SKIN "
             "OR IN YOUR EYES, FLUSH IT AWAY WITH WATER AND GET MEDICAL AID."},
    {"id": "wrn-spring", "kind": "warning", "match": ("role", "spring"), "where": "spring",
     "text": "THE SPRING IS COMPRESSED. HOLD THE PARTS AND RELEASE THE SPRING SLOWLY. IF YOU DO NOT, THE PARTS CAN "
             "MOVE SUDDENLY AND CAUSE INJURY."},
    {"id": "cau-packing", "kind": "caution", "match": ("role", "seal"), "where": "seal_removal",
     "text": "DO NOT USE A SHARP TOOL TO REMOVE THE PACKINGS. A SCRATCH ON THE SEAL GROOVE CAN CAUSE A LEAK."},
]


def _safety(ipl: dict) -> list[dict]:
    rows = ipl["rows"]
    out = []
    for rule in SAFETY_RULES:
        kind, pat = rule["match"]
        if kind == "text":
            # MBOM comments and STEP descriptions often say "(POUR SKYDROL)" on the packings
            ev = [r for r in rows if re.search(pat, " ".join(str(r.get(k) or "") for k in
                                                         ("nomenclature_mbom", "mbom_comments", "step_name", "step_description")), re.I)]
            if not ev:
                continue
            evidence = [f"{r['part_number']}: {r.get('step_description') or r.get('mbom_comments') or r.get('nomenclature_mbom')}" for r in ev][:3]
        else:
            ev = [r for r in rows if r["role"] == pat]
            if not ev:
                continue
            evidence = [f"{r['part_number']} ({r['role']})" for r in ev][:3]
        out.append({**rule, "evidence": evidence, "source": f"rule:{rule['id']}"})
    return out


_MINOR = {"fastener", "washer", "nut", "seal", "backup_ring", "lockwire"}


def _title(st: dict, kind: str, ipl: dict, names: dict | None) -> str:
    if st.get("title") and st["id"] in ("st-d00", "st-a98", "st-a99") or "stay installed" in (st.get("title") or ""):
        return st["title"]
    rows = {r["id"]: r for r in ipl["rows"]}
    if st.get("kind") == "attaching" and st.get("rows") and st["rows"][0] in rows:
        a = rows[st["rows"][0]]
        n = clean_name({**a, "count": 1}, names)
        return f"Remove the attaching parts from the {n}" if kind == "disassembly" else f"Install the attaching parts on the {n}"
    if st.get("kind") in ("disassemble", "assemble") and st.get("assy_row") in rows:
        a = rows[st["assy_row"]]
        return f"{'Disassemble' if kind == 'disassembly' else 'Assemble'} the {clean_name({**a, 'count': 1}, names)}"
    mains = [rows[r] for r in st["rows"] if r in rows and rows[r]["role"] not in _MINOR] or \
            [rows[r] for r in st["rows"][:1] if r in rows]
    parts = []
    for r in mains:
        n = clean_name({**r, "count": r.get("units_per_assy")}, names)
        n = plural(n) if (r.get("units_per_assy") or 1) > 1 else n
        if f"the {n}" not in parts:
            parts.append(f"the {n}")
    verb = "Remove" if kind == "disassembly" else "Install"
    if not mains and st.get("title"):
        return st["title"]
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    if mains and all(m["role"] in _MINOR for m in mains):
        return f"{verb} the attaching parts"
    return f"{verb} {joined}"


def generate(process: dict, ipl: dict, kind: str, *, title: str, provider: LLMProvider | None = None,
             names: dict[str, str] | None = None, checker: STEChecker | None = None) -> dict:
    assert kind in ("assembly", "disassembly")
    provider = provider or NoneProvider()
    checker = checker or STEChecker(technical_names=technical_names_from(ipl, names))
    graph = process[kind]
    ops = {o["id"]: o for o in graph["operations"]}
    items_in_ipl = {r["item"] for r in ipl["rows"] if r.get("item")}
    safety = _safety(ipl)
    seal_rows = {r["id"] for r in ipl["rows"] if r["role"] in ("seal", "backup_ring")}
    spring_rows = {r["id"] for r in ipl["rows"] if r["role"] == "spring"}

    subtasks, all_steps = [], []
    for st in graph["subtasks"]:
        steps = []
        warn, caut = [], []
        for oid in st["ops"]:
            op = ops[oid]
            for k, sent in enumerate(sentences_for(op, names)):
                sid = f"{oid}-{k + 1}"
                step = {"id": sid, "text": sent["text"], "kind": sent["kind"], "op_ids": [oid],
                        "occ_ids": op.get("occ_ids", []), "rows": [i["row_id"] for i in op.get("items", [])],
                        "origin": "template", "flags": list(op.get("flags", []))}
                steps.append(step)
            rows_here = {i["row_id"] for i in op.get("items", [])} | {i["row_id"] for i in op.get("with", [])}
            for s in safety:
                if s["where"] == "spring" and rows_here & spring_rows and s["id"] not in warn:
                    warn.append(s["id"])
                if s["where"] == "seal_removal" and op["type"] in ("remove_from", "install_on") and rows_here & seal_rows \
                        and s["id"] not in caut:
                    caut.append(s["id"])
        subtasks.append({"id": st["id"], "title": _title(st, kind, ipl, names), "warnings": warn, "cautions": caut,
                         "steps": steps, "rows": st["rows"]})
        all_steps += steps

    seen_titles: dict[str, int] = {}
    for st in subtasks:
        t = st["title"]
        if t in seen_titles:
            st["title"] = t.replace("Remove the ", "Remove the remaining ").replace("Install the ", "Install the remaining ")
        seen_titles[t] = seen_titles.get(t, 0) + 1

    # optional wording improvement, guarded by the same checks
    usage = {}
    if provider.available() and all_steps:
        ctx = f"Procedure: {title}. Component: {(process.get('top') or {}).get('nomenclature', '')}."
        suggestions, usage = refine_steps(provider, [s for s in all_steps if s["kind"] == "step"], ctx,
                                         batch_size=getattr(provider, "batch_size", 12))
        for s in all_steps:
            new = suggestions.get(s["id"])
            if not new or new.strip() == s["text"]:
                continue
            op = ops[s["op_ids"][0]]
            if not [i for i in checker.check(new) + fact_check(new, op, items_in_ipl) if i["severity"] == "error"]:
                s["template_text"], s["text"], s["origin"] = s["text"], new.strip(), "llm"

    # checks
    counts = defaultdict(int)
    for s in all_steps:
        op = ops[s["op_ids"][0]]
        s["issues"] = checker.check(s["text"], s["kind"]) + fact_check(s["text"], op, items_in_ipl)
        for i in s["issues"]:
            counts[i["severity"]] += 1

    # preliminary requirements
    torques = [o["torque"] for o in graph["operations"] if o.get("torque")] if kind == "assembly" else []
    support = []
    for t in sorted(torques, key=lambda t: t["max_nm"]):
        name = wrench_for(t["max_nm"])
        if name not in [s["name"] for s in support]:
            support.append({"id": f"seq-{len(support) + 1:02d}", "name": name, "qty": 1, "source": "rule: torque range"})
    supplies, seen = [], set()
    for o in graph["operations"]:
        for s in o.get("supplies", []) or []:
            if s["spec"] not in seen and s["spec"] != "ATS":
                seen.add(s["spec"])
                supplies.append({"id": f"sup-{re.sub(r'[^a-z0-9]+', '-', s['spec'].lower())}", "spec": s["spec"],
                                 "name": supply_ref(s), "source": "drawing note"})
    general = process.get("general_notes", [])
    if kind == "assembly":
        for g in general:
            if g.get("op") != "apply_compound":
                continue
            for sp in g["specs"]:
                if sp not in seen:
                    seen.add(sp)
                    desc = next((x["description"] for x in ipl.get("supplies", []) if x["spec"] == sp), g["label"])
                    from ..process.graph import note_supply_name
                    nm = note_supply_name(g["text"], sp) or re.sub(r"(?i)\bprevent\.", "preventive", desc or "")
                    supplies.append({"id": f"sup-{re.sub(r'[^a-z0-9]+', '-', sp.lower())}", "spec": sp,
                                     "name": supply_ref({"spec": sp, "name": nm}), "source": "drawing note"})
    spares = []
    if kind == "assembly":
        for o in graph["operations"]:
            if o["type"] == "install_on" and o.get("new_items"):
                for it in o["items"]:
                    if it["row_id"] in o["new_items"]:
                        ex = next((s for s in spares if s["part_number"] == it["part_number"]), None)
                        if ex:
                            ex["qty"] += it.get("count") or 1
                        else:
                            spares.append({"id": f"spa-{re.sub(r'[^a-z0-9]+', '-', it['part_number'].lower())}",
                                           "part_number": it["part_number"], "name": clean_name(it, names),
                                           "item": it["item"], "qty": it.get("count") or 1})
    prelim_warn = [s for s in safety if s["where"] == "preliminary"]
    common = []
    if kind == "assembly":
        common = [f"{g['label']} ({', '.join(g['specs'])})." for g in general if g["specs"] and g.get("op") != "note"]
    proc = {
        "format": "studio.procedure/1", "kind": kind, "title": title,
        "component": process.get("top"),
        "common_info": common,
        "preliminary": {"persons": [{"category": "Shop technician", "count": 1}],
                        "support_equipment": support, "supplies": supplies, "spares": spares,
                        "warnings": [s["id"] for s in prelim_warn]},
        "safety": {s["id"]: {"kind": s["kind"], "text": s["text"], "source": s["source"], "evidence": s["evidence"]}
                   for s in safety},
        "subtasks": subtasks,
        "summary": {"subtasks": len(subtasks), "steps": len(all_steps), "errors": counts["error"],
                    "warnings": counts["warning"], "llm": provider.describe(), "usage": usage,
                    "llm_steps": sum(1 for s in all_steps if s["origin"] == "llm")},
    }
    return proc
