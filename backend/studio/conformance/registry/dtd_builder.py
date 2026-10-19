"""Build an installable package from a folder of DTDs (ATA iSpec 2200 XML, ATA Spec 2300,
OEM DTDs such as an airframer's CMM/AMM DTDs).

  * Document types are the .dtd files (entity/module files .ent/.mod/.elm are dependencies).
  * Public identifiers come from the folder's catalog: an OASIS XML catalog and/or an
    SGML/TR9401 CATALOG file. They become identification rules and catalog mappings.
  * Each DTD's root element is the declared element that no other element contains,
    read from the DTD itself. If that is ambiguous, the build stops and asks for --root.
"""
from __future__ import annotations

import json
import re
import shlex
import zipfile
from pathlib import Path

from lxml import etree

from ..security.xml_safe import document_parser
from ..validation.dtd import validate_dtd
from .builder import BuildError

TEXT_EXT = {".dtd", ".ent", ".mod", ".elm", ".cat", ".soc", ".xml", ".txt"}
OASIS = "urn:oasis:names:tc:entity:xmlns:xml:catalog"


def read_catalogs(src: Path) -> dict[str, str]:
    """public/system id -> relative file, from OASIS XML catalogs and SGML CATALOG files."""
    out: dict[str, str] = {}
    src = src.resolve()
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() == ".xml":
            try:
                root = etree.parse(str(p), document_parser()).getroot()
            except etree.XMLSyntaxError:
                continue
            if etree.QName(root).namespace != OASIS:
                continue
            for el in root.iter():
                if not isinstance(el.tag, str):
                    continue
                tag = etree.QName(el).localname
                key = el.get("publicId") if tag == "public" else el.get("systemId") if tag == "system" else None
                uri = el.get("uri")
                if key and uri:
                    target = (p.parent / uri).resolve()
                    if target.is_file() and src in target.parents:
                        out[key] = target.relative_to(src).as_posix()
        elif p.name.lower() in ("catalog", "catalog.cat") or p.suffix.lower() in (".cat", ".soc"):
            text = re.sub(r"--.*?--", " ", p.read_text(encoding="utf-8", errors="ignore"), flags=re.S)
            try:
                toks = shlex.split(text, posix=True)
            except ValueError:
                continue
            for i, t in enumerate(toks[:-2]):
                if t.upper() in ("PUBLIC", "SYSTEM"):
                    target = (p.parent / toks[i + 2]).resolve()
                    if target.is_file() and src in target.parents:
                        out[toks[i + 1]] = target.relative_to(src).as_posix()
    return out


def _child_names(decl, acc: set[str]) -> None:
    if decl is None:
        return
    if decl.name:
        acc.add(decl.name)
    _child_names(decl.left, acc)
    _child_names(decl.right, acc)


def dtd_roots(src: Path, rel: str, catalog: dict[str, str]) -> list[str]:
    """Declared elements that no other element's content model contains."""
    _, _, dtd, blocked = validate_dtd("<asthraProbe/>", "asthraProbe", [src], catalog, rel)
    if blocked:
        raise BuildError(f"{rel}: {blocked}")
    if dtd is None:
        raise BuildError(f"{rel}: the DTD could not be loaded")
    declared, used = set(), set()
    for el in dtd.iterelements():
        declared.add(el.name)
        _child_names(el.content, used)
    return sorted(declared - used)


def build_dtd_package(src: Path, standard: str, issue: str, out: Path, name: str | None = None,
                      roots: dict[str, str] | None = None, only: list[str] | None = None) -> dict:
    src = src.resolve()
    files = sorted(p.relative_to(src).as_posix() for p in src.rglob("*")
                   if p.is_file() and (p.suffix.lower() in TEXT_EXT or p.name.lower() == "catalog"))
    dtds = [f for f in files if f.lower().endswith(".dtd")]
    if not dtds:
        raise BuildError(f"no .dtd files found in {src}")
    catalog = read_catalogs(src)
    # also allow documents to refer to a DTD by its bare file name
    for f in files:
        catalog.setdefault(Path(f).name, f)
    by_file: dict[str, list[str]] = {}
    for key, f in catalog.items():
        if re.match(r"^([-+]//|ISO[ /])", key):               # formal public identifier syntax
            by_file.setdefault(f, []).append(key)
    roots = {k.lower(): v for k, v in (roots or {}).items()}
    only = [o.lower() for o in only] if only else None
    doc_types, problems = [], []
    for rel in dtds:
        stem = Path(rel).stem
        if only and stem.lower() not in only:
            continue
        root = roots.get(stem.lower())
        if not root:
            cands = dtd_roots(src, rel, catalog)
            if len(cands) == 1:
                root = cands[0]
            elif stem in cands:
                root = stem
            else:
                problems.append(f"{rel}: top element unclear ({', '.join(cands[:8]) or 'none found'}); "
                                f"pass --root {stem}=<element>")
                continue
        pubs = by_file.get(rel, [])
        match = {"local_name": root, "namespace": None,
                 "system_id_pattern": rf"(?i)(^|[/\\]){re.escape(Path(rel).name)}$"}
        if pubs:
            match["public_id_pattern"] = "^(" + "|".join(re.escape(x) for x in pubs) + ")$"
        doc_types.append({"id": stem, "label": stem, "content_family": "other", "schema_file": rel,
                          "schema_kind": "dtd", "match": match})
    if problems:
        raise BuildError("\n  ".join(["could not determine every document type:"] + problems))
    if not doc_types:
        raise BuildError("no document types selected")
    manifest = {"package_id": "official", "name": name or f"{standard} {issue} DTDs", "standard": standard,
                "issue": issue, "provenance": ("synthetic" if "synthetic" in issue.lower() else
                                               "official" if standard.upper() != "OEM" else "oem"),
                "licence_note": "DTDs used under their owner's licence. Not redistributed by Procedure Studio.",
                "doc_types": doc_types, "files": files, "catalog": catalog}
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("studio-package.json", json.dumps(manifest, indent=2))
        for rel in files:
            zf.write(src / rel, rel)
    return manifest
