"""Unit tests for the rule-based pieces: drawing torque parsing, notes, roles, STE text, fact check, model diff."""
import pytest

from studio.drawing.extract import _parse_torque
from studio.drawing.notes import classify_note
from studio.engineering.roles import RoleClassifier
from studio.process.graph import torque_range, wrench_for
from studio.procedures.ste import STEChecker, fact_check
from studio.procedures.text import plural, torque_text
from studio.revisions.diff import diff_models


# ---------------------------------------------------------------- drawing
@pytest.mark.parametrize("text, nom, tol", [
    ("C.S./T.T.: 53,1 4,5N.m", 53.1, 4.5),          # French decimal comma, tolerance without the ± glyph
    ("C.S./T.T.: 24±3,7N.m", 24.0, 3.7),
    ("1,05N.m 0,05", 1.05, 0.05),
    ("TORQUE 10.5 ± 0.5 N.m", 10.5, 0.5),
])
def test_torque_parsing(text, nom, tol):
    got = _parse_torque(text)
    assert got is not None
    assert got[0] == pytest.approx(nom)
    assert got[1] == pytest.approx(tol)


@pytest.mark.parametrize("text", ["SEE SHEET 2", "SCALE 2:1", "0 N.m"])
def test_not_a_torque(text):
    assert _parse_torque(text) is None


def test_note_rules():
    n = classify_note("COMPOUND ACCORDING TO IFC30-141-07 ON ALL PARTS NOT IN CONTACT WITH THE OPERATING FLUID")
    assert n["op"] == "apply_compound" and n["phase"] == "general"
    n = classify_note("LOCKWIRE ACCORDING TO NCT60-300-04")
    assert n["op"] == "lockwire" and "fastener" in n["targets"]


# ---------------------------------------------------------------- roles
@pytest.mark.parametrize("pn, name, role", [
    ("M83461-1-014", "PACKING PREFORMED", "seal"),
    ("MS21914-6W", "CAP, PROTECTIVE", "cap"),
    ("NAS1149C0463R", "WASHER, FLAT", "washer"),
    ("C77004-12", "SCREW NAS1351N4-12", "fastener"),
    ("AS3510-0118C", "CABLE", "lockwire"),
])
def test_roles(pn, name, role):
    assert RoleClassifier().classify(pn, [name]).role == role


def test_role_fallbacks():
    c = RoleClassifier()
    assert c.classify("X1", ["THING"], is_assembly=True).role == "lru"
    assert c.classify("X2", ["THING"], geometry={"bbox": None}).role == "marker"


# ---------------------------------------------------------------- text and STE
def test_plural():
    assert plural("packing") == "packings"
    assert plural("bush") == "bushes"
    assert plural("assembly") == "assemblies"


def test_torque_text_and_wrench():
    t = torque_range({"nominal_nm": 10.5, "min_nm": 10, "max_nm": 11})
    assert torque_text(t).startswith("10 to 11 N.m (88.5 to 97.4 lbf.in)")
    assert "N.m" in wrench_for(11)


def test_ste_accepts_a_good_step():
    assert STEChecker().check("Remove the six screws (220) from the plugs (210).") == []


def test_ste_rules():
    rules = {i["rule"] for i in STEChecker().check(
        "Carefully removing the cap which is installed; you should keep it because it can be used again later by the shop.")}
    assert {"STE-9.1", "STE-ING", "STE-8.1", "STE-MOD", "STE-PAS"} <= rules
    long = "Remove " + " ".join(["the cap"] * 12) + "."
    assert any(i["rule"] == "STE-4.1" for i in STEChecker().check(long))


def test_fact_check_catches_invented_values():
    op = {"items": [{"item": "220", "qty": 6}], "torque": torque_range({"nominal_nm": 10.5, "min_nm": 10, "max_nm": 11})}
    ok = fact_check("Torque the six screws (220) to 10 to 11 N.m (88.5 to 97.4 lbf.in).", op, {"220"})
    assert ok == []
    bad = fact_check("Torque the four screws (221) to 12 N.m.", op, {"220"})
    assert {i["rule"] for i in bad} == {"FACT-NUM", "FACT-ITM"}


# ---------------------------------------------------------------- revisions
def _occ(oid, pn, c, size=(10, 10, 10), vol=100.0):
    return {"occ_id": oid, "part_number": pn, "name": pn, "is_assembly": False,
            "geometry": {"centroid": list(c), "bbox": [c[0], c[1], c[2], c[0] + size[0], c[1] + size[1], c[2] + size[2]],
                         "volume_mm3": vol}}


def test_diff_models():
    base = {"occurrences": [_occ("A_1", "A", (0, 0, 0)), _occ("A_2", "A", (50, 0, 0)), _occ("B_1", "B", (100, 0, 0)),
                            _occ("C_1", "C", (200, 0, 0)), _occ("D_1", "D", (300, 0, 0))]}
    new = {"occurrences": [_occ("A2_1", "A2", (0, 0, 0)), _occ("A2_2", "A2", (50, 0, 0)),     # A -> A2 at both places
                           _occ("B_1", "B", (105, 0, 0)),                                     # moved 5 mm
                           _occ("C_1", "C", (200, 0, 0), vol=130.0),                          # same PN, new geometry
                           _occ("E_1", "E", (400, 0, 0))]}                                    # D removed, E added
    ch = {c["type"]: c for c in diff_models(base, new)}
    assert ch["part_number_changed"]["count"] == 2 and ch["part_number_changed"]["geometry"] == "same"
    assert ch["moved"]["distance_mm"] == pytest.approx(5.0)
    assert ch["geometry_changed"]["part_number_new"] == "C"
    assert ch["removed"]["part_number_base"] == "D"
    assert ch["added"]["part_number_new"] == "E"


def test_diff_identical_models_is_empty():
    m = {"occurrences": [_occ("A_1", "A", (0, 0, 0))]}
    assert diff_models(m, m) == []


# ---------------------------------------------------------------- plug-in connectors
class _Echo:  # importable target for the class: entry below
    pass


def test_custom_connector_from_config(tmp_path):
    from studio.connectors.base import Bundle, BundleFile, Connector
    from studio.core.config import StudioConfig

    class Echo(Connector):
        def __init__(self, prefix=""):
            self.prefix = prefix

        def status(self):
            return {"name": self.name, "available": True}

        def publish(self, bundle):
            return {"connector": self.name, "documents": [{"file": self.prefix + f.name, "action": "sent"} for f in bundle.files]}

    import sys
    sys.modules[__name__].Echo = Echo
    cfg = StudioConfig(data_dir=tmp_path, connectors=[{"kind": "plm", "class": f"{__name__}:Echo", "prefix": "plm/"}])
    c = cfg.connector("plm")
    assert c.status() == {"name": "plm", "available": True}
    res = c.publish(Bundle(project={}, run={}, message="", files=[BundleFile("a.xml", b"<a/>")]))
    assert res["documents"][0]["file"] == "plm/a.xml"
    cfg.connectors.append({"kind": "bad", "class": f"{__name__}:_Echo"})
    with pytest.raises(TypeError):
        cfg.connector("bad")


def test_long_item_lists_are_grouped_and_split():
    from studio.procedures.text import item_sentences
    from studio.procedures.ste import STEChecker, technical_names_from
    names = ["packing", "back-up ring", "packing", "back-up ring", "packing", "back-up ring", "screw", "washer", "spring", "piston"]
    items = [{"row_id": f"r{i}", "item": str(100 + 10 * i), "nomenclature": n.upper(), "step_name": n, "count": 1}
             for i, n in enumerate(names)]
    out = item_sentences("Remove", items, " from the valve ball (950)")
    assert all(len(s.split()) <= 20 for s in out)
    assert sorted(x for s in out for x in __import__("re").findall(r"\b1\d0\b", s)) == sorted(i["item"] for i in items)
    new = item_sentences("Install", items[:6], " on the valve ball (950)", new=True)
    assert new == ["Install new packings (100, 120, 140) and new back-up rings (110, 130, 150) on the valve ball (950)."]
    tech = technical_names_from({"rows": [{"nomenclature": "RECOPY LEVER MACHINING"}]})
    assert STEChecker(technical_names=tech).check("Remove the recopy lever machining (600).", "step") == []


def test_regroup_puts_attaching_parts_next_to_their_part():
    from studio.sequence.hierarchy import regroup
    st = lambda n, u, role, layer: {"step": n, "layer": layer, "unit": u, "members": [u], "riders": [], "role": role,
                                    "direction": [0, 0, 1], "direction_label": "+Z", "forced": False}
    seq = {"base_unit": "body", "units": [{"id": x, "members": [x], "carrier": None} for x in
                                          ("body", "s1", "s2", "t1", "cover", "plate")],
           "disassembly": [st(1, "s1", "fastener", 1), st(2, "s2", "fastener", 1), st(3, "t1", "fastener", 1),
                           st(4, "cover", "closure", 2), st(5, "plate", "part", 2)],
           "precedence": [{"first": "s1", "then": "cover"}, {"first": "s2", "then": "cover"}, {"first": "t1", "then": "plate"}]}
    contacts = [{"a": "s1", "b": "cover"}, {"a": "s2", "b": "cover"}, {"a": "t1", "b": "plate"},
                {"a": "s1", "b": "body"}, {"a": "t1", "b": "body"}]
    regroup(seq, contacts)
    assert [s["unit"] for s in seq["disassembly"]] == ["s1", "s2", "cover", "t1", "plate"]
    assert seq["disassembly"][0]["attaches"] == "cover" and seq["disassembly"][2]["attached_by"] == ["s1", "s2"]
