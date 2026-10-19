"""One installer for every kind of schema source (spec §4, §5).

inspect(folder) looks at an unpacked folder or zip and proposes what to install:
  * "package"  a schema package (studio-package.json, or an ASTHRA asthra-package.json) -> install as is
  * "s1000d"   S1000D XSDs (schema annotations declare issue and root element); when the
               download holds several copies (original release, patches, data dictionary)
               every copy is offered, the latest patch preselected; ISO entity folders found
               alongside are offered too
  * "dtd"      XML DTDs (+ .ent/.mod + catalogs): ATA iSpec 2200 / ATA Spec 2300 / OEM (SGML is not supported)
  * "xsd"      any other XSD set (S2000M, S3000L, OEM): document types are the global
               elements that no other element uses, in schemas no other schema includes
build(folder, choices) builds the package the user confirmed and returns its zip path.
Nothing is guessed silently: every value the user must decide is marked as required.
"""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from lxml import etree

from ..security.xml_safe import document_parser
from .builder import BuildError, build_s1000d_package, declared_issue
from .dtd_builder import build_dtd_package, dtd_roots, read_catalogs

XS = "{http://www.w3.org/2001/XMLSchema}"
SCHEMA_EXT = {".xsd", ".dtd", ".ent", ".mod", ".elm", ".cat", ".soc", ".dcl", ".decl"}
KNOWN_STANDARDS = ["S1000D", "S2000M", "S3000L", "S4000P", "S5000F", "S6000T", "ATA2200", "ATA2300", "OEM"]


def wanted(rel: str, size: int) -> bool:
    """Files worth uploading/keeping from a schema download (never manuals, images, PDFs)."""
    name = rel.replace("\\", "/").rsplit("/", 1)[-1]
    low = name.lower()
    if low in ("isoentities", "catalog", "studio-package.json", "asthra-package.json"):
        return True
    ext = Path(low).suffix
    if ext in SCHEMA_EXT:
        return size <= 20 * 1024 * 1024
    if ext == ".xml" and re.search(r"^dmc-.+-022[a-z]-", low) and size <= 20 * 1024 * 1024:
        return True                                     # a BREX data module (information code 022)
    return ext == ".xml" and "catalog" in low and size <= 2 * 1024 * 1024


def find_brex(src: Path) -> list[dict]:
    """BREX data modules in a download (e.g. the S1000D default BREX shipped with an issue)."""
    from ..brex.model import parse_brex
    out = []
    for p in sorted(src.rglob("*")):
        if not (p.is_file() and p.suffix.lower() == ".xml" and re.search(r"^dmc-.+-022[a-z]-", p.name.lower())):
            continue
        try:
            b = parse_brex(p.read_bytes())
        except (ValueError, OSError):
            continue
        out.append({"path": p.relative_to(src).as_posix(), "dmc": b.dmc, "issue": b.issue,
                    "schema_issue": b.schema_issue, "title": b.title, "rules": len(b.rules)})
    return out


def install_brex_found(src: Path, library, issue: str | None = None) -> list[dict]:
    """Install the BREX data modules found in a download into the BREX library."""
    done = []
    for f in find_brex(src):
        if issue and f["schema_issue"] and f["schema_issue"] != issue and f["dmc"].upper().startswith("S1000D-"):
            continue                                  # a default BREX for another issue
        done.append(library.install((src / f["path"]).read_bytes(), f["path"]))
    return done


def new_staging(root: Path) -> tuple[str, Path]:
    sid = uuid.uuid4().hex
    d = root / sid / "src"
    d.mkdir(parents=True)
    return sid, d


def _annotation(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")[:8000]
    except OSError:
        return ""


def _s1000d_folders(src: Path) -> list[dict]:
    """Folders that contain S1000D document schemas (annotation 'Root element:' + S1000D URL)."""
    out = []
    dirs = sorted({p.parent for p in src.rglob("*.xsd")})
    for d in dirs:
        types = []
        for p in sorted(d.glob("*.xsd")):
            head = _annotation(p)
            if re.search(r"Root element:\s*\w+", head) and re.search(r"S1000D", head, re.I):
                types.append(p.stem.lower())
        if types:
            issue = declared_issue(d)
            rel = d.relative_to(src).as_posix() or "."
            m = re.search(r"patch[ _-]*[\d.]*([A-Z])\b", rel, re.I)
            out.append({"path": rel, "issue": issue, "doc_types": types, "files": len(list(d.glob('*.xsd'))),
                        "patch": m.group(1).upper() if m else ""})
    return out


def _entity_folders(src: Path) -> list[dict]:
    out = []
    for d in sorted({p.parent for p in src.rglob("*") if p.is_file() and
                     (p.name.lower() == "isoentities" or p.suffix.lower() == ".ent")}):
        n = sum(1 for p in d.iterdir() if p.is_file() and (p.name.lower() == "isoentities" or p.suffix.lower() == ".ent"))
        rel = d.relative_to(src).as_posix() or "."
        m = re.search(r"patch[ _-]*[\d.]*([A-Z])\b", rel, re.I)
        out.append({"path": rel, "files": n, "patch": m.group(1).upper() if m else ""})
    return out


def _pick(options: list[dict]) -> str | None:
    """Preselect the latest patch, else a folder named like the official schema folder."""
    if not options:
        return None
    patched = sorted((o for o in options if o.get("patch")), key=lambda o: o["patch"])
    if patched:
        return patched[-1]["path"]
    for o in options:
        if o["path"].lower().endswith(("xml_schema_flat", "schemas")):
            return o["path"]
    return options[0]["path"]


def _guess_standard(text: str) -> str | None:
    t = text.lower()
    for std in ("s1000d", "s2000m", "s3000l", "s4000p", "s5000f", "s6000t"):
        if std in t:
            return std.upper()
    if re.search(r"\bata\b|ispec|ata2200|ata 2200", t):
        return "ATA2200"
    return None


def _xsd_candidates(src: Path) -> list[dict]:
    """Entry schemas (not included/imported by another) and their unreferenced global elements."""
    xsds = sorted(src.rglob("*.xsd"))
    referenced_files, refs, globals_ = set(), set(), {}
    for p in xsds:
        try:
            root = etree.parse(str(p), document_parser()).getroot()
        except etree.XMLSyntaxError:
            continue
        for el in root.iter(f"{XS}include", f"{XS}import", f"{XS}redefine"):
            loc = el.get("schemaLocation")
            if loc and not re.match(r"^[a-z][a-z0-9+.-]*://", loc, re.I):
                referenced_files.add((p.parent / loc).resolve())
            elif loc:
                referenced_files.update(q.resolve() for q in xsds if q.name == loc.rsplit("/", 1)[-1])
        for el in root.iter(f"{XS}element"):
            if el.get("ref"):
                refs.add(el.get("ref").split(":")[-1])
        tns = root.get("targetNamespace")
        globals_[p] = (tns, [e.get("name") for e in root.findall(f"{XS}element") if e.get("name")])
    out = []
    for p, (tns, names) in globals_.items():
        if p.resolve() in referenced_files:
            continue
        for n in names:
            if n not in refs:
                out.append({"id": n, "schema_file": p.relative_to(src).as_posix(), "root": n, "namespace": tns})
    return out


_SGML_MIN = re.compile(r"<!ELEMENT\s+(?:\([^)]*\)|[\w.:-]+)\s+[-Oo]\s+[-Oo]\s", re.I)


def inspect(src: Path) -> dict:
    src = src.resolve()
    pkgs = [p for n in ("studio-package.json", "asthra-package.json") for p in src.rglob(n)]
    if len(pkgs) == 1:
        import json
        m = json.loads(pkgs[0].read_text(encoding="utf-8"))
        return {"kind": "package", "path": pkgs[0].parent.relative_to(src).as_posix() or ".",
                "standard": m.get("standard"), "issue": m.get("issue"), "name": m.get("name"),
                "doc_types": [{"id": d["id"], "label": d.get("label", d["id"]), "selected": True} for d in m.get("doc_types", [])],
                "notes": ["This is a ready-made schema package; it installs as it is."]}
    s1 = _s1000d_folders(src)
    if s1:
        pick = _pick(s1)
        ents = _entity_folders(src)
        issues = sorted({f["issue"] for f in s1 if f["issue"]})
        notes = []
        if len(s1) > 1:
            notes.append(f"{len(s1)} copies of the S1000D schemas were found (original release, patches, documentation). "
                         f"The latest patch is preselected.")
        if len(issues) > 1:
            notes.append(f"The copies declare different issues ({', '.join(issues)}); choose the one you need.")
        brex = find_brex(src)
        chosen_issue = next((f["issue"] for f in s1 if f["path"] == pick), None)
        defaults = [b for b in brex if b["dmc"].upper().startswith("S1000D-")]
        if defaults:
            notes.append("Default BREX found (" + ", ".join(f"DMC-{b['dmc']} issue {b['issue']}" for b in defaults[:3])
                         + "): it is installed with the schemas, so business rules can be checked.")
        else:
            notes.append("No default BREX was found in this folder. Business rules can still be checked once you add the "
                         "BREX your documents name (Schemas and rules → BREX).")
        return {"kind": "s1000d", "standard": "S1000D", "folders": s1, "folder": pick, "brex": brex,
                "issue": chosen_issue,
                "entity_folders": ents, "entity_folder": _pick(ents),
                "default_types": ["proced", "descript", "ipd"], "notes": notes}
    dtds = sorted(src.rglob("*.dtd"))
    if dtds:
        cat = read_catalogs(src)
        for f in (p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file()):
            cat.setdefault(Path(f).name, f)
        types = []
        for p in dtds:
            rel = p.relative_to(src).as_posix()
            try:
                cands = dtd_roots(src, rel, cat)
            except BuildError as e:
                types.append({"id": p.stem, "schema_file": rel, "root": None, "root_candidates": [], "problem": str(e), "selected": False})
                continue
            root = cands[0] if len(cands) == 1 else (p.stem if p.stem in cands else None)
            types.append({"id": p.stem, "schema_file": rel, "root": root, "root_candidates": cands[:40],
                          "selected": root is not None,
                          "problem": None if root else "The top element could not be determined; choose it."})
        guess = _guess_standard(" ".join(cat.keys()) + " " + " ".join(p.read_text(errors="ignore")[:2000] for p in dtds[:5]))
        return {"kind": "dtd", "standard": guess if guess in ("ATA2200", "ATA2300") else "OEM", "issue": None,
                "doc_types": types, "public_ids": sorted(k for k in cat if re.match(r"^([-+]//|ISO[ /])", k))[:50],
                "notes": ["Enter the revision of this DTD set (for example 2023.1). It cannot be read from the files."]}
    xsds = list(src.rglob("*.xsd"))
    if xsds:
        cands = _xsd_candidates(src)
        text = " ".join((c["namespace"] or "") + " " + c["schema_file"] for c in cands)
        guess = _guess_standard(text) or "OEM"
        return {"kind": "xsd", "standard": guess, "issue": None,
                "doc_types": [{**c, "label": c["id"], "selected": len(cands) <= 5} for c in cands],
                "notes": ["Enter the issue/revision of this schema set. Tick the elements that are document roots "
                          "(files you will import start with one of them)."]}
    raise BuildError("No schema files were found (.xsd, .dtd, or a schema package).")


def build_xsd_package(src: Path, standard: str, issue: str, out: Path, name: str | None,
                      types: list[dict]) -> dict:
    import json
    import zipfile
    src = src.resolve()
    files = sorted(p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file() and p.suffix.lower() in (".xsd", ".ent", ".dtd", ".mod"))
    catalog = {Path(f).name: f for f in files}
    doc_types = [{"id": t["id"], "label": t.get("label") or t["id"], "content_family": "other",
                  "schema_file": t["schema_file"], "match": {"local_name": t["root"], "namespace": t.get("namespace")}}
                 for t in types]
    if not doc_types:
        raise BuildError("Select at least one document type.")
    manifest = {"package_id": "official" if standard != "OEM" else "oem", "name": name or f"{standard} {issue} schemas",
                "standard": standard, "issue": issue, "provenance": "official" if standard != "OEM" else "oem",
                "licence_note": "Schemas used under their owner's licence. Not redistributed by Procedure Studio.",
                "doc_types": doc_types, "files": files, "catalog": catalog}
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("studio-package.json", json.dumps(manifest, indent=2))
        for rel in files:
            zf.write(src / rel, rel)
    return manifest


def build(src: Path, choice: dict, out: Path) -> Path:
    """Build the package the user confirmed. choice = inspect() result edited by the user."""
    kind = choice["kind"]
    src = src.resolve()
    if kind == "package":
        return src / choice.get("path", ".")
    issue = (choice.get("issue") or "").strip()
    if not issue:
        raise BuildError("The issue/revision is required.")
    selected = [t for t in choice.get("doc_types", []) if t.get("selected")] if kind != "s1000d" else None
    if kind == "s1000d":
        folder = src / choice["folder"]
        ent = [src / choice["entity_folder"]] if choice.get("entity_folder") else None
        build_s1000d_package(folder, issue, out, choice.get("types") or None, ent)
    elif kind == "dtd":
        if not selected:
            raise BuildError("Select at least one document type.")
        missing = [t["id"] for t in selected if not t.get("root")]
        if missing:
            raise BuildError(f"Choose the top element for: {', '.join(missing)}")
        build_dtd_package(src, choice.get("standard") or "OEM", issue, out, choice.get("name"),
                          {t["id"]: t["root"] for t in selected}, [t["id"] for t in selected])
    elif kind == "xsd":
        std = (choice.get("standard") or "OEM").upper()
        if std not in KNOWN_STANDARDS:
            raise BuildError(f"Unknown standard {std}.")
        build_xsd_package(src, std, issue, out, choice.get("name"), selected or [])
    else:
        raise BuildError(f"Unknown source kind: {kind}")
    return out


def cleanup(staging_root: Path, keep: str | None = None) -> None:
    for d in staging_root.glob("*"):
        if d.is_dir() and d.name != keep:
            shutil.rmtree(d, ignore_errors=True)
