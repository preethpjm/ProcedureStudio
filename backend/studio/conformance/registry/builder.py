"""Build an installable schema package from a folder of official S1000D schemas.

Everything is derived from the files themselves:
  * doc types = schemas whose annotation declares "Root element: X"
    (xlink.xsd, rdf.xsd, dc.xsd etc. have none and become dependencies);
  * remote schemaLocation URLs are mapped to local files of the same name in the
    catalog, and the build fails if any dependency is missing;
  * identification only matches documents whose schema location names this exact
    issue (e.g. .../S1000D_4-1/xml_schema_flat/proced.xsd), never a bare file name.
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

from lxml import etree

from ..security.xml_safe import document_parser

XS = "{http://www.w3.org/2001/XMLSchema}"
FAMILY = {"proced": "procedure", "descript": "description", "ipd": "ipd"}
LABELS = {
    "proced": "Procedure", "descript": "Description", "ipd": "Illustrated parts data", "crew": "Crew/operator",
    "fault": "Fault isolation", "checklist": "Checklist", "schedul": "Maintenance planning", "brex": "BREX",
    "learning": "Learning", "wrngdata": "Wiring data", "wrngflds": "Wiring fields", "process": "Process",
    "container": "Container", "comrep": "Common information repository", "frontmatter": "Front matter",
    "sb": "Service bulletin", "pm": "Publication module", "dml": "Data module list", "comment": "Comment",
    "ddn": "Data dispatch note", "appliccrossreftable": "Applicability cross-reference table",
    "condcrossreftable": "Conditions cross-reference table", "prdcrossreftable": "Product cross-reference table",
    "techrep": "Technical repository", "icnmetadata": "ICN metadata", "scocontent": "SCORM content",
}
IDENT = {k: f"/dmodule/identAndStatusSection/dmAddress/dmIdent/dmCode/@{k}" for k in (
    "modelIdentCode", "systemDiffCode", "systemCode", "subSystemCode", "subSubSystemCode", "assyCode",
    "disassyCode", "disassyCodeVariant", "infoCode", "infoCodeVariant", "itemLocationCode")}
IDENT.update({
    "issueNumber": "/dmodule/identAndStatusSection/dmAddress/dmIdent/issueInfo/@issueNumber",
    "inWork": "/dmodule/identAndStatusSection/dmAddress/dmIdent/issueInfo/@inWork",
    "techName": "/dmodule/identAndStatusSection/dmAddress//dmTitle/techName",
})


def content_element(xsd_root: etree._Element) -> str | None:
    """The element a data-module schema allows directly inside <content>, other than
    <refs> (e.g. proced.xsd -> procedure). Read from the schema, not assumed."""
    for el in xsd_root.iter(f"{XS}element"):
        if el.get("name") != "content":
            continue
        names = [e.get("ref") or e.get("name") for e in el.iter(f"{XS}element") if e is not el]
        typ = el.get("type")
        if not names and typ:
            ct = xsd_root.find(f"{XS}complexType[@name='{typ.split(':')[-1]}']")
            if ct is not None:
                names = [e.get("ref") or e.get("name") for e in ct.iter(f"{XS}element")]
        names = [n.split(":")[-1] for n in names if n and n.split(":")[-1] not in ("refs",)]
        if len(names) == 1:
            return names[0]
    # content model defined through a named type referenced by element/@type elsewhere
    return None


class BuildError(Exception):
    pass


def issue_marker(issue: str) -> str:
    """'4.1' -> 'S1000D_4-1', '4.0.1' -> 'S1000D_4-0-1' (as used in official schema URLs)."""
    if not re.fullmatch(r"\d+(\.\d+)*[A-Za-z]?", issue):
        raise BuildError(f"issue must look like 4.1 or 4.0.1, got {issue!r}")
    return "S1000D_" + re.sub(r"[A-Za-z]$", "", issue).replace(".", "-")


def _norm_issue(v: str) -> str:
    return ".".join(str(int(x)) for x in re.findall(r"\d+", v))


def declared_issue(folder: Path) -> str | None:
    """Issue number stated in the schemas' own annotations (e.g. 'Issue number: 4.1')."""
    for p in sorted(folder.rglob("*.xsd")):
        head = p.read_text(encoding="utf-8", errors="ignore")[:6000]
        m = re.search(r"Issue number:\s*([0-9][0-9.]*)", head)
        if m:
            return m.group(1).rstrip(".")
    return None


def build_s1000d_package(src: Path, issue: str, out: Path, only: list[str] | None = None,
                         entities: list[Path] | None = None) -> dict:
    """entities: folders of entity files (e.g. the S1000D ISO entity sets: ISOEntities, iso-*.ent).
    They are bundled under ent/ and registered by file name, so a DOCTYPE that refers to
    http://www.s1000d.org/.../ISOEntities resolves to the local copy, never the network."""
    src = src.resolve()
    xsds = sorted(p for p in src.rglob("*.xsd") if p.is_file())
    if not xsds:
        raise BuildError(f"no .xsd files found in {src}")
    by_name = {p.name.lower(): p for p in xsds}
    found = declared_issue(src)
    if found and _norm_issue(found) != _norm_issue(issue):
        raise BuildError(f"these schemas declare Issue {found}, not {issue}. "
                         f"Use --issue {found}, or download the Issue {issue} schemas.")
    marker = issue_marker(issue)
    only = [o.strip().lower() for o in only] if only else None

    # Parse every schema once: its dependencies and (if it is a document type) its root element.
    info: dict[Path, dict] = {}
    for p in xsds:
        try:
            root = etree.parse(str(p), document_parser()).getroot()
        except etree.XMLSyntaxError as e:
            info[p] = {"deps": [], "missing": [f"{p.name} is not well-formed: {e}"], "doc": ""}
            continue
        deps, missing, remote = [], [], {}
        for el in root.iter(f"{XS}import", f"{XS}include", f"{XS}redefine"):
            loc = el.get("schemaLocation")
            if not loc:
                continue
            base = loc.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if re.match(r"^[a-z][a-z0-9+.-]*://", loc, re.I):
                if base in by_name:
                    deps.append(by_name[base]); remote[loc] = by_name[base]
                else:
                    missing.append(f"{p.name} imports {loc}")
            else:
                target = (p.parent / loc).resolve()
                if target.is_file() and (target == src or src in target.parents):
                    deps.append(target)
                else:
                    missing.append(f"{p.name} includes {loc}")
        info[p] = {"deps": deps, "missing": missing, "remote": remote,
                   "doc": " ".join(t.text or "" for t in root.iter(f"{XS}documentation"))}

    def closure(entry: Path) -> tuple[set[Path], list[str]]:
        seen, todo, missing = set(), [entry], []
        while todo:
            q = todo.pop()
            if q in seen:
                continue
            seen.add(q)
            missing += info[q]["missing"]
            todo += info[q]["deps"]
        return seen, missing

    doc_types, needed, skipped, catalog = [], set(), [], {}
    candidates = []
    for p in xsds:
        m = re.search(r"Root element:\s*([A-Za-z_][\w.-]*)", info[p]["doc"])
        if m and (not only or p.stem.lower() in only):
            candidates.append((p, m.group(1)))
    if only:
        absent = sorted(set(only) - {p.stem.lower() for p, _ in candidates})
        if absent:
            raise BuildError(f"not found in this folder as document schemas: {', '.join(absent)}")
    for p, rootname in candidates:
        files, missing = closure(p)
        if missing:
            skipped.append(f"{p.stem}: " + "; ".join(sorted(set(missing))))
            continue
        needed |= files
        for q in files:
            for loc, tgt in info[q].get("remote", {}).items():
                catalog[loc] = tgt.relative_to(src).as_posix()
        stem = p.stem.lower()
        u = re.search(r"URL:\s*\S*?/(S1000D_[^/\s]+)/xml_schema_(?:flat|master)/", info[p]["doc"], re.I)
        file_marker = re.escape(u.group(1)) if u else marker
        doc_types.append({
            "id": stem,
            "label": LABELS.get(stem, stem),
            "content_family": FAMILY.get(stem, "other"),
            "schema_file": p.relative_to(src).as_posix(),
            "match": {"local_name": rootname, "namespace": None,
                      "schema_location_pattern": rf"(?i)(^|/){file_marker}/xml_schema_(flat|master)/{re.escape(p.name)}$"},
            "identity_xpaths": IDENT if rootname == "dmodule" else {},
            **({"discriminator": f"/dmodule/content/{ce}"} if rootname == "dmodule" and (ce := content_element(etree.parse(str(p), document_parser()).getroot())) else {}),
        })
    if only and skipped:
        raise BuildError("requested schemas have missing dependencies:\n  " + "\n  ".join(skipped))
    if not doc_types:
        detail = ("\n  " + "\n  ".join(skipped)) if skipped else ""
        raise BuildError("no usable document schemas found" + detail)
    files = sorted(q.relative_to(src).as_posix() for q in needed)
    extra: list[tuple[Path, str]] = []
    for folder in entities or []:
        folder = Path(folder).resolve()
        if not folder.is_dir():
            raise BuildError(f"entities folder not found: {folder}")
        ents = [p for p in folder.rglob("*") if p.is_file() and (p.suffix.lower() in (".ent", ".mod", "")
                                                                   or p.name.lower() == "isoentities")]
        if not ents:
            raise BuildError(f"no entity files (.ent, ISOEntities) in {folder}")
        for p in ents:
            rel = "ent/" + p.relative_to(folder).as_posix()
            if rel not in {r for _, r in extra}:
                extra.append((p, rel))
                catalog.setdefault(p.name, rel)
    files += [r for _, r in extra]
    manifest = {
        "package_id": "official", "name": f"S1000D Issue {issue} schemas", "standard": "S1000D",
        "issue": issue, "provenance": "official",
        "licence_note": "Official S1000D schemas, used under the S1000D Terms and Conditions. Not redistributed by Procedure Studio.",
        "doc_types": doc_types, "files": files, "catalog": catalog,
        "namespaces": {"xlink": "http://www.w3.org/1999/xlink"},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("studio-package.json", json.dumps(manifest, indent=2))
        for rel in files:
            if not rel.startswith("ent/"):
                zf.write(src / rel, rel)
        for p, rel in extra:
            zf.write(p, rel)
    manifest["_skipped"] = skipped          # reported to the user, not stored in the package
    return manifest
