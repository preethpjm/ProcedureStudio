"""Removal directions on real meshes: screws head first, nuts to the bolt end, parts lifted off what they sit on,
buried shanks block, and every step knows the part it comes off."""
import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("fcl")
pytest.importorskip("manifold3d")

from studio.sequence.geometry import contact_graph
from studio.sequence.planner import head_direction, plan_disassembly
from test_sequence_part_by_part import _box, _drill, _screw


def _pin(tip, axis, length, r=2.0):
    a = np.asarray(axis, float)
    m = trimesh.creation.cylinder(radius=r, height=length, sections=24)
    m.apply_translation([0, 0, length / 2])
    m.apply_transform(trimesh.geometry.align_vectors([0, 0, 1], a))
    m.apply_translation(tip)
    return m, {"dir": list(a), "point": list(map(float, tip)), "radius": r, "faces": 24, "length": length}


def _build(parts, edge=4.0):
    occs, meshes, roles = [], {}, {}
    for oid, (m, role, cyls, *fine) in parts.items():
        v, f = trimesh.remesh.subdivide_to_size(m.vertices, m.faces, max_edge=fine[0] if fine else edge)
        meshes[oid] = (np.asarray(v), np.asarray(f))
        b = m.bounds
        occs.append({"occ_id": oid, "is_assembly": False, "children": [], "part_number": oid.split("_")[0],
                     "name": oid, "geometry": {"bbox": list(b[0]) + list(b[1]), "volume_mm3": float(abs(m.volume)),
                                               "cylinders": cyls}})
        roles[oid] = {"role": role}
    root = {"occ_id": "ROOT", "is_assembly": True, "children": list(parts), "part_number": "ROOT", "name": "ROOT",
            "geometry": None}
    return {"root": "ROOT", "occurrences": [root] + occs}, meshes, roles


def _plan(parts):
    model, meshes, roles = _build(parts)
    seq = plan_disassembly(model, meshes, roles, contact_graph(model, meshes))
    return seq, {s["unit"]: s for s in seq["disassembly"]}, [s["unit"] for s in seq["disassembly"]]


def _scene(dowel_role="part"):
    P = {"HOUSING_1": (_box((0, 0, 0), (200, 100, 50)), "housing", [])}
    # a plate at the +X end (finely meshed), located by two dowels pressed into the housing, held by two screws
    plate = _drill(_box((150, 10, 50), (200, 90, 56)), [(170, 30, 0), (170, 70, 0)], (0, 0, 1))
    for y in (30, 70):
        plate = plate.difference(trimesh.creation.cylinder(radius=2.0, height=400).apply_translation((185, y, 0)))
    P["PLATE_1"] = (plate, "part", [], 1.5)
    for i, (x, y) in enumerate([(170, 30), (170, 70)], 1):
        m, c = _screw((x, y, 40), (0, 0, 1), 16)
        P[f"SCREW_{i}"] = (m, "fastener", [c])
    for i, y in enumerate((30, 70), 1):
        m, c = _pin((185, y, 40), (0, 0, 1), 16)
        P[f"DOWEL_{i}"] = (m, dowel_role, [c])
    # at the -X end a bracket, screwed to the housing, with a lid screwed into the bracket
    P["BRACKET_1"] = (_drill(_box((0, 20, 50), (40, 80, 70)), [(10, 50, 0)], (0, 0, 1)), "part", [])
    m, c = _screw((10, 50, 40), (0, 0, 1), 30)
    P["SCREW_3"] = (m, "fastener", [c])
    P["LID_1"] = (_drill(_box((20, 30, 70), (40, 70, 74)), [(30, 40, 0), (30, 60, 0)], (0, 0, 1)), "closure", [])
    for i, y in enumerate((40, 60), 4):
        m, c = _screw((30, y, 64), (0, 0, 1), 10)
        P[f"SCREW_{i}"] = (m, "fastener", [c])
    return P


def test_parts_lift_off_what_they_sit_on():
    seq, by, order = _plan(_scene())
    pos = {u: i for i, u in enumerate(order)}
    # lifted, not slid out sideways towards the nearest end
    assert by["PLATE_1"]["direction_label"] == "+Z" and by["PLATE_1"]["on"] == "HOUSING_1"
    assert by["LID_1"]["direction_label"] == "+Z" and by["LID_1"]["on"] == "BRACKET_1"
    assert by["BRACKET_1"]["direction_label"] == "+Z" and by["BRACKET_1"]["on"] == "HOUSING_1"
    # the lid's screws are buried in the bracket: the bracket waits for them, and for the lid on top of it
    assert pos["SCREW_4"] < pos["BRACKET_1"] and pos["SCREW_5"] < pos["BRACKET_1"]
    assert pos["LID_1"] < pos["BRACKET_1"]
    # screws: head first, removed from the part they hold
    for i in range(1, 6):
        s = by[f"SCREW_{i}"]
        assert s["direction_label"] == "+Z" and s["direction_from"] == "head" and s["on"] == s["attaches"]
    assert not seq["conflicts"]
    assert not any(s.get("check") for s in seq["disassembly"])


def test_dowels_stay_in_the_housing():
    seq, by, order = _plan(_scene(dowel_role="dowel"))
    assert "DOWEL_1" not in by and "DOWEL_2" not in by
    units = {u["id"]: u for u in seq["units"]}
    assert units["DOWEL_1"]["carrier"] == "HOUSING_1"
    assert by["PLATE_1"]["direction_label"] == "+Z"           # the dowels stop it sliding: it lifts off them


def test_head_direction_from_the_mesh():
    up, cyl = _screw((0, 0, 0), (0, 0, 1), 20)
    occ = {"geometry": {"cylinders": [cyl]}}
    assert np.allclose(head_direction(occ, (up.vertices, up.faces)), [0, 0, 1])
    down, cyl = _screw((0, 0, 0), (0, 0, -1), 20)
    assert np.allclose(head_direction({"geometry": {"cylinders": [cyl]}}, (down.vertices, down.faces)), [0, 0, -1])
    pin, cyl = _pin((0, 0, 0), (1, 0, 0), 20)
    assert head_direction({"geometry": {"cylinders": [cyl]}}, (pin.vertices, pin.faces)) is None


def test_nut_comes_off_towards_the_bolt_end_then_the_bolt_head_first():
    P = {"HOUSING_1": (_box((0, 0, 0), (100, 100, 50)), "housing", [])}
    # a flange overhanging the housing, bolted through with a nut underneath
    P["FLANGE_1"] = (_drill(_box((80, 40, 50), (120, 60, 55)), [(110, 50, 0)], (0, 0, 1)), "part", [])
    m, c = _screw((110, 50, 44), (0, 0, 1), 11)
    P["BOLT_1"] = (m, "fastener", [c])
    nut = trimesh.creation.cylinder(radius=4.5, height=5, sections=6).apply_translation((110, 50, 47.5))
    nut = nut.difference(trimesh.creation.cylinder(radius=2.0, height=20, sections=24).apply_translation((110, 50, 47.5)))
    P["NUT_1"] = (nut, "nut", [{"dir": [0, 0, 1], "point": [110, 50, 45], "radius": 2.0, "faces": 24, "length": 5}])
    m, c = _screw((90, 50, 40), (0, 0, 1), 15)
    P["SCREW_1"] = (m, "fastener", [c])
    seq, by, order = _plan(P)
    pos = {u: i for i, u in enumerate(order)}
    assert by["NUT_1"]["direction_label"] == "-Z" and by["NUT_1"]["direction_from"] == "nut"
    assert by["BOLT_1"]["direction_label"] == "+Z" and pos["NUT_1"] < pos["BOLT_1"] < pos["FLANGE_1"]


def test_procedure_removes_a_part_from_the_part_it_sits_on():
    from studio.process.graph import build_process
    from test_sequence_part_by_part import _row
    rows = [_row("r0", "part", [], depth=0), _row("housing", "housing", ["h"]), _row("bracket", "part", ["br"]),
            _row("lid", "closure", ["lid"]), _row("screw", "fastener", ["s1", "s2"], attaching_for="lid")]
    st = lambda n, u, role, on, att=None: {"step": n, "unit": u, "members": [u], "riders": [], "role": role,
                                           "direction": [0, 0, 1], "direction_label": "+Z", "forced": False,
                                           "on": on, **({"attaches": att} if att else {})}
    seq = {"base_unit": "h", "units": [{"id": x, "members": [x]} for x in ("h", "br", "lid", "s1", "s2")],
           "disassembly": [st(1, "s1", "fastener", "lid", "lid"), st(2, "s2", "fastener", "lid", "lid"),
                           st(3, "lid", "closure", "br"), st(4, "br", "part", "h")]}
    proc = build_process({"rows": rows, "supplies": []}, seq, None)
    rem = {o["items"][0]["row_id"]: o["from"]["row_id"] for o in proc["disassembly"]["operations"] if o["type"] == "remove"}
    assert rem == {"lid": "bracket", "bracket": "housing"}
    inst = {o["items"][0]["row_id"]: o["on"]["row_id"] for o in proc["assembly"]["operations"] if o["type"] == "install"}
    assert inst == {"lid": "bracket", "bracket": "housing"}
