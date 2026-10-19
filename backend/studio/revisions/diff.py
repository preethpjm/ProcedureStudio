"""Revision engine: compare a new run against a signed-off baseline.

Three levels, each traced to the next:

1. **3D** — occurrences matched by ``occ_id`` (part number + instance), then by part number
   and nearest position. Reports added / removed parts, part-number changes at the same
   position, geometry changes (volume, area, size) and moves.
2. **IPL** — rows matched by part number: added / removed, quantity, nomenclature, torque,
   effectivity, attaching relation and item number changes.
3. **Impact** — for every change, the baseline operations and procedure steps that use the
   changed parts (``occ_ids`` / IPL rows on each step). Those are regenerated; every other
   step keeps its approved text word for word (``carry_forward``).

``modification_procedure`` builds the delta procedure for units already in service
(configuration A -> B): remove the access path and the changed parts with the baseline
disassembly, install the new configuration with the new assembly, then inspect.
"""
from __future__ import annotations

import re
from collections import defaultdict

import numpy as np


def _key(pn: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (pn or "").upper())


def _centroid(o: dict) -> np.ndarray | None:
    g = o.get("geometry") or {}
    if g.get("centroid"):
        return np.array(g["centroid"], float)
    if g.get("bbox"):
        b = np.array(g["bbox"], float)
        return (b[:3] + b[3:]) / 2
    return None


def _dims(o: dict) -> list[float] | None:
    b = (o.get("geometry") or {}).get("bbox")
    if not b:
        return None
    return sorted([b[3] - b[0], b[4] - b[1], b[5] - b[2]], reverse=True)


def diff_models(base: dict, new: dict, move_mm: float = 0.2, geom_rel: float = 0.01) -> list[dict]:
    bparts = {o["occ_id"]: o for o in base["occurrences"] if not o["is_assembly"]}
    nparts = {o["occ_id"]: o for o in new["occurrences"] if not o["is_assembly"]}
    changes: list[dict] = []
    matched_b, matched_n = set(), set()
    pairs = []
    for oid in sorted(set(bparts) & set(nparts)):
        pairs.append((bparts[oid], nparts[oid]))
        matched_b.add(oid)
        matched_n.add(oid)
    # same part number, different instance id: nearest position
    by_pn_new = defaultdict(list)
    for o in nparts.values():
        if o["occ_id"] not in matched_n:
            by_pn_new[_key(o["part_number"])].append(o)
    for b in [o for o in bparts.values() if o["occ_id"] not in matched_b]:
        cands = by_pn_new.get(_key(b["part_number"]), [])
        cb = _centroid(b)
        if cands and cb is not None:
            best = min(cands, key=lambda n: float(np.linalg.norm((_centroid(n) if _centroid(n) is not None else cb + 1e9) - cb)))
            cands.remove(best)
            pairs.append((b, best))
            matched_b.add(b["occ_id"])
            matched_n.add(best["occ_id"])
    # different part number at the same position and similar size: a part number change
    rest_b = [o for o in bparts.values() if o["occ_id"] not in matched_b]
    rest_n = [o for o in nparts.values() if o["occ_id"] not in matched_n]
    for b in list(rest_b):
        cb, db = _centroid(b), _dims(b)
        if cb is None:
            continue
        near = [n for n in rest_n if _centroid(n) is not None and np.linalg.norm(_centroid(n) - cb) < 1.0]
        if not near:
            continue
        n = min(near, key=lambda n: float(np.linalg.norm(_centroid(n) - cb)))
        dn = _dims(n)
        same_size = db and dn and all(abs(x - y) <= max(0.05, geom_rel * max(x, 1)) for x, y in zip(db, dn))
        changes.append({"type": "part_number_changed", "occ_ids_base": [b["occ_id"]], "occ_ids_new": [n["occ_id"]],
                        "part_number_base": b["part_number"], "part_number_new": n["part_number"],
                        "name": n["name"], "geometry": "same" if same_size else "different",
                        "message": f"{b['part_number']} -> {n['part_number']} at the same position"
                                   + ("" if same_size else " (geometry differs too)")})
        rest_b.remove(b)
        rest_n.remove(n)
    for b, n in pairs:
        cb, cn = _centroid(b), _centroid(n)
        if cb is not None and cn is not None and np.linalg.norm(cb - cn) > move_mm:
            changes.append({"type": "moved", "occ_ids_base": [b["occ_id"]], "occ_ids_new": [n["occ_id"]],
                            "part_number_base": b["part_number"], "part_number_new": n["part_number"], "name": n["name"],
                            "distance_mm": round(float(np.linalg.norm(cb - cn)), 3),
                            "message": f"{n['part_number']} moved {np.linalg.norm(cb - cn):.2f} mm"})
        gb, gn = b.get("geometry") or {}, n.get("geometry") or {}
        vb, vn = gb.get("volume_mm3"), gn.get("volume_mm3")
        if vb and vn and abs(vb - vn) > geom_rel * max(vb, 1):
            changes.append({"type": "geometry_changed", "occ_ids_base": [b["occ_id"]], "occ_ids_new": [n["occ_id"]],
                            "part_number_base": b["part_number"], "part_number_new": n["part_number"], "name": n["name"],
                            "volume_base": vb, "volume_new": vn,
                            "message": f"{n['part_number']} geometry changed (volume {vb:.1f} -> {vn:.1f} mm3) "
                                       f"with the same part number: check whether a new part number is needed"})
    for b in rest_b:
        changes.append({"type": "removed", "occ_ids_base": [b["occ_id"]], "occ_ids_new": [], "part_number_base": b["part_number"],
                        "part_number_new": None, "name": b["name"], "message": f"{b['part_number']} is no longer in the 3D"})
    for n in rest_n:
        changes.append({"type": "added", "occ_ids_base": [], "occ_ids_new": [n["occ_id"]], "part_number_base": None,
                        "part_number_new": n["part_number"], "name": n["name"], "message": f"{n['part_number']} is new in the 3D"})
    # group identical changes of the same part number (6 washers -> one change)
    grouped: dict[tuple, dict] = {}
    for c in changes:
        k = (c["type"], c.get("part_number_base"), c.get("part_number_new"))
        if k in grouped:
            g = grouped[k]
            g["occ_ids_base"] += c["occ_ids_base"]
            g["occ_ids_new"] += c["occ_ids_new"]
            g["count"] += 1
        else:
            grouped[k] = {**c, "count": 1, "source": "3D"}
    for g in grouped.values():
        if g["count"] > 1:
            g["message"] = f"{g['message']} (x{g['count']})"
    return list(grouped.values())


def diff_ipl(base: dict, new: dict) -> list[dict]:
    def index(ipl):
        out = {}
        for r in ipl["rows"]:
            if r.get("status") != "rejected":
                out.setdefault(_key(r["part_number"]), r)
        return out
    b, n = index(base), index(new)
    changes = []
    for k in sorted(set(b) | set(n)):
        rb, rn = b.get(k), n.get(k)
        if rb and not rn:
            changes.append({"type": "ipl_removed", "part_number": rb["part_number"], "rows_base": [rb["id"]], "rows_new": [],
                            "nomenclature": rb.get("nomenclature"), "role": rb.get("role"),
                            "message": f"{rb['part_number']} ({rb['nomenclature']}) removed from the IPL"})
            continue
        if rn and not rb:
            changes.append({"type": "ipl_added", "part_number": rn["part_number"], "rows_base": [], "rows_new": [rn["id"]],
                            "nomenclature": rn.get("nomenclature"), "role": rn.get("role"),
                            "message": f"{rn['part_number']} ({rn['nomenclature']}) added to the IPL"})
            continue
        diffs = []
        for f, label in (("units_per_assy", "quantity"), ("nomenclature", "nomenclature"), ("effectivity", "effectivity"),
                         ("item", "item number")):
            if (rb.get(f) or None) != (rn.get(f) or None):
                diffs.append({"field": label, "base": rb.get(f), "new": rn.get(f)})
        tb = (rb.get("torque") or {}).get("nominal_nm")
        tn = (rn.get("torque") or {}).get("nominal_nm")
        if tb != tn:
            diffs.append({"field": "torque", "base": rb.get("torque"), "new": rn.get("torque")})
        ab = b.get(_key(next((x["part_number"] for x in base["rows"] if x["id"] == rb.get("attaching_for")), "")), {}) if rb.get("attaching_for") else None
        an = n.get(_key(next((x["part_number"] for x in new["rows"] if x["id"] == rn.get("attaching_for")), "")), {}) if rn.get("attaching_for") else None
        if (ab or {}).get("part_number") != (an or {}).get("part_number"):
            diffs.append({"field": "attaches", "base": (ab or {}).get("part_number"), "new": (an or {}).get("part_number")})
        if diffs:
            changes.append({"type": "ipl_changed", "part_number": rn["part_number"], "rows_base": [rb["id"]], "rows_new": [rn["id"]],
                            "fields": diffs,
                            "message": f"{rn['part_number']}: " + ", ".join(
                                f"{d['field']} {_fmt(d['base'])} -> {_fmt(d['new'])}" for d in diffs)})
    return changes


def _fmt(v):
    if isinstance(v, dict) and "nominal_nm" in v:
        return f"{v['nominal_nm']:g} N.m"
    return "—" if v in (None, "") else str(v)


def impact(changes: list[dict], base_ipl: dict, base_procs: dict[str, dict]) -> dict:
    """Which baseline steps each change touches."""
    rows_by_pn = defaultdict(list)
    for r in base_ipl["rows"]:
        rows_by_pn[_key(r["part_number"])].append(r["id"])
    out = {"steps": defaultdict(list), "subtasks": defaultdict(set)}
    for i, c in enumerate(changes):
        c["id"] = f"chg-{i + 1:03d}"
        occs = set(c.get("occ_ids_base", []))
        rows = set(c.get("rows_base", []))
        for pn in (c.get("part_number_base"), c.get("part_number")):
            if pn:
                rows |= set(rows_by_pn.get(_key(pn), []))
                rows |= set(rows_by_pn.get(_key(re.sub(r"^[A-Z0-9]{5}-", "", pn)), []))
        c["affected_steps"] = []
        for kind, proc in base_procs.items():
            for st in proc["subtasks"]:
                for s in st["steps"]:
                    if occs & set(s.get("occ_ids", [])) or rows & set(s.get("rows", [])):
                        c["affected_steps"].append({"procedure": kind, "subtask": st["id"], "step": s["id"], "text": s["text"]})
                        out["steps"][f"{kind}:{s['id']}"].append(c["id"])
                        out["subtasks"][f"{kind}:{st['id']}"].add(c["id"])
        c["affected_rows"] = sorted(rows)
    return {"steps": dict(out["steps"]), "subtasks": {k: sorted(v) for k, v in out["subtasks"].items()}}


def op_signature(op: dict) -> tuple:
    items = tuple(sorted((i["part_number"], i.get("count") or 0) for i in op.get("items", [])))
    tq = (op.get("torque") or {}).get("nominal_nm")
    tgt = (op.get("from") or op.get("on") or {}).get("part_number")
    return (op["type"], items, tq, tgt, tuple(sorted(s["spec"] for s in op.get("supplies", []) or [])))


def carry_forward(new_proc: dict, new_process: dict, base_proc: dict | None, base_process: dict | None) -> dict:
    """Keep the baseline wording (including writer edits) for steps whose operation did not change."""
    if not base_proc or not base_process:
        for st in new_proc["subtasks"]:
            for s in st["steps"]:
                s["revision"] = "new"
        return {"kept": 0, "changed": sum(len(st["steps"]) for st in new_proc["subtasks"]), "removed": []}
    kind = new_proc["kind"]
    bops = {o["id"]: o for o in base_process[kind]["operations"]}
    nops = {o["id"]: o for o in new_process[kind]["operations"]}
    base_text: dict[tuple, list[dict]] = defaultdict(list)
    for st in base_proc["subtasks"]:
        for s in st["steps"]:
            op = bops.get(s["op_ids"][0])
            if op:
                idx = s["id"].rsplit("-", 1)[-1]
                base_text[(op_signature(op), idx)].append(s)
    kept = changed = 0
    used = set()
    for st in new_proc["subtasks"]:
        for s in st["steps"]:
            op = nops.get(s["op_ids"][0])
            idx = s["id"].rsplit("-", 1)[-1]
            cands = [b for b in base_text.get((op_signature(op), idx), []) if id(b) not in used] if op else []
            if cands:
                b = cands[0]
                used.add(id(b))
                if b.get("origin") == "writer" or b["text"] != s["text"]:
                    s["generated_text"] = s["text"]
                    s["text"] = b["text"]
                    s["origin"] = b.get("origin", "template")
                s["revision"] = "unchanged"
                s["baseline_step"] = b["id"]
                kept += 1
            else:
                s["revision"] = "changed"
                changed += 1
    removed = [{"step": s["id"], "text": s["text"]} for lst in base_text.values() for s in lst if id(s) not in used]
    return {"kept": kept, "changed": changed, "removed": removed}


def compare(base: dict, new: dict) -> dict:
    """base / new: {"model", "ipl", "process", "procedures": {"assembly", "disassembly"}}"""
    model_changes = diff_models(base["model"], new["model"])
    ipl_changes = diff_ipl(base["ipl"], new["ipl"])
    # fold "IPL row removed X + added Y" into the 3D part-number change X -> Y
    strip = lambda pn: _key(re.sub(r"^[A-Z0-9]{5}-", "", pn or ""))
    for mc in [c for c in model_changes if c["type"] == "part_number_changed"]:
        rem = next((c for c in ipl_changes if c["type"] == "ipl_removed" and _key(c["part_number"]) == strip(mc["part_number_base"])), None)
        add = next((c for c in ipl_changes if c["type"] == "ipl_added" and _key(c["part_number"]) == strip(mc["part_number_new"])), None)
        if rem and add:
            ipl_changes.remove(rem)
            ipl_changes.remove(add)
            mc["rows_base"], mc["rows_new"] = rem["rows_base"], add["rows_new"]
            mc["ipl"] = f"IPL: {rem['part_number']} replaced by {add['part_number']}"
            mc["source"] = "3D + IPL"
    # fold "IPL row removed X + added Y" with the same name into one part-number change (a superseding part
    # that the 3D does not show, e.g. a parts-list-only change or a variant with a new BOM)
    name = lambda c: re.sub(r"[^A-Z]", "", (c.get("description") or c.get("nomenclature") or "").upper())
    for rem in [c for c in ipl_changes if c["type"] == "ipl_removed"]:
        add = next((c for c in ipl_changes if c["type"] == "ipl_added" and name(c) and name(c) == name(rem)
                    and c.get("role") == rem.get("role")), None)
        if add:
            ipl_changes.remove(rem)
            ipl_changes.remove(add)
            ipl_changes.append({"type": "part_number_changed", "occ_ids_base": [], "occ_ids_new": [],
                                "part_number_base": rem["part_number"], "part_number_new": add["part_number"],
                                "name": add.get("nomenclature"), "geometry": "not in the 3D change", "count": 1,
                                "source": "IPL", "rows_base": rem.get("rows_base", []), "rows_new": add.get("rows_new", []),
                                "message": f"{rem['part_number']} -> {add['part_number']} in the parts list",
                                "ipl": f"IPL: {rem['part_number']} replaced by {add['part_number']}"})
    changes = model_changes + ipl_changes
    imp = impact(changes, base["ipl"], base.get("procedures", {}))
    return {"format": "studio.changeset/1", "changes": changes, "impact": imp,
            "summary": {"changes": len(changes),
                        "by_type": {t: sum(1 for c in changes if c["type"] == t) for t in sorted({c["type"] for c in changes})},
                        "affected_steps": len(imp["steps"]), "affected_subtasks": len(imp["subtasks"])}}


def modification_procedure(base: dict, new: dict, changeset: dict) -> dict:
    """Delta procedure A -> B for units in service: access + remove (baseline), install (new), inspect."""
    affected = {k.split(":", 1)[1] for k in changeset["impact"]["subtasks"] if k.startswith("disassembly:")}
    bdis = base["procedures"]["disassembly"]
    order = [st["id"] for st in bdis["subtasks"]]
    if not affected:
        return {"format": "studio.procedure/1", "kind": "modification", "title": "No modification needed", "subtasks": [],
                "preliminary": bdis["preliminary"], "safety": bdis.get("safety", {}), "common_info": []}
    last = max(order.index(a) for a in affected if a in order)
    remove = [st for st in bdis["subtasks"][: last + 1] if not st["title"].startswith("Parts that stay")]
    removed_titles = {st["title"].replace("Remove", "Install") for st in remove}
    nas = new["procedures"]["assembly"]
    install = [st for st in nas["subtasks"] if st["title"] in removed_titles or st["title"].replace("remaining ", "") in removed_titles
               or any(c in (changeset["impact"]["subtasks"]) for c in [f"assembly:{st['id']}"])]
    subs = []
    for st in remove:
        subs.append({**st, "id": "mod-r-" + st["id"], "phase": "remove"})
    for st in install:
        subs.append({**st, "id": "mod-i-" + st["id"], "phase": "install"})
    subs.append({"id": "mod-check", "title": "Make sure that the modification is correct", "phase": "check", "warnings": [],
                 "cautions": [], "rows": [], "steps": [{"id": "mod-check-1", "kind": "step", "op_ids": [], "occ_ids": [], "rows": [],
                                                         "text": "Do the acceptance test procedure (ATS).", "origin": "template",
                                                         "issues": [], "flags": []}]})
    return {"format": "studio.procedure/1", "kind": "modification",
            "title": f"Modification of {(new['ipl']['figure'] or {}).get('title', 'the unit')}",
            "changes": [c["message"] for c in changeset["changes"]],
            "preliminary": nas["preliminary"], "safety": {**bdis.get("safety", {}), **nas.get("safety", {})},
            "common_info": nas.get("common_info", []), "subtasks": subs,
            "summary": {"subtasks": len(subs), "steps": sum(len(s["steps"]) for s in subs)}}
