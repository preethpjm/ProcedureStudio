"""Format and schema identification (spec §7 intro, §24.4).

Identification is evidence-based. It never guesses between candidates: if two
installed packages both match, the result is `ambiguous` and the user chooses.
"""
from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from typing import Literal

from lxml import etree

from ..registry.package import DocType
from ..registry.service import InstalledPackage
from ..security.xml_safe import document_parser

XSI = "http://www.w3.org/2001/XMLSchema-instance"
_BOMS = [
    (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
]
_DECL_ENC = re.compile(rb"""^<\?xml[^>]*?encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")
_SGML_DECL = re.compile(rb"^\s*<!SGML\b", re.I)
_DOCTYPE = re.compile(rb"<!DOCTYPE\s+([A-Za-z_][\w.:-]*)", re.I)


@dataclass
class Sniff:
    encoding: str | None
    bom: str | None
    declared_encoding: str | None
    has_xml_declaration: bool
    doctype_name: str | None
    has_internal_subset: bool
    syntax_hint: Literal["xml", "sgml", "unknown"]
    problems: list[str] = field(default_factory=list)


def mislabelled_8bit(data: bytes) -> str | None:
    """The encoding a document declares when its bytes cannot be in it: an XML declaration that says UTF-16
    (or UTF-32 / UCS-2) on a file with no byte-order mark and no zero bytes - typically a UTF-16 file
    re-saved as UTF-8 by an editor that kept the first line. None when the declaration is plausible."""
    if any(data.startswith(b) for b, _ in _BOMS):
        return None
    m = _DECL_ENC.match(data[:200].lstrip())
    if not m:
        return None
    declared = m.group(1).decode("ascii", "replace").lower()
    if declared.replace("-", "").replace("_", "").startswith(("utf16", "utf32", "ucs2", "ucs4")) and b"\x00" not in data[:512]:
        return declared
    return None


def fix_declared_encoding(data: bytes) -> bytes:
    """Bytes a parser can read: a mislabelled UTF-16 declaration on 8-bit bytes is read as UTF-8
    (only the declaration's value changes; the stored original is never touched)."""
    if not mislabelled_8bit(data):
        return data
    lead = len(data) - len(data.lstrip())
    return data[:lead] + re.sub(rb"""(encoding\s*=\s*["'])[A-Za-z0-9._-]+(["'])""", rb"\1UTF-8\2", data[lead:], count=1)


def sniff(data: bytes) -> Sniff:
    bom = next((name for b, name in _BOMS if data.startswith(b)), None)
    body = data[len(next((b for b, n in _BOMS if n == bom), b"")):] if bom else data
    probe = body[:4096]
    if bom and bom.startswith("utf-16"):
        probe = body[:8192].decode(bom, errors="ignore").encode("utf-8")
    m = _DECL_ENC.match(probe.lstrip())
    declared = m.group(1).decode().lower() if m else None
    has_decl = probe.lstrip().startswith(b"<?xml")
    dt = _DOCTYPE.search(probe)
    internal = bool(dt and re.search(rb"<!DOCTYPE[^>\[]*\[", probe))
    problems: list[str] = []
    encoding = bom or declared or "utf-8"
    if mislabelled_8bit(data):
        problems.append(f"declares {declared} but the bytes are 8-bit (re-saved as UTF-8?): read as UTF-8")
        encoding = "utf-8"
    if bom and declared and codecs.lookup(bom.replace("-le", "").replace("-be", "")).name != codecs.lookup(declared).name \
            and not (bom.startswith("utf-16") and declared.startswith("utf-16")):
        problems.append(f"byte-order mark ({bom}) contradicts declared encoding ({declared})")
    try:
        codecs.lookup(encoding)
    except LookupError:
        problems.append(f"unknown encoding: {encoding}")
        encoding = None
    if _SGML_DECL.match(probe):
        hint = "sgml"
    elif has_decl:
        hint = "xml"
    elif dt:
        hint = "unknown"   # DOCTYPE without XML declaration: XML or SGML — decided by parsing
    elif probe.lstrip().startswith(b"<"):
        hint = "xml"
    else:
        hint = "unknown"
    return Sniff(encoding, bom, declared, has_decl, dt.group(1).decode() if dt else None, internal, hint, problems)


@dataclass
class Candidate:
    package_id: str
    standard: str
    issue: str
    doc_type: str
    content_family: str
    evidence: list[str]


@dataclass
class Identification:
    syntax: Literal["xml", "sgml", "unknown"]
    status: Literal["identified", "ambiguous", "needs-choice", "unidentified", "not-parsed"]
    root_local_name: str | None = None
    root_namespace: str | None = None
    schema_location: str | None = None
    public_id: str | None = None
    system_id: str | None = None
    candidates: list[Candidate] = field(default_factory=list)     # exact matches
    compatible: list[Candidate] = field(default_factory=list)     # root/content fit, declaration does not
    notes: list[str] = field(default_factory=list)

    @property
    def chosen(self) -> Candidate | None:
        return self.candidates[0] if self.status == "identified" else None

    def as_dict(self) -> dict:
        return {
            "syntax": self.syntax, "status": self.status,
            "root": {"local_name": self.root_local_name, "namespace": self.root_namespace,
                     "schema_location": self.schema_location, "public_id": self.public_id,
                     "system_id": self.system_id},
            "candidates": [c.__dict__ for c in self.candidates],
            "compatible": [c.__dict__ for c in self.compatible], "notes": self.notes,
        }


def s1000d_location_matches(issue: str, schema_file: str, loc: str | None) -> bool:
    """S1000D schema URLs follow .../S1000D_<issue with '-'>/xml_schema_(flat|master)/<file>.xsd
    (e.g. S1000D_4-1, S1000D_6). Decided from the package's issue and file name alone, so it
    does not depend on how a schema annotation happens to be worded."""
    if not loc:
        return False
    parts = re.split(r"[\\/]", loc.strip())
    if len(parts) < 3:
        return False
    fname, folder, marker = parts[-1].lower(), parts[-2].lower(), parts[-3]
    want = "S1000D_" + re.sub(r"[A-Za-z]$", "", issue).replace(".", "-")
    return (fname == schema_file.replace("\\", "/").rsplit("/", 1)[-1].lower()
            and folder in ("xml_schema_flat", "xml_schema_master")
            and marker.lower() == want.lower())


def _declared(dt: DocType, loc: str | None, pub: str | None, sysid: str | None,
              standard: str | None = None, issue: str | None = None) -> list[str] | None:
    """Evidence that the document declares exactly this schema, or None."""
    m = dt.match
    ev = []
    if standard == "S1000D" and issue and s1000d_location_matches(issue, dt.schema_file, loc):
        ev.append(f"schema location names S1000D issue {issue}, {dt.schema_file.rsplit('/', 1)[-1]}")
    if m.schema_location_pattern:
        if loc and re.search(m.schema_location_pattern, loc):
            ev.append(f"schema location matches /{m.schema_location_pattern}/")
    if m.public_id_pattern and pub and re.search(m.public_id_pattern, pub):
        ev.append(f"DOCTYPE public identifier '{pub}'")
    if m.system_id_pattern and sysid and re.search(m.system_id_pattern, sysid):
        ev.append(f"DOCTYPE system identifier matches /{m.system_id_pattern}/")
    has_rule = bool(m.schema_location_pattern or m.public_id_pattern or m.system_id_pattern)
    if not has_rule:
        return ["package declares no schema-location rule; root element is sufficient"]
    return ev or None


def declares(dt: DocType, root: etree._Element, standard: str | None = None, issue: str | None = None) -> bool:
    """Does this document declare the given doc type's schema (URL / public id / system id)?"""
    info = root.getroottree().docinfo
    loc = root.get(f"{{{XSI}}}noNamespaceSchemaLocation") or root.get(f"{{{XSI}}}schemaLocation")
    return _declared(dt, loc, info.public_id, info.system_url, standard, issue) is not None


def root_name_fits(dt: DocType, root: etree._Element) -> bool:
    """For XML documents. SGML DTD sets never claim an XML document (and are matched
    separately, from the SGML DOCTYPE)."""
    if dt.schema_kind == "sgml":
        return False
    q = etree.QName(root)
    return dt.match.local_name == q.localname and (dt.match.namespace or None) == (q.namespace or None)


def root_fits(dt: DocType, root: etree._Element) -> bool:
    """Root element fits and, if the doc type has a discriminator, the content matches it."""
    if not root_name_fits(dt, root):
        return False
    if dt.discriminator:
        try:
            return bool(root.getroottree().xpath(dt.discriminator))
        except etree.XPathError:
            return False
    return True


_SGML_DT = re.compile(r"""<!DOCTYPE\s+([^\s\[>]+)(?:\s+PUBLIC\s+("[^"]*"|'[^']*')(?:\s+("[^"]*"|'[^']*'))?|\s+SYSTEM\s+("[^"]*"|'[^']*'))?""", re.I)


def identify_sgml(text: str, packages: list[InstalledPackage]) -> Identification:
    """SGML documents are identified from their DOCTYPE: document type name (case-insensitive),
    public identifier and system identifier, against installed SGML packages."""
    ident = Identification(syntax="sgml", status="unidentified")
    m = _SGML_DT.search(text)
    if not m:
        ident.status = "not-parsed"
        ident.notes.append("SGML without a DOCTYPE cannot be identified.")
        return ident
    name = m.group(1)
    pub = (m.group(2) or "")[1:-1] or None
    sysid = ((m.group(3) or m.group(4) or "")[1:-1]) or None
    ident.root_local_name, ident.public_id, ident.system_id = name.lower(), pub, sysid
    for pkg in packages:
        if not pkg.enabled:
            continue
        for dt in pkg.manifest.doc_types:
            if dt.schema_kind != "sgml" or dt.match.local_name.lower() != name.lower():
                continue
            ev = [f"DOCTYPE <{name}>"]
            declared = []
            if dt.match.public_id_pattern and pub and re.search(dt.match.public_id_pattern, pub):
                declared.append(f"public identifier '{pub}'")
            if dt.match.system_id_pattern and sysid and re.search(dt.match.system_id_pattern, sysid):
                declared.append("system identifier matches")
            c = Candidate(pkg.manifest.key, pkg.manifest.standard, pkg.manifest.issue, dt.id, dt.content_family, ev + declared)
            (ident.candidates if declared else ident.compatible).append(c)
    if len(ident.candidates) == 1:
        ident.status = "identified"
    elif len(ident.candidates) > 1:
        ident.status = "ambiguous"
        ident.notes.append("More than one installed SGML package matches exactly. Choose one.")
    elif ident.compatible:
        ident.status = "needs-choice"
        ident.notes.append(f"This SGML document's DOCTYPE <{name}> fits installed DTD sets, but its identifiers do "
                           "not match; choose one to validate against.")
    else:
        ident.notes.append(f"No installed SGML DTD set is for DOCTYPE <{name}>. Install its DTD set "
                           "(Schemas and rules → Add schemas).")
    return ident


def recover_root(data: bytes):
    """The readable part of a document that is not well-formed (lxml recovery), or None. Nothing
    external is loaded."""
    from ..security.xml_safe import NoExternalResolver
    p = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    p.resolvers.add(NoExternalResolver())
    try:
        root = etree.fromstring(fix_declared_encoding(data), p)
    except (etree.XMLSyntaxError, ValueError):
        return None
    return root if root is not None and isinstance(root.tag, str) else None


def identify(root: etree._Element | None, packages: list[InstalledPackage], sniffed: Sniff,
             data: bytes | None = None) -> Identification:
    if root is None:
        syntax = "sgml" if sniffed.syntax_hint in ("sgml", "unknown") and sniffed.doctype_name else sniffed.syntax_hint
        if syntax == "sgml" and data is not None:
            return identify_sgml(data.decode(sniffed.encoding or "utf-8", errors="replace"), packages)
        if syntax == "xml" and data is not None:
            rec = recover_root(data)
            if rec is not None:
                # not well-formed (e.g. cut off): identified from the part that can be read, so it can
                # still be checked against its schema up to the point where it breaks
                ident = identify(rec, packages, sniffed, None)
                ident.notes.append("The document is not well-formed XML; it was identified from the part that can be read.")
                return ident
        ident = Identification(syntax=syntax, status="not-parsed")
        if syntax == "sgml":
            ident.notes.append("Document appears to be SGML. Install its DTD set to identify it.")
        return ident
    q = etree.QName(root)
    info = root.getroottree().docinfo
    loc = root.get(f"{{{XSI}}}noNamespaceSchemaLocation") or root.get(f"{{{XSI}}}schemaLocation")
    ident = Identification(syntax="xml", status="unidentified", root_local_name=q.localname,
                           root_namespace=q.namespace, schema_location=loc,
                           public_id=info.public_id, system_id=info.system_url)
    for pkg in packages:
        if not pkg.enabled:
            continue
        for dt in pkg.manifest.doc_types:
            if not root_name_fits(dt, root):
                continue
            base = [f"root element <{q.localname}>", f"namespace {q.namespace!r}" if q.namespace else "no namespace"]
            ev = _declared(dt, loc, info.public_id, info.system_url, pkg.manifest.standard, pkg.manifest.issue)
            if ev is not None and (dt.match.schema_location_pattern or dt.match.public_id_pattern or dt.match.system_id_pattern):
                # the document names this exact schema: that decides it
                ident.candidates.append(Candidate(pkg.manifest.key, pkg.manifest.standard, pkg.manifest.issue,
                                                  dt.id, dt.content_family, base + ev))
            elif root_fits(dt, root):
                target = ident.candidates if ev is not None else ident.compatible
                target.append(Candidate(pkg.manifest.key, pkg.manifest.standard, pkg.manifest.issue,
                                        dt.id, dt.content_family, base + (ev or [])))
    declared = loc or info.public_id or info.system_url
    if len(ident.candidates) == 1:
        ident.status = "identified"
    elif len(ident.candidates) > 1:
        ident.status = "ambiguous"
        ident.notes.append("More than one installed schema matches this document exactly. Choose one.")
    elif ident.compatible:
        ident.status = "needs-choice"
        what = f"declares '{declared}', which no installed package matches" if declared else \
            "does not declare which schema or issue it uses"
        ident.notes.append(f"This document {what}. Installed schemas that fit its structure are listed; "
                           "choose one to validate against. Procedure Studio will not pick one for you.")
    else:
        ident.notes.append("No enabled schema package fits this document's root element. "
                           "Install its schema package; Procedure Studio will not guess.")
    return ident


_ENTREF = re.compile(r"&([A-Za-z_:][\w.:-]*);")
_PREDEF = {"amp", "lt", "gt", "quot", "apos"}


def _strict_parse(data: bytes):
    from ..security.xml_safe import structural_parser
    parser = structural_parser()   # per-parse log; never loads external files
    try:
        return etree.ElementTree(etree.fromstring(fix_declared_encoding(data), parser)), []
    except etree.XMLSyntaxError as e:
        seen, entries = set(), []
        for x in parser.error_log:
            k = (x.line, x.column, x.message)
            if k not in seen:
                seen.add(k); entries.append(x)
        return None, entries or str(e)


def _may_declare_externally(text: str) -> bool:
    """Does the DOCTYPE import declarations the quick parse does not read (an external
    subset, or a parameter-entity reference such as %ISOEntities;)?"""
    m = re.search(r"<!DOCTYPE\s+[^\s\[>]+([^\[>]*)(\[(.*?)\])?\s*>", text, re.S)
    if not m:
        return False
    return bool(re.search(r"\b(PUBLIC|SYSTEM)\b", m.group(1) or "")) or bool(re.search(r"%[A-Za-z_:][\w.:-]*;", m.group(3) or ""))


def _without_pe_refs(text: str) -> str:
    """The same text with %name; references removed from the DOCTYPE's internal subset (as if the
    external entity set were not referenced). Line numbers are kept."""
    m = re.search(r"<!DOCTYPE[^\[>]*\[", text, re.I)
    if not m:
        return text
    depth, q, i = 1, "", m.end()
    while i < len(text) and depth:
        c = text[i]
        if q:
            if c == q:
                q = ""
        elif c in "\"'":
            q = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        i += 1
    subset = re.sub(r"%[A-Za-z_:][\w.:-]*;", lambda r: " " * len(r.group(0)), text[m.end():i - 1])
    return text[:m.end()] + subset + text[i - 1:]


def parse_xml(data: bytes) -> tuple[etree._ElementTree | None, list[etree._LogEntry] | str]:
    """Well-formedness parse, without loading any DTD or entity file.

    XML 1.0 (WFC: Entity Declared) says an undeclared entity is NOT a well-formedness
    error when the DOCTYPE imports declarations from elsewhere; it is checked at
    validation, where the package's entity files are loaded. Some libxml2 builds
    (e.g. on Windows) still fail here, so in exactly that case the references are
    replaced, for this structural parse only, by placeholders of the same length
    (line and column numbers stay exact)."""
    tree, errors = _strict_parse(data)
    if tree is not None or isinstance(errors, str):
        return tree, errors
    # errors that only say an external entity set could not be read, or an entity it would have
    # declared is unknown: harmless when the DOCTYPE imports declarations (see below)
    external_only = re.compile(r"Entity '[^']+' not defined|PEReference: %[^;]+; not found|"
                               r"failed to load external entity|Entity '[^']+' failed to parse")
    if not all(external_only.search((e.message or "").strip()) for e in errors):
        return None, errors
    enc = sniff(data).encoding or "utf-8"
    try:
        text = data.decode(enc)
    except (UnicodeError, LookupError):
        return None, errors
    if not _may_declare_externally(text):
        return None, errors                  # a standalone document really is not well-formed
    body = _without_pe_refs(text)
    body = _ENTREF.sub(lambda m: m.group(0) if m.group(1) in _PREDEF else f"[{m.group(1)}]", body)
    body = re.sub(r"""^(\ufeff?<\?xml[^>]*?)\s+encoding\s*=\s*["'][^"']*["']""", r"\1", body, count=1).lstrip("\ufeff")
    tree2, errors2 = _strict_parse(body.encode("utf-8"))
    return (tree2, []) if tree2 is not None else (None, errors)


def explain_match(root: etree._Element, packages: list[InstalledPackage]) -> list[dict]:
    """Why each installed doc type does or does not match this document (for `asthra why`)."""
    info = root.getroottree().docinfo
    loc = root.get(f"{{{XSI}}}noNamespaceSchemaLocation") or root.get(f"{{{XSI}}}schemaLocation")
    out = []
    for pkg in packages:
        for dt in pkg.manifest.doc_types:
            disc_ok = None
            if dt.discriminator:
                try:
                    disc_ok = bool(root.getroottree().xpath(dt.discriminator))
                except etree.XPathError:
                    disc_ok = False
            out.append({"package": pkg.manifest.key, "enabled": pkg.enabled, "doc_type": dt.id,
                        "root_expected": dt.match.local_name, "root_ok": root_name_fits(dt, root),
                        "declares": _declared(dt, loc, info.public_id, info.system_url,
                                              pkg.manifest.standard, pkg.manifest.issue) is not None,
                        "pattern": dt.match.schema_location_pattern or dt.match.public_id_pattern or dt.match.system_id_pattern,
                        "discriminator": dt.discriminator, "discriminator_ok": disc_ok})
    return out
