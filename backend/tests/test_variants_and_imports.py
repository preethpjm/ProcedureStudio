"""Parts-list import with ambiguity, stable item numbers, local-LLM answers (no server needed)."""
import json

import pandas as pd
import pytest

from studio.engineering import parts_import as pi
from studio.engineering.item_numbers import stabilize
from studio.llm.providers import LLMProvider, _json_from, refine_steps


# ---------------------------------------------------------------- parts list import
def _xlsx(tmp_path, rows, extra_sheets=None):
    f = tmp_path / "bom.xlsx"
    with pd.ExcelWriter(f) as w:
        pd.DataFrame(rows).to_excel(w, sheet_name="EBOM", header=False, index=False)
        for name, r in (extra_sheets or {}).items():
            pd.DataFrame(r).to_excel(w, sheet_name=name, header=False, index=False)
    return f


FRENCH = [["Export ERP 2026", None, None, None, None], [None] * 5,
          ["Repère", "Réf. pièce", "Libellé article", "Nb", "Fournisseur"],
          ["10", "C75970", "CORPS EQUIPE", "1", "F6137"], ["20", "MS21209F4-15", "INSERT FILETE", "3", "81343"],
          ["30", "NAS1351N4-12", "VIS CHC SPECIALE", "6", "80205"], ["40", "M83461-1-014", "JOINT TORIQUE", "2", "81349"]]


def test_unfamiliar_export_is_read_and_flagged_for_review(tmp_path):
    f = _xlsx(tmp_path, FRENCH, {"Matrix": [["Seat pos", "A1", "A2"], ["C75970", "1", "1"], ["X", "2", "2"]]})
    r = pi.inspect(f)
    m = {k: v["header"] for k, v in r["chosen"]["mapping"].items()}
    assert m["part_number"] == "Réf. pièce" and m["description"] == "Libellé article"
    assert m["qty"] == "Nb" and m["find_number"] == "Repère"          # 10, 20, 30 are item numbers, not quantities
    assert r["needs_review"] and any("guessed" in p for p in r["problems"])
    recs = pi.load(f, pi.mapping_from_proposal(r["chosen"]))
    assert recs[2] == {"part_number": "NAS1351N4-12", "description": "VIS CHC SPECIALE", "qty": "6",
                       "find_number": "30", "manuf_code": "80205"}


def test_known_headers_need_no_review(tmp_path):
    f = tmp_path / "bom.csv"
    f.write_text("Level,Part Number,Description,Qty.,CAGE\n0,D1,VALVE,1,F6137\n1,.C1,BODY,1,F6137\n1,.C2,SCREW,4,F6137\n")
    r = pi.inspect(f)
    assert not r["needs_review"] and r["chosen"]["mapping"]["part_number"]["how"] == "exact"
    assert len(pi.load(f, pi.mapping_from_proposal(r["chosen"]))) == 3


def test_writer_mapping_overrides_the_proposal(tmp_path):
    f = _xlsx(tmp_path, FRENCH)
    chosen = pi.inspect(f)["chosen"]
    recs = pi.load(f, {"table": chosen["id"], "columns": {"part_number": "Réf. pièce", "description": "Libellé article",
                                                          "qty": "Nb", "comments": "Fournisseur"}})
    assert recs[0]["comments"] == "F6137" and "manuf_code" not in recs[0]


def test_no_part_number_column_says_what_to_do(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("a,b\nhello world,there\n")
    r = pi.inspect(f)
    assert r["chosen"] is None and r["needs_review"] and r["problems"]


# ---------------------------------------------------------------- item numbers
def _row(i, pn, item, order, nom="PART", parent="r0", alt=None, depth=1, role="part"):
    return {"id": i, "part_number": pn, "item": item, "order": order, "nomenclature": nom, "parent_row": parent,
            "alternate_of": alt, "depth": depth, "role": role}


def test_item_numbers_continue_from_the_baseline():
    base = {"rows": [_row("r0", "TOP", "-1", 0, depth=0), _row("a", "P1", "10", 1), _row("b", "SCREW-10", "20", 2, "SCREW"),
                     _row("c", "P3", "30", 3), _row("c2", "P3ALT", "30A", 4, alt="c"), _row("d", "P4", "40", 5)]}
    # new issue: SCREW-10 replaced by SCREW-12 (same name), P5 added after P1, numbering from scratch shifted everything
    new = {"rows": [_row("r0", "TOP", "-1", 0, depth=0), _row("x1", "P1", "10", 1), _row("x5", "P5", "20", 2),
                    _row("x2", "SCREW-12", "30", 3, "SCREW"), _row("x3", "P3", "40", 4), _row("x3a", "P3ALT", "40A", 5, alt="x3"),
                    _row("x4", "P4", "50", 6)]}
    st = stabilize(new, base)
    items = {r["part_number"]: r["item"] for r in new["rows"]}
    assert items["P1"] == "10" and items["P3"] == "30" and items["P3ALT"] == "30A" and items["P4"] == "40"
    assert items["SCREW-12"] == "25"                 # next to the screw it replaces (20)
    assert items["P5"] not in ("10", "20", "25", "30") and 10 < int(items["P5"]) < 30
    assert st["retired"] == ["20"] and len(st["new"]) == 2
    assert len({r["item"] for r in new["rows"]}) == len(new["rows"])


# ---------------------------------------------------------------- local LLM answers
def test_thinking_and_code_fences_are_removed():
    text = '<think>Let me check {"not": "this"}</think>\n```json\n{"steps": [{"id": "s1", "text": "Remove the cap (10)."}]}\n```'
    assert _json_from(text)["steps"][0]["text"] == "Remove the cap (10)."


class _Fake(LLMProvider):
    name = "fake"

    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    def complete(self, system, user, max_tokens=2000):
        self.calls += 1
        steps = json.loads(user.split("Steps:\n", 1)[1])
        if self.calls == 2:
            return "sorry, no JSON here", {}
        return json.dumps({"steps": [{"id": s["id"], "text": s["text"].upper()} for s in steps]}), {"input_tokens": 1}


def test_steps_go_in_batches_and_a_failed_batch_keeps_templates():
    p = _Fake()
    steps = [{"id": f"s{i}", "text": f"step {i}"} for i in range(20)]
    out, usage = refine_steps(p, steps, "ctx", batch_size=8)
    assert p.calls == 3 and usage["batches"] == 3 and usage["failed_batches"] == 1
    assert len(out) == 12 and "s8" not in out and out["s0"] == "STEP 0"
