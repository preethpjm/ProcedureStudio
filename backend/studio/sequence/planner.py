"""Disassembly sequence planning from 3D geometry ("assembly by disassembly").

1. Units: each child of the root is a unit (a STEP sub-assembly moves as one rigid unit).
2. Riders: permanent parts (inserts, Lee plugs, bonded plates) ride with the unit they are
   fixed to; seals and back-up rings ride with the part they sit on (their coaxial carrier).
   Reference markers are ignored.
3. Base: the unit with the largest volume stays put.
4. Layers: a unit is *free* when it can translate along some candidate direction without
   hitting any remaining unit. Candidate directions are the six model axes plus the unit's
   own cylinder axes and the shared axes with its neighbours (so a screw comes out along its
   own axis even when that axis is not a model axis).

   The free test is a directional blocking test on the meshes: rays are cast from the moving
   group's front-facing surface (normal · d > 0.25) along d; any hit on another remaining
   unit blocks the move. Rays that start inside another part (threads, press fits) always
   hit it, so buried parts are correctly blocked sideways.
5. All units free at the start of a layer are removed in that layer, in role order
   (caps → lock wire → fasteners → washers → closures → fittings → …). If nothing is free,
   the unit with the fewest blockers is removed and the step is flagged as a conflict for
   the writer to review.

Assembly is the reverse of disassembly. The result is a proposal: the writer can reorder
it in the UI, and the precedence list says which moves the geometry forbids.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger("studio.sequence")

ROLE_ORDER = ["cap", "lockwire", "fastener", "nut", "washer", "closure", "fitting", "lru", "bush", "spring",
              "moving", "seal", "backup_ring", "part", "housing"]
AXES = {"+X": (1, 0, 0), "-X": (-1, 0, 0), "+Y": (0, 1, 0), "-Y": (0, -1, 0), "+Z": (0, 0, 1), "-Z": (0, 0, -1)}
RIDER_ROLES_CARRIED = {"seal", "backup_ring"}
PERMANENT_ROLES = {"insert", "press_plug", "plate"}


@dataclass
class Unit:
    id: str
    members: list[str]
    role: str
    volume: float
    riders: list[str] = field(default_factory=list)       # occ_ids of riders (parts)
    rider_units: list[str] = field(default_factory=list)  # unit ids riding on this unit
    carrier: str | None = None

    def moving(self) -> list[str]:
        return self.members + self.riders


def _label(d: np.ndarray) -> str:
    best = max(AXES.items(), key=lambda kv: float(np.dot(d, kv[1])))
    if float(np.dot(d, best[1])) > 0.999:
        return best[0]
    return "axis(" + ", ".join(f"{v:.2f}" for v in d) + ")"


class _MeshCache:
    def __init__(self, meshes: dict):
        import trimesh
        self.trimesh = trimesh
        self.meshes = meshes
        self._tm: dict[str, object] = {}
        self._ray: dict[str, object] = {}

    def group(self, key: str, occs: list[str]):
        if key not in self._tm:
            vs, fs, base = [], [], 0
            for o in occs:
                if o not in self.meshes:
                    continue
                v, f = self.meshes[o]
                if len(f) == 0:
                    continue
                vs.append(v)
                fs.append(f + base)
                base += len(v)
            if not vs:
                self._tm[key] = None
            else:
                self._tm[key] = self.trimesh.Trimesh(np.vstack(vs), np.vstack(fs), process=False)
        return self._tm[key]

    def ray(self, key: str, occs: list[str]):
        if key not in self._ray:
            m = self.group(key, occs)
            if m is None:
                self._ray[key] = None
            else:
                from trimesh.ray.ray_triangle import RayMeshIntersector
                self._ray[key] = RayMeshIntersector(m)
        return self._ray[key]


def _aabb(model_by_id: dict, occs: list[str]) -> np.ndarray | None:
    bs = [model_by_id[o]["geometry"]["bbox"] for o in occs if (model_by_id[o].get("geometry") or {}).get("bbox")]
    if not bs:
        return None
    b = np.array(bs)
    return np.concatenate([b[:, :3].min(0), b[:, 3:].max(0)])


def _swept(b: np.ndarray, d: np.ndarray, length: float) -> np.ndarray:
    lo, hi = b[:3].copy(), b[3:].copy()
    lo2, hi2 = lo + d * length, hi + d * length
    return np.concatenate([np.minimum(lo, lo2), np.maximum(hi, hi2)])


def _overlap(a: np.ndarray, b: np.ndarray, tol: float = 0.01) -> bool:
    return bool(np.all(a[:3] - tol <= b[3:]) and np.all(b[:3] - tol <= a[3:]))


def build_units(model: dict, roles: dict[str, dict], contacts: list[dict]) -> tuple[dict[str, Unit], str, list[str]]:
    by_id = {o["occ_id"]: o for o in model["occurrences"]}
    root = by_id[model["root"]]

    def leaves(occ_id: str) -> list[str]:
        o = by_id[occ_id]
        if not o["is_assembly"]:
            return [occ_id]
        out = []
        for c in o["children"]:
            out += leaves(c)
        return out

    units: dict[str, Unit] = {}
    ignored: list[str] = []
    for cid in root["children"]:
        role = roles.get(cid, {}).get("role", "part")
        if role in ("marker", "process"):
            ignored.append(cid)
            continue
        mem = [m for m in leaves(cid) if roles.get(m, {}).get("role") not in ("marker", "process")]
        vol = sum((by_id[m].get("geometry") or {}).get("volume_mm3") or 0 for m in mem)
        units[cid] = Unit(cid, mem, role, vol)
    if not units:
        raise ValueError("no physical parts under the root assembly")
    base = max(units.values(), key=lambda u: u.volume).id

    member_unit = {m: u.id for u in units.values() for m in u.members}
    nb: dict[str, list[tuple[str, dict]]] = {}
    for c in contacts:
        ua, ub = member_unit.get(c["a"]), member_unit.get(c["b"])
        if ua and ub and ua != ub:
            nb.setdefault(ua, []).append((ub, c))
            nb.setdefault(ub, []).append((ua, c))

    # riders: permanent items onto the largest neighbour; seals onto the smallest coaxial non-seal neighbour
    for u in sorted(units.values(), key=lambda u: u.id):
        if u.id == base:
            continue
        neigh = [(units[n], c) for n, c in nb.get(u.id, []) if n in units]
        if u.role in PERMANENT_ROLES and neigh:
            carrier = max(neigh, key=lambda t: t[0].volume)[0]
        elif u.role in RIDER_ROLES_CARRIED:
            cands = [(n, c) for n, c in neigh if n.role not in RIDER_ROLES_CARRIED | PERMANENT_ROLES]
            coax = [(n, c) for n, c in cands if c["coaxial"]]
            pool = coax or cands
            carrier = min(pool, key=lambda t: t[0].volume)[0] if pool else None
        else:
            continue
        if carrier is None:
            continue
        u.carrier = carrier.id
    # resolve chains (seal on a permanent rider etc.): attach riders to their top carrier
    for u in units.values():
        if u.carrier:
            top, seen = u.carrier, {u.id}
            while units[top].carrier and top not in seen:
                seen.add(top)
                top = units[top].carrier
            u.carrier = top
            units[top].riders += u.members
            units[top].rider_units.append(u.id)
    return units, base, ignored


def plan_disassembly(model: dict, meshes: dict, roles: dict[str, dict], contacts: list[dict],
                     max_rays: int = 700) -> dict:
    by_id = {o["occ_id"]: o for o in model["occurrences"]}
    units, base, ignored = build_units(model, roles, contacts)
    cache = _MeshCache(meshes)
    all_b = _aabb(by_id, [m for u in units.values() for m in u.members])
    diag = float(np.linalg.norm(all_b[3:] - all_b[:3])) if all_b is not None else 1000.0

    movers = [u for u in units.values() if u.id != base and not u.carrier]
    remaining = {u.id for u in units.values() if not u.carrier}

    member_unit = {m: u.id for u in units.values() for m in u.members}

    def candidate_dirs(u: Unit) -> list[np.ndarray]:
        dirs: list[np.ndarray] = []
        for m in u.members:
            for f in sorted((by_id[m].get("geometry") or {}).get("cylinders", []),
                            key=lambda f: -(f["faces"] * max(f.get("length") or 1, 1)))[:2]:
                dirs.append(np.array(f["dir"], float))
        for c in contacts:
            if c["coaxial"] and (member_unit.get(c["a"]) == u.id or member_unit.get(c["b"]) == u.id):
                dirs.append(np.array(c["axes"][0]["dir"], float))
        dirs += [np.array(v, float) for v in AXES.values()]
        out: list[np.ndarray] = []
        for d in dirs:
            for s in (d, -d):
                s = s / np.linalg.norm(s)
                if not any(float(np.dot(s, o)) > 0.999 for o in out):
                    out.append(s)
        # outward first: prefer directions pointing away from the base's centre
        bb = _aabb(by_id, units[base].members)
        ub = _aabb(by_id, u.moving())
        if bb is not None and ub is not None:
            away = (ub[:3] + ub[3:]) / 2 - (bb[:3] + bb[3:]) / 2
            if np.linalg.norm(away) > 1e-6:
                away /= np.linalg.norm(away)
                axial = len(out) - 6 * 2  # keep feature axes first, then sort each group by outwardness
                out = sorted(out, key=lambda d: -float(np.dot(d, away)))
        return out

    def origins(u: Unit, d: np.ndarray):
        m = cache.group("mv:" + u.id, u.moving())
        if m is None:
            return None
        n = m.face_normals
        front = np.where(n @ d > 0.25)[0]
        if len(front) == 0:
            return np.zeros((0, 3))
        if len(front) > max_rays:
            # keep the largest faces plus an even spread, deterministically
            areas = m.area_faces[front]
            big = front[np.argsort(-areas)[: max_rays // 2]]
            spread = front[np.linspace(0, len(front) - 1, max_rays // 2).astype(int)]
            front = np.unique(np.concatenate([big, spread]))
        return m.triangles_center[front] + n[front] * 0.02

    # shared fit axes between units: threads, press fits and bores look like overlaps in CAD,
    # so a part may slide along such an axis through its own engagement zone
    fit_axes: dict[tuple[str, str], list[dict]] = {}
    for c in contacts:
        ua, ub = member_unit.get(c["a"]), member_unit.get(c["b"])
        if not (ua and ub) or ua == ub or not c["coaxial"]:
            continue
        ua = units[ua].carrier or ua
        ub = units[ub].carrier or ub
        if ua == ub:
            continue
        fits = [ax for ax in c["axes"] if ax["fit_gap_mm"] <= 0.6]   # real fits only (not a head over a washer)
        if not fits:
            continue
        ax = {**fits[0], "r_max": max(max(f["r_a"], f["r_b"]) for f in fits)}
        fit_axes.setdefault((ua, ub), []).append(ax)
        fit_axes.setdefault((ub, ua), []).append(ax)

    def centre(occs: list[str]) -> np.ndarray:
        b = _aabb(by_id, occs)
        return (b[:3] + b[3:]) / 2 if b is not None else np.zeros(3)

    def engagement_mask(uid: str, oid: str, org: np.ndarray, d: np.ndarray) -> np.ndarray:
        """True for ray origins inside a fit cylinder shared with the obstacle, aligned with d, when d
        points away from the obstacle (a fitting unscrews outwards, never deeper into its port)."""
        mask = np.zeros(len(org), dtype=bool)
        axes = fit_axes.get((uid, oid), [])
        if not axes:
            return mask
        if float(np.dot(d, centre(units[uid].moving()) - centre(units[oid].moving()))) < -0.5:
            return mask
        for ax in axes:
            a = np.array(ax["dir"], float)
            if abs(float(np.dot(a, d))) < 0.999:
                continue
            p = np.array(ax["point"], float)
            w = org - p
            radial = np.linalg.norm(w - np.outer(w @ a, a), axis=1)
            mask |= radial <= ax["r_max"] + 0.35
        return mask

    def blockers(u: Unit, d: np.ndarray, rem: set[str]) -> list[str]:
        org = origins(u, d)
        if org is None or len(org) == 0:
            return []
        ub = _aabb(by_id, u.moving())
        sw = _swept(ub, d, diag)
        found = []
        for oid in sorted(rem):
            if oid == u.id:
                continue
            o = units[oid]
            ob = _aabb(by_id, o.moving())
            if ob is None or not _overlap(sw, ob):
                continue
            r = cache.ray("ob:" + oid, o.moving())
            if r is None:
                continue
            keep = ~engagement_mask(u.id, oid, org, d)
            if not keep.any():
                continue
            o2 = org[keep]
            if bool(np.any(r.intersects_any(o2, np.repeat(d[None, :], len(o2), axis=0)))):
                found.append(oid)
        return found

    steps, conflicts, precedence = [], [], []
    seen_block: dict[str, dict[str, set[str]]] = {}     # unit -> direction -> every part that ever blocked it there
    layer = 0
    memo: dict[tuple[str, int], list[str]] = {}
    pending = {u.id for u in movers}
    while pending:
        layer += 1
        free: list[tuple[Unit, np.ndarray, list[tuple[str, list[str]]]]] = []
        blocked_info: dict[str, list[tuple[str, list[str]]]] = {}
        for uid in sorted(pending):
            u = units[uid]
            tried = []
            chosen = None
            for k, d in enumerate(candidate_dirs(u)):
                key = (uid, k)
                prev = memo.get(key)
                if prev is not None and any(b in remaining for b in prev):
                    tried.append((_label(d), [b for b in prev if b in remaining]))
                    continue
                bl = blockers(u, d, remaining)
                memo[key] = bl
                if not bl:
                    chosen = d
                    break
                tried.append((_label(d), bl))
            for lab, bl in tried:
                seen_block.setdefault(uid, {}).setdefault(lab, set()).update(bl)
            if chosen is not None:
                free.append((u, chosen, tried))
            else:
                blocked_info[uid] = tried
        forced = False
        if not free:
            # a part held only by its own coaxial carrier leaves with it (cartridge): merge, then retry
            merged = False
            for uid in sorted(blocked_info, key=lambda x: units[x].volume):
                tried = blocked_info[uid]
                singles = {bl[0] for _, bl in tried if len(bl) == 1}
                for v in sorted(singles):
                    if v == base or v not in remaining or (uid, v) not in fit_axes:
                        continue
                    u, cv = units[uid], units[v]
                    u.carrier = v
                    cv.riders += u.moving()
                    cv.rider_units += [uid] + u.rider_units
                    remaining.discard(uid)
                    pending.discard(uid)
                    for k in ("mv:" + v, "ob:" + v):
                        cache._tm.pop(k, None)
                        cache._ray.pop(k, None)
                    for k in [k for k in memo if k[0] == v]:
                        memo.pop(k)
                    merged = True
                    break
                if merged:
                    break
            if merged:
                layer -= 1
                continue
            # deadlock: remove the unit with the fewest distinct blockers and flag it
            def nblk(uid):
                s = {b for _, bl in blocked_info[uid] for b in bl}
                return (len(s), units[uid].volume)
            uid = min(blocked_info, key=nblk)
            u = units[uid]
            best = min(blocked_info[uid], key=lambda t: len(t[1]))
            d = next(dd for dd in candidate_dirs(u) if _label(dd) == best[0])
            free = [(u, d, blocked_info[uid])]
            forced = True
            conflicts.append({"unit": uid, "layer": layer, "blocked_by": best[1], "direction": best[0],
                              "message": f"No collision-free direction found; removed along {best[0]} although "
                                         f"{', '.join(best[1])} still block it. Check the 3D (interference or "
                                         f"flexible part) and confirm the order."})
        free.sort(key=lambda t: (ROLE_ORDER.index(t[0].role) if t[0].role in ROLE_ORDER else 99, t[0].id))
        for u, d, tried in free:
            # a real ordering constraint only along the direction the part actually leaves by: whatever blocked
            # that direction in an earlier layer must come off first (other directions are just alternatives)
            lab = _label(d)
            for b in sorted(seen_block.get(u.id, {}).get(lab, set())):
                precedence.append({"first": b, "then": u.id, "reason": f"{b} blocks {u.id} along {lab}"})
            steps.append({"step": len(steps) + 1, "layer": layer, "unit": u.id, "members": u.members,
                          "riders": u.riders, "rider_units": u.rider_units, "role": u.role,
                          "direction": [round(float(v), 5) for v in d], "direction_label": _label(d),
                          "forced": forced})
            remaining.discard(u.id)
            pending.discard(u.id)

    # unique precedence pairs where the blocker was itself removed earlier (a real ordering constraint)
    order = {s["unit"]: s["step"] for s in steps}
    seen, prec = set(), []
    for p in precedence:
        k = (p["first"], p["then"])
        if p["first"] in order and k not in seen:
            seen.add(k)
            prec.append(p)
    result = {
        "format": "studio.sequence/1",
        "base_unit": base,
        "ignored": ignored,
        "units": [{"id": u.id, "members": u.members, "role": u.role, "volume_mm3": round(u.volume, 2),
                   "carrier": u.carrier, "riders": u.riders, "rider_units": u.rider_units} for u in units.values()],
        "disassembly": steps,
        "precedence": prec,
        "conflicts": conflicts,
        "layers": max((s["layer"] for s in steps), default=0),
    }
    from .hierarchy import regroup
    regroup(result, contacts)                    # part by part, each after its attaching parts
    result["assembly"] = [{**s, "step": i + 1, "direction": [-v for v in s["direction"]],
                           "direction_label": _label(-np.array(s["direction"]))}
                          for i, s in enumerate(reversed(result["disassembly"]))]
    log.info("sequence: %d steps in %d layers, %d conflicts", len(steps), result["layers"], len(conflicts))
    return result
