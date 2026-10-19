"""The staged pipeline. Each stage reads the previous stages' files and writes its own, so a
writer edit re-runs only what depends on it (IPL edit -> process -> procedures -> checks).

convert -> analyse (roles, contacts, sequence) -> reconcile -> drawing -> ipl -> process -> procedures -> revision -> check
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path

from ..cad.converters import get_converter
from ..cad.occ_converter import load_meshes
from ..drawing.extract import extract_drawing
from ..engineering.ipl import build_ipl
from ..engineering.roles import RoleClassifier
from ..llm.providers import get_provider
from ..procedures.generator import generate
from ..process.graph import build_process
from ..reconcile.reconciler import detect_dominant_prefix, reconcile
from ..revisions.diff import carry_forward, compare, modification_procedure
from ..sequence.geometry import contact_graph
from ..sequence.planner import plan_disassembly
from .config import StudioConfig
from .store import Store, read_json, write_json

log = logging.getLogger("studio.pipeline")

STAGES = ["convert", "analyse", "reconcile", "drawing", "ipl", "process", "procedures", "revision", "check"]
STAGE_LABEL = {"convert": "STEP → GLB + product model", "analyse": "Contacts and disassembly sequence",
               "reconcile": "3D ↔ MBOM reconciliation", "drawing": "Drawing torques and notes",
               "ipl": "Proposed IPL", "process": "Process graph", "procedures": "STE procedures",
               "revision": "Compare with baseline", "check": "Check documents (schema, BREX, rules)"}


class Pipeline:
    def __init__(self, cfg: StudioConfig, store: Store):
        self.cfg, self.store = cfg, store
        self._threads: dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------ helpers
    def _settings(self, r: dict) -> dict:
        p = self.store.project(r["project"])
        return {**self.cfg.defaults, **(p.get("settings") or {})}

    def _classifier(self, settings: dict) -> RoleClassifier:
        extra = (self.cfg.roles or {}).get("extra_rules", []) + (settings.get("role_rules") or [])
        return RoleClassifier([tuple(x) for x in extra])

    def _input(self, d: Path, r: dict, kind: str) -> Path | None:
        i = r["inputs"].get(kind)
        return d / "inputs" / i["file"] if i else None

    def _progress(self, r: dict, stage: str, frac: float, msg: str = "") -> None:
        r["progress"] = {"stage": stage, "fraction": round(frac, 3), "message": msg}
        self.store.save_run(r)

    # ------------------------------------------------------------------ run control
    def start(self, rid: str, from_stage: str = "convert") -> None:
        t = self._threads.get(rid)
        if t and t.is_alive():
            raise RuntimeError("this run is already being processed")
        t = threading.Thread(target=self.run, args=(rid, from_stage), daemon=True, name=f"run-{rid}")
        self._threads[rid] = t
        t.start()

    def wait(self, rid: str, timeout: float = 120) -> None:
        t = self._threads.get(rid)
        if t and t.is_alive():
            t.join(timeout)

    def busy(self, rid: str) -> bool:
        t = self._threads.get(rid)
        return bool(t and t.is_alive())

    def mark_interrupted(self, rid: str) -> dict | None:
        """A run left 'running' with no process behind it (the server stopped, or the process failed while
        saving): mark it interrupted so the writer can run it again from the stage that was running."""
        r = self.store.run(rid)
        if r.get("status") not in ("running", "queued") or self.busy(rid):
            return None
        st = next((k for k, v in (r.get("stages") or {}).items() if v.get("status") == "running"), None) or \
            (r.get("progress") or {}).get("stage")
        if st and st in STAGES:
            r["stages"][st] = {"status": "failed", "label": STAGE_LABEL[st],
                               "error": "Interrupted before it finished. Run it again from here."}
        r["status"] = "failed"
        r["error"] = f"{STAGE_LABEL.get(st, 'The run')}: interrupted before it finished (the server stopped or could not " \
                     "save its progress). Use Run again on that stage."
        r["progress"] = {"stage": None, "fraction": 0.0, "message": "Interrupted"}
        self.store.save_run(r)
        return r

    def recover(self) -> int:
        """At start-up: every run still marked running belongs to a process that no longer exists."""
        n = 0
        for d in self.store.root.glob("*/runs/*"):
            try:
                if self.mark_interrupted(d.name):
                    n += 1
            except Exception:                            # a damaged run folder never stops the server
                log.exception("could not check run %s", d.name)
        return n

    def run(self, rid: str, from_stage: str = "convert") -> dict:
        try:
            return self._run(rid, from_stage)
        except Exception:                                # last resort: never leave a run 'running' with no process
            log.exception("run %s stopped unexpectedly", rid)
            try:
                r = self.store.run(rid)
                r["status"], r["error"] = "failed", "The run stopped unexpectedly; see the server log. Use Run again."
                self.store.save_run(r)
            except Exception:
                pass
            raise

    def _run(self, rid: str, from_stage: str = "convert") -> dict:
        r = self.store.run(rid)
        pid = r["project"]
        d = self.store.run_dir(pid, rid)
        r["status"], r["error"] = "running", None
        self.store.save_run(r)
        start = STAGES.index(from_stage)
        for st in STAGES[start:]:
            t0 = time.monotonic()
            r["stages"][st] = {"status": "running", "label": STAGE_LABEL[st]}
            self._progress(r, st, 0.0, STAGE_LABEL[st])
            try:
                summary = getattr(self, f"_stage_{st}")(r, d)
                r["stages"][st] = {"status": "done" if summary is not False else "skipped", "label": STAGE_LABEL[st],
                                   "seconds": round(time.monotonic() - t0, 2),
                                   "summary": summary if isinstance(summary, dict) else None}
            except Exception as e:
                log.exception("stage %s failed", st)
                r["stages"][st] = {"status": "failed", "label": STAGE_LABEL[st], "error": str(e),
                                   "trace": traceback.format_exc()[-2000:]}
                r["status"], r["error"] = "failed", f"{STAGE_LABEL[st]}: {e}"
                self.store.save_run(r)
                return r
            self.store.save_run(r)
        r["status"] = "done"
        r["progress"] = {"stage": None, "fraction": 1.0, "message": "Done"}
        self.store.save_run(r)
        return r

    # ------------------------------------------------------------------ stages
    def _stage_convert(self, r, d):
        step = self._input(d, r, "step")
        if not step:
            raise ValueError("no STEP file in this run")
        conv = get_converter(self.cfg.converter)
        ok, why = conv.available()
        if not ok:
            raise RuntimeError(f"converter '{conv.kind}' unavailable: {why}")
        res = conv.convert(step, d, on_progress=lambda s, f: self._progress(r, "convert", f, s))
        r["model_file"] = Path(res["glb"]).name
        return res["stats"]

    def _stage_analyse(self, r, d):
        settings = self._settings(r)
        model = read_json(d / "product_model.json")
        meshes = load_meshes(d / "meshes.npz")
        prefix = settings.get("manuf_prefix") or detect_dominant_prefix(model["bom"])
        r["manuf_prefix"] = prefix
        rc = self._classifier(settings)
        roles = {o["occ_id"]: rc.classify(o["part_number"], [o["name"], o["description"]], o.get("geometry"),
                                          o["is_assembly"], prefix).as_dict() for o in model["occurrences"]}
        self._progress(r, "analyse", 0.2, "Contact graph")
        contacts = contact_graph(model, meshes)
        self._progress(r, "analyse", 0.5, "Disassembly sequence")
        seq = plan_disassembly(model, meshes, roles, contacts)
        # each STEP sub-assembly gets its own plan: how it comes apart once it is off the unit
        subs = [o for o in model["occurrences"] if o["is_assembly"] and o["occ_id"] != model["root"]
                and len(o["children"]) >= 2]
        seq["sub"] = {}
        for k, o in enumerate(subs):
            self._progress(r, "analyse", 0.6 + 0.35 * k / max(len(subs), 1), f"Disassembly of {o['name'] or o['part_number']}")
            try:
                seq["sub"][o["occ_id"]] = plan_disassembly({**model, "root": o["occ_id"]}, meshes, roles, contacts, max_rays=400)
            except Exception as e:                 # a sub-assembly that cannot be planned keeps the parts-list order
                log.warning("no disassembly plan for %s: %s", o["occ_id"], e)
        write_json(d / "roles.json", roles)
        write_json(d / "contacts.json", contacts)
        write_json(d / "sequence.json", seq)
        return {"contacts": len(contacts), "steps": len(seq["disassembly"]), "layers": seq["layers"],
                "conflicts": len(seq["conflicts"]), "manuf_prefix": prefix}

    def _stage_reconcile(self, r, d):
        mbom_path = self._input(d, r, "mbom")
        if not mbom_path:
            raise ValueError("no MBOM / APL in this run")
        settings = self._settings(r)
        model = read_json(d / "product_model.json")
        recs, imp = self._read_parts_list(mbom_path, settings)
        write_json(d / "mbom.json", recs)
        write_json(d / "mbom_import.json", imp)
        root_pn = settings.get("root_part_number") or re.sub(
            rf"^{re.escape(r.get('manuf_prefix') or '')}-", "", model["occurrences"][0]["part_number"])
        res = reconcile(model["bom"], recs, manuf_prefix=r.get("manuf_prefix"), root_product_number=root_pn,
                        assembly_tree=model["assembly_tree"], expected_variant=settings.get("expected_variant"))
        out = {"success": True,
               "summary": {"matched": len(res["matched"]),
                           "exact_matches": sum(1 for m in res["matched"] if m.status == "matched"),
                           "supersession_matches": sum(1 for m in res["matched"] if m.status == "matched_via_supersession"),
                           "fuzzy_matches": sum(1 for m in res["matched"] if m.status not in ("matched", "matched_via_supersession")),
                           "quantity_mismatches": sum(1 for m in res["matched"] if m.quantity_match is False),
                           "unmatched_or_ambiguous_step": len(res["unmatched_step"]),
                           "unmatched_mbom_lines": len(res["unmatched_mbom"]),
                           "parts_list_lines": len(recs), "parts_list_needs_review": imp["needs_review"]},
               "matched": [asdict(m) for m in res["matched"]],
               "unmatched_step": [asdict(m) for m in res["unmatched_step"]],
               "unmatched_mbom": res["unmatched_mbom"]}
        write_json(d / "reconciliation.json", out)
        r["root_part_number"] = root_pn
        return out["summary"]

    @staticmethod
    def _read_parts_list(path, settings: dict) -> tuple[list, dict]:
        """The parts list with a mapping the writer confirmed for this file layout, else the proposed one
        (marked for review when anything was guessed)."""
        from ..engineering import parts_import as pi
        insp = pi.inspect(path)
        if not insp["chosen"]:
            raise ValueError("The parts list could not be read: " + " ".join(insp["problems"])
                             + " Map its columns on Match 3D to parts list → Parts list import.")
        saved = settings.get("parts_mappings") or {}
        confirmed = next((saved[sig] for sig in [insp["chosen"]["signature"], *insp.get("layouts", [])] if sig in saved), None)
        mapping = confirmed or pi.mapping_from_proposal(insp["chosen"])
        recs = pi.load(path, mapping)
        for t in insp["tables"]:
            t["sample"] = t["sample"][:6]
        return recs, {"file": insp["file"], "mapping": mapping, "source": "confirmed" if confirmed else "proposed",
                      "needs_review": bool(not confirmed and insp["needs_review"]), "problems": [] if confirmed else insp["problems"],
                      "tables": insp["tables"], "chosen": insp["chosen"]["id"], "rows": len(recs)}

    def _stage_drawing(self, r, d):
        pdf = self._input(d, r, "drawing")
        if not pdf:
            write_json(d / "drawing.json", {"format": "studio.drawing-facts/1", "torques": [], "callouts": [], "notes": []})
            return False
        recs = read_json(d / "mbom.json", [])
        rc = self._classifier(self._settings(r))
        roles = {re.sub(r"[^A-Z0-9]", "", x["part_number"].lstrip(".").upper()):
                 rc.classify(x["part_number"], [x.get("description", "")]).role for x in recs}
        facts = extract_drawing(pdf, [x["part_number"] for x in recs], roles)
        write_json(d / "drawing.json", facts)
        return {"torques": len(facts["torques"]),
                "assigned": sum(1 for t in facts["torques"] if t["target_pn"]), "notes": len(facts["notes"])}

    def _stage_ipl(self, r, d):
        model = read_json(d / "product_model.json")
        ipl = build_ipl(model, read_json(d / "reconciliation.json"), read_json(d / "mbom.json"),
                        read_json(d / "sequence.json"), read_json(d / "drawing.json"), read_json(d / "roles.json"),
                        read_json(d / "contacts.json"), r.get("manuf_prefix"), self._classifier(self._settings(r)))
        if r.get("base_run"):                    # item numbers continue from the baseline (issues, SBs, variants)
            from ..engineering.item_numbers import stabilize
            _, bd = self.store.find_run(r["base_run"])
            stabilize(ipl, read_json(bd / "ipl.json"))
        old = read_json(d / "ipl.json")
        if old:                                  # keep writer decisions when the IPL is rebuilt
            prev = {(x["part_number"], x.get("item")): x for x in old["rows"]}
            for row in ipl["rows"]:
                o = prev.get((row["part_number"], row.get("item")))
                if o and o.get("status") in ("accepted", "edited", "rejected"):
                    for k in o.get("edited_fields", []):
                        row[k] = o[k]
                    row["status"], row["edited_fields"] = o["status"], o.get("edited_fields", [])
        write_json(d / "ipl.json", ipl)
        return {**ipl["summary"], **({"items_kept": ipl["item_numbers"]["kept"], "items_new": len(ipl["item_numbers"]["new"]),
                                      "items_retired": len(ipl["item_numbers"]["retired"])} if ipl.get("item_numbers") else {})}

    def _stage_process(self, r, d):
        proc = build_process(read_json(d / "ipl.json"), read_json(d / "sequence.json"), read_json(d / "drawing.json"))
        write_json(d / "process.json", proc)
        return {"disassembly_ops": len(proc["disassembly"]["operations"]), "assembly_ops": len(proc["assembly"]["operations"])}

    def _stage_procedures(self, r, d):
        settings = self._settings(r)
        ipl, process = read_json(d / "ipl.json"), read_json(d / "process.json")
        provider = get_provider({**self.cfg.llm_settings(), **(settings.get("llm") or {})})
        names = settings.get("names") or {}
        from .publishing import publication_title
        title = publication_title(ipl)
        out = {}
        for kind in ("disassembly", "assembly"):
            p = generate(process, ipl, kind, title=f"{title}: {kind}", provider=provider, names=names)
            write_json(d / f"procedure_{kind}.json", p)
            out[kind] = {k: p["summary"][k] for k in ("subtasks", "steps", "errors", "warnings", "llm_steps")}
        return out

    def _stage_revision(self, r, d):
        base_id = r.get("base_run")
        if not base_id:
            return False
        _, bd = self.store.find_run(base_id)
        load = lambda dd: {"model": read_json(dd / "product_model.json"), "ipl": read_json(dd / "ipl.json"),
                           "process": read_json(dd / "process.json"),
                           "procedures": {k: read_json(dd / f"procedure_{k}.json") for k in ("assembly", "disassembly")}}
        base, new = load(bd), load(d)
        cs = compare(base, new)
        kept = {}
        for kind in ("assembly", "disassembly"):
            p = new["procedures"][kind]
            kept[kind] = carry_forward(p, new["process"], base["procedures"][kind], base["process"])
            write_json(d / f"procedure_{kind}.json", p)
        cs["carry_forward"] = kept
        change = r.get("change") or {}
        base_run = self.store.run(base_id)
        base_project = self.store.project(base_run["project"])
        cs["context"] = {"kind": change.get("kind") or "revision", "reference": change.get("reference"),
                         "title": change.get("title"), "reason": change.get("reason"),
                         "base": {"run": base_id, "label": base_run.get("label"), "project": base_project["id"],
                                  "product": base_project["name"]}}
        write_json(d / "changeset.json", cs)
        new["procedures"] = {k: read_json(d / f"procedure_{k}.json") for k in ("assembly", "disassembly")}
        mod = modification_procedure(base, new, cs)
        from .publishing import publication_title
        name = publication_title(new["ipl"]).lower()
        if cs["context"]["kind"] == "modification":
            mod["title"] = f"{change['reference']}: " + (change.get("title") or f"modification of the {name}")
            mod["reference"] = change["reference"]
        elif cs["context"]["kind"] == "variant":
            mod["title"] = f"Conversion of a {base_project['name']} unit to {self.store.project(r['project'])['name']}"
        mod["context"] = cs["context"]
        write_json(d / "procedure_modification.json", mod)
        return {**cs["summary"], "kept_steps": sum(v["kept"] for v in kept.values()),
                "changed_steps": sum(v["changed"] for v in kept.values())}

    def _stage_check(self, r, d):
        from .checking import check_run
        return check_run(self.cfg.conformance(), self.store, r["id"])["summary"]
