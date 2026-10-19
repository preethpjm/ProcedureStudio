"""Hardened XML parsing (spec §10, §19).

* No network access: lxml `no_network=True`, plus a resolver that refuses any
  URL that is not a file inside an explicitly approved directory.
* No entity expansion when parsing instance documents (XXE / billion-laughs).
* DTDs are not loaded when parsing instance documents.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from lxml import etree


class BlockedResolution(Exception):
    pass


_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def to_local_path(url: str) -> Path | None:
    """Turn a system id into a local path, or None if it is not local.

    Handles Windows drive paths (C:\\x — which urlparse misreads as scheme 'c'),
    UNC paths, file:// URLs (incl. file:///C:/x) and relative/POSIX paths.
    Anything with a real scheme (http, https, ftp, urn, ...) returns None."""
    if not url:
        return None
    if _WIN_DRIVE.match(url) or url.startswith("\\\\"):
        return Path(url)
    parsed = urlparse(url)
    if parsed.scheme == "file":
        path = url2pathname(parsed.path)
        if parsed.netloc and parsed.netloc != "localhost":
            path = "\\\\" + parsed.netloc + path   # UNC share
        return Path(path)
    if parsed.scheme == "":
        return Path(unquote(url))
    return None


class ConfinedResolver(etree.Resolver):
    """Resolve schema includes/imports only from approved local roots."""

    def __init__(self, roots: list[Path], catalog: dict[str, str] | None = None):
        super().__init__()
        self.roots = [r.resolve() for r in roots]
        self.catalog = catalog or {}   # URL or public id -> path relative to a root
        self.log: list[str] = []

    def _allowed(self, p: Path) -> bool:
        p = p.resolve()
        return any(p == r or r in p.parents for r in self.roots)

    def resolve(self, system_url, public_id, context):
        mapped = self.catalog.get(system_url) or (self.catalog.get(public_id) if public_id else None)
        if not mapped and system_url and re.match(r"^https?://", system_url):
            # a remote URL is never fetched; if the package ships a file of the same name
            # (registered in its catalog by bare name), that local copy is used instead
            mapped = self.catalog.get(system_url.rstrip("/").rsplit("/", 1)[-1])
        if not mapped and system_url:
            # a relative or file reference that is not an approved local file (e.g. "ent/ISOEntities"
            # next to the document): the package's file of the same name, never anything else
            path = to_local_path(system_url)
            if path is None or not (self._allowed(path) and path.is_file()):
                mapped = self.catalog.get(re.split(r"[\\/]", system_url.rstrip("/\\"))[-1])
        if mapped:
            for r in self.roots:
                cand = (r / mapped).resolve()
                if self._allowed(cand) and cand.is_file():
                    self.log.append(f"catalog: {system_url} -> {cand.name}")
                    return self.resolve_filename(str(cand), context)
        path = to_local_path(system_url or "")
        if path is not None and self._allowed(path) and path.is_file():
            return self.resolve_filename(str(path), context)
        self.log.append(f"BLOCKED: {system_url}")
        raise BlockedResolution(f"resolution refused (not in approved local catalog): {system_url}")


class NoExternalResolver(etree.Resolver):
    """For structural parses: nothing external is ever read. Some libxml2 builds (seen on
    Windows) still try to load an external parameter entity such as %ISOEntities; ->
    "ent/ISOEntities" even when DTD loading is off; they get empty content, never a file."""

    def resolve(self, url, public_id, context):
        return self.resolve_string("", context)


def document_parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False,
        huge_tree=False, remove_blank_text=False, remove_comments=False,
        remove_pis=False, strip_cdata=False, recover=False,
    )


def structural_parser() -> etree.XMLParser:
    """document_parser for parsing documents from memory (never by file name: lxml would ask the
    resolver for the file itself). Nothing external is read, on any libxml2 build."""
    p = document_parser()
    p.resolvers.add(NoExternalResolver())
    return p


def schema_parser(resolver: ConfinedResolver) -> etree.XMLParser:
    p = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    p.resolvers.add(resolver)
    return p
