"""Assembly process graph: operations + precedence, compiled from the validated IPL and sequence.

Each operation says *what is done to which items, in which order, with what* — so text
generation only has to word decisions that are already made. Every operation lists its
IPL rows and 3D occurrences, which is what lets a revision find the steps a changed part
touches.

Disassembly is built from the planned removal order; assembly is derived from it (reverse
order, remove -> install, unfasten -> fasten with torque, discarded parts -> new parts),
then drawing notes add their operations (conductive compound, sealant, lock wire, test).
"""
from __future__ import annotations

import re
from collections import defaultdict

NM_TO_LBFIN = 8.8507458


def torque_range(tq: dict | None) -> dict | None:
    if not tq:
        return None
    lo = tq.get("min_nm") if tq.get("min_nm") is not None else tq["nominal_nm"]
    hi = tq.get("max_nm") if tq.get("max_nm") is not None else tq["nominal_nm"]
    return {"min_nm": round(lo, 2), "max_nm": round(hi, 2), "min_lbfin": round(lo * NM_TO_LBFIN, 1),
            "max_lbfin": round(hi * NM_TO_LBFIN, 1), "nominal_nm": tq["nominal_nm"]}


def note_supply_name(note_text: str, spec: str) -> str | None:
    """'... WITH CONDUCTIVE COMPOUND ACCORDING TO IFC30-145-07' -> 'conductive compound'."""
    up = " ".join(note_text.upper().split())
    m = re.search(rf"([A-Z][A-Z \-]{{2,60}}?)\s+(?:ACCORDING TO|PER|TO)\s+{re.escape(spec)}", up)
    if not m:
        return None
    phrase = re.split(r"\b(?:WITH|BY|OF|FOR|AND INSTALLATION|INSTALLATION)\b", m.group(1))[-1].strip()
    words = [w for w in phrase.split() if w not in ("THE", "A", "AN", "ALL")]
    if len(words) <= 1:
        return None                      # too generic ("compound"): the MBOM description says more
    return " ".join(words[-6:]).lower()


# sub-assemblies that are not taken apart in a CMM (joined for good, or replaced as a unit)
NO_DISASSY = re.compile(r"\b(SHRUNK|SHRINK|WOUND|BONDED|WELDED|BRAZED|RIVETED|CRIMPED|SWAGED|STAKED|LAMINAT\w*|POTTED|"
                        r"EQUIPPED|MACHINING|STACK|PRINTED CIRCUIT|PCB|HARNESS|CABLE ASSY)\b", re.I)
# parts left in place when their sub-assembly is disassembled (press fits, the body itself)
NO_REMOVE = re.compile(r"\b(EXPANSION|KOENIG|LEE|INSERT|HELICOIL|DOWEL|MACHINING|IDENTIFICATION|ISO8734|"
                       r"RULES OF DESIGN|GREASE|TAPE|SHEATH|COMPOUND|ADHESIVE|SEALANT|VARNISH|PAINT)\b", re.I)


def wrench_for(max_nm: float) -> str:
    for lo, hi in ((0.2, 2.0), (1, 12), (5, 30), (20, 110), (60, 340)):
        if max_nm <= hi * 0.9:
            return f"Torque wrench, {lo:g} to {hi:g} N.m"
    return "Torque wrench"


class _Ids:
    def __init__(self, prefix: str):
        self.prefix, self.n = prefix, 0

    def __call__(self) -> str:
        self.n += 1
        return f"{self.prefix}{self.n:03d}"


def _item(row: dict) -> dict:
    return {"row_id": row["id"], "item": row["item"], "part_number": row["part_number"],
            "nomenclature": row["nomenclature"], "role": row["role"], "qty": row.get("units_per_assy"),
            "step_name": row.get("step_name")}


def build_process(ipl: dict, sequence: dict, drawing: dict | None, options: dict | None = None) -> dict:
    options = options or {}
    rows = [r for r in ipl["rows"] if r.get("status") != "rejected"]
    by_id = {r["id"]: r for r in rows}
    row_of_occ = {o: r for r in rows for o in r["occ_ids"] if not r.get("alternate_of")}
    top = next((r for r in rows if r["depth"] == 0), None)
    notes = (drawing or {}).get("notes", [])
    supplies_by_spec = {s["spec"]: s for s in ipl.get("supplies", [])}

    def item_rows(occs: list[str]) -> list[dict]:
        seen, out = set(), []
        for o in occs:
            r = row_of_occ.get(o)
            if r and r["id"] not in seen:
                seen.add(r["id"])
                out.append(r)
        return out

    def count(row: dict, occs: list[str]) -> int:
        return sum(1 for o in occs if o in row["occ_ids"]) or (row.get("units_per_assy") or 1)

    # ------------------------------------------------------------ disassembly, level by level
    # Level 1: the parts of the unit, part by part (attaching parts, the part, the parts fitted on it), in
    # the planner's order. Then each sub-assembly that came off is disassembled in its own subtask, from its
    # own plan (the STEP sub-assembly) or, without one, in parts-list order; and so on down the levels.
    kids_of: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("parent_row") and not r.get("alternate_of"):
            kids_of[r["parent_row"]].append(r)

    def child_of(row: dict | None, pid: str) -> dict | None:
        r, seen = row, set()
        while r is not None and r.get("parent_row") != pid:
            if r["id"] in seen or not r.get("parent_row"):
                return None
            seen.add(r["id"])
            r = by_id.get(r["parent_row"])
        return r

    op_id = _Ids("op-d")
    subtasks: list[dict] = []
    ops: list[dict] = []
    used_occs: set[str] = set()                 # 3D occurrences already taken off by an operation
    used_rows: set[str] = set()                 # rows without 3D taken off

    def done_row(r: dict) -> bool:
        """Already taken off by an earlier operation (a shared part number counts per occurrence)."""
        return all(o in used_occs for o in r["occ_ids"]) if r["occ_ids"] else r["id"] in used_rows
    prev_op = None

    def new_st(title: str = "", **kw) -> dict:
        st = {"id": f"st-d{len(subtasks) + 1:02d}", "title": title, "ops": [], "rows": [], **kw}
        subtasks.append(st)
        return st

    def add(op: dict, st: dict) -> dict:
        nonlocal prev_op
        op["id"] = op_id()
        op["after"] = [prev_op] if prev_op else []
        prev_op = op["id"]
        ops.append(op)
        st["ops"].append(op["id"])
        here = set(op.get("occ_ids", []))
        for i in op.get("items", []) + op.get("with", []):
            r = by_id.get(i["row_id"])
            if r and r["occ_ids"]:
                hit = [o for o in r["occ_ids"] if o in here]
                # a sub-assembly row linked to its assembly occurrence: its parts are in the op, so it is all off
                used_occs.update(hit or r["occ_ids"])
            else:
                used_rows.add(i["row_id"])
        return op

    def flags_for(forced: bool) -> list[str]:
        return ["Order forced by the planner: check the 3D."] if forced else []

    def level(steps: list[dict], pid: str, base_occ: str | None, default_from: dict | None,
              one_subtask: dict | None = None, only=None) -> list[dict]:
        """Removal ops for the children of row ``pid``. -> the child rows removed, in order."""
        grouped: list[dict] = []
        level_ids = {r["id"] for r in kids_of.get(pid, [])}
        unit_members = {s["unit"]: s["members"] for s in steps}

        def unit_row(t: str | None) -> str | None:
            """The row at this level of a sequence unit (a part, a sub-assembly, the base unit)."""
            if not t:
                return None
            for r in item_rows(unit_members.get(t) or all_units.get(t) or [t]):
                m = child_of(r, pid)
                if m is not None:
                    return m["id"]
            return None

        def held_row(s: dict) -> str | None:
            """The row at this level of the part this step's attaching part holds (the sequence's ``attaches``,
            worked out per occurrence: one screw part number can hold several parts)."""
            return unit_row(s.get("attaches"))

        for s in steps:
            rws = item_rows(s["members"])
            mains = [m for m in (child_of(r, pid) for r in rws) if m is not None and m["depth"] != 0]
            if not mains:
                continue
            main = mains[0]
            if only is not None and not only(main):
                continue
            # carried parts at this level: a part of a carried sub-assembly counts as that sub-assembly
            riders = []
            for r in item_rows(s["riders"]):
                lr = child_of(r, pid) or r
                if lr["id"] != main["id"] and lr["id"] not in {x["id"] for x in riders} and not done_row(lr):
                    riders.append(lr)
            target = None
            if main["attaching"]:
                h = held_row(s)
                target = h if h in level_ids and h != main["id"] else main.get("attaching_for")
            g = next((g for g in grouped if g["main"]["id"] == main["id"] and g["target"] == target), None)
            if g:
                g["occs"] += s["members"]
                g["rider_occs"] += s["riders"]
                g["steps"].append(s["step"])
                g["forced"] = g["forced"] or s["forced"]
                if s.get("check") and s["check"] not in g["checks"]:
                    g["checks"].append(s["check"])
                for r in riders:
                    if r["id"] not in {x["id"] for x in g["riders"]}:
                        g["riders"].append(r)
            else:
                grouped.append({"main": main, "occs": list(s["members"]), "rider_occs": list(s["riders"]),
                                "riders": riders, "steps": [s["step"]], "direction": s["direction_label"],
                                "forced": s["forced"], "target": target, "on": unit_row(s.get("on")),
                                "checks": [s["check"]] if s.get("check") else []})

        def is_att(g: dict) -> bool:
            return bool(g["main"]["attaching"] and g["target"] in level_ids and g["target"] != g["main"]["id"])

        attaching_groups: dict[str, list[dict]] = defaultdict(list)
        for g in grouped:
            if is_att(g):
                attaching_groups[g["target"]].append(g)
        main_groups = [g for g in grouped if not is_att(g)]
        for t, gs in attaching_groups.items():                  # attaching parts whose item is never removed
            if t not in {g["main"]["id"] for g in main_groups}:
                main_groups.append({"main": by_id[t], "occs": [], "rider_occs": [], "riders": [],
                                    "steps": [min(min(x["steps"]) for x in gs)], "direction": None,
                                    "forced": False, "stays": True})
        main_groups.sort(key=lambda g: min([min(g["steps"])] + [min(x["steps"]) for x in attaching_groups.get(g["main"]["id"], [])]))

        base_row = next((child_of(r, pid) for r in rows if base_occ and base_occ in r["occ_ids"]), None)
        from_row = base_row or default_from
        removed: list[dict] = []
        for g in main_groups:
            main = g["main"]
            if done_row(main) and not attaching_groups.get(main["id"]):
                continue
            st = one_subtask or new_st()
            st["rows"].append(main["id"])
            for ag in sorted(attaching_groups.get(main["id"], []), key=lambda x: min(x["steps"])):
                ar = ag["main"]
                if done_row(ar) or (ar["role"] == "washer" and ar.get("fastener_row")):
                    continue
                st["rows"].append(ar["id"])
                washers = [r for r in rows if r["role"] == "washer" and r.get("fastener_row") == ar["id"]]
                items = [{**_item(ar), "count": count(ar, ag["occs"])}]
                w_occs: list[str] = []
                for w in washers:
                    # the washers under these screws: the 3D washer steps that hold the same part
                    mine = [o for x in attaching_groups.get(main["id"], []) if x["main"]["id"] == w["id"]
                            for o in x["occs"] if o not in used_occs]
                    if not mine and any(x["main"]["id"] == w["id"] for x in grouped):
                        continue                    # its washers are under the screws of another part
                    if not mine:
                        mine = [o for o in w["occ_ids"] if o not in used_occs]
                    if w["occ_ids"] and not mine:
                        continue
                    items.append({**_item(w), "count": len(mine) if mine else (w.get("units_per_assy") or 1)})
                    w_occs += mine
                    st["rows"].append(w["id"])
                add({"type": "unfasten", "phase": "disassembly", "items": items,
                     "occ_ids": ag["occs"] + w_occs,
                     "from": {**_item(main), "count": count(main, g["occs"]) if g["occs"] else (main.get("units_per_assy") or 1)},
                     "torque": torque_range(ar.get("torque")), "direction": ag["direction"], "flags": flags_for(ag["forced"])}, st)
            if g.get("stays") or done_row(main):
                if not one_subtask:
                    st["title"] = f"Remove the attaching parts from the {main['nomenclature'].lower()}"
                    st["kind"] = "attaching"
                continue
            loose = [r for r in g["riders"] if not r["permanent"] and child_of(r, main["id"]) is None and not done_row(r)]
            # removed from the part it sits on (from the 3D), else from the base of this level
            on = by_id.get(g.get("on") or "")
            frm = on if on is not None and on["id"] != main["id"] and on["id"] in level_ids else \
                from_row if from_row and from_row["id"] != main["id"] else default_from
            add({"type": "remove", "phase": "disassembly", "items": [{**_item(main), "count": count(main, g["occs"])}],
                 "occ_ids": g["occs"] + g["rider_occs"], "from": _item(frm) if frm else None,
                 "discard": bool(main.get("discard_on_removal")) or main["role"] in ("seal", "backup_ring"),
                 "with": [{**_item(r), "count": count(r, g["rider_occs"])} for r in g["riders"] if not done_row(r)],
                 "direction": g["direction"], "flags": flags_for(g["forced"]) + [c for c in g.get("checks", [])
                                                                               if not g["forced"]]}, st)
            removed.append(main)
            removed += [r for r in loose if kids_of.get(r["id"])]      # carried sub-assemblies are disassembled too
            if loose:
                discard = [r for r in loose if r["discard_on_removal"]]
                add({"type": "remove_from", "phase": "disassembly",
                     "items": [{**_item(r), "count": count(r, g["rider_occs"])} for r in loose],
                     "occ_ids": [o for r in loose for o in r["occ_ids"] if o in g["rider_occs"]],
                     "from": {**_item(main), "count": count(main, g["occs"])},
                     "discard": bool(discard) and len(discard) == len(loose), "discard_items": [r["id"] for r in discard]}, st)
            if not one_subtask:
                st["title"] = f"Remove the {main['nomenclature'].lower()}"
        return removed

    def _k(x: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", (x or "").upper())

    def plan_for(row: dict, kids: list[dict]) -> dict | None:
        """The STEP sub-assembly plan for an IPL sub-assembly: by its 3D link, its part number, or the plan
        that holds most of its parts (the 3D link of an assembly row can be a fuzzy match)."""
        for o in row["occ_ids"]:
            if o in sub_plans:
                return sub_plans[o]
        k = _k(row["part_number"])
        for oid, p in sub_plans.items():
            if k and _k(oid.rsplit("_", 1)[0]).endswith(k):
                return p
        kid_occs = {o for x in kids for o in x["occ_ids"]}
        best, n = None, 1
        for p in sub_plans.values():
            m = len(kid_occs & {o for s in p.get("disassembly", []) for o in s["members"] + s["riders"]})
            if m > n:
                best, n = p, m
        return best

    def stays_assembled(row: dict) -> bool:
        return bool(NO_DISASSY.search(row["nomenclature"] or ""))

    def removable(r: dict) -> bool:
        return bool(r["occ_ids"]) and not r["permanent"] and (r.get("units_per_assy") or 0) > 0 \
            and r["role"] not in ("insert", "press_plug", "plate", "dowel") and not NO_REMOVE.search(r["nomenclature"] or "") \
            and not r.get("process")

    def disassemble(row: dict, keep_note: list[dict]) -> None:
        """'Disassemble the <sub-assembly>': its own parts, then its sub-assemblies, depth first."""
        kids = [k for k in kids_of.get(row["id"], []) if (k.get("units_per_assy") or 0) > 0]
        if not kids:
            return
        todo = [k for k in kids if removable(k) and not done_row(k)]
        if stays_assembled(row) or not todo:
            if any(k["occ_ids"] for k in kids):
                keep_note.append(row)
            return
        if len(todo) == 1 and not any(k["attaching"] for k in todo):
            keep_note.append(row)                    # one part left: that is the body, nothing comes apart
            return
        st = new_st(f"Disassemble the {row['nomenclature'].lower()}", kind="disassemble", assy_row=row["id"])
        st["rows"].append(row["id"])
        plan = plan_for(row, kids)
        base_kid = None
        if plan:
            base_kid = next((child_of(r, row["id"]) for r in rows if plan.get("base_unit") in r["occ_ids"]), None)
            removed = level(plan.get("disassembly", []), row["id"], plan.get("base_unit"), row, one_subtask=st,
                            only=removable)
        else:
            removed = []
        # parts the plan did not reach (or no plan): parts-list order, attaching parts before their part
        for k in [k for k in kids if removable(k) and not done_row(k) and k is not base_kid
                  and not (k["attaching"] and k.get("attaching_for") in {x["id"] for x in kids})]:
            for a in [a for a in kids if a["attaching"] and a.get("attaching_for") == k["id"] and not done_row(a) and a["occ_ids"]]:
                add({"type": "unfasten", "phase": "disassembly", "items": [{**_item(a), "count": a.get("units_per_assy") or 1}],
                     "occ_ids": a["occ_ids"], "from": _item(k), "torque": torque_range(a.get("torque")), "flags": []}, st)
                st["rows"].append(a["id"])
            add({"type": "remove", "phase": "disassembly", "items": [{**_item(k), "count": k.get("units_per_assy") or 1}],
                 "occ_ids": list(k["occ_ids"]), "from": _item(row), "with": [],
                 "discard": bool(k.get("discard_on_removal")) or k["role"] in ("seal", "backup_ring"),
                 "flags": [] if plan else
                 ["No 3D sub-assembly to plan from: parts-list order. Check the order."]}, st)
            st["rows"].append(k["id"])
            removed.append(k)
        if not st["ops"]:
            subtasks.remove(st)
            return
        notes: list[dict] = []
        for k in list({k["id"]: k for k in removed}.values()):
            disassemble(k, notes)
        notes = list({n["id"]: n for n in notes}.values())
        if notes:
            add({"type": "do_not_disassemble", "phase": "disassembly", "items": [{**_item(n), "count": 1} for n in notes],
                 "occ_ids": [o for n in notes for o in n["occ_ids"]]}, st)

    # preliminary: unmodelled caps and lock wire come off first
    pre = [r for r in rows if not r["occ_ids"] and r["depth"] >= 1 and not r.get("alternate_of")
           and r["role"] in ("cap", "lockwire") and (r.get("units_per_assy") or 0) > 0 and r["depth"] == 1]
    if pre:
        st = {"id": "st-d00", "title": "Remove the protective items", "ops": [], "rows": [r["id"] for r in pre]}
        subtasks.append(st)
        for r in pre:
            add({"type": "remove", "phase": "disassembly", "items": [{**_item(r), "count": r.get("units_per_assy") or 1}],
                 "occ_ids": [], "from": _item(top) if top else None, "discard": r["role"] == "lockwire",
                 "flags": ["Not in the 3D model: confirm where this item is installed."]}, st)

    sub_plans = sequence.get("sub") or {}
    all_units = {u["id"]: u["members"] for p in [sequence, *sub_plans.values()] for u in p.get("units", [])}
    top_id = top["id"] if top else None
    removed_top = level(sequence.get("disassembly", []), top_id, sequence.get("base_unit"), None) if top_id else []
    kept: list[dict] = []
    base_top = next((child_of(r, top_id) for r in rows if sequence.get("base_unit") in r["occ_ids"]), None) if top_id else None
    for row in removed_top + ([base_top] if base_top and base_top not in removed_top else []):
        disassemble(row, kept)

    perm = [r for r in rows if r["permanent"] and r["occ_ids"] and r["role"] in ("insert", "press_plug", "dowel") and not r.get("alternate_of")]
    if perm or kept:
        st = new_st("Parts that stay installed")
        st["id"] = f"st-d{len(subtasks):02d}x"
        st["rows"] = [r["id"] for r in perm + kept]
        if kept:
            add({"type": "do_not_disassemble", "phase": "disassembly", "items": [{**_item(n), "count": 1} for n in kept],
                 "occ_ids": [o for n in kept for o in n["occ_ids"]]}, st)
        if perm:
            add({"type": "do_not_remove", "phase": "disassembly", "items": [{**_item(r), "count": r.get("units_per_assy") or 1} for r in perm],
                 "occ_ids": [o for r in perm for o in r["occ_ids"]], "condition": "unless they are damaged"}, st)

    disassembly = {"subtasks": subtasks, "operations": ops}

    # ------------------------------------------------------------ assembly: derived
    aop = _Ids("op-a")
    a_ops: list[dict] = []
    a_sub: list[dict] = []
    prev_a = None
    by_op = {o["id"]: o for o in ops}
    note_by_op = defaultdict(list)
    for n in notes:
        note_by_op[n["op"]].append(n)

    def add_a(op: dict, st: dict) -> dict:
        nonlocal prev_a
        op["id"] = aop()
        op["after"] = [prev_a] if prev_a else []
        prev_a = op["id"]
        a_ops.append(op)
        st["ops"].append(op["id"])
        return op

    def supply(sp: str, n: dict) -> dict:
        name = note_supply_name(n["text"], sp) or (supplies_by_spec.get(sp) or {}).get("description") or n["label"]
        return {"spec": sp, "name": name, "description": (supplies_by_spec.get(sp) or {}).get("description"),
                "note": n["text"]}

    def note_supply(op_kind: str, role: str) -> list[dict]:
        out = []
        for n in note_by_op.get(op_kind, []):
            if not n["targets"] or role in n["targets"]:
                out += [supply(sp, n) for sp in n["specs"]]
        return out

    NOTES = ("do_not_remove", "do_not_disassemble")
    for st in reversed([s for s in subtasks if s["id"] != "st-d00"]):
        d_ops = [by_op[o] for o in st["ops"] if by_op[o]["type"] not in NOTES]
        if not d_ops:
            continue
        main_row = by_id[st["rows"][0]]
        ast = {"id": st["id"].replace("st-d", "st-a"), "title": "", "ops": [], "rows": list(st["rows"]),
               **({"kind": "assemble", "assy_row": st["assy_row"]} if st.get("kind") == "disassemble" else
                  {"kind": "attaching"} if st.get("kind") == "attaching" else {})}
        a_sub.append(ast)
        for o in reversed(d_ops):
            if o["type"] == "remove_from":
                add_a({"type": "install_on", "phase": "assembly", "items": o["items"], "occ_ids": o["occ_ids"],
                       "on": o["from"], "new_parts": o.get("discard", False),
                       "new_items": o.get("discard_items", [])}, ast)
            elif o["type"] == "remove":
                role = o["items"][0]["role"]
                add_a({"type": "install", "phase": "assembly", "items": o["items"], "occ_ids": o["occ_ids"],
                       "on": o.get("from"), "with": o.get("with", []), "new_parts": bool(o.get("discard")),
                       "torque": torque_range((by_id.get(o["items"][0]["row_id"]) or main_row).get("torque")),
                       "references": note_supply("install_fittings", role) if role == "fitting" else [],
                       "direction": o.get("direction")}, ast)
            elif o["type"] == "unfasten":
                role = o["items"][0]["role"]
                fop = add_a({"type": "fasten", "phase": "assembly", "items": o["items"], "occ_ids": o["occ_ids"],
                             "on": o["from"], "torque": o.get("torque"),
                             "supplies": note_supply("apply_conductive_compound", role),
                             "flags": [] if o.get("torque") else ["No torque found on the drawing for these fasteners."]}, ast)
                seal_notes = [n for n in note_by_op.get("apply_sealant", []) if n["phase"] == "after_install"
                              and role in n["targets"]]
                if seal_notes:
                    add_a({"type": "apply", "phase": "assembly", "what": "sealant", "on_items": fop["items"],
                           "items": fop["items"], "occ_ids": o["occ_ids"],
                           "supplies": [supply(sp, n) for n in seal_notes for sp in n["specs"]]}, ast)
        ast["title"] = ("Assemble the " if st.get("kind") == "disassemble" else
                        "Install the attaching parts on the " if st.get("kind") == "attaching" else "Install the ") + main_row["nomenclature"].lower()

    close = []
    caps = [r for r in rows if r["role"] == "cap" and not r["occ_ids"] and not r.get("alternate_of") and r["depth"] == 1
            and (r.get("units_per_assy") or 0) > 0]
    for n in note_by_op.get("lockwire", []):
        close.append({"type": "lockwire", "spec": n["specs"], "text": n["text"],
                      "items": [_item(r) for r in rows if r["role"] == "lockwire" and not r.get("alternate_of") and r["depth"] == 1],
                      "flags": ["The drawing calls for lock wire; the parts to safety are not identified — add them."]})
    for n in note_by_op.get("test", []):
        close.append({"type": "test", "spec": n["specs"], "text": n["text"]})
    for n in note_by_op.get("apply_sealant", []):
        if n["phase"] == "after_test":
            plugs = [r for r in rows if r["role"] == "press_plug" and not r.get("alternate_of")]
            close.append({"type": "apply_after_test", "spec": n["specs"], "text": n["text"],
                          "items": [_item(r) for r in plugs]})
    if close:
        st = {"id": "st-a99", "title": "Safety and test", "ops": [], "rows": []}
        a_sub.append(st)
        for c in close:
            add_a({"phase": "assembly", "occ_ids": [], **c,
                   "supplies": [supply(sp, {"text": c["text"], "label": c["text"][:60]}) for sp in c.get("spec", []) if sp != "ATS"]}, st)
    if caps:
        st = {"id": "st-a98", "title": "Install the protective items", "ops": [], "rows": [r["id"] for r in caps]}
        a_sub.append(st)
        for r in caps:
            add_a({"type": "install", "phase": "assembly", "items": [{**_item(r), "count": r.get("units_per_assy") or 1}],
                   "occ_ids": [], "on": None, "flags": ["Not in the 3D model: confirm where this item is installed."]}, st)

    general = [{"text": n["text"], "specs": n["specs"], "label": n["label"], "op": n["op"]}
               for n in notes if n["phase"] == "general"]
    assembly = {"subtasks": a_sub, "operations": a_ops}
    return {"format": "studio.process/1", "disassembly": disassembly, "assembly": assembly,
            "general_notes": general, "top": _item(top) if top else None}
