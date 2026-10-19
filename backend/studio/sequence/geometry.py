"""Contact graph between part occurrences.

Pairs whose bounding boxes come within ``near_mm`` are checked on their meshes with FCL:
``distance <= touch_mm`` is a contact, a penetration is an interference (threads, press
fits and modelling overlaps all look like this in CAD). Each contact also records whether
the two parts share a cylinder axis (a fastener in its hole, a seal on a piston, a plug in
its bore) and that axis, which is what the sequence planner uses as a removal direction.
"""
from __future__ import annotations

import itertools
import logging

import numpy as np

log = logging.getLogger("studio.sequence")


def _aabb_near(a: list[float], b: list[float], tol: float) -> bool:
    return all(a[i] - tol <= b[i + 3] and b[i] - tol <= a[i + 3] for i in range(3))


def _axis_match(fa: dict, fb: dict, ang_deg: float = 2.0, off_mm: float = 0.15) -> bool:
    da, db = np.array(fa["dir"]), np.array(fb["dir"])
    if abs(float(np.dot(da, db))) < np.cos(np.radians(ang_deg)):
        return False
    pa, pb = np.array(fa["point"]), np.array(fb["point"])
    w = pb - pa
    off = np.linalg.norm(w - np.dot(w, da) * da)
    return off <= off_mm


def shared_axes(ga: dict, gb: dict) -> list[dict]:
    out = []
    for fa in ga.get("cylinders", []):
        for fb in gb.get("cylinders", []):
            if _axis_match(fa, fb):
                out.append({"dir": fa["dir"], "point": fa["point"], "r_a": fa["radius"], "r_b": fb["radius"],
                            "fit_gap_mm": round(abs(fa["radius"] - fb["radius"]), 4)})
    out.sort(key=lambda e: e["fit_gap_mm"])
    return out[:3]


def build_fcl_objects(meshes: dict):
    import fcl
    objs = {}
    for occ, (v, f) in meshes.items():
        if len(f) == 0:
            continue
        m = fcl.BVHModel()
        m.beginModel(len(v), len(f))
        m.addSubModel(v.astype(np.float64), f.astype(np.int64))
        m.endModel()
        objs[occ] = fcl.CollisionObject(m, fcl.Transform())
    return objs


def contact_graph(model: dict, meshes: dict, near_mm: float = 0.3, touch_mm: float = 0.05) -> list[dict]:
    import fcl
    parts = [o for o in model["occurrences"] if not o["is_assembly"] and (o.get("geometry") or {}).get("bbox")
             and o["occ_id"] in meshes]
    objs = build_fcl_objects({o["occ_id"]: meshes[o["occ_id"]] for o in parts})
    contacts = []
    for a, b in itertools.combinations(parts, 2):
        ga, gb = a["geometry"], b["geometry"]
        if not _aabb_near(ga["bbox"], gb["bbox"], near_mm):
            continue
        oa, ob = objs.get(a["occ_id"]), objs.get(b["occ_id"])
        if oa is None or ob is None:
            continue
        creq, cres = fcl.CollisionRequest(num_max_contacts=1, enable_contact=True), fcl.CollisionResult()
        hit = fcl.collide(oa, ob, creq, cres)
        if hit:
            depth = float(cres.contacts[0].penetration_depth) if cres.contacts else 0.0
            kind, dist = ("interference" if depth > touch_mm else "contact"), -depth
        else:
            dreq, dres = fcl.DistanceRequest(), fcl.DistanceResult()
            dist = float(fcl.distance(oa, ob, dreq, dres))
            if dist > touch_mm:
                continue
            kind = "contact"
        axes = shared_axes(ga, gb)
        contacts.append({"a": a["occ_id"], "b": b["occ_id"], "kind": kind, "distance_mm": round(dist, 4),
                         "coaxial": bool(axes), "axes": axes})
    log.info("contact graph: %d contacts among %d parts", len(contacts), len(parts))
    return contacts
