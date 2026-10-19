"""Stable IPL item numbers across issues, modifications and variants.

A parts catalogue keeps an item's number for its whole life: a new issue or a variant must not renumber
every item after an added or removed part (that would change every procedure step that cites them, and
every reader's references). Given the baseline IPL, ``stabilize`` keeps each continuing part's item number,
gives new parts a free number between their neighbours (225 between 220 and 230; if no whole number is
free, a letter: 220B), never reuses a number a removed part retired, and lists the retired numbers.

Alternates keep the ATA convention: their main item's number plus a letter (30A).
"""
from __future__ import annotations

import re
from collections import defaultdict


def _key(pn: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (pn or "").upper().lstrip("."))


def _num(item: str | None) -> int | None:
    return int(item) if item and re.fullmatch(r"\d+", item) else None


def stabilize(ipl: dict, base: dict | None) -> dict:
    """Re-number ``ipl`` rows in place from ``base``. -> stats."""
    if not base or not base.get("rows"):
        return {"kept": 0, "new": [], "retired": []}
    rows = sorted(ipl["rows"], key=lambda r: r.get("order", 0))
    by_id = {r["id"]: r for r in rows}
    bparent = {r["id"]: r for r in base["rows"]}
    pool: dict[tuple, list] = defaultdict(list)          # (pn, parent pn) -> base items in order
    pool_pn: dict[str, list] = defaultdict(list)
    for b in sorted(base["rows"], key=lambda r: r.get("order", 0)):
        if not b.get("item") or b.get("depth") == 0:
            continue
        ppn = _key((bparent.get(b.get("parent_row")) or {}).get("part_number"))
        pool[(_key(b["part_number"]), ppn)].append(b["item"])
        pool_pn[_key(b["part_number"])].append(b["item"])
    used: set[str] = set()

    def take(lst: list) -> str | None:
        while lst:
            it = lst.pop(0)
            if it not in used:
                return it
        return None

    # 1. continuing parts keep their numbers (same part under the same parent first, then same part anywhere)
    for r in rows:
        if r.get("depth") == 0:
            continue
        ppn = _key((by_id.get(r.get("parent_row")) or {}).get("part_number"))
        it = take(pool[(_key(r["part_number"]), ppn)]) or take(pool_pn[_key(r["part_number"])])
        r["_kept"] = it
        if it:
            used.add(it)
    # a new part that takes the place of a removed part with the same name and role goes next to it
    def norm(x):
        return re.sub(r"[^A-Z]", "", (x or "").upper())
    retired_base = [b for b in base["rows"] if b.get("item") and b.get("depth") != 0 and b["item"] not in used
                    and not b.get("alternate_of") and _num(b["item"])]
    anchors: dict[str, int] = {}
    for r in rows:
        if r.get("depth") != 0 and not r["_kept"] and not r.get("alternate_of"):
            hit = next((b for b in retired_base if norm(b.get("nomenclature")) == norm(r.get("nomenclature"))
                        and b.get("role") == r.get("role")), None)
            if hit:
                anchors[r["id"]] = _num(hit["item"])
                retired_base.remove(hit)
    base_nums = sorted(_num(b["item"]) for b in base["rows"] if _num(b.get("item")))
    base_nums_set = set(base_nums)
    # 2. new main items: a free number between the neighbours that kept theirs
    mains = [r for r in rows if r.get("depth") != 0 and not r.get("alternate_of")]
    new = []
    for i, r in enumerate(mains):
        if r["_kept"] and not re.search(r"[A-Z]$", r["_kept"]):
            r["item"] = r["_kept"]
            continue
        if r["id"] in anchors:                  # replaces a removed part: right after its number
            prev = anchors[r["id"]]
            nxt = next((n for n in base_nums if n > prev), None)
        else:
            prev = next((_num(m["item"]) for m in reversed(mains[:i]) if _num(m.get("item"))), 0)
            nxt = next((_num(m["_kept"]) for m in mains[i + 1:] if _num(m.get("_kept"))), None)
        hi = nxt if nxt is not None else prev + 20
        cand = [n for n in range(prev + 1, hi) if str(n) not in used and n not in base_nums_set]   # never reuse a retired number
        nice = [n for n in cand if n % 5 == 0] or cand
        if nice:
            pick = str(min(nice, key=lambda n: abs(n - (prev + hi) / 2)))
        else:
            base_items = {b.get("item") for b in base["rows"]}
            pick = next(f"{prev}{c}" for c in "BCDEFGHJKLMNPRSTUVWXYZ" if f"{prev}{c}" not in used and f"{prev}{c}" not in base_items)
        r["item"] = pick
        used.add(pick)
        new.append({"row": r["id"], "item": pick, "part_number": r["part_number"],
                    **({"replaces_item": str(anchors[r["id"]])} if r["id"] in anchors else {})})
    # 3. alternates: main item number + letter
    letters = defaultdict(set)
    for r in rows:
        if r.get("alternate_of") and r["_kept"] and re.search(r"[A-Z]$", r["_kept"]):
            main = by_id.get(r["alternate_of"])
            if main and r["_kept"].rstrip("ABCDEFGHJKLMNPRSTUVWXYZ") == main.get("item"):
                r["item"] = r["_kept"]
                letters[r["alternate_of"]].add(r["_kept"][-1])
                continue
        if r.get("alternate_of"):
            r["_pending"] = True
    for r in rows:
        if r.pop("_pending", False):
            main = by_id.get(r["alternate_of"]) or {}
            c = next(c for c in "ABCDEFGHJKLMNPRSTUVWXYZ" if c not in letters[r["alternate_of"]])
            letters[r["alternate_of"]].add(c)
            r["item"] = f"{main.get('item', '?')}{c}"
            if not r["_kept"]:
                new.append({"row": r["id"], "item": r["item"], "part_number": r["part_number"]})
    for r in rows:
        r.pop("_kept", None)
    current = {r["item"] for r in rows}
    retired = sorted({b["item"] for b in base["rows"] if b.get("item") and b.get("depth") != 0} - current,
                     key=lambda x: (_num(re.sub(r"\D", "", x)) or 0, x))
    stats = {"kept": sum(1 for r in rows if r.get("depth") != 0) - len(new), "new": new, "retired": retired}
    ipl["item_numbers"] = stats
    return stats
