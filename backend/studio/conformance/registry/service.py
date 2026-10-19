"""Versioned local schema registry (spec §4, §5)."""
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import xmlschema
from lxml import etree

from ..security.hashing import tree_checksum
from ..security.paths import PathViolation, confine, safe_extract_zip
from ..security.xml_safe import BlockedResolution, ConfinedResolver, schema_parser
from ..storage.db import Database
from .package import DocType, Manifest

MANIFEST_NAME = "studio-package.json"
MANIFEST_NAMES = (MANIFEST_NAME, "asthra-package.json")     # ASTHRA packages install unchanged


def manifest_file(root: Path) -> Path | None:
    return next((root / n for n in MANIFEST_NAMES if (root / n).is_file()), None)


class RegistryError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class InstalledPackage:
    manifest: Manifest
    path: Path
    checksum: str
    enabled: bool

    def summary(self) -> dict:
        m = self.manifest
        return {
            "id": m.key, "name": m.name, "standard": m.standard, "issue": m.issue,
            "provenance": m.provenance, "licence_note": m.licence_note,
            "enabled": self.enabled, "checksum": self.checksum,
            "doc_types": [{"id": d.id, "label": d.label, "content_family": d.content_family,
                           "root": d.match.local_name, "namespace": d.match.namespace}
                          for d in m.doc_types],
            "dependencies": m.dependencies, "file_count": len(m.files),
        }


# Standard W3C schemas that industry schemas import by web address (e.g. S2000M 7.0 imports XML Signature).
# Procedure Studio never goes online: these addresses resolve to the official copies shipped with the xmlschema
# library (already a dependency). Every other external address stays blocked.
WELL_KNOWN_SCHEMAS = {
    "http://www.w3.org/TR/2002/REC-xmldsig-core-20020212/xmldsig-core-schema.xsd": "DSIG/xmldsig-core-schema.xsd",
    "http://www.w3.org/TR/2008/REC-xmldsig-core-20080610/xmldsig-core-schema.xsd": "DSIG/xmldsig-core-schema.xsd",
    "http://www.w3.org/TR/xmldsig-core/xmldsig-core-schema.xsd": "DSIG/xmldsig-core-schema.xsd",
    "xmldsig-core-schema.xsd": "DSIG/xmldsig-core-schema.xsd",
    "http://www.w3.org/TR/xmldsig-core1/xmldsig11-schema.xsd": "DSIG/xmldsig11-schema.xsd",
    "xmldsig11-schema.xsd": "DSIG/xmldsig11-schema.xsd",
    "http://www.w3.org/TR/2002/REC-xmlenc-core-20021210/xenc-schema.xsd": "XENC/xenc-schema.xsd",
    "xenc-schema.xsd": "XENC/xenc-schema.xsd",
    "http://www.w3.org/2001/xml.xsd": "XML/xml.xsd",
    "http://www.w3.org/2009/01/xml.xsd": "XML/xml.xsd",
}


def well_known_root() -> Path:
    return Path(xmlschema.__file__).resolve().parent / "schemas"


class SchemaRegistry:
    def __init__(self, db: Database, root: Path):
        self.db = db
        self.root = root
        self._compiled: dict[tuple[str, str], etree.XMLSchema] = {}
        self._idref: dict[tuple[str, str], xmlschema.XMLSchema] = {}
        self._models: dict[tuple[str, str, str], dict] = {}

    # ---------------------------------------------------------------- install
    def install(self, source: Path) -> InstalledPackage:
        """Install from a directory or .zip. The source is copied; never modified."""
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "pkg"
            if source.is_file() and source.suffix.lower() == ".zip":
                try:
                    safe_extract_zip(source, staging)
                except PathViolation as e:
                    raise RegistryError(f"unsafe archive rejected: {e}") from e
                roots = sorted({p.parent for n in MANIFEST_NAMES for p in staging.rglob(n)})
                if len(roots) != 1:
                    raise RegistryError(f"archive must contain exactly one {MANIFEST_NAME}")
                staging = roots[0]
            elif source.is_dir():
                shutil.copytree(source, staging, symlinks=False)
            else:
                raise RegistryError(f"not a package directory or zip: {source}")
            manifest, checksum = self._verify_staged(staging)
            if self.db.one("SELECT 1 FROM schema_package WHERE id=?", (manifest.key,)):
                raise RegistryError(f"package already installed: {manifest.key}")
            dest = confine(self.root, manifest.key)
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staging, dest)
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO schema_package VALUES (?,?,?,?,?,?,?,?,1,?)",
                (manifest.key, manifest.standard, manifest.issue, manifest.name,
                 manifest.provenance, str(dest), checksum, manifest.model_dump_json(), _now()),
            )
            c.execute("INSERT INTO audit_event(at,actor,action,subject,detail_json) VALUES (?,?,?,?,?)",
                      (_now(), "local", "schema.install", manifest.key, json.dumps({"checksum": checksum})))
        return InstalledPackage(manifest, dest, checksum, True)

    def replace(self, source: Path) -> InstalledPackage:
        """Replace an installed package with a new build of the same package id (for example
        adding Description and IPD to a Procedure-only package). Documents keep their link to
        the package; every schema is recompiled and the new checksum recorded."""
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "pkg"
            if source.is_file() and source.suffix.lower() == ".zip":
                try:
                    safe_extract_zip(source, staging)
                except PathViolation as e:
                    raise RegistryError(f"unsafe archive rejected: {e}") from e
                roots = sorted({p.parent for n in MANIFEST_NAMES for p in staging.rglob(n)})
                if len(roots) != 1:
                    raise RegistryError(f"archive must contain exactly one {MANIFEST_NAME}")
                staging = roots[0]
            elif source.is_dir():
                shutil.copytree(source, staging, symlinks=False)
            else:
                raise RegistryError(f"not a package directory or zip: {source}")
            manifest, checksum = self._verify_staged(staging)
            old = self.get(manifest.key)            # raises if not installed
            dest = confine(self.root, manifest.key)
            backup = dest.with_name(dest.name + ".old")
            if backup.exists():
                shutil.rmtree(backup)
            dest.rename(backup)
            try:
                shutil.copytree(staging, dest)
            except Exception:
                shutil.rmtree(dest, ignore_errors=True)
                backup.rename(dest)
                raise
        shutil.rmtree(backup, ignore_errors=True)
        with self.db.tx() as c:
            c.execute("UPDATE schema_package SET name=?, install_path=?, checksum=?, manifest_json=?, installed_at=? WHERE id=?",
                      (manifest.name, str(dest), checksum, manifest.model_dump_json(), _now(), manifest.key))
            c.execute("INSERT INTO audit_event(at,actor,action,subject,detail_json) VALUES (?,?,?,?,?)",
                      (_now(), "local", "schema.replace", manifest.key,
                       json.dumps({"old_checksum": old.checksum, "checksum": checksum})))
        self._compiled = {k: v for k, v in self._compiled.items() if k[0] != manifest.key}
        self._idref = {k: v for k, v in self._idref.items() if k[0] != manifest.key}
        return self.get(manifest.key)

    # ---------------------------------------------------------------- remove / export
    def remove(self, key: str) -> None:
        """Uninstall a package. Documents already validated keep their reports."""
        pkg = self.get(key)
        with self.db.tx() as c:
            c.execute("DELETE FROM schema_package WHERE id=?", (key,))
            c.execute("INSERT INTO audit_event(at,actor,action,subject,detail_json) VALUES (?,?,?,?,?)",
                      (_now(), "local", "schema.remove", key, "{}"))
        shutil.rmtree(pkg.path, ignore_errors=True)
        self._compiled = {k: v for k, v in self._compiled.items() if k[0] != key}
        self._idref = {k: v for k, v in self._idref.items() if k[0] != key}

    def export_package(self, key: str, out: Path) -> Path:
        """The installed package as a zip that installs unchanged on another system."""
        import zipfile
        pkg = self.get(key)
        if not self.verify_integrity(key):
            raise RegistryError(f"package {key} failed its integrity check; reinstall it before exporting")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(manifest_file(pkg.path), MANIFEST_NAME)
            for rel in pkg.manifest.files:
                zf.write(pkg.path / rel, rel)
        return out

    def export_set(self, out: Path) -> Path:
        """Every installed package in one file: configure a new system with import_set()."""
        import zipfile
        pkgs = self.list()
        with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
            index = []
            for p in pkgs:
                name = p.manifest.key.replace("/", "__") + ".zip"
                z = Path(tmp) / name
                self.export_package(p.manifest.key, z)
                zf.write(z, f"packages/{name}")
                index.append({"id": p.manifest.key, "file": f"packages/{name}", "enabled": p.enabled,
                              "checksum": p.checksum})
            zf.writestr("studio-schema-set.json", json.dumps({"format_version": 1, "packages": index}, indent=2))
        return out

    def import_set(self, source: Path) -> list[dict]:
        """Install every package of a schema set. Already-installed identical packages are
        skipped; a different build of an installed package is replaced."""
        import zipfile
        results = []
        with tempfile.TemporaryDirectory() as tmp:
            try:
                safe_extract_zip(source, Path(tmp))
            except PathViolation as e:
                raise RegistryError(f"unsafe archive rejected: {e}") from e
            idx_path = next((Path(tmp) / n for n in ("studio-schema-set.json", "asthra-schema-set.json")
                             if (Path(tmp) / n).is_file()), None)
            if idx_path is None:
                raise RegistryError("this is not a schema set (studio-schema-set.json missing)")
            index = json.loads(idx_path.read_text(encoding="utf-8"))
            for entry in index.get("packages", []):
                z = confine(Path(tmp), entry["file"])
                try:
                    existing = self.get(entry["id"])
                except RegistryError:
                    existing = None
                try:
                    if existing and existing.checksum == entry.get("checksum"):
                        results.append({"id": entry["id"], "result": "already installed"})
                        continue
                    pkg = self.replace(z) if existing else self.install(z)
                    if not entry.get("enabled", True):
                        self.set_enabled(pkg.manifest.key, False)
                    results.append({"id": entry["id"], "result": "replaced" if existing else "installed"})
                except RegistryError as e:
                    results.append({"id": entry["id"], "result": f"failed: {e}"})
        return results

    def schema_model(self, key: str, doc_type_id: str) -> dict:
        """The editor's schema model for one document type (see ..schema.model), cached per build."""
        from ..schema.model import dtd_model, xsd_model
        pkg = self.get(key)
        ck = (key, doc_type_id, pkg.checksum)
        if ck in self._models:
            return self._models[ck]
        dt = next((d for d in pkg.manifest.doc_types if d.id == doc_type_id), None)
        if dt is None:
            raise RegistryError(f"package {key} does not define doc type {doc_type_id}")
        if dt.schema_kind == "xsd":
            model = xsd_model(self.identity_checker(key, doc_type_id), dt.match.local_name)
        elif dt.schema_kind == "dtd":
            from ..validation.dtd import validate_dtd
            roots, catalog = self.dtd_context(pkg)
            name = dt.match.local_name
            _, _, dtd, blocked = validate_dtd(f"<{name}/>", name, roots, catalog, dt.schema_file)
            if dtd is None:
                raise RegistryError(f"the DTD for {doc_type_id} could not be loaded: {blocked or 'unknown error'}")
            model = dtd_model(dtd, name)
        else:
            raise RegistryError(f"{doc_type_id}: SGML document types are not supported")
        model["kind"] = dt.schema_kind
        self._models[ck] = model
        return model

    def _verify_staged(self, root: Path) -> tuple[Manifest, str]:
        mf = manifest_file(root)
        if mf is None:
            raise RegistryError(f"missing {MANIFEST_NAME}")
        try:
            manifest = Manifest.model_validate_json(mf.read_text(encoding="utf-8"))
        except Exception as e:
            raise RegistryError(f"invalid manifest: {e}") from e
        for rel in manifest.files:
            try:
                p = confine(root, rel)
            except PathViolation as e:
                raise RegistryError(str(e)) from e
            if not p.is_file():
                raise RegistryError(f"manifest lists missing file: {rel}")
        listed = {f.replace("\\", "/") for f in manifest.files}
        for dt in manifest.doc_types:
            if dt.schema_file not in listed:
                raise RegistryError(f"doc type {dt.id}: entry schema {dt.schema_file} not listed in files")
        for dep in manifest.dependencies:
            if not self.db.one("SELECT 1 FROM schema_package WHERE id=?", (dep,)):
                raise RegistryError(f"unresolved package dependency: {dep} (install it first)")
        checksum = tree_checksum(root, manifest.files)
        if manifest.checksum and manifest.checksum != checksum:
            raise RegistryError("package checksum does not match publisher-declared checksum")
        # Compile every entry schema / load every DTD now, so missing files fail at install.
        for dt in manifest.doc_types:
            if dt.schema_kind == "xsd":
                self._compile(root, manifest, dt)
            elif dt.schema_kind == "dtd":
                self._check_dtd(root, manifest, dt)
            else:
                raise RegistryError(f"doc type {dt.id}: SGML is not supported; install the XML (XSD or DTD) schemas")
        return manifest, checksum

    # ---------------------------------------------------------------- query
    def list(self, include_disabled: bool = True) -> list[InstalledPackage]:
        rows = self.db.query("SELECT * FROM schema_package ORDER BY standard, issue, id")
        out = [self._row(r) for r in rows]
        return [p for p in out if include_disabled or p.enabled]

    def get(self, key: str) -> InstalledPackage:
        r = self.db.one("SELECT * FROM schema_package WHERE id=?", (key,))
        if not r:
            raise RegistryError(f"no such package: {key}")
        return self._row(r)

    def _row(self, r) -> InstalledPackage:
        return InstalledPackage(Manifest.model_validate_json(r["manifest_json"]),
                                Path(r["install_path"]), r["checksum"], bool(r["enabled"]))

    def set_enabled(self, key: str, enabled: bool) -> None:
        self.get(key)
        with self.db.tx() as c:
            c.execute("UPDATE schema_package SET enabled=? WHERE id=?", (int(enabled), key))
            c.execute("INSERT INTO audit_event(at,actor,action,subject) VALUES (?,?,?,?)",
                      (_now(), "local", "schema.enable" if enabled else "schema.disable", key))

    def verify_integrity(self, key: str) -> bool:
        pkg = self.get(key)
        return tree_checksum(pkg.path, pkg.manifest.files) == pkg.checksum

    # ---------------------------------------------------------------- schemas
    def dependency_roots(self, manifest: Manifest) -> list[Path]:
        roots: list[Path] = []
        for dep in manifest.dependencies:
            d = self.get(dep)
            roots.append(d.path)
            roots.extend(self.dependency_roots(d.manifest))
        return roots

    def _xmlschema(self, root: Path, manifest: Manifest, dt: DocType, version: str):
        """The schema compiled by the xmlschema library (XSD 1.0 or 1.1), with the package's own
        catalog and no remote access."""
        roots = [root] + self.dependency_roots(manifest) + [well_known_root()]
        catalog = {}
        for url, rel in {**WELL_KNOWN_SCHEMAS, **manifest.catalog}.items():
            for r in roots:
                cand = confine(r, rel)
                if cand.is_file():
                    catalog[url] = cand.as_uri()
                    break

        def mapper(uri: str) -> str:
            return catalog.get(uri, uri)

        cls = xmlschema.XMLSchema11 if version == "1.1" else xmlschema.XMLSchema10
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            schema = cls(str(confine(root, dt.schema_file)), allow="local", defuse="always", uri_mapper=mapper)
        # xmlschema skips an import/include it cannot load and carries on; Studio never validates against
        # a partial schema
        bad = [str(w.message) for w in caught if "import" in type(w.message).__name__.lower()
               or "include" in type(w.message).__name__.lower() or "could not be" in str(w.message).lower()]
        if bad:
            raise RegistryError("a schema file it imports or includes could not be loaded: " + bad[0][:300])
        return schema

    def _compile(self, root: Path, manifest: Manifest, dt: DocType):
        """libxml2 first (fast); if it cannot compile the schema — typically XSD 1.1 features such
        as xs:assert in the S-Series 2021 block release — the xmlschema library (XSD 1.0 / 1.1)."""
        from ..validation.xsd11 import XmlschemaValidator, looks_like_xsd11
        roots = [root] + self.dependency_roots(manifest) + [well_known_root()]
        catalog = {**WELL_KNOWN_SCHEMAS, **manifest.catalog}
        resolver = ConfinedResolver(roots, catalog)
        entry = confine(root, dt.schema_file)
        try:
            doc = etree.parse(str(entry), schema_parser(resolver))
            return etree.XMLSchema(doc)
        except (etree.XMLSchemaParseError, etree.XMLSyntaxError, BlockedResolution) as e:
            first_error = f"{e} {'; '.join(resolver.log[-3:])}".strip()
            refused = isinstance(e, BlockedResolution) or any("refus" in l.lower() or "blocked" in l.lower() for l in resolver.log)
            if refused or isinstance(e, etree.XMLSyntaxError):
                # a reference outside the package, or a broken file: never worked around
                raise RegistryError(f"schema {dt.schema_file} for {dt.id} failed to compile: {first_error}") from e
        files = [p for p in root.rglob("*.xsd")]
        versions = ["1.1"] if looks_like_xsd11(files) else ["1.0", "1.1"]
        last = None
        for v in versions:
            try:
                return XmlschemaValidator(self._xmlschema(root, manifest, dt, v), v)
            except Exception as e2:                     # noqa: BLE001 - report both engines' reasons
                last = e2
        raise RegistryError(f"schema {dt.schema_file} for {dt.id} failed to compile: libxml2: {first_error}; "
                            f"xmlschema: {last}")

    def _check_dtd(self, root: Path, manifest: Manifest, dt: DocType) -> None:
        from ..validation.dtd import validate_dtd
        name = dt.match.local_name
        _, entries, dtd, blocked = validate_dtd(f"<{name}/>", name, [root] + self.dependency_roots(manifest),
                                                manifest.catalog, dt.schema_file)
        if blocked:
            raise RegistryError(f"DTD {dt.schema_file} for {dt.id} needs a file that is not in the package: {blocked}")
        bad = [e for e in entries if e.filename and not e.filename.startswith("<")]
        if bad or dtd is None:
            detail = "; ".join(f"{Path(e.filename).name}:{e.line} {e.message}" for e in bad[:3]) or "DTD did not load"
            raise RegistryError(f"DTD {dt.schema_file} for {dt.id} has errors: {detail}")

    def dtd_context(self, pkg: InstalledPackage) -> tuple[list[Path], dict[str, str]]:
        return [pkg.path] + self.dependency_roots(pkg.manifest), dict(pkg.manifest.catalog)

    def schema_for(self, key: str, doc_type_id: str) -> tuple[etree.XMLSchema | None, InstalledPackage, DocType]:
        """Compiled XSD (None for DTD doc types, which are validated per document)."""
        pkg = self.get(key)
        if not pkg.enabled:
            raise RegistryError(f"package disabled: {key}")
        dt = next((d for d in pkg.manifest.doc_types if d.id == doc_type_id), None)
        if dt is None:
            raise RegistryError(f"package {key} does not define doc type {doc_type_id}")
        if dt.schema_kind in ("dtd", "sgml"):
            if not self.verify_integrity(key):
                raise RegistryError(f"package {key} failed integrity check; reinstall it")
            return None, pkg, dt
        ck = (key, doc_type_id)
        if ck not in self._compiled:
            if not self.verify_integrity(key):
                raise RegistryError(f"package {key} failed integrity check; reinstall it")
            self._compiled[ck] = self._compile(pkg.path, pkg.manifest, dt)
        return self._compiled[ck], pkg, dt

    def identity_checker(self, key: str, doc_type_id: str) -> xmlschema.XMLSchema:
        """Second, independent XSD engine used for ID/IDREF integrity, which libxml2's
        XSD validator does not enforce. Same local catalog; remote access disallowed."""
        ck = (key, doc_type_id)
        if ck not in self._idref:
            _, pkg, dt = self.schema_for(key, doc_type_id)
            roots = [pkg.path] + self.dependency_roots(pkg.manifest) + [well_known_root()]
            catalog = {}
            for url, rel in {**WELL_KNOWN_SCHEMAS, **pkg.manifest.catalog}.items():
                for r in roots:
                    cand = confine(r, rel)
                    if cand.is_file():
                        catalog[url] = cand.as_uri()
                        break

            def mapper(uri: str) -> str:
                return catalog.get(uri, uri)

            primary, _, _ = self.schema_for(key, doc_type_id)
            if hasattr(primary, "schema"):             # already compiled by xmlschema (e.g. XSD 1.1): reuse it
                self._idref[ck] = primary.schema
            else:
                self._idref[ck] = xmlschema.XMLSchema(str(confine(pkg.path, dt.schema_file)), allow="local",
                                                      defuse="always", uri_mapper=mapper)
        return self._idref[ck]
