"""The "check" stage: render the run's publications, apply the transform stylesheets, validate each document
(schema, BREX, rule sets), and keep the result.

    <run>/documents/<file>      the documents exactly as checked (and as published)
    <run>/check.json            one report per document + a summary
    <run>/doc_edits/<file>      a writer's edit of a document (replaces the generated one when checking and
    <run>/doc_edits/<file>.json   publishing) + the hash of the generated document it was made from

When the generated document changes after an edit (a step reworded, the IPL changed), the edit is kept and
the check warns that it is stale, so nothing a writer changed is lost silently.

Publishing and exports always use these files, after checking again, so what leaves Studio is what was checked.
"""
from __future__ import annotations

import hashlib
import shutil
import threading
from collections import defaultdict
from pathlib import Path

from ..connectors.base import BundleFile
from .publishing import render
from .store import now, read_json, write_json

STANDARD_OF = {"s1000d_proced": "S1000D", "s1000d_ipd": "S1000D", "ata_cmm": "ATA2200"}
DOC_TYPE_OF = {"s1000d_proced": "proced", "s1000d_ipd": "ipd", "ata_cmm": "cmm"}
CHECKED_TARGETS = ("s1000d", "ata")
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)     # one check at a time per run


def check_run(conf, store, rid: str, targets: list[str] | None = None) -> dict:
    with _locks[rid]:
        return _check_run(conf, store, rid, targets)


def _check_run(conf, store, rid: str, targets: list[str] | None = None) -> dict:
    run = store.run(rid)
    pid, d = store.find_run(rid)
    project = store.project(pid)
    settings = project.get("settings") or {}
    targets = [t for t in (targets or settings.get("check_targets") or CHECKED_TARGETS) if t in CHECKED_TARGETS]
    files = render(d, project, run, targets)
    out = d / "documents"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    schemas = settings.get("schemas") or {}
    docs = []
    for f in files:
        std, dtype = STANDARD_OF.get(f.target), DOC_TYPE_OF.get(f.target)
        data, used = f.data, []
        try:
            data, used = conf.transform(f.data, std, dtype)
            transform_error = None
        except ValueError as e:
            transform_error = str(e)
        generated_sha = hashlib.sha256(data).hexdigest()
        edit = edit_of(d, f.name)
        edited = None
        if edit:
            data = (d / EDITS / f.name).read_bytes()
            edited = {"at": edit.get("at"), "by": edit.get("by"), "stale": edit.get("base_sha256") != generated_sha}
        (out / f.name).write_bytes(data)
        report = conf.check(data, f.name, prefer=schemas.get(f.target), standard_hint=std)
        if edited and edited["stale"]:
            report["diagnostics"].insert(0, {"stage": 1, "severity": "warning", "rule_id": "STUDIO-EDIT-STALE",
                                             "message": "This document was edited by hand, and the generated document has changed since "
                                                        "(steps, IPL or settings). The edit is what is checked and published.",
                                             "suggestion": "Compare with the generated document, then save the edit again or discard it."})
            report["counts"]["warning"] += 1
        if transform_error:
            report["diagnostics"].insert(0, {"stage": 1, "severity": "error", "rule_id": "STUDIO-TRANSFORM",
                                             "message": f"A transform stylesheet failed, so the document was checked untransformed: {transform_error}"})
            report["counts"]["error"] += 1
            report["passed"] = False
        docs.append({"name": f.name, "key": f.key, "target": f.target, "sha256": hashlib.sha256(data).hexdigest(),
                     "generated_sha256": generated_sha, "edited": edited,
                     "bytes": len(data), "transforms": [{"id": u["id"], "name": u["name"]} for u in used],
                     "report": report})
    summary = {"documents": len(docs), "passed": sum(1 for x in docs if x["report"]["passed"]),
               "errors": sum(x["report"]["counts"]["error"] + x["report"]["counts"]["fatal"] for x in docs),
               "warnings": sum(x["report"]["counts"]["warning"] for x in docs),
               "not_validated": sum(1 for x in docs if x["report"]["statuses"]["structural"] in ("unsupported", "not-run")),
               "business_rules_checked": sum(1 for x in docs if x["report"]["statuses"]["business_rules"] in ("passed", "failed"))}
    result = {"format": "studio.check/1", "at": now(), "targets": targets, "documents": docs, "summary": summary}
    write_json(d / "check.json", result)
    return result


def checked_files(conf, store, rid: str, targets: list[str]) -> tuple[list[BundleFile], dict | None]:
    """The files to publish or export for ``targets``: checked XML for S1000D/ATA (checked again now),
    generated files for the others. -> (files, check result or None)."""
    pid, d = store.find_run(rid)
    files: list[BundleFile] = []
    result = None
    xml_targets = [t for t in targets if t in CHECKED_TARGETS]
    if xml_targets:
        result = check_run(conf, store, rid, xml_targets)
        for doc in result["documents"]:
            files.append(BundleFile(doc["name"], (d / "documents" / doc["name"]).read_bytes(), key=doc["key"],
                                    target=doc["target"]))
    other = [t for t in targets if t not in CHECKED_TARGETS]
    if other:
        files += render(d, store.project(pid), store.run(rid), other)
    return files, result


EDITS = "doc_edits"


def edit_of(run_dir: Path, name: str) -> dict | None:
    meta = run_dir / EDITS / f"{Path(name).name}.json"
    return read_json(meta) if (run_dir / EDITS / Path(name).name).is_file() else None


def save_edit(run_dir: Path, name: str, data: bytes, base_sha256: str, by: str = "writer") -> dict:
    """Keep a writer's edit of a checked document. ``base_sha256``: the generated document it was made from."""
    name = Path(name).name
    (run_dir / EDITS).mkdir(exist_ok=True)
    (run_dir / EDITS / name).write_bytes(data)
    meta = {"at": now(), "by": by, "base_sha256": base_sha256, "sha256": hashlib.sha256(data).hexdigest()}
    write_json(run_dir / EDITS / f"{name}.json", meta)
    return meta


def discard_edit(run_dir: Path, name: str) -> bool:
    name = Path(name).name
    hit = False
    for p in (run_dir / EDITS / name, run_dir / EDITS / f"{name}.json"):
        if p.is_file():
            p.unlink()
            hit = True
    return hit


def last_check(store, rid: str) -> dict | None:
    _, d = store.find_run(rid)
    return read_json(d / "check.json")
