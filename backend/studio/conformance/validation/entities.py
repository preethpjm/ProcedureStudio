"""Entities for XSD-validated documents (e.g. S1000D files that pull in the ISO entity
sets through a parameter entity in their DOCTYPE, then use &deg; &mdash; ...).

The document is re-parsed with entity expansion ON but loading OFF-network and confined
to the schema package: external entity files resolve only through the package catalog
(by public id, URL, or bare file name). Anything else is refused (XXE), and libxml2's
amplification limit stops entity bombs. The source text is not changed, so line numbers
stay exact."""
from __future__ import annotations

from pathlib import Path

from lxml import etree

from ..security.xml_safe import BlockedResolution, ConfinedResolver


def parse_with_entities(data: bytes, roots: list[Path], catalog: dict[str, str]):
    """-> (tree or None, [error log entries], blocked message or None)"""
    parser = etree.XMLParser(load_dtd=True, dtd_validation=False, resolve_entities=True, no_network=True,
                             huge_tree=False, recover=False)
    parser.resolvers.add(ConfinedResolver(roots, catalog))
    try:
        from ..identify.service import fix_declared_encoding
        tree = etree.fromstring(fix_declared_encoding(data), parser).getroottree()
    except BlockedResolution as e:
        return None, [], str(e)
    except etree.XMLSyntaxError:
        return None, [e for e in parser.error_log if e.level_name in ("ERROR", "FATAL")], None
    return tree, [], None
