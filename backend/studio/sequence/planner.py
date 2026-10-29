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
   The test runs both ways: rays from the moving unit's leading faces, and rays from each
   obstacle's faces that face it, cast backwards, so a part buried in the mover (a screw shank
   inside a bracket) blocks it too. Ray start points are spread over the whole surface (one per
   grid cell, plus extra points on big triangles), so small faces such as hole walls are kept.

   Directions are tried in groups, the part's natural way out first: a screw only head first
   (its wide end, from the mesh), a nut towards the free end of its bolt, a fitting / plug / bush
   wide end first; then the part's own cylinder axes and shared axes; the six model axes last.
   Among the free directions of a group, one that lifts the part off what it sits on beats one
   that slides it along it; then the most outward. A part that can only slide out is flagged.
5. All units free at the start of a layer are removed in that layer, in role order
   (caps → lock wire → fasteners → washers → closures → fittings → …), a part before the part
   it sits on. Each step records that part (``on``): what it is removed from, and installed on.
   If nothing is free, the unit with the fewest blockers is removed and the step is flagged as a
   conflict for the writer to review.

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
PERMANENT_ROLES = {"insert", "press_plug", "plate", "dowel"}


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


def _intersector(mesh):
    """Embree (``pip install embreex``) when it is there: about 50 times faster than trimesh's own ray caster,
    which matters on big assemblies. Same answers; the pure-Python caster is the fallback."""
    try:
        from trimesh.ray.ray_pyembree import RayMeshIntersector as Fast
        return Fast(mesh)
    except Exception:                                   # embreex not installed, or no wheel for this platform
        from trimesh.ray.ray_triangle import RayMeshIntersector
        return RayMeshIntersector(mesh)


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
                self._ray[key] = _intersector(m)
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


HEADED_ROLES = {"fastener", "fitting", "closure", "bush"}     # the wide end leads: head, hex, flange
SUPPORT_ROLES_SKIP = {"fastener", "nut", "washer", "lockwire", "seal", "backup_ring"}


def head_direction(occ: dict, mesh) -> np.ndarray | None:
    """Unit vector along the part's main cylinder axis towards its wide end (screw head, fitting hex, bush
    flange), or None when both ends are about as wide (pin, stud, nut)."""
    cyls = sorted((occ.get("geometry") or {}).get("cylinders", []),
                  key=lambda f: -(f["faces"] * max(f.get("length") or 1, 1)))
    if not cyls or mesh is None or len(mesh[0]) < 8:
        return None
    a = np.asarray(cyls[0]["dir"], float)
    a /= np.linalg.norm(a)
    p = np.asarray(cyls[0]["point"], float)
    w = np.asarray(mesh[0], float) - p
    t = w @ a
    r = np.linalg.norm(w - np.outer(t, a), axis=1)
    lo, hi = float(t.min()), float(t.max())
    span = hi - lo
    if span <= 1e-6:
        return None
    r_lo = float(r[t <= lo + 0.2 * span].max())
    r_hi = float(r[t >= hi - 0.2 * span].max())
    if r_hi > 1.25 * r_lo:
        return a
    if r_lo > 1.25 * r_hi:
        return -a
    return None


def _spread_faces(centres: np.ndarray, areas: np.ndarray, k: int) -> np.ndarray:
    """Indices of up to ~k faces covering the surface evenly: one face per grid cell (the largest), so small
    faces at their own place (hole walls, shoulders, bosses) are kept, then the largest of the rest."""
    n = len(centres)
    if n <= k:
        return np.arange(n)
    cell = max(float(np.sqrt(areas.sum() / k)), 1e-6)
    keys = np.floor(centres / cell).astype(np.int64)
    order = np.lexsort((-areas, keys[:, 2], keys[:, 1], keys[:, 0]))
    ks = keys[order]
    first = np.ones(len(order), dtype=bool)
    first[1:] = np.any(ks[1:] != ks[:-1], axis=1)
    picked = order[first]
    if len(picked) > k:
        picked = picked[np.linspace(0, len(picked) - 1, k).astype(int)]
    elif len(picked) < k:
        rest = np.setdiff1d(np.argsort(-areas), picked, assume_unique=False)[: k - len(picked)]
        picked = np.concatenate([picked, rest])
    return np.unique(picked)


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

    def top(uid: str | None) -> str | None:
        return (units[uid].carrier or uid) if uid in units else None

    # unit-level contact neighbours
    neighbours: dict[str, set[str]] = {}
    for c in contacts:
        ua, ub = top(member_unit.get(c["a"])), top(member_unit.get(c["b"]))
        if ua and ub and ua != ub:
            neighbours.setdefault(ua, set()).add(ub)
            neighbours.setdefault(ub, set()).add(ua)

    # caches: per-unit box (what it moves), coaxial axes, the bolts a nut sits on, candidate directions
    all_ids = sorted(units)
    idx = {uid: i for i, uid in enumerate(all_ids)}
    all_boxes = np.full((len(all_ids), 6), np.nan)
    for uid in all_ids:
        b = _aabb(by_id, units[uid].moving())
        if b is not None:
            all_boxes[idx[uid]] = b
    base_box = _aabb(by_id, units[base].members)
    coax_dirs: dict[str, list[np.ndarray]] = {}
    bolts_of: dict[str, list[str]] = {}
    for c in contacts:
        if not c["coaxial"]:
            continue
        ua, ub = member_unit.get(c["a"]), member_unit.get(c["b"])
        for x, y in ((ua, ub), (ub, ua)):
            if x:
                coax_dirs.setdefault(x, []).append(np.array(c["axes"][0]["dir"], float))
            if x in units and y in units and units[y].role == "fastener":
                bolts_of.setdefault(x, []).append(y)
    groups_cache: dict[str, list] = {}

    def ubox(uid: str) -> np.ndarray | None:
        b = all_boxes[idx[uid]]
        return None if np.isnan(b[0]) else b

    def main_member(u: Unit) -> str:
        return max(u.members, key=lambda m: (by_id[m].get("geometry") or {}).get("volume_mm3") or 0)

    head_cache: dict[str, np.ndarray | None] = {}

    def head_of(u: Unit) -> np.ndarray | None:
        if u.id not in head_cache:
            m = main_member(u)
            head_cache[u.id] = head_direction(by_id[m], meshes.get(m)) if u.role in HEADED_ROLES else None
        return head_cache[u.id]

    def nut_direction(u: Unit) -> np.ndarray | None:
        """A nut comes off towards the free end of its bolt: away from the bolt head."""
        for other in bolts_of.get(u.id, []):
            h = head_of(units[other])
            if h is not None:
                return -h
        return None

    def outward(u: Unit) -> np.ndarray | None:
        bb, ub = base_box, ubox(u.id)
        if bb is None or ub is None:
            return None
        away = (ub[:3] + ub[3:]) / 2 - (bb[:3] + bb[3:]) / 2
        n = float(np.linalg.norm(away))
        return away / n if n > 1e-6 else None

    def candidate_groups(u: Unit) -> list[tuple[str, list[np.ndarray]]]:
        """Directions to try, in groups: the part's natural way out first, the model axes last. Inside a group,
        the most outward first."""
        if u.id in groups_cache:
            return groups_cache[u.id]
        groups_cache[u.id] = _candidate_groups(u)
        return groups_cache[u.id]

    def _candidate_groups(u: Unit) -> list[tuple[str, list[np.ndarray]]]:
        seen: list[np.ndarray] = []

        def uniq(ds):
            out = []
            for d in ds:
                d = np.asarray(d, float)
                d = d / np.linalg.norm(d)
                if not any(float(np.dot(d, o)) > 0.999 for o in seen):
                    seen.append(d)
                    out.append(d)
            return out

        away = outward(u)

        def by_out(ds):
            return sorted(ds, key=lambda d: -float(np.dot(d, away))) if away is not None else ds

        groups: list[tuple[str, list[np.ndarray]]] = []
        head = head_of(u)
        if u.role == "nut":
            nd = nut_direction(u)
            if nd is not None:
                groups.append(("nut", uniq([nd])))
        if head is not None:
            groups.append(("head", uniq([head])))
            if u.role == "fastener":
                return groups                     # a screw comes out head first, or not at all
        axes = []
        for m in u.members:
            for f in sorted((by_id[m].get("geometry") or {}).get("cylinders", []),
                            key=lambda f: -(f["faces"] * max(f.get("length") or 1, 1)))[:2]:
                axes.append(np.array(f["dir"], float))
        axes += coax_dirs.get(u.id, [])
        both = [s for d in axes for s in (d, -d)]
        if head is not None:                      # never the narrow end first along the head axis
            both = [d for d in both if float(np.dot(d / np.linalg.norm(d), head)) > -0.999]
        g = uniq(by_out(both))
        if g:
            groups.append(("axis", g))
        g = uniq(by_out([np.array(v, float) for v in AXES.values()]))
        if g:
            groups.append(("model", g))
        return groups

    origin_cache: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}

    def origins(key: str, occs: list[str], d: np.ndarray):
        """Ray start points on the faces of ``occs`` that face ``d``, spread over the whole surface."""
        ck = (key, tuple(np.round(d, 4)))
        if ck in origin_cache:
            return origin_cache[ck]
        m = cache.group(key, occs)
        if m is None:
            origin_cache[ck] = None
            return None
        n = m.face_normals
        front = np.where(n @ d > 0.25)[0]
        if len(front) == 0:
            res = np.zeros((0, 3))
        else:
            cen = m.triangles_center[front]
            areas = m.area_faces[front]
            pick = front[_spread_faces(cen, areas, max_rays)]
            pts = [m.triangles_center[pick]]
            # big triangles: three more points, halfway to each corner, so a small obstacle over part of a
            # large flat face is not missed by the single centre ray
            cell2 = float(areas.sum()) / max_rays
            big = pick[m.area_faces[pick] > 4 * cell2]
            if len(big):
                tri = m.triangles[big]
                c = m.triangles_center[big][:, None, :]
                pts.append(((tri + c) / 2).reshape(-1, 3))
                nrm = np.repeat(n[big], 3, axis=0)
                res = np.vstack(pts) + np.vstack([n[pick], nrm]) * 0.02
            else:
                res = pts[0] + n[pick] * 0.02
        origin_cache[ck] = res
        return res

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

    def hit_counts(u: Unit, d: np.ndarray, rem) -> dict[str, int]:
        """How many rays hit each unit in ``rem`` when ``u`` moves along d. Both ways: rays from the faces of u
        that lead the move, and rays from the obstacle's faces that face u, cast backwards. The second catches
        what is buried in u (a screw shank inside a bracket), which no ray from u's own surface can see."""
        org = origins("mv:" + u.id, u.moving(), d)
        ub = ubox(u.id)
        if ub is None:
            return {}
        sw = _swept(ub, d, diag)
        mine = cache.ray("mv:" + u.id, u.moving())
        found: dict[str, int] = {}
        # obstacles whose box meets the swept box, all at once
        cand = [oid for oid in rem if oid != u.id and oid in idx]
        if not cand:
            return found
        bx = all_boxes[[idx[o] for o in cand]]
        hit = np.all(sw[:3] - 0.01 <= bx[:, 3:], axis=1) & np.all(bx[:, :3] - 0.01 <= sw[3:], axis=1)
        for oid in sorted(o for o, h in zip(cand, hit) if h):
            o = units[oid]
            n = 0
            r = cache.ray("ob:" + oid, o.moving())
            if r is not None and org is not None and len(org):
                keep = ~engagement_mask(u.id, oid, org, d)
                if keep.any():
                    o2 = org[keep]
                    n += int(np.count_nonzero(r.intersects_any(o2, np.repeat(d[None, :], len(o2), axis=0))))
            if mine is not None:
                back = origins("ob:" + oid, o.moving(), -d)
                if back is not None and len(back):
                    # only the obstacle's faces inside the swept volume of u can be reached
                    inside = np.all((back >= sw[:3] - 0.01) & (back <= sw[3:] + 0.01), axis=1)
                    back = back[inside]
                    if len(back):
                        keep = ~engagement_mask(u.id, oid, back, d)
                        b2 = back[keep]
                        if len(b2):
                            n += int(np.count_nonzero(mine.intersects_any(b2, np.repeat(-d[None, :], len(b2), axis=0))))
            if n:
                found[oid] = n
        return found

    nuts_on: dict[str, set[str]] = {}               # bolt unit -> the nuts screwed on it (coaxial)
    for c in contacts:
        ua, ub = member_unit.get(c["a"]), member_unit.get(c["b"])
        if c["coaxial"] and ua in units and ub in units:
            for b, n in ((ua, ub), (ub, ua)):
                if units[b].role == "fastener" and units[n].role == "nut":
                    nuts_on.setdefault(b, set()).add(n)

    def blockers(u: Unit, d: np.ndarray, rem: set[str]) -> list[str]:
        found = set(hit_counts(u, d, rem))
        # the thread lets a bolt turn out of its nut, but the nut comes off first
        found |= {n for n in nuts_on.get(u.id, ()) if n in rem}
        return sorted(found)

    def support(u: Unit, d: np.ndarray, rem: set[str]) -> str | None:
        """The part ``u`` sits on when it leaves along d: the touching part behind it (hit along -d), the one
        with the most hits. Attaching parts and seals do not count as supports."""
        near = {n for n in neighbours.get(u.id, set()) if n in rem and n != u.id
                and units[n].role not in SUPPORT_ROLES_SKIP}
        if not near:
            return None
        hc = hit_counts(u, -d, near)
        return max(hc, key=lambda k: (hc[k], units[k].volume)) if hc else None

    steps, conflicts, precedence = [], [], []
    seen_block: dict[str, dict[str, set[str]]] = {}     # unit -> direction -> every part that ever blocked it there
    layer = 0
    memo: dict[tuple, list[str]] = {}

    def test(uid: str, d: np.ndarray) -> list[str]:
        key = (uid, tuple(np.round(d, 4)))
        prev = memo.get(key)
        if prev is not None and any(b in remaining for b in prev):
            return [b for b in prev if b in remaining]
        bl = blockers(units[uid], d, remaining)
        memo[key] = bl
        return bl

    def choose(u: Unit):
        """-> (direction or None, the directions tried with their blockers, how it was chosen)."""
        tried: list[tuple[str, list[str], np.ndarray]] = []
        for gname, group in candidate_groups(u):
            ok = []
            for d in group:
                bl = test(u.id, d)
                if bl:
                    tried.append((_label(d), bl, d))
                else:
                    ok.append(d)
            if ok:
                # lift off what it sits on rather than slide along it; then the most outward
                lifts = [d for d in ok if support(u, d, remaining | {base})]
                kind = "slide" if not lifts and gname == "model" else gname
                return (lifts or ok)[0], tried, kind
        return None, tried, None

    pending = {u.id for u in movers}
    while pending:
        layer += 1
        free: list[tuple[Unit, np.ndarray, list, str]] = []
        blocked_info: dict[str, list] = {}
        for uid in sorted(pending):
            u = units[uid]
            chosen, tried, kind = choose(u)
            for lab, bl, _ in tried:
                seen_block.setdefault(uid, {}).setdefault(lab, set()).update(bl)
            if chosen is not None:
                free.append((u, chosen, tried, kind))
            else:
                blocked_info[uid] = tried
        forced = False
        if not free:
            # a part held only by its own coaxial carrier leaves with it (cartridge): merge, then retry
            merged = False
            for uid in sorted(blocked_info, key=lambda x: units[x].volume):
                tried = blocked_info[uid]
                singles = {bl[0] for _, bl, _ in tried if len(bl) == 1}
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
                    for k in [k for k in origin_cache if k[0] in ("mv:" + v, "ob:" + v)]:
                        origin_cache.pop(k)
                    nb_ = _aabb(by_id, cv.moving())
                    if nb_ is not None:
                        all_boxes[idx[v]] = nb_
                    groups_cache.pop(v, None)
                    for n in neighbours.pop(uid, set()):
                        neighbours.get(n, set()).discard(uid)
                        if n != v:
                            neighbours.setdefault(v, set()).add(n)
                            neighbours.setdefault(n, set()).add(v)
                    merged = True
                    break
                if merged:
                    break
            if merged:
                layer -= 1
                continue
            # deadlock: remove the unit with the fewest distinct blockers and flag it
            def nblk(uid):
                s = {b for _, bl, _ in blocked_info[uid] for b in bl}
                return (len(s), units[uid].volume)
            uid = min(blocked_info, key=nblk)
            u = units[uid]
            best = min(blocked_info[uid], key=lambda t: len(t[1]))
            free = [(u, best[2], blocked_info[uid], "forced")]
            forced = True
            conflicts.append({"unit": uid, "layer": layer, "blocked_by": best[1], "direction": best[0],
                              "message": f"No collision-free direction found; removed along {best[0]} although "
                                         f"{', '.join(best[1])} still block it. Check the 3D (interference, "
                                         f"flexible part, or a part that needs two moves) and confirm the order."})
        free.sort(key=lambda t: (ROLE_ORDER.index(t[0].role) if t[0].role in ROLE_ORDER else 99, t[0].id))
        sits_on = {u.id: support(u, d, remaining) for u, d, _, _ in free}
        # in one layer, a part comes off before the part it sits on
        ids = [t[0].id for t in free]
        for _ in range(len(ids)):
            moved = False
            for i, a in enumerate(ids):
                s = sits_on.get(a)
                if s in ids and ids.index(s) < i:
                    ids.insert(ids.index(s), ids.pop(i))
                    moved = True
                    break
            if not moved:
                break
        free = sorted(free, key=lambda t: ids.index(t[0].id))
        for k, (u, d, tried, kind) in enumerate(free):
            if k and not forced:
                # parts taken off earlier in this layer may have opened a better way out (the lid that sat on
                # the bracket is off: the bracket now lifts instead of sliding)
                d2, tried2, kind2 = choose(u)
                if d2 is not None:
                    d, kind = d2, kind2
                    for lab2, bl2, _ in tried2:
                        seen_block.setdefault(u.id, {}).setdefault(lab2, set()).update(bl2)
                    sits_on[u.id] = support(u, d, remaining)
            # a real ordering constraint only along the direction the part actually leaves by: whatever blocked
            # that direction in an earlier layer must come off first (other directions are just alternatives)
            lab = _label(d)
            for b in sorted(seen_block.get(u.id, {}).get(lab, set())):
                precedence.append({"first": b, "then": u.id, "reason": f"{b} blocks {u.id} along {lab}"})
            on = sits_on.get(u.id) or (base if base in neighbours.get(u.id, set()) else None)
            check = None
            if kind == "slide":
                check = (f"Slides out along {lab} without lifting off the part it sits on: check for a lip, a clip "
                         f"or a second move.")
            elif kind == "forced":
                check = "No collision-free direction: check the 3D and the order."
            steps.append({"step": len(steps) + 1, "layer": layer, "unit": u.id, "members": u.members,
                          "riders": u.riders, "rider_units": u.rider_units, "role": u.role,
                          "direction": [round(float(v), 5) for v in d], "direction_label": lab,
                          "direction_from": kind, "on": on, "check": check, "forced": forced})
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
    boxes = {u.id: [float(v) for v in b] for u in units.values() if not u.carrier
             for b in [_aabb(by_id, u.moving())] if b is not None}
    regroup(result, contacts, boxes)             # part by part, each after its attaching parts
    by_step = {s["unit"]: s for s in result["disassembly"]}
    for s in result["disassembly"]:
        if s.get("attaches"):                    # attaching parts come off the part they hold
            s["on"] = s["attaches"]
        elif s.get("attached_by"):
            # a part held by screws comes off the part they screw into (not the piston touching the plug)
            into = [by_step[a].get("threads_into") for a in s["attached_by"] if a in by_step]
            into = [t for t in into if t and t != s["unit"]]
            if into:
                s["on"] = max(set(into), key=into.count)
    result["assembly"] = [{**s, "step": i + 1, "direction": [-v for v in s["direction"]],
                           "direction_label": _label(-np.array(s["direction"]))}
                          for i, s in enumerate(reversed(result["disassembly"]))]
    log.info("sequence: %d steps in %d layers, %d conflicts", len(steps), result["layers"], len(conflicts))
    return result
