"""End to end through the HTTP API on a real product: STEP + APL + drawing -> IPL -> procedures -> S1000D / ATA
-> checked against the installed schemas, BREX and rule sets -> published; then a revision (one screw part number
changed) compared with the signed-off baseline.

Needs the sample files; set STUDIO_SAMPLES to a folder with
  F6137-D24907000.stp, F6137-D24907000_APL.pdf, F6137-D24907000_Drawing_1.pdf and revB/ (modified STEP + APL CSV).
Skipped when they are not there. Takes about a minute (two full pipeline runs).
"""
import os
import time
from pathlib import Path

import pytest

SAMPLES = Path(os.environ.get("STUDIO_SAMPLES", Path(__file__).resolve().parents[3] / "samples"))
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
pytestmark = pytest.mark.skipif(not (SAMPLES / "F6137-D24907000.stp").exists(), reason="sample product not available")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    os.environ["STUDIO_DATA"] = str(tmp_path_factory.mktemp("studio"))
    os.environ["STUDIO_CONFIG"] = str(tmp_path_factory.mktemp("cfg") / "none.yaml")
    from fastapi.testclient import TestClient
    from studio.api import create_app
    from studio.core.config import StudioConfig
    with TestClient(create_app(StudioConfig.load())) as c:
        installed = c.post("/api/conformance/packages/evaluation").json()
        assert all(x["installed"] for x in installed) and len(installed) == 2
        yield c


def wait(client, rid, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = client.get(f"/api/runs/{rid}").json()
        if r["status"] in ("done", "failed") and not r.get("busy"):
            return r
        time.sleep(1)
    raise TimeoutError(rid)


def files(*names):
    out = {}
    for kind, name in names:
        p = SAMPLES / name
        out[kind] = (p.name, p.read_bytes())
    return out


@pytest.fixture(scope="module")
def baseline(client):
    pid = client.post("/api/projects", json={"name": "BSELV test"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/runs", data={"label": "Rev A"},
                    files=files(("step", "F6137-D24907000.stp"), ("mbom", "F6137-D24907000_APL.pdf"),
                                ("drawing", "F6137-D24907000_Drawing_1.pdf")))
    assert r.status_code == 201, r.text
    run = wait(client, r.json()["id"])
    assert run["status"] == "done", run.get("error")
    return pid, run


def test_baseline_outputs(client, baseline):
    pid, run = baseline
    st = {k: v["summary"] for k, v in run["stages"].items() if v.get("summary")}
    assert st["convert"]["occurrences"] >= 80
    assert st["analyse"]["conflicts"] == 0
    assert st["drawing"]["assigned"] == st["drawing"]["torques"] >= 8
    assert st["ipl"]["rows"] >= 45 and st["ipl"]["with_3d"] >= 30
    for kind in ("disassembly", "assembly"):
        assert st["procedures"][kind]["errors"] == 0
        assert st["procedures"][kind]["steps"] >= 25

    seq = client.get(f"/api/runs/{run['id']}/artifacts/sequence").json()
    pos = {s["unit"]: i for i, s in enumerate(seq["disassembly"])}
    assert not [p for p in seq["precedence"] if p["first"] in pos and pos[p["first"]] > pos[p["then"]]]

    ipl = client.get(f"/api/runs/{run['id']}/artifacts/ipl").json()
    items = [r["item"] for r in ipl["rows"]]
    assert len(items) == len(set(items)), "IPL item numbers are unique"
    assert all(r["sources"] for r in ipl["rows"])


def test_documents_checked_against_installed_schemas(client, baseline):
    _, run = baseline
    assert run["stages"]["check"]["status"] == "done"
    chk = client.get(f"/api/runs/{run['id']}/check").json()
    assert {d["key"] for d in chk["documents"]} == {"S1000D-530", "S1000D-710", "S1000D-941", "ATA-CMM"}
    for d in chk["documents"]:
        r = d["report"]
        assert r["statuses"]["structural"] == "passed", (d["name"], r["diagnostics"][:3])
        assert r["identification"]["package_id"], d["name"]
        assert r["passed"]
    s1000d = [d for d in chk["documents"] if d["key"].startswith("S1000D")]
    # the S1000D default BREX is not installed: said so, never reported as passed
    assert all(any(x["rule_id"] == "VAL-BREX-MISSING" for x in d["report"]["diagnostics"]) for d in s1000d)
    name = s1000d[0]["name"]
    view = client.get(f"/api/runs/{run['id']}/documents/{name}/view").json()
    assert view["stylesheet"]["id"] == "builtin-s1000d-view" and "Brake selector valve" in view["html"]
    xml = client.get(f"/api/runs/{run['id']}/documents/{name}").text
    assert xml.count("\n") > 50, "documents are indented, so problems point at useful lines"


def test_brex_and_rule_sets_gate_publishing(client, baseline):
    pid, run = baseline
    rid = run["id"]
    brex = EXAMPLES / "brex" / "DMC-SLSBSELV-A-00-00-00-00A-022A-D_001-00_EN-US.XML"
    assert client.post("/api/conformance/brex", files={"file": (brex.name, brex.read_bytes())}).status_code == 200
    rules = EXAMPLES / "rules" / "house-rules-s1000d.yaml"
    assert client.post("/api/conformance/rules", files={"file": (rules.name, rules.read_bytes())}).status_code == 200
    sch = EXAMPLES / "rules" / "cmm-checks.sch"
    # a low step limit so the report fires on this small product (subtasks are part by part)
    sch_bytes = sch.read_bytes().replace(b"count(prclist1/prcitem1) &gt; 15", b"count(prclist1/prcitem1) &gt; 2")
    assert client.post("/api/conformance/rules", files={"file": (sch.name, sch_bytes)},
                       data={"applies_to": '{"roots": ["cmm"]}'}).status_code == 200
    client.patch(f"/api/projects/{pid}/settings", json={"publication": {"brex_dmc": "SLSBSELV-A-00-00-00-00A-022A-D",
                                                                        "model_ident_code": "SLSBSELV"}})
    chk = client.post(f"/api/runs/{rid}/check").json()
    by = {d["key"]: d["report"] for d in chk["documents"]}
    rules_hit = {x["rule_id"] for x in by["S1000D-530"]["diagnostics"]}
    assert "SLS-005" in rules_hit                         # project BREX: inWork must be 00
    assert by["S1000D-530"]["statuses"]["business_rules"] == "failed"
    assert any(x["rule_id"] == "CMM-011" for x in by["ATA-CMM"]["diagnostics"])   # Schematron report (warning)
    assert by["ATA-CMM"]["passed"]
    # errors hold publishing back, unless the writer publishes anyway (recorded)
    r = client.post(f"/api/runs/{rid}/publish", json={"connector": "folder", "targets": ["s1000d"]})
    assert r.status_code == 409
    client.patch(f"/api/projects/{pid}/settings", json={"publication": {"in_work": "00"}})
    chk = client.post(f"/api/runs/{rid}/check").json()
    assert "SLS-005" not in {x["rule_id"] for d in chk["documents"] for x in d["report"]["diagnostics"]}
    # a transform stylesheet is applied before the check
    xsl = EXAMPLES / "stylesheets" / "set-originator.xsl"
    st = client.post("/api/conformance/stylesheets", files={"file": (xsl.name, xsl.read_bytes())},
                     data={"kind": "transform", "applies_to": '{"standards": ["S1000D"]}'}).json()
    chk = client.post(f"/api/runs/{rid}/check").json()
    doc = next(d for d in chk["documents"] if d["key"] == "S1000D-710")
    assert doc["transforms"][0]["id"] == st["id"]
    assert "firstVerification" in client.get(f"/api/runs/{rid}/documents/{doc['name']}").text
    client.delete(f"/api/conformance/stylesheets/{st['id']}")


def test_writer_edits_and_checks(client, baseline):
    _, run = baseline
    rid = run["id"]
    proc = client.get(f"/api/runs/{rid}/artifacts/procedure_disassembly").json()
    step = proc["subtasks"][1]["steps"][0]
    r = client.patch(f"/api/runs/{rid}/procedures/disassembly/steps/{step['id']}",
                     json={"text": "Carefully removing the screws (999)."}).json()
    assert {"STE-9.1", "FACT-ITM"} <= {i["rule"] for i in r["issues"]}
    r = client.patch(f"/api/runs/{rid}/procedures/disassembly/steps/{step['id']}", json={"text": step["text"]}).json()
    assert r["issues"] == []
    assert client.post(f"/api/runs/{rid}/ipl/accept", json={"rows": []}).json()["accepted"] > 20


def _retry(fn, tries=60):
    import time
    for _ in range(tries):
        r = fn()
        if r.status_code != 409:
            return r
        time.sleep(0.5)
    return r


def test_document_edit(client, baseline):
    _, run = baseline
    rid = run["id"]
    chk = client.get(f"/api/runs/{rid}/check").json()
    name = next(d["name"] for d in chk["documents"] if d["key"] == "S1000D-530")
    url = f"/api/runs/{rid}/documents/{name}"
    xml = client.get(url).text
    # live validation of unsaved text, nothing written
    bad = xml.replace("<content>", "<content><notAnElement/>", 1)
    rep = client.post(f"{url}/validate", json={"text": bad}).json()
    assert rep["counts"]["error"] + rep["counts"]["fatal"] > 0
    assert client.get(url).text == xml
    assert client.post(f"{url}/view", json={"text": xml}).json()["html"]
    # not well-formed: refused
    assert _retry(lambda: client.put(url, json={"text": xml[:-20]})).status_code == 400
    # saved edit replaces the generated document when checking and publishing
    edited = xml.replace("<techName>", "<techName>Edited ", 1)
    r = _retry(lambda: client.put(url, json={"text": edited}))
    assert r.status_code == 200, r.text
    doc = next(d for d in r.json()["documents"] if d["name"] == name)
    assert doc["edited"] and not doc["edited"]["stale"]
    assert "Edited " in client.get(url).text
    # discard: back to the generated document
    r = _retry(lambda: client.delete(f"{url}/edit"))
    doc = next(d for d in r.json()["documents"] if d["name"] == name)
    assert doc["edited"] is None and "Edited " not in client.get(url).text


def test_publish_to_folder(client, baseline):
    _, run = baseline
    r = client.post(f"/api/runs/{run['id']}/publish", json={"connector": "folder", "targets": ["s1000d", "ata", "techpub"],
                                                              "allow_errors": True})
    assert r.status_code == 200, r.text
    res = r.json()
    written = [Path(d["path"]) for d in res["documents"]]
    assert len(written) == 7 and all(p.exists() for p in written)
    assert any(p.suffix == ".xlsx" for p in written)
    xml = [d for d in res["documents"] if d["file"].upper().endswith(".XML")]
    assert len(xml) == 4 and all("statuses" in d for d in xml)       # the check travels with the file
    published = next(p for p in written if "530" in p.name).read_bytes()
    checked = client.get(f"/api/runs/{run['id']}/documents/{next(p for p in written if '530' in p.name).name}").content
    assert published == checked


def test_revision_against_baseline(client, baseline):
    pid, run = baseline
    assert client.post(f"/api/runs/{run['id']}/signoff", json={"by": "test"}).status_code == 200
    # a signed-off baseline is read-only
    assert client.post(f"/api/runs/{run['id']}/ipl/accept", json={}).status_code == 400
    if not (SAMPLES / "revB" / "F6137-D24907000_revB.stp").exists():
        pytest.skip("revision sample not available")
    r = client.post(f"/api/projects/{pid}/runs", data={"label": "Rev B"},
                    files=files(("step", "revB/F6137-D24907000_revB.stp"), ("mbom", "revB/F6137-D24907000_APL_revB.csv")))
    new = wait(client, r.json()["id"])
    assert new["status"] == "done", new.get("error")
    assert new["base_run"] == run["id"]
    assert "drawing" in new["inputs"], "inputs not uploaded again are kept from the previous run"
    cs = client.get(f"/api/runs/{new['id']}/artifacts/changeset").json()
    assert cs["summary"]["changes"] == 1
    c = cs["changes"][0]
    assert (c["type"], c["part_number_base"], c["part_number_new"], c["count"]) == \
        ("part_number_changed", "F6137-C77004-10", "F6137-C77004-12", 6)
    assert cs["summary"]["affected_steps"] >= 3
    assert cs["carry_forward"]["assembly"]["kept"] > 25
    mod = client.get(f"/api/runs/{new['id']}/artifacts/procedure_modification").json()
    assert mod["kind"] == "modification" and mod["subtasks"]


def test_variant_and_service_bulletin(client, baseline):
    """A variant that differs only in its parts list, and an SB modification on the shipped product."""
    pid, run = baseline
    if not (SAMPLES / "revB" / "F6137-D24907000_APL_revB.csv").exists():
        pytest.skip("revision sample not available")
    # variant: created from the baseline, only the parts list uploaded
    v = client.post("/api/projects", json={"name": "BSELV variant B", "parent": pid}).json()
    assert v["kind"] == "variant" and v["parent"]["run"] == run["id"]
    assert v["settings"]["publication"]["disassy_code_variant"] == "B"
    r = client.post(f"/api/projects/{v['id']}/runs", files=files(("mbom", "revB/F6137-D24907000_APL_revB.csv")))
    assert r.status_code == 201, r.text
    vr = wait(client, r.json()["id"])
    assert vr["status"] == "done", vr.get("error")
    assert vr["change"]["kind"] == "variant" and vr["base_run"] == run["id"]
    assert set(vr["inputs"]) == {"step", "mbom", "drawing"}            # 3D and drawing come from the parent
    cs = client.get(f"/api/runs/{vr['id']}/artifacts/changeset").json()
    assert cs["context"]["kind"] == "variant" and cs["summary"]["changes"] == 1
    assert cs["changes"][0]["type"] == "part_number_changed" and cs["changes"][0]["source"] == "IPL"
    nums = client.get(f"/api/runs/{vr['id']}/artifacts/ipl").json()["item_numbers"]
    assert nums["new"][0]["replaces_item"] in nums["retired"] and not set(n["item"] for n in nums["new"]) & set(nums["retired"])
    chk = client.get(f"/api/runs/{vr['id']}/check").json()
    assert any("-00B-" in d["name"] for d in chk["documents"])         # the variant's own data module codes

    # SB on the shipped product: needs its reference, raises the issue number, attaches the bulletin
    assert client.post(f"/api/projects/{pid}/runs", data={"change_kind": "modification"}).status_code == 400
    r = client.post(f"/api/projects/{pid}/runs",
                    data={"change_kind": "modification", "reference": "SB 32-0042", "change_title": "Replace the plug screws"},
                    files={**files(("step", "revB/F6137-D24907000_revB.stp"), ("mbom", "revB/F6137-D24907000_APL_revB.csv")),
                           "reference_doc": ("SB_32-0042.pdf", b"%PDF-1.4 bulletin")})
    assert r.status_code == 201, r.text
    sb = r.json()
    assert sb["label"] == "SB 32-0042" and sb["publication"]["issue_number"] == "002" and "reference" in sb["inputs"]
    sb = wait(client, sb["id"])
    assert sb["status"] == "done", sb.get("error")
    mod = client.get(f"/api/runs/{sb['id']}/artifacts/procedure_modification").json()
    assert mod["title"].startswith("SB 32-0042:") and mod["subtasks"]
    names = [d["name"] for d in client.get(f"/api/runs/{sb['id']}/check").json()["documents"]]
    assert any("_002-00_" in n for n in names)
    assert client.get(f"/api/runs/{sb['id']}/parts-import").json()["source"] in ("proposed", "confirmed")


def test_confirm_parts_mapping_and_llm_settings(client, baseline):
    pid, _ = baseline
    rid = client.get(f"/api/projects/{pid}").json()["runs"][-1]
    if client.get(f"/api/runs/{rid}").json().get("signed_off"):
        pytest.skip("last run is a baseline")
    imp = client.get(f"/api/runs/{rid}/parts-import").json()
    r = client.put(f"/api/runs/{rid}/parts-import", json={"table": imp["mapping"]["table"], "columns": imp["mapping"]["columns"]})
    assert r.status_code == 200, r.text and r.json()["rows"] > 40
    wait(client, rid)
    imp = client.get(f"/api/runs/{rid}/parts-import").json()
    assert imp["source"] == "confirmed" and not imp["needs_review"]
    assert client.put(f"/api/runs/{rid}/parts-import", json={"table": "nope", "columns": {}}).status_code == 400
    # language model settings: saved in the app, key never returned, test reports an unreachable server
    s = client.put("/api/settings/llm", json={"provider": "ollama", "base_url": "http://127.0.0.1:9/v1", "model": "qwen2.5:7b",
                                              "api_key": "secret-1234"}).json()
    assert s["settings"]["api_key"].endswith("1234") and "secret" not in s["settings"]["api_key"]
    assert client.get("/api/settings/llm").json()["source"] == "app"
    assert client.post("/api/settings/llm/test", json={}).json()["ok"] is False
    client.put("/api/settings/llm", json={"provider": "none"})
