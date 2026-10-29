"""Proposed IPL from the engineering model: MBOM (APL) + 3D + drawing + sequence.

The MBOM gives the structure, quantities, CAGE codes and variants; the 3D gives which
parts are physically present, where they are and the disassembly order; the drawing gives
torques. Each row carries the evidence for each value (``sources``), an overall
``confidence``, ``flags`` for the writer, and the ``occ_ids`` that link it to the 3D view
and later to procedure steps.

Conventions (ATA iSpec 2200 IPL; S1000D IPD rendering converts them):
* Figure 1, top assembly item ``-1`` at indenture 1, then items 10, 20, 30… in disassembly
  order; sub-assembly details follow their parent with the next indenture.
* Attaching parts follow the item they attach and are marked ``attaching_for``.
* Interchangeable alternates (MBOM Equiv J/K in the same slot) share the item number with a
  letter suffix (``20``/``20A``) and carry their effectivity code.
* Process / material specifications (NCT, IFC, PCS…) are not items: they are attached to the
  item they belong to and listed as supplies for the procedures.
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..reconcile.mbom_loader import get_hierarchy_depth, strip_hierarchy_dots
from ..reconcile.reconciler import is_likely_process_spec, parse_supersession
from .roles import ROLES, RoleClassifier

NOUNS = {"SCREW", "WASHER", "SEAL", "RING", "SPRING", "VALVE", "PLUG", "INSERT", "PIN", "BOLT", "NUT", "BUSH",
         "BUSHING", "PLATE", "CAP", "COVER", "HOUSING", "BODY", "SHAFT", "GASKET", "BEARING", "CLAMP", "SLEEVE",
         "PISTON", "ROTOR", "STATOR", "MAGNET", "COIL", "WIRE", "CABLE", "BRACKET", "PACKING", "ASSEMBLY", "BOBBIN",
         "UNION", "FERRULE", "VALVE", "MANIFOLD", "LABEL", "FITTING"}
PROCESS_PREFIXES = ("NCT", "IFC", "PCS-", "PCS", "DRSE")


def kwd_nomenclature(desc: str) -> str:
    """ATA keyword style: basic noun first, then modifiers ("THREAD INSERT" -> "INSERT, THREAD")."""
    d = " ".join((desc or "").upper().replace(",", " , ").split()).replace(" ,", ",")
    if not d or "," in d:
        return d
    toks = d.split()
    if len(toks) >= 2 and toks[-1].rstrip(".") in NOUNS and toks[0] not in NOUNS:
        return f"{toks[-1]}, {' '.join(toks[:-1])}"
    return d


def _qty(v) -> int | None:
    try:
        return int(float(str(v).strip())) if str(v).strip() not in ("", "None") else None
    except ValueError:
        return None


def _key(pn: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", strip_hierarchy_dots((pn or "").strip())[0].upper())


def is_process(pn: str) -> bool:
    p = strip_hierarchy_dots(pn or "")[0].upper()
    return p.startswith(PROCESS_PREFIXES) or is_likely_process_spec({"part_number": p})


def build_ipl(model: dict, reconciliation: dict, mbom: list[dict], sequence: dict, drawing: dict | None,
              roles_by_occ: dict[str, dict], contacts: list[dict], manuf_prefix: str | None = None,
              classifier: RoleClassifier | None = None) -> dict:
    classifier = classifier or RoleClassifier()
    occ = {o["occ_id"]: o for o in model["occurrences"]}
    root_occ = occ[model["root"]]

    # ---------------------------------------------------------------- MBOM rows with tree
    rows_in: list[dict] = []
    stack: list[int] = []
    for i, rec in enumerate(mbom):
        depth = get_hierarchy_depth(rec)
        pn = strip_hierarchy_dots(rec.get("part_number", ""))[0]
        while stack and rows_in[stack[-1]]["depth"] >= depth:
            stack.pop()
        rows_in.append({"i": i, "depth": depth, "pn": pn, "key": _key(pn), "desc": rec.get("description", ""),
                        "qty": _qty(rec.get("qty")), "uom": rec.get("unit_measure", "ea"),
                        "cage": rec.get("manuf_code"), "equiv": (rec.get("equiv") or "").strip().upper() or None,
                        "seq": rec.get("seq_number"), "find": rec.get("find_number"),
                        "comments": " ".join((rec.get("comments") or "").split()),
                        "parent": stack[-1] if stack else None, "process": is_process(pn)})
        stack.append(i)
    if not rows_in:
        raise ValueError("the MBOM has no rows")

    # ---------------------------------------------------------------- 3D links from the reconciliation
    step_to_occs: dict[str, list[str]] = defaultdict(list)
    for o in model["occurrences"]:
        step_to_occs[o["part_number"]].append(o["occ_id"])
    links: dict[str, dict] = {}                 # MBOM key -> link info
    displaced: list[dict] = []
    for m in reconciliation.get("matched", []):
        k = _key(m["mbom_part_number"])
        occs = [x for pn in (m.get("member_part_numbers") or [m["step_part_number"]]) for x in step_to_occs.get(pn, [])]
        prev = links.get(k)
        rank = {"matched": 0, "manual": 0, "matched_via_supersession": 1, "fuzzy_candidate": 2,
                "translated_candidate": 3, "last_resort_candidate": 4, "shape_suggested": 5}
        info = {"occ_ids": occs, "status": m["status"], "confidence": m["confidence"], "flags": m.get("flags", []),
                "step_part_number": m["step_part_number"], "step_name": m.get("step_name"),
                "step_qty": m.get("step_qty"), "quantity_match": m.get("quantity_match"),
                "effectivity_code": m.get("effectivity_code")}
        if prev and rank.get(info["status"], 9) < rank.get(prev["status"], 9):
            displaced.append(prev)
            links[k] = info
        elif prev and info["status"] == prev["status"] == "matched":
            prev["occ_ids"] += occs
        elif prev:
            displaced.append(info)      # a weaker 3D candidate for a line that already has a better one
        else:
            links[k] = info
    # STEP sub-assemblies (not in the flat BOM) link by part number directly
    asm_links: dict[str, dict] = {}
    for o in model["occurrences"]:
        if o["is_assembly"] and o["occ_id"] != model["root"]:
            k = _key(re.sub(rf"^{re.escape(manuf_prefix)}-", "", o["part_number"]) if manuf_prefix else o["part_number"])
            asm_links.setdefault(k, {"occ_ids": [], "status": "matched", "confidence": 1.0, "flags": [],
                                     "step_part_number": o["part_number"], "step_name": o["name"],
                                     "step_qty": 0, "quantity_match": None, "effectivity_code": None})
            asm_links[k]["occ_ids"].append(o["occ_id"])
            asm_links[k]["step_qty"] += 1

    # disassembly step per occurrence (via its unit, or the unit that carries it)
    step_of: dict[str, int] = {}
    unit_of: dict[str, str] = {}
    units = {u["id"]: u for u in sequence.get("units", [])}
    for s in sequence.get("disassembly", []):
        for o in s["members"] + s["riders"]:
            step_of.setdefault(o, s["step"])
            unit_of.setdefault(o, s["unit"])
    base = sequence.get("base_unit")
    if base and base in units:
        last = len(sequence.get("disassembly", [])) + 1
        for o in units[base]["members"] + units[base]["riders"]:
            step_of.setdefault(o, last)
            unit_of.setdefault(o, base)

    for o in model["occurrences"]:
        if o["is_assembly"] and o["occ_id"] in units:
            u = units[o["occ_id"]]
            step_of[o["occ_id"]] = min((step_of.get(m, 10_000) for m in u["members"]), default=10_000)
            unit_of[o["occ_id"]] = o["occ_id"]

    # ---------------------------------------------------------------- torques by part number
    torques: dict[str, dict] = {}
    for t in (drawing or {}).get("torques", []):
        if not t.get("target_pn"):
            continue
        k = _key(t["target_pn"])
        if k not in torques:
            torques[k] = {"nominal_nm": t["nominal_nm"], "tolerance_nm": t["tolerance_nm"], "min_nm": t["min_nm"],
                          "max_nm": t["max_nm"], "evidence": [], "confidence": t["confidence"]}
        torques[k]["evidence"].append({"page": t["page"], "text": t["text"], "bbox": t["bbox"]})
        if torques[k]["nominal_nm"] != t["nominal_nm"]:
            torques[k]["conflict"] = True

    # ---------------------------------------------------------------- build candidate IPL rows
    rows: list[dict] = []
    row_by_i: dict[int, dict] = {}
    processes_by_parent: dict[int, list[dict]] = defaultdict(list)
    supplies: dict[str, dict] = {}
    for r in rows_in:
        if r["process"]:
            processes_by_parent[r["parent"]].append({"spec": r["pn"], "description": r["desc"]})
            supplies.setdefault(r["pn"], {"spec": r["pn"], "description": r["desc"], "cage": r["cage"], "used_by": []})
            if r["parent"] is not None:
                supplies[r["pn"]]["used_by"].append(rows_in[r["parent"]]["pn"])
            continue
        parent = rows_in[r["parent"]] if r["parent"] is not None else None
        # engineering blank / raw part of its parent (EBC72152 under C72152-1): material, not an item
        pbase = _key(re.sub(r"-\d+$", "", parent["pn"])) if parent else ""
        if parent and not parent["process"] and r["key"] != parent["key"] and len(pbase) >= 4 and \
                (pbase in r["key"] or r["key"] in parent["key"]) and \
                r["desc"].split()[:1] == parent["desc"].split()[:1]:
            processes_by_parent[r["parent"]].append({"spec": r["pn"], "description": f"{r['desc']} (blank)"})
            continue
        link = links.get(r["key"])
        if link is None and r["key"] in asm_links:
            link = asm_links[r["key"]]
        names = [r["desc"]] + ([link["step_name"]] if link and link.get("step_name") else [])
        occ_ids = (link or {}).get("occ_ids", [])
        geom = occ[occ_ids[0]].get("geometry") if occ_ids else None
        is_asm = bool(occ_ids) and occ[occ_ids[0]]["is_assembly"]
        rd = classifier.classify(r["pn"], names, geom, is_asm, manuf_prefix)
        if occ_ids and roles_by_occ.get(occ_ids[0], {}).get("role") not in (None, "part") and rd.role == "part":
            rd_role = roles_by_occ[occ_ids[0]]["role"]
        else:
            rd_role = rd.role
        if r["depth"] == 0:
            rd_role = "lru"
        sources = {"part_number": "MBOM", "nomenclature": "MBOM", "units_per_assy": "MBOM", "cage": "MBOM"}
        flags = []
        conf = 1.0
        if link:
            sources["3d"] = f"STEP {link['step_part_number']} ({link['status']})"
            conf = min(conf, float(link["confidence"] or 0))
            if link["status"] not in ("matched",):
                flags.append(f"3D link is '{link['status'].replace('_', ' ')}' — confirm {link['step_part_number']} is this part.")
            if link.get("quantity_match") is False:
                flags.append(f"Quantity differs: 3D has {link['step_qty']}, MBOM has {r['qty']}.")
            flags += [f for f in link["flags"] if "Quantity resolved" in f or "variant" in f.lower()][:1]
        elif not is_asm and r["depth"] >= 1:
            sources["3d"] = "not modelled"
            if ROLES[rd_role]["permanent"] is False and rd_role not in ("lockwire",) and (r["qty"] or 0) > 0:
                flags.append("Not in the 3D model — position and sequence come from the MBOM only.")
            conf = min(conf, 0.7)
        tq = torques.get(r["key"])
        if tq and rd_role in ("washer", "seal", "backup_ring", "insert", "press_plug", "plate"):
            # a torque applies to the threaded part of a stack; on a washer it means the drawing names a
            # fastener that is not in this MBOM (typically a drawing not yet updated to the new part number)
            stack = next((t for t in (drawing or {}).get("torques", []) if t.get("target_pn") and _key(t["target_pn"]) == r["key"]), {})
            others = [p for p in stack.get("stack_pns", []) if _key(p) != r["key"]]
            pending_flags = [f"The drawing gives a torque of {tq['nominal_nm']:g} N.m on this {rd_role}'s callout"
                             + (f" (with {', '.join(others)})" if others else "")
                             + ": the fastener it belongs to is not in this MBOM. Check the drawing revision."]
            tq = None
        else:
            pending_flags = []
        row = {
            "id": f"r{r['i']:03d}", "mbom_index": r["i"], "depth": r["depth"], "indent": r["depth"] + 1,
            "part_number": r["pn"], "cage": r["cage"], "nomenclature": kwd_nomenclature(r["desc"]),
            "nomenclature_mbom": r["desc"], "step_name": (link or {}).get("step_name"),
            "step_description": occ[occ_ids[0]]["description"] if occ_ids else None,
            "mbom_comments": r["comments"] or None,
            "units_per_assy": r["qty"] if r["qty"] is not None else (1 if r["depth"] == 0 else None),
            "uom": (r["uom"] or "ea").upper(), "role": rd_role, "role_rule": rd.rule,
            "attaching": ROLES[rd_role]["attaching"], "permanent": ROLES[rd_role]["permanent"],
            "discard_on_removal": ROLES[rd_role]["consumable_on_removal"],
            "equiv": r["equiv"], "seq": r["seq"], "effectivity": None, "alternate_of": None,
            "occ_ids": occ_ids, "disassembly_step": min((step_of.get(o, 10_000) for o in occ_ids), default=None),
            "unit": unit_of.get(occ_ids[0]) if occ_ids else None,
            "torque": tq, "parent_row": row_by_i[r["parent"]]["id"] if r["parent"] in row_by_i else None,
            "attaching_for": None, "processes": [], "notes": [],
            "supersession": parse_supersession(r["comments"]) if r["comments"] else None,
            "sources": sources, "confidence": round(conf, 2), "flags": flags, "status": "proposed",
            "fig": "1", "item": None,
        }
        row["flags"] += pending_flags
        if tq:
            sources["torque"] = f"drawing p{tq['evidence'][0]['page']}"
            if tq.get("conflict"):
                row["flags"].append("Drawing shows different torque values for this part — check the drawing.")
        if row["supersession"]:
            row["notes"].append(f"{row['supersession']['relation'].title()}: {row['supersession']['other_part_number']}")
        rows.append(row)
        row_by_i[r["i"]] = row
    for pi, plist in processes_by_parent.items():
        if pi in row_by_i:
            row_by_i[pi]["processes"] = plist

    # ---------------------------------------------------------------- variants (same parent + seq, J/K)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        if row["equiv"] and row["seq"]:
            groups[(row["parent_row"], row["seq"])].append(row)
    for g in groups.values():
        if len(g) < 2:
            continue
        g.sort(key=lambda x: (-(x["units_per_assy"] or 0), 0 if x["occ_ids"] else 1, x["equiv"] or ""))
        primary = g[0]
        modelled = next((x for x in g if x["occ_ids"]), None)
        if modelled is not None and modelled is not primary:
            primary["occ_ids"] = list(modelled["occ_ids"])
            primary["disassembly_step"] = modelled["disassembly_step"]
            primary["unit"] = modelled["unit"]
            primary["sources"]["3d"] = f"modelled as alternate {modelled['part_number']} ({modelled['equiv']})"
            primary["flags"] = [f for f in primary["flags"] if not f.startswith("Not in the 3D")]
        for row in g:
            row["effectivity"] = row["equiv"]
            row["variant_group"] = primary["id"]
            if row is not primary:
                row["alternate_of"] = primary["id"]
                row["flags"] = [f for f in row["flags"] if not f.startswith("Not in the 3D")]
                if not row["occ_ids"]:
                    row["sources"]["3d"] = f"alternate of {primary['part_number']}"

    # ---------------------------------------------------------------- attaching parts
    member_row: dict[str, dict] = {o: row for row in rows for o in row["occ_ids"]}
    neighbours: dict[str, set[str]] = defaultdict(set)
    for c in contacts:
        neighbours[c["a"]].add(c["b"])
        neighbours[c["b"]].add(c["a"])
    # the part each attaching occurrence holds, as the sequence worked it out (head side, not the threaded part)
    held_members: dict[str, list[str]] = {}
    for s in sequence.get("disassembly", []):
        t = s.get("attaches")
        if t:
            for o in s["members"]:
                held_members[o] = (units.get(t) or {}).get("members") or [t]

    def top_row(nr: dict) -> dict:
        while nr["parent_row"] and next(x for x in rows if x["id"] == nr["parent_row"])["depth"] >= 1:
            nr = next(x for x in rows if x["id"] == nr["parent_row"])
        return nr

    for row in rows:
        if not row["attaching"] or not row["occ_ids"]:
            continue
        targets: dict[str, int] = defaultdict(int)
        my_step = row["disassembly_step"] or 0
        for o in row["occ_ids"]:
            held = next((member_row[m] for m in held_members.get(o, []) if m in member_row
                         and member_row[m] is not row), None)
            if held is not None:
                targets[top_row(held)["id"]] += 1
                continue
            cands = []
            reach = set(neighbours[o])
            for n in list(reach):          # through the washers under this fastener
                nr = member_row.get(n)
                if nr and nr["role"] == "washer":
                    reach |= neighbours[n]
            reach.discard(o)
            for n in reach:
                nr = member_row.get(n)
                if not nr or nr is row or nr["attaching"] or nr["role"] in ("seal", "backup_ring"):
                    continue
                # climb to the top-level row of a sub-assembly member (the body inside GD69029 -> GD69029)
                while nr["parent_row"] and next(x for x in rows if x["id"] == nr["parent_row"])["depth"] >= 1:
                    nr = next(x for x in rows if x["id"] == nr["parent_row"])
                cands.append(nr)
            later = [c for c in cands if (c["disassembly_step"] or 10_000) > my_step]
            pick = min(later or cands, key=lambda c: (c["disassembly_step"] or 10_000), default=None)
            if pick:
                targets[pick["id"]] += 1
        if targets:
            row["attaching_for"] = max(targets, key=targets.get)
            row["sources"]["attaching_for"] = "3D contacts + sequence"
            if len(targets) > 1:
                # one part number, several parts held: the procedures split it per part; the IPL lists it once
                row["attaching_targets"] = dict(targets)
                row["flags"].append("Attaches more than one part (" + ", ".join(
                    f"{n} on {next((x['nomenclature'] for x in rows if x['id'] == t), t)}" for t, n in targets.items())
                    + "): listed under the one with the most.")

    # washers follow the fastener group they sit under
    for row in rows:
        if row["role"] == "washer" and row["occ_ids"]:
            fast = {}
            for o in row["occ_ids"]:
                for n in neighbours[o]:
                    nr = member_row.get(n)
                    if nr and nr["role"] in ("fastener", "nut"):
                        fast[nr["id"]] = nr
            if fast:
                f = next(iter(fast.values()))
                row["fastener_row"] = f["id"]
                if f.get("attaching_for"):
                    row["attaching_for"] = f["attaching_for"]

    # ---------------------------------------------------------------- order and item numbers
    by_id = {r["id"]: r for r in rows}
    children: dict[str | None, list[dict]] = defaultdict(list)
    for row in rows:
        if row["alternate_of"]:
            continue
        children[row["parent_row"]].append(row)

    def sort_key(row):
        s = row["disassembly_step"]
        if s is None:   # unmodelled: caps first, lock wire with its fasteners, the rest at the end
            s = {"cap": 0, "lockwire": 0.5}.get(row["role"], 9000)
        return (s, row["mbom_index"])

    ordered: list[dict] = []

    def emit_kids(row, attached_listed: set):
        for k in sorted(children.get(row["id"], []), key=sort_key):
            if k["attaching"] and k["attaching_for"]:
                continue                   # listed with the item it attaches
            emit(k, attached_listed)

    def emit(row, attached_listed: set):
        ordered.append(row)
        for alt in [r for r in rows if r["alternate_of"] == row["id"]]:
            ordered.append(alt)
            emit_kids(alt, attached_listed)
        emit_kids(row, attached_listed)
        att = [r for r in rows if r["attaching_for"] == row["id"] and not r["alternate_of"] and r["id"] not in attached_listed]
        att.sort(key=lambda r: (0 if r["role"] in ("fastener", "nut") else 1 if r["role"] == "washer" else 2, sort_key(r)))
        for a in att:
            attached_listed.add(a["id"])
            a["attaching_block"] = row["id"]
            ordered.append(a)
            for alt in [r for r in rows if r["alternate_of"] == a["id"]]:
                ordered.append(alt)
            emit_kids(a, attached_listed)

    tops = children.get(None, [])
    listed: set[str] = set()
    for t in tops:
        emit(t, listed)
    # any attaching part whose target was not found
    for row in rows:
        if row not in ordered:
            row["flags"].append("Placed at the end: no parent or attached item could be determined.")
            ordered.append(row)

    item = 0
    last_item: dict[str, str] = {}
    for row in ordered:
        if row["depth"] == 0:
            row["item"] = "-1"
            continue
        if row["alternate_of"]:
            base_item = last_item.get(row["alternate_of"], "?")
            n = sum(1 for r in ordered if r.get("alternate_of") == row["alternate_of"] and r["item"])
            row["item"] = f"{base_item}{chr(ord('A') + n)}"
            continue
        item += 10
        row["item"] = str(item)
        last_item[row["id"]] = row["item"]
    for i, row in enumerate(ordered):
        row["order"] = i

    # flags for confidence
    for row in ordered:
        if row["flags"]:
            row["confidence"] = min(row["confidence"], 0.8)

    unmatched_3d = [{"step_part_number": u["step_part_number"], "name": u.get("step_name"),
                     "occ_ids": step_to_occs.get(u["step_part_number"], []), "status": u["status"],
                     "flags": u.get("flags", [])} for u in reconciliation.get("unmatched_step", [])]
    for dlink in displaced:
        unmatched_3d.append({"step_part_number": dlink["step_part_number"], "name": dlink.get("step_name"),
                             "occ_ids": dlink["occ_ids"], "status": "displaced",
                             "flags": [f"The reconciliation proposed the same MBOM line as a better match "
                                       f"({dlink['status'].replace('_', ' ')}); link it to the right line."] + dlink["flags"][:1]})
    # structural fill: an unlinked 3D group whose STEP parent maps to an MBOM sub-assembly that has an
    # unlinked child of the same role and quantity is very likely that child
    by_rowkey = {r["id"]: r for r in rows}
    for u in unmatched_3d:
        if not u["occ_ids"] or u["status"] == "unmatched" and not u["name"]:
            continue
        parent_occ = occ[u["occ_ids"][0]].get("parent")
        prow = next((r for r in rows if parent_occ in r["occ_ids"]), None)
        if not prow:
            continue
        rd = classifier.classify(u["step_part_number"], [u["name"] or ""], None, False, manuf_prefix)
        cands = [r for r in rows if r["parent_row"] == prow["id"] and not r["occ_ids"] and not r["alternate_of"]
                 and r["role"] == rd.role and (r["units_per_assy"] or 0) == len(u["occ_ids"])]
        if len(cands) == 1:
            r = cands[0]
            r["occ_ids"] = list(u["occ_ids"])
            r["disassembly_step"] = min((step_of.get(o, 10_000) for o in r["occ_ids"]), default=None)
            r["sources"]["3d"] = f"STEP {u['step_part_number']} (structural candidate)"
            r["flags"] = [f for f in r["flags"] if not f.startswith("Not in the 3D")] + [
                f"3D item {u['step_part_number']} ({u['name']}) linked by position: same sub-assembly, role and "
                f"quantity ({len(u['occ_ids'])}). Its CAD part number differs — confirm."]
            r["confidence"] = min(r["confidence"], 0.6)
            u["status"] = "linked_structurally"
            u["linked_row"] = r["id"]
    unmatched_3d = [u for u in unmatched_3d if u["status"] != "linked_structurally"]
    return {
        "format": "studio.ipl/1",
        "figure": {"number": "1", "title": root_occ["name"] or rows[0]["nomenclature"],
                   "top_part_number": rows[0]["part_number"]},
        "rows": ordered,
        "supplies": sorted(supplies.values(), key=lambda s: s["spec"]),
        "unmatched_3d": unmatched_3d,
        "summary": {
            "rows": len(ordered), "with_3d": sum(1 for r in ordered if r["occ_ids"]),
            "flagged": sum(1 for r in ordered if r["flags"]), "with_torque": sum(1 for r in ordered if r["torque"]),
            "attaching": sum(1 for r in ordered if r["attaching_for"]),
            "alternates": sum(1 for r in ordered if r["alternate_of"]),
        },
    }
