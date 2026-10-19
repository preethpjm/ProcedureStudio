"""Render a run into publication files, and publish the checked files through a connector."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from ..connectors.base import Bundle, BundleFile
from ..publish.ata import render_cmm
from ..publish.config import PublicationConfig
from ..publish.s1000d import render_ipd, render_procedure
from ..publish.techpub import editor_json, ipl_excel
from .store import read_json, now, write_json

TARGETS = {
    "s1000d": "S1000D data modules (530 disassemble, 710 assemble, 941 IPD)",
    "ata": "ATA iSpec 2200 CMM (XML): disassembly, assembly, IPL",
    "techpub": "TechPub: IPL Excel (Generate-from-Excel format) + procedure editor JSON",
}


def publication_config(project: dict, run: dict, ipl: dict, overrides: dict | None = None) -> PublicationConfig:
    s = {**(project.get("settings") or {}).get("publication", {}), **(run.get("publication") or {}), **(overrides or {})}
    top = next((r for r in ipl["rows"] if r["depth"] == 0), {})
    s.setdefault("enterprise_code", top.get("cage") or (run.get("manuf_prefix") or ""))
    s.setdefault("applicability", top.get("part_number", ""))
    s.setdefault("cmm_doc_number", top.get("part_number", ""))
    return PublicationConfig.from_dict(s)


def publication_title(ipl: dict) -> str:
    """The product's name for titles: the parts list's name of the top item ("Brake selector valve"),
    else the IPL nomenclature turned around ("VALVE, BRAKE SELECTOR"), else the figure title."""
    top = next((r for r in ipl["rows"] if r["depth"] == 0), {})
    name = top.get("nomenclature_mbom")
    if not name and top.get("nomenclature") and "," in top["nomenclature"]:
        noun, adj = top["nomenclature"].split(",", 1)
        name = f"{adj.strip()} {noun.strip()}"
    name = name or top.get("nomenclature") or (ipl.get("figure") or {}).get("title") or "Component"
    return name[:1].upper() + name[1:].lower() if name.isupper() else name


def render(run_dir: Path, project: dict, run: dict, targets: list[str], overrides: dict | None = None) -> list[BundleFile]:
    ipl = read_json(run_dir / "ipl.json")
    procs = [read_json(run_dir / f"procedure_{k}.json") for k in ("disassembly", "assembly")]
    cfg = publication_config(project, run, ipl, overrides)
    title = publication_title(ipl)
    files: list[BundleFile] = []
    if "s1000d" in targets:
        for p in procs:
            n, x = render_procedure(p, cfg, title)
            files.append(BundleFile(n, x.encode("utf-8"), key=f"S1000D-{'530' if p['kind'] == 'disassembly' else '710'}",
                                    target="s1000d_proced"))
        n, x = render_ipd(ipl, cfg, title)
        files.append(BundleFile(n, x.encode("utf-8"), key="S1000D-941", target="s1000d_ipd"))
    if "ata" in targets:
        n, x = render_cmm(procs, ipl, cfg, title)
        files.append(BundleFile(n, x.encode("utf-8"), key="ATA-CMM", target="ata_cmm"))
    files = [BundleFile(f.name, _pretty(f.data), key=f.key, target=f.target, kind=f.kind) for f in files]
    if "techpub" in targets:
        files.append(BundleFile("IPL_TechPub.xlsx", ipl_excel(ipl), key="TECHPUB-IPL", target="techpub_ipl", kind="export"))
        for p in procs:
            files.append(BundleFile(f"procedure_{p['kind']}_editor.json", json.dumps(editor_json(p), indent=1).encode(),
                                    key=f"TECHPUB-{p['kind'].upper()}", target="techpub_editor", kind="export"))
    return files


def _pretty(xml: bytes) -> bytes:
    """Indented XML, so validation messages point at useful line numbers."""
    from lxml import etree
    parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(xml, parser)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def zip_bundle(files: list[BundleFile]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.writestr(f.name, f.data)
    return buf.getvalue()


class PublishBlocked(Exception):
    """Documents with validation errors are not published unless the writer says so."""

    def __init__(self, message: str, check: dict):
        super().__init__(message)
        self.check = check


def publish(cfg, store, conf, rid: str, connector: str, targets: list[str], overrides: dict | None = None,
            allow_errors: bool = False) -> dict:
    from .checking import checked_files
    run = store.run(rid)
    pid, d = store.find_run(rid)
    project = store.project(pid)
    if overrides:
        store.update_settings(pid, {"publication": overrides})
    files, check = checked_files(conf, store, rid, targets)
    if check and check["summary"]["errors"] and not allow_errors:
        bad = [x["name"] for x in check["documents"] if not x["report"]["passed"]]
        raise PublishBlocked(f"{len(bad)} document(s) have validation errors: {', '.join(bad)}. Fix them, or publish "
                             "anyway with the errors recorded.", check)
    reports = {x["name"]: x["report"] for x in (check or {}).get("documents", [])}
    conn = cfg.connector(connector)
    bundle = Bundle(project={"name": project["name"], "description": f"{project['name']} — Procedure Studio"},
                    run={"run_id": rid, "label": run.get("label"), "tool": "techpub-procedure-studio",
                         "baseline": bool(run.get("signed_off"))},
                    message=f"Procedure Studio {run.get('label', rid)}"
                            + (" (signed-off baseline)" if run.get("signed_off") else ""),
                    files=files)
    res = conn.publish(bundle)
    for doc in res.get("documents", []):                 # what was checked travels with what was published
        rep = reports.get(doc.get("file"))
        if rep:
            doc["statuses"], doc["counts"], doc["passed"] = rep["statuses"], rep["counts"], rep["passed"]
            doc["schema"] = rep["identification"].get("package_id")
    res["at"] = now()
    res["targets"] = targets
    res["published_with_errors"] = bool(check and check["summary"]["errors"])
    pub = d / "publish"
    pub.mkdir(exist_ok=True)
    n = len(list(pub.glob("*.json"))) + 1
    write_json(pub / f"{n:03d}_{connector}.json", res)
    return res
