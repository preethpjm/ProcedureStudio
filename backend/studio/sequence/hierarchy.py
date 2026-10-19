"""Readable removal order: each part with its attaching parts, not "every screw first, then every part".

The planner removes, layer by layer, everything that is free; inside a layer that puts all the screws of the
unit together. A maintenance manual works part by part: remove the attaching parts of the cover, remove the
cover (with its seals), then the next part. ``regroup`` keeps the planner's geometry (no step moves before a
part that blocks it) and reorders it that way:

* an attaching part (screw, nut, washer, lock wire) belongs to the part it holds: the neighbour that is removed
  after it (the first one; the base unit when nothing else is);
* when an attaching part comes off, the part it holds follows, with its other attaching parts, as soon as the
  geometry allows;
* otherwise the planner's order is kept.
"""
from __future__ import annotations

from collections import defaultdict

ATTACHING_ROLES = {"fastener", "nut", "washer", "lockwire"}
NOT_HELD = {"seal", "backup_ring"}


def attaching_targets(steps: list[dict], units: dict[str, dict], contacts: list[dict], base: str) -> dict[str, str]:
    """unit id of an attaching part -> unit id of the part it holds (the base unit included)."""
    unit_of: dict[str, str] = {}
    for u in units.values():
        top = u["id"]
        while units.get(top, {}).get("carrier"):
            top = units[top]["carrier"]
        for m in u["members"]:
            unit_of[m] = top
    order = {s["unit"]: s["step"] for s in steps}
    role = {s["unit"]: s["role"] for s in steps}
    role[base] = units.get(base, {}).get("role", "housing")
    nb: dict[str, set[str]] = defaultdict(set)
    for c in contacts:
        a, b = unit_of.get(c["a"]), unit_of.get(c["b"])
        if a and b and a != b:
            nb[a].add(b)
            nb[b].add(a)
    out: dict[str, str] = {}
    for s in steps:
        u = s["unit"]
        if s["role"] not in ATTACHING_ROLES - {"washer"}:
            continue
        reach = set(nb[u])
        for n in list(reach):                       # through the washers under a screw head
            if role.get(n) == "washer":
                reach |= nb[n]
        reach.discard(u)
        cands = [n for n in reach if role.get(n) not in ATTACHING_ROLES | NOT_HELD]
        later = [n for n in cands if order.get(n, 10**9) > s["step"]]
        if later:
            out[u] = min(later, key=lambda n: order.get(n, 10**9))
    for s in steps:                                 # washers follow their screw
        u = s["unit"]
        if s["role"] == "washer":
            screws = [n for n in nb[u] if n in out]
            if screws:
                out[u] = out[screws[0]]
            else:
                later = [n for n in nb[u] if role.get(n) not in ATTACHING_ROLES | NOT_HELD and order.get(n, 10**9) > s["step"]]
                if later:
                    out[u] = min(later, key=lambda n: order.get(n, 10**9))
    return out


def regroup(seq: dict, contacts: list[dict]) -> dict:
    """Reorder ``seq['disassembly']`` part by part (see the module docstring). In place; returns seq."""
    steps = seq.get("disassembly") or []
    if len(steps) < 3:
        return seq
    units = {u["id"]: u for u in seq.get("units", [])}
    base = seq.get("base_unit")
    att = attaching_targets(steps, units, contacts, base)
    blockers: dict[str, set[str]] = defaultdict(set)
    for p in seq.get("precedence", []):
        blockers[p["then"]].add(p["first"])
    held_by: dict[str, list[str]] = defaultdict(list)       # part -> its attaching parts, in planner order
    for s in steps:
        if s["unit"] in att:
            held_by[att[s["unit"]]].append(s["unit"])

    by_unit = {s["unit"]: s for s in steps}
    remaining = [s["unit"] for s in steps]
    done: set[str] = set()
    out: list[str] = []
    open_part: str | None = None

    def ready(u: str) -> bool:
        return all(b in done or b not in by_unit for b in blockers[u])

    blocks: dict[str, set[str]] = defaultdict(set)
    for u, bs in blockers.items():
        for b in bs:
            blocks[b].add(u)

    def nearly_ready(t: str) -> bool:
        """The part could come off once its own attaching parts are out."""
        own = set(held_by.get(t, []))
        return t == base or t in done or all(b in done or b in own or b not in by_unit for b in blockers[t])

    def can_start(u: str) -> bool:
        """Attaching parts wait for their part, unless something else needs them out first."""
        t = att.get(u)
        if not t or nearly_ready(t):
            return True
        return any(x not in done and att.get(x) != t and x != t for x in blocks[u])

    while remaining:
        pick = None
        if open_part:
            pick = next((a for a in held_by[open_part] if a in remaining and ready(a)), None)
            if pick is None and open_part in remaining and ready(open_part) and \
                    not any(a in remaining for a in held_by[open_part]):
                pick = open_part
            if pick is None and (open_part not in remaining or open_part == base):
                rest = [a for a in held_by[open_part] if a in remaining and ready(a)]
                pick = rest[0] if rest else None
                if pick is None:
                    open_part = None
        if pick is None:
            pick = next((u for u in remaining if ready(u) and can_start(u)), None) or \
                next((u for u in remaining if ready(u)), remaining[0])
            tgt = att.get(pick)
            if tgt and tgt not in done:
                open_part = tgt
            elif held_by.get(pick) and any(a in remaining for a in held_by[pick]):
                # a part whose own attaching parts are still in: they come first if the geometry allows
                first = next((a for a in held_by[pick] if a in remaining and ready(a)), None)
                if first:
                    open_part, pick = pick, first
        out.append(pick)
        remaining.remove(pick)
        done.add(pick)
        if pick == open_part:
            open_part = None

    new = []
    for i, u in enumerate(out, start=1):
        s = dict(by_unit[u], step=i)
        if u in att:
            s["attaches"] = att[u]
        if held_by.get(u):
            s["attached_by"] = list(held_by[u])
        new.append(s)
    seq["planner_order"] = [s["unit"] for s in steps]
    seq["disassembly"] = new
    seq["grouping"] = "part by part: each part after its attaching parts"
    return seq
