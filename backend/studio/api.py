"""HTTP API for the Procedure Studio UI (and for TechPub / scripts: everything the UI does is here)."""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .core import publishing
from .core.checking import check_run, checked_files, discard_edit, save_edit, STANDARD_OF
from .core.config import StudioConfig
from .core.pipeline import STAGE_LABEL, STAGES, Pipeline
from .core.store import Store, StoreError, read_json, write_json
from .procedures.ste import STEChecker, fact_check, technical_names_from

log = logging.getLogger("studio.api")

IPL_EDITABLE = {"nomenclature", "units_per_assy", "item", "effectivity", "attaching_for", "role", "torque",
                "status", "occ_ids", "notes", "cage"}
DOWNSTREAM_OF = {"role": "process", "attaching_for": "process", "occ_ids": "process", "torque": "process",
                 "units_per_assy": "process", "nomenclature": "process", "item": "process", "effectivity": "process",
                 "status": "process", "notes": None, "cage": None}


class ProjectIn(BaseModel):
    name: str
    settings: dict = {}
    parent: str | None = None          # make this product a variant of a product with a signed-off baseline


class RowEdit(BaseModel):
    fields: dict


class StepEdit(BaseModel):
    text: str


class SequenceEdit(BaseModel):
    order: list[str]           # unit ids in the new disassembly order


class DocText(BaseModel):
    text: str
    stylesheet: str | None = None


class PublishIn(BaseModel):
    connector: str = "folder"
    targets: list[str] = ["s1000d", "ata"]
    publication: dict = {}
    allow_errors: bool = False          # publish documents that failed validation (recorded in the result)


def create_app(cfg: StudioConfig | None = None) -> FastAPI:
    cfg = cfg or StudioConfig.load()
    store = Store(cfg.data_dir)
    pipe = Pipeline(cfg, store)
    cfg.conformance()                     # open the schema registry once, before requests arrive
    if pipe.recover():
        log.warning("runs left running by a previous server were marked interrupted")
    app = FastAPI(title="TechPub Procedure Studio", version=__version__, docs_url="/api/docs")
    app.state.cfg, app.state.store, app.state.pipe = cfg, store, pipe
    origins = os.environ.get("STUDIO_CORS", r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$")
    app.add_middleware(CORSMiddleware, allow_origin_regex=origins, allow_methods=["*"], allow_headers=["*"])

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except StoreError as e:
            raise HTTPException(404 if "no such" in str(e) else 400, str(e))

    def run_dir(rid: str) -> Path:
        return guard(store.find_run, rid)[1]

    # ------------------------------------------------------------------ system
    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/system")
    def system():
        from .cad.converters import get_converter
        conv = get_converter(cfg.converter)
        ok, why = conv.available()
        conns = []
        for c in cfg.connectors:
            try:
                conns.append(cfg.connector(c["kind"]).status())
            except Exception as e:
                conns.append({"name": c.get("kind"), "available": False, "error": str(e)})
        return {**cfg.describe(), "converter_status": {"kind": conv.kind, "available": ok, "detail": why},
                "connectors_status": conns, "stages": [{"id": s, "label": STAGE_LABEL[s]} for s in STAGES],
                "targets": publishing.TARGETS, "version": __version__,
                "conformance": {k: len(v) if isinstance(v, list) else v for k, v in cfg.conformance().status().items()
                                if k in ("packages", "brex", "rules", "stylesheets")}}

    from .conformance_api import mount_conformance
    from .settings_api import mount_settings
    mount_conformance(app, cfg)
    mount_settings(app, cfg)

    # ------------------------------------------------------------------ projects
    @app.get("/api/projects")
    def projects():
        return store.projects()

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectIn):
        return guard(store.create_project, body.name, body.settings, body.parent)

    @app.get("/api/projects/{pid}")
    def project(pid: str):
        p = guard(store.project, pid)
        p["run_list"] = [guard(store.run, r, pid) for r in p["runs"]]
        return p

    @app.patch("/api/projects/{pid}/settings")
    def settings(pid: str, body: dict):
        return guard(store.update_settings, pid, body)

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        guard(store.delete_project, pid)
        return {"ok": True}

    # ------------------------------------------------------------------ runs
    @app.post("/api/projects/{pid}/runs", status_code=201)
    async def create_run(pid: str, step: UploadFile | None = File(None), mbom: UploadFile | None = File(None),
                         drawing: UploadFile | None = File(None), reference_doc: UploadFile | None = File(None),
                         label: str = Form(""), base_run: str | None = Form(None), reuse_from: str | None = Form(None),
                         change_kind: str = Form(""), reference: str = Form(""), change_title: str = Form(""),
                         reason: str = Form("")):
        """Start a run. What it is decides what it is compared with:

        * ``initial``       the first run of a product: nothing to compare with
        * ``revision``      a new issue of the same product (new drawing, 3D or BOM): compared with its baseline
        * ``modification``  a change to units in service (SB, MOD, ECN): compared with the baseline, and a
                            modification procedure is written; ``reference`` is required
        * ``variant``       the first run of a variant product: compared with the parent product's baseline

        Files not uploaded are taken from the previous run (or, for a variant's first run, from the parent's
        baseline), so a run can carry only what changed."""
        p = guard(store.project, pid)
        parent = p.get("parent")
        kind = change_kind or ("revision" if p.get("baseline") else "variant" if parent else "initial")
        if kind not in ("initial", "revision", "modification", "variant"):
            raise HTTPException(400, f"unknown change kind {kind}")
        if kind == "modification" and not reference.strip():
            raise HTTPException(400, "a modification needs its reference (SB, MOD or ECN number)")
        if kind == "variant" and not parent:
            raise HTTPException(400, "this product is not a variant of another product")
        if kind in ("revision", "modification") and not (base_run or p.get("baseline")):
            raise HTTPException(400, "sign off a run of this product first: revisions and modifications are compared "
                                     "with the signed-off baseline")
        base = base_run or (parent["run"] if kind == "variant" else p.get("baseline") if kind != "initial" else None)
        with tempfile.TemporaryDirectory() as tmp:
            inputs: dict[str, Path] = {}
            for k, up, exts in (("step", step, (".stp", ".step")), ("mbom", mbom, (".pdf", ".xlsx", ".xls", ".xlsm", ".csv")),
                                ("drawing", drawing, (".pdf",)),
                                ("reference", reference_doc, (".pdf", ".docx", ".doc", ".xlsx", ".txt", ".xml", ".zip"))):
                if up is not None and up.filename:
                    name = Path(up.filename).name
                    if not name.lower().endswith(exts):
                        raise HTTPException(400, f"{k}: expected {', '.join(exts)}, got {name}")
                    dst = Path(tmp) / name
                    with open(dst, "wb") as f:
                        shutil.copyfileobj(up.file, f)
                    inputs[k] = dst
            src_run = reuse_from or (p["runs"][-1] if p["runs"] else parent["run"] if parent else None)
            if src_run:                           # inputs not uploaded again are taken from the previous run
                sr = guard(store.run, src_run)
                _, sd = store.find_run(src_run)
                for k, info in sr["inputs"].items():
                    if k != "reference":          # a bulletin belongs to its own run
                        inputs.setdefault(k, sd / "inputs" / info["file"])
            if "step" not in inputs or "mbom" not in inputs:
                raise HTTPException(400, "a run needs at least the STEP model and the parts list (MBOM / APL)")
            change = {"kind": kind, "reference": reference.strip() or None, "title": change_title.strip() or None,
                      "reason": reason.strip() or None, "source_run": src_run}
            publication = {}
            if kind in ("revision", "modification") and base:
                br = guard(store.run, base)
                prev = (br.get("publication") or {}).get("issue_number") or \
                    ((p.get("settings") or {}).get("publication") or {}).get("issue_number") or "001"
                publication = {"issue_number": f"{int(prev) + 1:03d}" if str(prev).isdigit() else prev, "in_work": "00"}
            default_label = {"initial": "Issue 1", "variant": f"{p['name']} first issue",
                             "revision": f"Issue {int(publication.get('issue_number', '2'))}",
                             "modification": reference.strip()}[kind]
            r = store.create_run(pid, inputs, base_run=base, label=label or default_label, change=change,
                                 publication=publication)
        pipe.start(r["id"])
        return r

    @app.get("/api/runs/{rid}")
    def get_run(rid: str):
        r = guard(store.run, rid)
        r["busy"] = pipe.busy(rid)
        if not r["busy"] and r.get("status") in ("running", "queued"):
            r = pipe.mark_interrupted(rid) or store.run(rid)
            r["busy"] = False
        return r

    @app.post("/api/runs/{rid}/rerun")
    def rerun(rid: str, from_stage: str = "convert"):
        guard(store.guard_editable, rid)
        if from_stage not in STAGES:
            raise HTTPException(400, f"unknown stage {from_stage}")
        pipe.start(rid, from_stage)
        return {"started": True, "from": from_stage}

    @app.get("/api/runs/{rid}/artifacts/{name}")
    def artifact(rid: str, name: str):
        obj = guard(store.artifact, rid, name if name.endswith(".json") else name + ".json")
        if obj is None:
            raise HTTPException(404, f"{name} not produced yet")
        return JSONResponse(obj)

    @app.get("/api/runs/{rid}/model.glb")
    def model(rid: str):
        r = guard(store.run, rid)
        p = run_dir(rid) / (r.get("model_file") or "")
        if not r.get("model_file") or not p.exists():
            raise HTTPException(404, "no 3D model yet")
        return FileResponse(p, media_type="model/gltf-binary", headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/runs/{rid}/inputs/{kind}")
    def input_file(rid: str, kind: str):
        r = guard(store.run, rid)
        i = r["inputs"].get(kind)
        if not i:
            raise HTTPException(404, "no such input")
        return FileResponse(run_dir(rid) / "inputs" / i["file"], filename=i["file"])

    # ------------------------------------------------------------------ parts list import
    def _mbom_path(rid: str) -> Path:
        r = guard(store.run, rid)
        i = r["inputs"].get("mbom")
        if not i:
            raise HTTPException(404, "this run has no parts list")
        return run_dir(rid) / "inputs" / i["file"]

    @app.get("/api/runs/{rid}/parts-import")
    def parts_import_get(rid: str):
        """How the parts list was read (or, if reading failed, what was found), with every candidate table."""
        from .engineering import parts_import as pi
        saved = read_json(run_dir(rid) / "mbom_import.json")
        if saved:
            return saved
        insp = pi.inspect(_mbom_path(rid))
        return {"file": insp["file"], "mapping": pi.mapping_from_proposal(insp["chosen"]) if insp["chosen"] else None,
                "source": "proposed", "needs_review": True, "problems": insp["problems"], "tables": insp["tables"],
                "chosen": insp["chosen"]["id"] if insp["chosen"] else None, "rows": 0}

    @app.put("/api/runs/{rid}/parts-import")
    def parts_import_put(rid: str, body: dict):
        """Confirm a mapping {table, columns: {field: header text}}. It is checked by reading the file, kept for
        this product and file layout (the next issue of the same export loads without asking), and the run is
        processed again from the reconciliation."""
        from .engineering import parts_import as pi
        guard(store.guard_editable, rid)
        path = _mbom_path(rid)
        insp = pi.inspect(path)
        table = next((t for t in insp["tables"] if t["id"] == body.get("table")), None) or \
            (next((t for t in insp["tables"] if t["id"] in (insp["chosen"] or {}).get("parts", [])), None)
             if body.get("table") == "pdf:*" else None)
        if table is None:
            raise HTTPException(400, "choose one of the tables found in the file")
        mapping = {"table": body["table"], "signature": table["signature"],
                   "columns": {f: h for f, h in (body.get("columns") or {}).items() if h}}
        try:
            recs = pi.load(path, mapping)
        except ValueError as e:
            raise HTTPException(400, str(e))
        pid, _ = store.find_run(rid)
        store.update_settings(pid, {"parts_mappings": {table["signature"]: {**mapping, "confirmed_by": "writer"}}})
        store.log_edit(rid, {"kind": "parts-import", "mapping": mapping, "rows": len(recs)})
        if pipe.busy(rid):
            raise HTTPException(409, "the run is being processed; the mapping is saved and applies to the next run")
        pipe.start(rid, "reconcile")
        return {"rows": len(recs), "sample": recs[:5], "recompute": "reconcile"}

    # ------------------------------------------------------------------ IPL review
    @app.patch("/api/runs/{rid}/ipl/rows/{row_id}")
    def edit_row(rid: str, row_id: str, body: RowEdit):
        guard(store.guard_editable, rid)
        d = run_dir(rid)
        ipl = read_json(d / "ipl.json")
        row = next((x for x in ipl["rows"] if x["id"] == row_id), None)
        if not row:
            raise HTTPException(404, "no such IPL row")
        bad = set(body.fields) - IPL_EDITABLE
        if bad:
            raise HTTPException(400, f"not editable: {', '.join(sorted(bad))}")
        before = {k: row.get(k) for k in body.fields}
        for k, v in body.fields.items():
            row[k] = v
            if k != "status":
                row.setdefault("edited_fields", [])
                if k not in row["edited_fields"]:
                    row["edited_fields"].append(k)
                row["sources"][k] = "writer"
        if set(body.fields) - {"status"}:
            row["status"] = "edited"
        write_json(d / "ipl.json", ipl)
        store.log_edit(rid, {"kind": "ipl", "row": row_id, "before": before, "after": body.fields})
        stage = next((DOWNSTREAM_OF[k] for k in body.fields if DOWNSTREAM_OF.get(k)), None)
        if stage:
            pipe.start(rid, stage)
        return {"row": row, "recompute": stage}

    @app.post("/api/runs/{rid}/ipl/accept")
    def accept_rows(rid: str, body: dict):
        guard(store.guard_editable, rid)
        d = run_dir(rid)
        ipl = read_json(d / "ipl.json")
        ids = set(body.get("rows") or [])
        n = 0
        for row in ipl["rows"]:
            if (not ids or row["id"] in ids) and row.get("status") == "proposed" and (ids or not row.get("flags")):
                row["status"] = "accepted"
                n += 1
        write_json(d / "ipl.json", ipl)
        store.log_edit(rid, {"kind": "ipl-accept", "rows": sorted(ids) or "all unflagged", "count": n})
        return {"accepted": n}

    # ------------------------------------------------------------------ sequence
    @app.put("/api/runs/{rid}/sequence")
    def reorder(rid: str, body: SequenceEdit):
        guard(store.guard_editable, rid)
        d = run_dir(rid)
        seq = read_json(d / "sequence.json")
        by_unit = {s["unit"]: s for s in seq["disassembly"]}
        if set(body.order) != set(by_unit):
            raise HTTPException(400, "the new order must contain every disassembly step exactly once")
        prec = [(p["first"], p["then"]) for p in seq.get("precedence", [])]
        pos = {u: i for i, u in enumerate(body.order)}
        broken = [f"{a} must come off before {b}" for a, b in prec if a in pos and b in pos and pos[a] > pos[b]]
        steps = []
        for i, u in enumerate(body.order, start=1):
            s = dict(by_unit[u])
            s["step"], s["layer"] = i, i
            s["edited"] = True
            steps.append(s)
        seq["disassembly"] = steps
        seq["assembly"] = [{**s, "step": i + 1} for i, s in enumerate(reversed(steps))]
        seq["writer_order"] = True
        seq["order_warnings"] = broken
        write_json(d / "sequence.json", seq)
        store.log_edit(rid, {"kind": "sequence", "order": body.order, "warnings": broken})
        pipe.start(rid, "ipl")
        return {"warnings": broken}

    # ------------------------------------------------------------------ procedure text
    @app.patch("/api/runs/{rid}/procedures/{kind}/steps/{step_id}")
    def edit_step(rid: str, kind: str, step_id: str, body: StepEdit):
        guard(store.guard_editable, rid)
        if kind not in ("assembly", "disassembly"):
            raise HTTPException(400, "kind must be assembly or disassembly")
        d = run_dir(rid)
        proc = read_json(d / f"procedure_{kind}.json")
        process = read_json(d / "process.json")
        ipl = read_json(d / "ipl.json")
        ops = {o["id"]: o for o in process[kind]["operations"]}
        for st in proc["subtasks"]:
            for s in st["steps"]:
                if s["id"] == step_id:
                    before = s["text"]
                    s["text"], s["origin"] = body.text.strip(), "writer"
                    op = ops.get(s["op_ids"][0]) if s["op_ids"] else {}
                    s["issues"] = STEChecker(technical_names=technical_names_from(ipl)).check(s["text"], s["kind"]) + (
                        fact_check(s["text"], op, {r["item"] for r in ipl["rows"] if r.get("item")}) if op else [])
                    write_json(d / f"procedure_{kind}.json", proc)
                    store.log_edit(rid, {"kind": "step", "procedure": kind, "step": step_id, "before": before, "after": s["text"]})
                    if not pipe.busy(rid):
                        pipe.start(rid, "check")        # the documents change with the text: check them again
                    return s
        raise HTTPException(404, "no such step")

    # ------------------------------------------------------------------ baseline / revisions
    @app.post("/api/runs/{rid}/signoff")
    def signoff(rid: str, body: dict | None = None):
        return guard(store.sign_off, rid, (body or {}).get("by", "writer"))

    # ------------------------------------------------------------------ publish / export
    @app.post("/api/runs/{rid}/publish")
    def publish(rid: str, body: PublishIn):
        r = guard(store.run, rid)
        if pipe.busy(rid) and (r.get("progress") or {}).get("stage") == "check":
            pipe.wait(rid)                     # a re-check after an edit: publishing checks again anyway
            r = store.run(rid)
        if r["status"] != "done":
            raise HTTPException(400, "the run is not finished")
        try:
            return publishing.publish(cfg, store, cfg.conformance(), rid, body.connector, body.targets, body.publication,
                                      body.allow_errors)
        except publishing.PublishBlocked as e:
            raise HTTPException(409, {"message": str(e), "summary": e.check["summary"]})
        except KeyError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            log.exception("publish failed")
            raise HTTPException(502, str(e))

    @app.get("/api/runs/{rid}/publications")
    def publications(rid: str):
        d = run_dir(rid) / "publish"
        return [read_json(p) | {"file": p.name} for p in sorted(d.glob("*.json"))] if d.is_dir() else []

    @app.get("/api/runs/{rid}/export/{target}")
    def export(rid: str, target: str):
        guard(store.run, rid)
        if target not in publishing.TARGETS:
            raise HTTPException(400, f"unknown target {target}")
        files, _ = checked_files(cfg.conformance(), store, rid, [target])
        if len(files) == 1:
            f = files[0]
            mt = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if f.name.endswith(".xlsx") else "application/xml"
            return Response(f.data, media_type=mt, headers={"Content-Disposition": f'attachment; filename="{f.name}"'})
        return Response(publishing.zip_bundle(files), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{target}_{rid}.zip"'})

    # ------------------------------------------------------------------ checked documents
    @app.get("/api/runs/{rid}/check")
    def get_check(rid: str):
        obj = read_json(run_dir(rid) / "check.json")
        if obj is None:
            raise HTTPException(404, "the documents have not been checked yet")
        return obj

    @app.post("/api/runs/{rid}/check")
    def run_check(rid: str):
        """Check the documents again (after new schemas, BREX, rules or settings). Allowed on a baseline:
        it does not change the content."""
        if pipe.busy(rid):
            raise HTTPException(409, "the run is being processed")
        r = guard(store.run, rid)
        if r["status"] != "done" and r["stages"].get("procedures", {}).get("status") != "done":
            raise HTTPException(400, "the procedures are not generated yet")
        return _recheck(rid)

    def _recheck(rid: str) -> dict:
        res = check_run(cfg.conformance(), store, rid)
        r = store.run(rid)
        r["stages"]["check"] = {"status": "done", "label": STAGE_LABEL["check"], "summary": res["summary"], "seconds": None}
        store.save_run(r)
        return res

    def _document(rid: str, name: str) -> bytes:
        p = run_dir(rid) / "documents" / Path(name).name
        if not p.is_file():
            raise HTTPException(404, "no such checked document; check the documents first")
        return p.read_bytes()

    @app.get("/api/runs/{rid}/documents/{name}")
    def document_source(rid: str, name: str):
        return Response(_document(rid, name), media_type="application/xml")

    @app.get("/api/runs/{rid}/documents/{name}/view")
    def document_view(rid: str, name: str, stylesheet: str | None = None):
        try:
            return cfg.conformance().view(_document(rid, name), stylesheet)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e))

    def _checked_doc(rid: str, name: str) -> dict:
        res = read_json(run_dir(rid) / "check.json") or {}
        doc = next((x for x in res.get("documents", []) if x["name"] == Path(name).name), None)
        if not doc:
            raise HTTPException(404, "no such checked document; check the documents first")
        return doc

    def _xml_or_400(text: str) -> bytes:
        from lxml import etree
        from .conformance.security.xml_safe import document_parser
        data = text.encode("utf-8")
        try:
            etree.fromstring(data, document_parser())
        except etree.XMLSyntaxError as e:
            raise HTTPException(400, f"The XML is not well-formed, so it was not saved: {e}")
        return data

    @app.post("/api/runs/{rid}/documents/{name}/validate")
    def document_validate(rid: str, name: str, body: DocText):
        """Check unsaved text exactly as the saved document would be checked (nothing is written)."""
        doc = _checked_doc(rid, name)
        project = store.project(store.find_run(rid)[0])
        prefer = ((project.get("settings") or {}).get("schemas") or {}).get(doc["target"])
        return cfg.conformance().check(body.text.encode("utf-8"), doc["name"], prefer=prefer,
                                       standard_hint=STANDARD_OF.get(doc["target"]))

    @app.post("/api/runs/{rid}/documents/{name}/view")
    def document_view_text(rid: str, name: str, body: DocText):
        _checked_doc(rid, name)
        try:
            return cfg.conformance().view(body.text.encode("utf-8"), body.stylesheet)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e))

    @app.put("/api/runs/{rid}/documents/{name}")
    def document_save(rid: str, name: str, body: DocText):
        """Save a writer's edit of a document; it replaces the generated one when checking and publishing."""
        guard(store.guard_editable, rid)
        if pipe.busy(rid):
            raise HTTPException(409, "the run is being processed")
        _checked_doc(rid, name)
        data = _xml_or_400(body.text)
        # the generated document as it is now (settings, steps or rules may have changed since the last check)
        res = check_run(cfg.conformance(), store, rid)
        doc = next(x for x in res["documents"] if x["name"] == Path(name).name)
        d = run_dir(rid)
        before = (d / "documents" / doc["name"]).read_bytes() if (d / "documents" / doc["name"]).is_file() else b""
        save_edit(d, doc["name"], data, doc.get("generated_sha256") or "")
        store.log_edit(rid, {"kind": "document", "document": doc["name"], "bytes_before": len(before), "bytes_after": len(data)})
        return _recheck(rid)

    @app.delete("/api/runs/{rid}/documents/{name}/edit")
    def document_discard(rid: str, name: str):
        """Go back to the generated document."""
        guard(store.guard_editable, rid)
        if pipe.busy(rid):
            raise HTTPException(409, "the run is being processed")
        doc = _checked_doc(rid, name)
        if discard_edit(run_dir(rid), doc["name"]):
            store.log_edit(rid, {"kind": "document", "document": doc["name"], "discarded": True})
        return _recheck(rid)

    # ------------------------------------------------------------------ UI
    dist = Path(__file__).resolve().parent / "web"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")
    return app
