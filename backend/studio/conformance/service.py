"""One entry point for checking documents: identify -> validate (schema) -> business rules (BREX + rule sets).

    conf = Conformance(data_dir / "conformance")
    report = conf.check(xml_bytes, "DMC-....XML", prefer="s1000d/4.2/acme:proced")
    html, used = conf.view(xml_bytes)

Reports are plain dicts (JSON-ready). Nothing here reaches the network.
"""
from __future__ import annotations

import io
import shutil
import tempfile
from pathlib import Path

from lxml import etree

from .brex.library import BrexLibrary
from .identify.service import identify, parse_xml, sniff
from .registry import installer
from .registry.builder import BuildError
from .registry.service import RegistryError, SchemaRegistry
from .rules import RuleLibrary
from .storage.db import Database
from .stylesheets import StylesheetLibrary
from .validation.diagnostics import finalize
from .validation.model import Diagnostic, Severity, Stage, StageResult, Status
from .validation.pipeline import brex_stage, validate_bytes

# What Studio generates, and which document types can validate it (for status and schema choice)
TARGETS = {
    "s1000d_proced": {"label": "S1000D procedure (530, 710)", "standard": "S1000D", "roots": ["dmodule"], "doc_types": ["proced"]},
    "s1000d_ipd": {"label": "S1000D IPD (941)", "standard": "S1000D", "roots": ["dmodule"], "doc_types": ["ipd"]},
    "ata_cmm": {"label": "ATA iSpec 2200 CMM", "standard": "ATA2200", "roots": ["cmm"], "doc_types": ["cmm"]},
}


class Conformance:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.root / "registry.sqlite3")
        self.registry = SchemaRegistry(self.db, self.root / "packages")
        self.brex = BrexLibrary(self.root / "brex")
        self.registry.brex = self.brex
        self.rules = RuleLibrary(self.root / "rules")
        self.styles = StylesheetLibrary(self.root / "stylesheets")
        self.staging = self.root / "staging"

    # ------------------------------------------------------------------ status
    def coverage(self) -> dict:
        """For each Studio output: the installed document types that can validate it."""
        out = {}
        for key, t in TARGETS.items():
            hits = []
            for p in self.registry.list(include_disabled=False):
                if p.manifest.standard != t["standard"]:
                    continue
                for dt in p.manifest.doc_types:
                    if dt.match.local_name in t["roots"] and (dt.id in t["doc_types"] or dt.content_family in
                                                              ("procedure" if key == "s1000d_proced" else "ipd", "other")):
                        hits.append({"package_id": p.manifest.key, "doc_type": dt.id, "label": f"{p.manifest.name} · {dt.label}",
                                     "provenance": p.manifest.provenance})
            out[key] = {"label": t["label"], "options": hits}
        return out

    def status(self) -> dict:
        return {"packages": [p.summary() | {"path": None} for p in self.registry.list()],
                "brex": self.brex.list(), "brex_substitutes": self.brex.substitutes(),
                "rules": self.rules.list(), "stylesheets": self.styles.list(), "coverage": self.coverage()}

    # ------------------------------------------------------------------ schema packages
    def inspect_upload(self, files: list[tuple[str, bytes]]) -> dict:
        """Stage an upload (a zip, or the files of a folder with their relative paths) and say what it holds."""
        sid, src = installer.new_staging(self.staging)
        for rel, data in files:
            rel = rel.replace("\\", "/").lstrip("/")
            if rel.lower().endswith(".zip") and len(files) == 1:
                zp = src.parent / "upload.zip"
                zp.write_bytes(data)
                from .security.paths import safe_extract_zip
                safe_extract_zip(zp, src)
                zp.unlink()
                continue
            if not installer.wanted(rel, len(data)):
                continue
            from .security.paths import confine
            dst = confine(src, rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        try:
            proposal = installer.inspect(src)
        except BuildError as e:
            shutil.rmtree(src.parent, ignore_errors=True)
            raise ValueError(str(e)) from e
        return {"staging": sid, **proposal}

    def install_staged(self, sid: str, choice: dict, replace: bool = False) -> dict:
        src = (self.staging / sid / "src")
        if not src.is_dir():
            raise ValueError("this upload has expired; add the schemas again")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                built = installer.build(src, choice, Path(tmp) / "package.zip")
                try:
                    pkg = self.registry.replace(built) if replace else self.registry.install(built)
                except RegistryError as e:
                    if not replace and "already installed" in str(e):
                        raise ValueError(f"{e}. Choose “Replace” to install this build over it.") from e
                    raise ValueError(str(e)) from e
            brex = []
            if choice.get("kind") == "s1000d":
                brex = installer.install_brex_found(src, self.brex, choice.get("issue"))
            return {"package": pkg.summary(), "brex_installed": brex}
        except BuildError as e:
            raise ValueError(str(e)) from e
        finally:
            shutil.rmtree(self.staging / sid, ignore_errors=True)

    def install_package_dir(self, path: Path) -> dict:
        """Install a ready-made package folder or zip (used for the evaluation packages and by tests)."""
        try:
            return self.registry.install(Path(path)).summary()
        except RegistryError as e:
            raise ValueError(str(e)) from e

    # ------------------------------------------------------------------ checking
    def check(self, data: bytes, file: str, prefer: str | None = None, standard_hint: str | None = None) -> dict:
        """Full check of one document. ``prefer`` = "package_id:doc_type" to use when the document's own
        declaration does not decide (Studio's per-output schema choice)."""
        sn = sniff(data)
        tree0, _ = parse_xml(data)
        root0 = tree0.getroot() if tree0 is not None else None
        packages = [p for p in self.registry.list(include_disabled=False)]
        ident = identify(root0, packages, sn, data)
        pkg_id = dt_id = None
        how = None
        pref = tuple(prefer.split(":", 1)) if prefer and ":" in prefer else None
        if ident.chosen:
            pkg_id, dt_id, how = ident.chosen.package_id, ident.chosen.doc_type, "declared by the document"
            if pref and pref != (pkg_id, dt_id) and any((c.package_id, c.doc_type) == pref for c in ident.compatible):
                pkg_id, dt_id, how = pref[0], pref[1], "chosen for this output"
        elif pref and any((c.package_id, c.doc_type) == pref for c in ident.candidates + ident.compatible):
            pkg_id, dt_id, how = pref[0], pref[1], "chosen for this output"
        elif ident.status == "needs-choice" and len(ident.compatible) == 1:
            c = ident.compatible[0]
            pkg_id, dt_id, how = c.package_id, c.doc_type, "the only installed schema that fits"
        rep, tree = validate_bytes(data, file, self.registry, pkg_id, dt_id)
        standard = next((p.manifest.standard for p in packages if p.manifest.key == pkg_id), None) or standard_hint
        if standard is None and root0 is not None:
            standard = {"dmodule": "S1000D", "pm": "S1000D", "cmm": "ATA2200"}.get(etree.QName(root0).localname)
        self._business_rules(tree, rep, file, pkg_id, dt_id, standard)
        finalize(rep)
        for d in rep.diagnostics:                         # keep the rule-set category after finalize
            if d.stage == Stage.BUSINESS_RULES:
                d.category = d.category if d.category in ("brex-missing", "brex-substitute", "brex-issue", "brex-not-checked") \
                    else "business-rule"
        res = {"file": file, "identification": {**ident.as_dict(), "package_id": pkg_id, "doc_type": dt_id,
                                                 "standard": standard, "how": how},
               "statuses": rep.statuses(), "counts": rep.counts(),
               "stages": [{"stage": int(s.stage), "name": STAGE_NAMES.get(int(s.stage), str(s.stage)),
                           "status": s.status.value, "note": s.note} for s in sorted(rep.stages, key=lambda s: s.stage)],
               "diagnostics": [d.model_dump(mode="json", exclude_none=True) for d in rep.diagnostics],
               "entities": rep.entities}
        res["passed"] = res["statuses"]["structural"] == "passed" and res["counts"]["error"] + res["counts"]["fatal"] == 0
        return res

    def _business_rules(self, tree, rep, file: str, pkg_id, dt_id, standard) -> None:
        if tree is None:
            if not any(s.stage == Stage.BUSINESS_RULES for s in rep.stages):
                rep.stages.append(StageResult(stage=Stage.BUSINESS_RULES, status=Status.NOT_RUN, note="document could not be read"))
            return
        rep.stages = [s for s in rep.stages if s.stage != Stage.BUSINESS_RULES]
        notes, failed, ran = [], False, False
        if (standard or "").upper() == "S1000D" and pkg_id:
            try:
                _, pkg, dt = self.registry.schema_for(pkg_id, dt_id)
                before = len(rep.stages)
                brex_stage(tree, self.brex, pkg, dt, file, rep)
                st = rep.stages.pop() if len(rep.stages) > before else None
                if st is not None:
                    notes.append("BREX: " + st.note)
                    ran = ran or st.status in (Status.PASSED, Status.FAILED)
                    failed = failed or st.status == Status.FAILED
            except RegistryError as e:
                notes.append(f"BREX not checked: {e}")
        root = etree.QName(tree.getroot()).localname
        outcome, used = self.rules.check(tree, standard, dt_id, file)
        if used:
            ran = True
            rep.diagnostics += outcome.diagnostics
            failed = failed or any(d.severity in (Severity.ERROR, Severity.FATAL) for d in outcome.diagnostics)
            notes.append(f"rule sets: {', '.join(e['name'] for e in used)} ({outcome.checked} rules checked)")
        for rid, why in outcome.not_checked:
            rep.diagnostics.append(Diagnostic(stage=Stage.BUSINESS_RULES, severity=Severity.WARNING, rule_id="VAL-RULE-NOT-CHECKED",
                                              category="rule-not-checked", source_file=file,
                                              message=f"Rule {rid} could not be evaluated and was NOT checked: {why}"))
        if not ran and not notes:
            notes.append(f"no BREX or rule set applies to <{root}>")
        rep.stages.append(StageResult(stage=Stage.BUSINESS_RULES,
                                      status=Status.FAILED if failed else Status.PASSED if ran else Status.NOT_RUN,
                                      note="; ".join(notes)))

    def view(self, data: bytes, stylesheet: str | None = None, standard: str | None = None, doc_type: str | None = None) -> dict:
        from .security.xml_safe import document_parser
        try:
            tree = etree.parse(io.BytesIO(data), document_parser())
        except etree.XMLSyntaxError as e:
            raise ValueError(f"the document is not well-formed XML: {e}") from e
        if standard is None:
            standard = {"dmodule": "S1000D", "pm": "S1000D", "cmm": "ATA2200"}.get(etree.QName(tree.getroot()).localname)
        html, used = self.styles.view(tree, standard, doc_type, stylesheet)
        return {"html": html, "stylesheet": used and {k: used[k] for k in ("id", "name", "builtin")}}

    def transform(self, data: bytes, standard: str | None, doc_type: str | None) -> tuple[bytes, list[dict]]:
        from .security.xml_safe import document_parser
        try:
            root = etree.QName(etree.fromstring(data, document_parser()).tag).localname
        except etree.XMLSyntaxError:
            return data, []                                # not XML: nothing to transform; the check reports why
        return self.styles.transform(data, standard, doc_type, root)


STAGE_NAMES = {1: "Integrity", 2: "Well-formed XML", 3: "Schema", 4: "Business rules", 5: "References"}
