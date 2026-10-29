"""STE sentences for process operations (ASD-STE100 style).

Deterministic templates: one instruction per sentence, approved verbs at the start
(REMOVE, INSTALL, DISCARD, TORQUE, APPLY, SAFETY, DO, MAKE SURE), at most 20 words,
item numbers in parentheses, quantities in words up to ten, torque in N.m with lbf.in.

Names come from the IPL nomenclature reduced to the basic noun phrase a technician reads
("SCREW NAS1351N4-20 .2500-28UNRF" -> "screw"; "VALVE, PILOT SOLENOID" -> "pilot solenoid
valve"). Projects can override names per part number (``names`` in the procedure options).
"""
from __future__ import annotations

import re

WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
ROLE_NOUN = {"fastener": "screw", "washer": "washer", "nut": "nut", "seal": "packing", "backup_ring": "back-up ring",
             "insert": "insert", "press_plug": "expansion plug", "cap": "protective cap", "lockwire": "safety cable",
             "spring": "spring", "bush": "bush", "dowel": "dowel pin"}
IRREGULAR = {"bush": "bushes", "packing": "packings", "body": "bodies", "assembly": "assemblies"}


def plural(noun: str) -> str:
    last = noun.split()[-1]
    if last in IRREGULAR:
        return noun[: -len(last)] + IRREGULAR[last]
    if last.endswith(("s", "x", "ch", "sh")):
        return noun + "es"
    if last.endswith("y") and last[-2:-1] not in "aeiou":
        return noun[:-1] + "ies"
    return noun + "s"


def qty_word(n: int | None) -> str:
    if not n:
        return ""
    return WORDS.get(n, str(n))


def clean_name(item: dict, names: dict[str, str] | None = None) -> str:
    """Basic noun phrase for running text."""
    names = names or {}
    if item.get("part_number") in names:
        return names[item["part_number"]]
    role = item.get("role")
    nom = (item.get("nomenclature") or "").upper()
    if role in ("fastener",) and re.search(r"\bBOLT\b", nom):
        return "bolt"
    if role == "seal":
        if "O RING" in nom or "O-RING" in nom or "PACKING" in nom:
            return "packing"
        if "DYNAMIC" in nom:
            return "dynamic seal"
        return "seal"
    if role in ROLE_NOUN and role not in ("cap",):
        return ROLE_NOUN[role]
    # "VALVE, PILOT SOLENOID" -> "pilot solenoid valve"; drop part numbers / thread specs
    parts = [p.strip() for p in nom.split(",")]
    phrase = " ".join(parts[1:] + parts[:1]) if len(parts) > 1 else parts[0]
    toks = [t for t in phrase.split() if not re.search(r"\d", t) and t not in ("WITH", "PNR", "ASSY")]
    phrase = " ".join(toks).lower().replace("equiped", "equipped")
    if role == "housing" and phrase == "body equipped":
        phrase = "equipped body"
    if phrase in ("bague",):
        phrase = "bush"
    return phrase or (item.get("step_name") or "part").lower()


def group_ref(items: list[dict], names: dict | None = None) -> str:
    """Parts of the same kind together: 'the inserts (160, 350, 400) and the expansion plugs (380, 390)'.
    For long lists (notes about parts that stay installed); quantities are left to the IPL."""
    groups: dict[str, list[str]] = {}
    for it in items:
        groups.setdefault(clean_name(it, names), []).append(str(it["item"]))
    out = []
    for noun, nums in groups.items():
        many = len(nums) > 1 or any((it.get("count") or it.get("qty") or 1) > 1 for it in items if clean_name(it, names) == noun)
        out.append(f"the {plural(noun) if many else noun} ({', '.join(nums)})")
    return out[0] if len(out) == 1 else ", ".join(out[:-1]) + " and " + out[-1]


def ref(items: list[dict], names: dict | None = None, the: bool = True) -> str:
    """'the six screws (220) and the six washers (230)'"""
    out = []
    for it in items:
        n = it.get("count") or it.get("qty") or 1
        noun = clean_name(it, names)
        noun = plural(noun) if n > 1 else noun
        q = qty_word(n) if n > 1 else ""
        art = "the " if the else ""
        out.append(f"{art}{q + ' ' if q else ''}{noun} ({it['item']})")
    if len(out) <= 1:
        return out[0] if out else ""
    return ", ".join(out[:-1]) + " and " + out[-1]


def torque_text(t: dict) -> str:
    if t["min_nm"] == t["max_nm"]:
        return f"{_n(t['min_nm'])} N.m ({_n(t['min_lbfin'])} lbf.in)"
    return f"{_n(t['min_nm'])} to {_n(t['max_nm'])} N.m ({_n(t['min_lbfin'])} to {_n(t['max_lbfin'])} lbf.in)"


def _n(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def target(it: dict | None, names: dict | None = None) -> str:
    """'the plugs (210)' — plural when the operation acts on several of them."""
    if not it:
        return ""
    n = it.get("count") or 1
    noun = clean_name(it, names)
    return f"the {plural(noun) if n > 1 else noun} ({it['item']})"


def new_ref(items: list[dict], names: dict | None = None) -> str:
    """'two new packings (260)' / 'a new dynamic seal (300)'"""
    out = []
    for it in items:
        n = it.get("count") or 1
        noun = clean_name(it, names)
        out.append(f"{qty_word(n)} new {plural(noun)} ({it['item']})" if n > 1 else f"a new {noun} ({it['item']})")
    return out[0] if len(out) == 1 else ", ".join(out[:-1]) + " and " + out[-1]


def supply_ref(s: dict) -> str:
    if s.get("name"):
        return f"{s['name'].strip().lower()} ({s['spec']})"
    d = (s.get("description") or s.get("name") or "").strip().lower()
    d = re.sub(r"\bprevent\.", "preventive", d)
    return f"{d} ({s['spec']})" if d else s["spec"]


MAX_WORDS = 20


def _words(t: str) -> int:
    return len(t.split())


def new_group_ref(items: list[dict], names: dict | None = None) -> str:
    """'new packings (750, 490) and new back-up rings (100, 760)' — quantities are left to the IPL."""
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(clean_name(it, names), []).append(it)
    out = []
    for noun, its in groups.items():
        many = len(its) > 1 or any((i.get("count") or 1) > 1 for i in its)
        out.append(f"new {plural(noun)} ({', '.join(str(i['item']) for i in its)})" if many
                   else f"a new {noun} ({its[0]['item']})")
    return out[0] if len(out) == 1 else ", ".join(out[:-1]) + " and " + out[-1]


def item_sentences(verb: str, items: list[dict], tail: str = "", names: dict | None = None,
                   new: bool = False) -> list[str]:
    """'Remove the six screws (220) and the washers (230) from the cover (10).' — and for long lists,
    the same kinds together, split into several sentences so each keeps to the STE length (20 words)."""
    if not items:
        return []
    one_ref, grp_ref = (new_ref, new_group_ref) if new else (ref, group_ref)
    one = f"{verb} {one_ref(items, names)}{tail}."
    if len(items) <= 3 and _words(one) <= MAX_WORDS:
        return [one]
    grouped = f"{verb} {grp_ref(items, names)}{tail}."
    if _words(grouped) <= MAX_WORDS:
        return [grouped]
    kinds: dict[str, list[dict]] = {}
    for it in items:
        kinds.setdefault(clean_name(it, names), []).append(it)
    out, cur = [], []
    for grp in kinds.values():
        trial = cur + grp
        if cur and _words(f"{verb} {grp_ref(trial, names)}{tail}.") > MAX_WORDS:
            out.append(f"{verb} {grp_ref(cur, names)}{tail}.")
            cur = list(grp)
        else:
            cur = trial
    if cur:
        out.append(f"{verb} {grp_ref(cur, names)}{tail}.")
    return out


def sentences_for(op: dict, names: dict | None = None) -> list[dict]:
    """Return [{text, kind}] for one operation. kind: step | note | warning | caution."""
    t = op["type"]
    items = op.get("items", [])
    out: list[dict] = []
    frm = op.get("from")
    on = op.get("on")
    plural_items = sum((i.get("count") or 1) for i in items) > 1 or len(items) > 1
    them = "them" if plural_items else "it"
    if t == "unfasten":
        tgt = f" from {target(frm, names)}" if frm else ""
        out += [{"text": x, "kind": "step"} for x in item_sentences("Remove", items, tgt, names)]
    elif t == "remove":
        tgt = f" from {target(frm, names)}" if frm else ""
        out += [{"text": x, "kind": "step"} for x in item_sentences("Remove", items, tgt, names)]
        if op.get("discard"):
            out += [{"text": x, "kind": "step"} for x in item_sentences("Discard", items, "", names)]
    elif t == "remove_from":
        out += [{"text": x, "kind": "step"} for x in item_sentences("Remove", items, f" from {target(frm, names)}", names)]
        disc = [i for i in items if i["row_id"] in op.get("discard_items", [])]
        if disc:
            out += [{"text": x, "kind": "step"} for x in item_sentences("Discard", disc, "", names)]
    elif t == "do_not_remove":
        what = ref(items, names) if len(items) <= 2 else group_ref(items, names)
        out.append({"text": f"Do not remove {what} {op.get('condition', '')}.".replace("  ", " ").replace(" .", "."),
                    "kind": "note"})
    elif t == "do_not_disassemble":
        what = ref(items, names) if len(items) <= 3 else group_ref(items, names)
        out.append({"text": f"Do not disassemble {what} further.", "kind": "note"})
    elif t == "install_on":
        new = [i for i in items if i["row_id"] in op.get("new_items", [])]
        rest = [i for i in items if i not in new]
        if new:
            out += [{"text": x, "kind": "step"} for x in item_sentences("Install", new, f" on {target(on, names)}", names, new=True)]
        if rest:
            out += [{"text": x, "kind": "step"} for x in item_sentences("Install", rest, f" on {target(on, names)}", names)]
    elif t == "install":
        tgt = f" in {target(on, names)}" if on else ""
        out += [{"text": x, "kind": "step"} for x in item_sentences("Install", items, tgt, names, new=bool(op.get("new_parts")))]
        for r in op.get("references") or []:
            out.append({"text": f"Refer to {r['spec']} for the installation.", "kind": "step"})
        if op.get("torque"):
            out.append({"text": f"Torque {ref(items, names)} to {torque_text(op['torque'])}.", "kind": "step"})
    elif t == "fasten":
        sup = op.get("supplies") or []
        if sup:
            out.append({"text": f"Apply {supply_ref(sup[0])} to {ref(items, names)}.", "kind": "step"})
        if on:
            out.append({"text": f"Attach {target(on, names)} with {ref(items, names)}.", "kind": "step"})
        else:
            out.append({"text": f"Install {ref(items, names)}.", "kind": "step"})
        if op.get("torque"):
            fast = [i for i in items if i["role"] in ("fastener", "nut")] or items[:1]
            out.append({"text": f"Torque {ref(fast, names)} to {torque_text(op['torque'])}.", "kind": "step"})
    elif t == "apply":
        sup = (op.get("supplies") or [{}])[0]
        its = op.get("on_items") or []
        heads = [i for i in its if i["role"] in ("fastener", "nut")]
        rest = [i for i in its if i not in heads]
        where = []
        if heads:
            where.append("on the heads of " + ref([{**i, "count": 0} for i in heads], names))
        if rest:
            where.append("on " + ref([{**i, "count": 0} for i in rest], names))
        what = supply_ref(sup) if sup else op.get("what", "sealant")
        out.append({"text": f"Apply {what} {' and '.join(where)}.".replace("  ", " "), "kind": "step"})
    elif t == "lockwire":
        spec = ", ".join(op.get("spec") or [])
        out.append({"text": f"Safety the parts shown on the drawing with lock wire or safety cable ({spec}).", "kind": "step"})
    elif t == "test":
        out.append({"text": "Do the acceptance test procedure (ATS).", "kind": "step"})
    elif t == "apply_after_test":
        spec = ", ".join(op.get("spec") or [])
        its = op.get("items") or []
        where = f"the holes of {ref(its, names)}" if its else "the Lee plug holes"
        out.append({"text": f"After the test, fill {where} with thick sealant ({spec}).", "kind": "step"})
    else:
        out.append({"text": op.get("text") or t, "kind": "note"})
    return out
