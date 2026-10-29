"""Readable removal order: each part with its attaching parts, not "every screw first, then every part".

The planner removes, layer by layer, everything that is free; inside a layer that puts all the screws of the
unit together. A maintenance manual works part by part: remove the attaching parts of the cover, remove the
cover (with its seals), then the next part. ``regroup`` keeps the planner's geometry (no step moves before a
part that blocks it) and reorders it that way:

* an attaching part (screw, nut, lock wire) belongs to the part it *holds*: the part under its head, not the
  part it threads into. With the screw's removal direction ``d`` (head side = +d), the threaded part is the
  neighbour it interferes with (the thread) or, failing that, the one that reaches deepest along -d. The held
  part is the earliest-removed of the others. A washer belongs to the part its screw holds;
* when the 3D contact between a screw and the part under its head was missed (tessellated countersinks, small
  modelling gaps), the parts whose bounding box the screw passes through are used instead, so the screw is not
  wrongly given to the base unit;
* when an attaching part comes off, the part it holds follows, with its other attaching parts, as soon as the
  geometry allows. A part is only started (its first screw out) when nothing but its own attaching parts
  blocks it;
* attaching parts that hold nothing removable (they hold the base unit) are not swept out together at the
  start: they come off when something needs them out, otherwise after the other parts;
* identical parts on the same level (same part number, same attaching and fitted parts, not sub-assemblies) are
  one entry: all their screws, then all their washers, then the parts ("Remove the six screws (70) and the six
  washers (80). Remove the two plugs (60).");
* the next part is looked for first among the parts touching the last ones taken off, so an area is finished
  before the next is started (the plug, then the spring and the valve behind it);
* otherwise the planner's order is kept.
"""
from __future__ import annotations

import re
from collections import defaultdict

import numpy as np

ATTACHING_ROLES = {"fastener", "nut", "washer", "lockwire"}
NOT_HELD = {"seal", "backup_ring"}
BIG = 10**9


def _depth_lo(box, d: np.ndarray) -> float:
    """Lowest point of an axis-aligned box along direction d."""
    lo, hi = np.asarray(box[:3], float), np.asarray(box[3:], float)
    return float(np.sum(np.minimum(lo * d, hi * d)))


def _overlap(a, b, tol: float) -> bool:
    return all(a[i] - tol <= b[i + 3] and b[i] - tol <= a[i + 3] for i in range(3))


def attaching_targets(steps: list[dict], units: dict[str, dict], contacts: list[dict], base: str,
                      boxes: dict[str, list[float]] | None = None, info: dict | None = None) -> dict[str, str]:
    """unit id of an attaching part -> unit id of the part it holds (the base unit included).

    ``info``, when given, receives ``threads`` (attaching part -> the part it threads into, when that is not
    the part it holds) and ``nb`` (the unit-level contact graph)."""
    boxes = boxes or {}
    threads: dict[str, str] = {}
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
    direction = {s["unit"]: np.asarray(s.get("direction") or (0, 0, 1), float) for s in steps}
    nb: dict[str, set[str]] = defaultdict(set)
    interferes: set[tuple[str, str]] = set()
    for c in contacts:
        a, b = unit_of.get(c["a"]), unit_of.get(c["b"])
        if a and b and a != b:
            nb[a].add(b)
            nb[b].add(a)
            if c.get("kind") == "interference":
                interferes.add((a, b))
                interferes.add((b, a))

    def holdable(n: str) -> bool:
        return role.get(n) not in ATTACHING_ROLES | NOT_HELD

    def choose(u: str, cands: set[str]) -> str | None:
        """The part ``u`` holds among the neighbours ``cands`` (all removed after it, or the base)."""
        later = sorted((n for n in cands if order.get(n, BIG) > order[u]), key=lambda n: (order.get(n, BIG), n))
        if not later:
            return None
        if len(later) == 1:
            return later[0]
        d = direction[u]
        thr = [n for n in later if (u, n) in interferes]
        thread = thr[0] if len(thr) == 1 else None
        if thread is None and all(n in boxes for n in later):
            thread = min(later, key=lambda n: _depth_lo(boxes[n], d))
        clamped = [n for n in later if n != thread] or later
        nonbase = [n for n in clamped if n != base]
        if nonbase:
            if thread is not None:
                threads[u] = thread
            return nonbase[0]                       # earliest removed of the clamped stack
        if thread is not None and thread != base:
            threads[u] = clamped[0]                 # held against the (base) part it is clamped to
            return thread                           # screwed in from the base side: it holds what it threads into
        return clamped[0]

    def by_overlap(u: str) -> set[str]:
        bu = boxes.get(u)
        if bu is None:
            return set()
        return {n for n, bn in boxes.items() if n != u and n in role and holdable(n) and _overlap(bu, bn, 0.5)}

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
        held = choose(u, {n for n in reach if holdable(n)})
        if held is None or held == base:
            # the contact under the head may have been missed: look at what the screw passes through
            alt = choose(u, by_overlap(u) | ({base} if held == base else set()))
            if alt is not None and alt != base:
                held = alt
        if held is not None:
            out[u] = held
    for s in steps:                                 # washers follow their screw
        u = s["unit"]
        if s["role"] == "washer":
            screws = sorted((n for n in nb[u] if n in out), key=lambda n: order.get(n, BIG))
            if screws:
                out[u] = out[screws[0]]
            else:
                held = choose(u, {n for n in nb[u] if holdable(n)})
                if held is not None:
                    out[u] = held
    if info is not None:
        info["threads"] = {u: t for u, t in threads.items() if out.get(u) and t != out[u]}
        info["nb"] = nb
    return out


def regroup(seq: dict, contacts: list[dict], boxes: dict[str, list[float]] | None = None) -> dict:
    """Reorder ``seq['disassembly']`` part by part (see the module docstring). In place; returns seq.

    ``boxes`` (unit id -> [xmin, ymin, zmin, xmax, ymax, zmax] of what the unit moves) lets the held part be
    found from the geometry; without it only the contacts are used."""
    steps = seq.get("disassembly") or []
    if len(steps) < 3:
        return seq
    units = {u["id"]: u for u in seq.get("units", [])}
    base = seq.get("base_unit")
    info: dict = {}
    att = attaching_targets(steps, units, contacts, base, boxes, info)
    nb: dict[str, set[str]] = info.get("nb", {})
    blockers: dict[str, set[str]] = defaultdict(set)
    for p in seq.get("precedence", []):
        blockers[p["then"]].add(p["first"])
    blocks: dict[str, set[str]] = defaultdict(set)
    for u, bs in blockers.items():
        for b in bs:
            blocks[b].add(u)
    held_by: dict[str, list[str]] = defaultdict(list)       # part -> its attaching parts, in planner order
    for s in steps:
        if s["unit"] in att:
            held_by[att[s["unit"]]].append(s["unit"])

    by_unit = {s["unit"]: s for s in steps}
    planner_pos = {s["unit"]: i for i, s in enumerate(steps)}
    remaining = [s["unit"] for s in steps]
    done: set[str] = set()
    out: list[str] = []
    batch: list[str] = []                       # the part(s) being taken off now: one part, or identical twins

    def pn(u: str) -> str:
        """Part number of a single-part unit (occurrence id without its _n suffix)."""
        return re.sub(r"_\d+$", "", u)

    def signature(u: str) -> tuple | None:
        """What makes two parts 'the same entry': one part, same part number and role, same attaching parts
        and same fitted parts by part number. Sub-assemblies are never merged."""
        s = by_unit.get(u)
        if s is None or len(s["members"]) != 1:
            return None
        return (pn(u), s["role"], tuple(sorted(pn(a) for a in held_by.get(u, []))),
                tuple(sorted(pn(r) for r in s.get("riders", []))))

    def ready(u: str) -> bool:
        return all(b in done or b not in by_unit for b in blockers[u])

    def nearly_ready(t: str) -> bool:
        """The part could come off once its own attaching parts are out."""
        own = set(held_by.get(t, []))
        return t in done or all(b in done or b in own or b not in by_unit for b in blockers[t])

    def needed(u: str) -> bool:
        """Something other than the part ``u`` holds (and its attaching parts) waits for ``u`` to come out."""
        t = att.get(u)
        return any(x not in done and x != t and att.get(x) != t for x in blocks[u])

    def open_batch(t: str) -> list[str]:
        """Part ``t`` and its identical twins that can come off now too (the two plugs (60) with their screws):
        a manual removes them as one entry, "Remove the six screws ... Remove the two plugs"."""
        sig = signature(t)
        if sig is None:
            return [t]
        twins = [q for q in remaining if q != t and q not in att and signature(q) == sig and nearly_ready(q)]
        return [t] + twins

    recent: list[str] = []                      # the last parts (not attaching parts, not the base) taken off

    def take(u: str) -> None:
        out.append(u)
        remaining.remove(u)
        done.add(u)
        if u not in att and u != base:
            recent[:] = [u] + [x for x in recent if x != u][:3]

    def att_rank(a: str) -> tuple:
        return (1 if by_unit[a]["role"] == "washer" else 0, planner_pos[a])

    while remaining:
        pick = None
        if batch:
            batch = [t for t in batch if t in remaining or any(a in remaining for a in held_by.get(t, []))]
            pend = [a for t in batch for a in held_by.get(t, []) if a in remaining]
            ready_att = sorted((a for a in pend if ready(a)), key=att_rank)   # all screws, then all washers
            if ready_att:
                pick = ready_att[0]
            if pick is None:
                pick = next((t for t in batch if t in remaining and ready(t)
                             and not any(a in remaining for a in held_by.get(t, []))), None)
            if pick is None:
                # attaching parts in the way that belong to nothing removable (screws given to the base)
                pick = next((b for t in batch if t in remaining for b in remaining if b in blockers[t] and ready(b)
                             and by_unit[b]["role"] in ATTACHING_ROLES and att.get(b, base) in (base, t)), None)
            if pick is None:                         # finished, or held up by another part: carry on elsewhere
                batch = []
        if pick is None:
            # finish the area being worked on: first what touches the last parts taken off (the spring and the
            # valve in the bore the plug closed), then the rest in the planner's order
            near = [u for x in recent for u in remaining if u in nb.get(x, ())]     # the latest part's first
            near = list(dict.fromkeys(near))
            for u in near + [u for u in remaining if u not in near]:
                if not ready(u):
                    continue
                t = att.get(u)
                if t is not None:                    # an attaching part
                    if t == base or t not in by_unit:
                        if needed(u):
                            pick = u
                            waiting = [x for x in remaining if x in blocks[u] and x != u]
                            batch = [waiting[0]] if waiting else []
                            break
                        continue                     # holds nothing that comes off: later
                    if nearly_ready(t) or needed(u):
                        pick, batch = u, open_batch(t)
                        break
                    continue                         # its part is still blocked by something else
                own = [a for a in held_by.get(u, []) if a in remaining]
                if own:
                    first = next((a for a in own if ready(a)), None)
                    if first and nearly_ready(u):
                        pick, batch = first, open_batch(u)
                        break
                    continue
                pick = u
                twins = open_batch(u)
                batch = [t for t in twins if t != u]  # identical loose parts follow it (the three bobbins)
                break
        if pick is None:                             # only deferred or waiting steps left
            pick = next((u for u in remaining if ready(u)), remaining[0])
            t = att.get(pick)
            if t and t != base and t in remaining:
                batch = open_batch(t)
        take(pick)

    new = []
    for i, u in enumerate(out, start=1):
        s = dict(by_unit[u], step=i)
        if u in att:
            s["attaches"] = att[u]
            if u in info.get("threads", {}):
                s["threads_into"] = info["threads"][u]
        if held_by.get(u):
            s["attached_by"] = list(held_by[u])
        new.append(s)
    seq["planner_order"] = [s["unit"] for s in steps]
    seq["disassembly"] = new
    seq["grouping"] = "part by part: each part after its attaching parts"
    return seq
