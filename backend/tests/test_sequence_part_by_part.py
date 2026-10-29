"""Part-by-part disassembly on real meshes: screws come off with the part they hold, not all first."""
import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("fcl")

from studio.sequence.geometry import contact_graph
from studio.sequence.planner import plan_disassembly


def _box(lo, hi):
    m = trimesh.creation.box(extents=np.subtract(hi, lo))
    m.apply_translation(np.add(lo, hi) / 2)
    return m


def _screw(tip, axis, length, head_gap=0.0):
    """Shank r=2 from ``tip`` along ``axis`` for ``length``; head r=4, 3 long, ``head_gap`` above the shank end."""
    a = np.asarray(axis, float)
    rot = trimesh.geometry.align_vectors([0, 0, 1], a)
    shank = trimesh.creation.cylinder(radius=2, height=length, sections=24)
    shank.apply_translation([0, 0, length / 2])
    head = trimesh.creation.cylinder(radius=4, height=3, sections=24)
    head.apply_translation([0, 0, length + head_gap + 1.5])
    m = trimesh.util.concatenate([shank, head])
    m.apply_transform(rot)
    m.apply_translation(tip)
    return m, {"dir": list(a), "point": list(map(float, tip)), "radius": 2.0, "faces": 24, "length": length}


def _drill(m, holes, axis):
    """Clearance holes r=2.4 through ``m`` along ``axis`` at the given centres (the shank does not touch)."""
    rot = trimesh.geometry.align_vectors([0, 0, 1], axis)
    for c in holes:
        h = trimesh.creation.cylinder(radius=2.4, height=400, sections=24)
        h.apply_transform(rot)
        h.apply_translation(c)
        m = m.difference(h)
    return m


def _fine(m):
    v, f = trimesh.remesh.subdivide_to_size(m.vertices, m.faces, max_edge=4.0)
    return v, f


def _model():
    parts = {}
    parts["HOUSING_1"] = (_box((0, 0, 0), (100, 100, 50)), "housing", [])
    cover = trimesh.util.concatenate([_box((0, 0, 55), (100, 100, 60)),            # top slab
                                      _box((0, 0, 50), (15, 100, 55)), _box((85, 0, 50), (100, 100, 55)),
                                      _box((15, 0, 50), (85, 15, 55)), _box((15, 85, 50), (85, 100, 55))])
    cover = trimesh.boolean.union(cover.split(only_watertight=False)) if hasattr(trimesh, "boolean") else cover
    cover = _drill(cover, [(x, y, 0) for x, y in [(7, 7), (93, 7), (7, 93), (93, 93)]], (0, 0, 1))
    parts["COVER_1"] = (cover, "closure", [])
    parts["PLATE_1"] = (_drill(_box((30, 30, 50), (70, 70, 55)), [(40, 50, 0), (60, 50, 0)], (0, 0, 1)), "part", [])
    for i, (x, y) in enumerate([(7, 7), (93, 7), (7, 93), (93, 93)], 1):           # cover screws, +Z
        m, cyl = _screw((x, y, 40), (0, 0, 1), 20)
        parts[f"SCREW_{i}"] = (m, "fastener", [cyl])
    for i, (x, y) in enumerate([(40, 50), (60, 50)], 5):                           # plate screws, under the cover
        m, cyl = _screw((x, y, 42), (0, 0, 1), 13)
        parts[f"SCREW_{i}"] = (m, "fastener", [cyl])
    parts["SIDE_1"] = (_drill(_box((100, 20, 10), (105, 80, 40)), [(0, 35, 25), (0, 65, 25)], (1, 0, 0)), "closure", [])
    for i, (y, z) in enumerate([(35, 25), (65, 25)], 7):                          # side screws, +X, heads 0.1 off
        m, cyl = _screw((90, y, z), (1, 0, 0), 15, head_gap=0.1)
        parts[f"SCREW_{i}"] = (m, "fastener", [cyl])
    parts["SIDE_2"] = (_drill(_box((20, -5, 10), (80, 0, 40)), [(35, 0, 25), (65, 0, 25)], (0, 1, 0)), "closure", [])
    for i, (x, z) in enumerate([(35, 25), (65, 25)], 9):                          # second side, -Y, heads 0.1 off
        m, cyl = _screw((x, 10, z), (0, -1, 0), 15, head_gap=0.1)
        parts[f"SCREW_{i}"] = (m, "fastener", [cyl])
    occs, meshes, roles = [], {}, {}
    for oid, (m, role, cyls) in parts.items():
        v, f = _fine(m)
        meshes[oid] = (np.asarray(v), np.asarray(f))
        b = m.bounds
        occs.append({"occ_id": oid, "is_assembly": False, "children": [], "part_number": oid.split("_")[0],
                     "name": oid, "geometry": {"bbox": list(b[0]) + list(b[1]), "volume_mm3": float(abs(m.volume)),
                                               "cylinders": cyls}})
        roles[oid] = {"role": role}
    root = {"occ_id": "ROOT", "is_assembly": True, "children": list(parts), "part_number": "ROOT", "name": "ROOT",
            "geometry": None}
    return {"root": "ROOT", "occurrences": [root] + occs}, meshes, roles


def test_part_by_part_on_meshes():
    model, meshes, roles = _model()
    contacts = contact_graph(model, meshes)
    seq = plan_disassembly(model, meshes, roles, contacts)
    order = [s["unit"] for s in seq["disassembly"]]
    pos = {u: i for i, u in enumerate(order)}
    att = {s["unit"]: s.get("attaches") for s in seq["disassembly"]}
    # every screw is given to the part under its head, not the housing it threads into
    for i in range(1, 5):
        assert att[f"SCREW_{i}"] == "COVER_1"
    assert att["SCREW_5"] == att["SCREW_6"] == "PLATE_1"
    assert att["SCREW_7"] == att["SCREW_8"] == "SIDE_1"          # head contact missed: found by overlap
    assert att["SCREW_9"] == att["SCREW_10"] == "SIDE_2"
    # each part right after its own screws
    for part, screws in (("COVER_1", [1, 2, 3, 4]), ("PLATE_1", [5, 6])):
        ps = sorted(pos[f"SCREW_{i}"] for i in screws)
        assert ps == list(range(ps[0], ps[0] + len(ps))), order
        assert pos[part] == ps[-1] + 1, order
    # the two side covers are identical (same part number, same screws): one entry, the four screws then the covers
    ps = sorted(pos[f"SCREW_{i}"] for i in (7, 8, 9, 10))
    assert ps == list(range(ps[0], ps[0] + 4)), order
    assert sorted([pos["SIDE_1"], pos["SIDE_2"]]) == [ps[-1] + 1, ps[-1] + 2], order
    # geometry kept: cover before the plate it covers
    assert pos["COVER_1"] < pos["SCREW_5"]
    for p in seq["precedence"]:
        assert pos[p["first"]] < pos[p["then"]]


def _row(id, role, occs, depth=1, parent="r0", **kw):
    return {"id": id, "item": id.upper(), "part_number": id.upper(), "nomenclature": id.upper(), "role": role,
            "units_per_assy": max(len(occs), 1), "step_name": None, "occ_ids": occs, "depth": depth,
            "parent_row": parent if depth else None, "alternate_of": None, "status": "proposed",
            "attaching": role in ("fastener", "nut", "washer", "lockwire"), "attaching_for": None,
            "permanent": False, "discard_on_removal": False, "torque": None, "process": None, **kw}


def test_procedure_splits_one_screw_part_number_over_two_parts():
    from studio.process.graph import build_process
    rows = [_row("r0", "part", [], depth=0), _row("housing", "housing", ["h"]),
            _row("cover a", "closure", ["ca"]), _row("cover b", "closure", ["cb"]),
            _row("screw", "fastener", ["s1", "s2", "s3", "s4"], attaching_for="cover a"),
            _row("washer", "washer", ["w1", "w2", "w3", "w4"], attaching_for="cover a", fastener_row="screw")]
    st = lambda n, u, role, att=None: {"step": n, "unit": u, "members": [u], "riders": [], "role": role,
                                       "direction": [0, 0, 1], "direction_label": "+Z", "forced": False,
                                       **({"attaches": att} if att else {})}
    seq = {"base_unit": "h", "disassembly": [st(1, "s1", "fastener", "ca"), st(2, "w1", "washer", "ca"),
                                             st(3, "s2", "fastener", "ca"), st(4, "w2", "washer", "ca"),
                                             st(5, "ca", "closure"),
                                             st(6, "s3", "fastener", "cb"), st(7, "w3", "washer", "cb"),
                                             st(8, "s4", "fastener", "cb"), st(9, "w4", "washer", "cb"),
                                             st(10, "cb", "closure")]}
    proc = build_process({"rows": rows, "supplies": []}, seq, None)
    ops = proc["disassembly"]["operations"]
    got = [(o["type"], [(i["row_id"], i.get("count")) for i in o["items"]], (o.get("from") or {}).get("row_id"))
           for o in ops if o["type"] in ("unfasten", "remove")]
    assert got == [("unfasten", [("screw", 2), ("washer", 2)], "cover a"), ("remove", [("cover a", 1)], "housing"),
                   ("unfasten", [("screw", 2), ("washer", 2)], "cover b"), ("remove", [("cover b", 1)], "housing")], got
    assert sorted(ops[0]["occ_ids"]) == ["s1", "s2", "w1", "w2"]


def test_back_up_rings_are_not_bushes():
    from studio.engineering.roles import RoleClassifier
    rc = RoleClassifier()
    ring = {"bbox": [0, 0, 0, 17.5, 17.5, 1.6], "cylinders": []}
    sleeve = {"bbox": [0, 0, 0, 17.5, 17.5, 25.0], "cylinders": []}
    assert rc.classify("NSA8671-8", ["BAGUE"], ring).role == "backup_ring"          # by part number
    assert rc.classify("X123", ["BAGUE"], ring).role == "backup_ring"               # flat ring named BAGUE
    assert rc.classify("X123", ["BAGUE"], sleeve).role == "bush"                    # a real bush stays a bush
    assert rc.classify("X124", ["CONTRE-BAGUE"], None).role == "backup_ring"


def test_regroup_finishes_one_area_before_the_next():
    """Plug A closes a bore with a spring and a valve; plug B the other end with a piston. The planner frees
    them layer by layer (both plugs, then spring and piston, then valve); the manual empties one bore at a time."""
    from studio.sequence.hierarchy import regroup
    st = lambda n, u, role, layer: {"step": n, "layer": layer, "unit": u, "members": [u], "riders": [], "role": role,
                                    "direction": [1, 0, 0], "direction_label": "+X", "forced": False}
    seq = {"base_unit": "body", "units": [{"id": x, "members": [x], "carrier": None} for x in
                                          ("body", "plugA", "plugB", "spring", "piston", "valve")],
           "disassembly": [st(1, "plugA", "closure", 1), st(2, "plugB", "closure", 1), st(3, "piston", "moving", 2),
                           st(4, "spring", "spring", 2), st(5, "valve", "moving", 3)],
           "precedence": [{"first": "plugA", "then": "spring"}, {"first": "spring", "then": "valve"},
                          {"first": "plugB", "then": "piston"}]}
    contacts = [{"a": "plugA", "b": "spring"}, {"a": "spring", "b": "valve"}, {"a": "plugB", "b": "piston"},
                {"a": "valve", "b": "piston"}] + [{"a": x, "b": "body"} for x in ("plugA", "plugB", "valve", "piston")]
    regroup(seq, contacts)
    assert [s["unit"] for s in seq["disassembly"]] == ["plugA", "spring", "valve", "plugB", "piston"]


def test_identical_parts_are_one_entry():
    """Two identical plugs, each with three screws and three washers: the six screws, the six washers, the two
    plugs. A different part with the same screws is not merged in."""
    from studio.sequence.hierarchy import regroup
    st = lambda u, role: {"step": 0, "layer": 1, "unit": u, "members": [u], "riders": [], "role": role,
                          "direction": [1, 0, 0], "direction_label": "+X", "forced": False}
    screws = {"A": ["SCREW_1", "SCREW_2", "SCREW_3"], "B": ["SCREW_4", "SCREW_5", "SCREW_6"]}
    washers = {"A": ["WASHER_1", "WASHER_2", "WASHER_3"], "B": ["WASHER_4", "WASHER_5", "WASHER_6"]}
    plug = {"A": "PLUG_1", "B": "PLUG_2"}
    plan = [st(u, "fastener") for x in "AB" for u in screws[x]] + [st(u, "washer") for x in "AB" for u in washers[x]]
    plan += [st("SCREW_7", "fastener"), st("PLUG_1", "closure"), st("PLUG_2", "closure"), st("COVER_1", "closure")]
    for i, s in enumerate(plan, 1):
        s["step"] = i
    contacts, prec = [], []
    for x in "AB":
        for sc, w in zip(screws[x], washers[x]):
            contacts += [{"a": sc, "b": w}, {"a": w, "b": plug[x]}, {"a": sc, "b": "BODY", "kind": "interference"}]
            prec += [{"first": sc, "then": plug[x]}, {"first": w, "then": plug[x]}]
    contacts += [{"a": "SCREW_7", "b": "COVER_1"}, {"a": "SCREW_7", "b": "BODY", "kind": "interference"}]
    contacts += [{"a": u, "b": "BODY"} for u in ("PLUG_1", "PLUG_2", "COVER_1")]
    prec += [{"first": "SCREW_7", "then": "COVER_1"}]
    seq = {"base_unit": "BODY", "units": [{"id": s["unit"], "members": [s["unit"]], "carrier": None} for s in plan],
           "disassembly": plan, "precedence": prec}
    regroup(seq, contacts)
    order = [s["unit"] for s in seq["disassembly"]]
    assert order == screws["A"] + screws["B"] + washers["A"] + washers["B"] + ["PLUG_1", "PLUG_2", "SCREW_7", "COVER_1"], order
