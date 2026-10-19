"""Standard adapter interface (see docs/03-SCHEMA-ADAPTER-INTERFACE.md).

Every standard — S1000D, S2000M, S3000L today; S4000P, S5000F, S6000T, ATA iSpec 2200
and OEM schemas later — plugs in through this one interface. The core never
branches on standard names; it asks the adapter.

Capabilities are declared per adapter so the UI and API never imply support that
does not exist. A capability is "implemented" only when its acceptance tests pass.
"""
from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from lxml import etree

from ..registry.package import DocType, Manifest


class Cap(str, Enum):
    IDENTIFY = "identify"
    VALIDATE_STRUCTURE = "validate-structure"      # XSD/DTD, stage 3
    VALIDATE_RULES = "validate-business-rules"     # BREX / Schematron, stage 4
    VALIDATE_REFERENCES = "validate-references"    # stage 5
    EXTRACT_CANONICAL = "extract-canonical"        # source -> knowledge core
    EXPORT = "export"                              # knowledge core -> target
    AUTHOR = "visual-authoring"


@dataclass(frozen=True)
class CapabilityStatus:
    status: str          # implemented | planned
    milestone: int
    note: str = ""


class NotYetImplemented(Exception):
    def __init__(self, adapter: str, cap: Cap, milestone: int):
        super().__init__(f"{adapter}: '{cap.value}' is planned for Milestone {milestone} and is not implemented")
        self.adapter, self.cap, self.milestone = adapter, cap, milestone


@dataclass
class DocumentIdentity:
    """Standard-specific identity (e.g. S1000D data module code) as extracted."""
    fields: dict[str, str] = field(default_factory=dict)
    display: str = ""
    warnings: list[str] = field(default_factory=list)


class StandardAdapter(ABC):
    family: str = ""                 # matches Manifest.standard
    title: str = ""
    workspace: str = ""              # departmental workspace this standard primarily serves
    capabilities: dict[Cap, CapabilityStatus] = {}

    # ---- identity ---------------------------------------------------------
    def extract_identity(self, root: etree._Element, manifest: Manifest, dt: DocType) -> DocumentIdentity:
        """Evaluate identity XPaths declared *by the schema package*. Subclasses
        add a display format; they never hardcode element names for a real issue."""
        ident = DocumentIdentity()
        ns = {k: v for k, v in manifest.namespaces.items() if k}
        for name, xp in dt.identity_xpaths.items():
            try:
                val = root.xpath(f"string({xp})", namespaces=ns)
            except etree.XPathError as e:
                ident.warnings.append(f"identity xpath {name!r} failed: {e}")
                continue
            if val:
                ident.fields[name] = str(val).strip()
            else:
                ident.warnings.append(f"identity field {name!r} not present")
        ident.display = self.format_identity(ident.fields) if ident.fields else ""
        return ident

    def format_identity(self, fields: dict[str, str]) -> str:
        return " ".join(f"{k}={v}" for k, v in fields.items())

    # ---- later milestones (explicitly unimplemented) ------------------------
    def business_rule_packages(self, manifest: Manifest) -> list[Any]:
        raise NotYetImplemented(self.family, Cap.VALIDATE_RULES, self.capabilities[Cap.VALIDATE_RULES].milestone)

    def extract_canonical(self, root: etree._Element, manifest: Manifest, dt: DocType) -> Any:
        raise NotYetImplemented(self.family, Cap.EXTRACT_CANONICAL, self.capabilities[Cap.EXTRACT_CANONICAL].milestone)

    def export(self, canonical: Any, manifest: Manifest, dt: DocType) -> Any:
        raise NotYetImplemented(self.family, Cap.EXPORT, self.capabilities[Cap.EXPORT].milestone)

    def describe(self) -> dict:
        return {
            "family": self.family, "title": self.title, "workspace": self.workspace,
            "capabilities": {c.value: {"status": s.status, "milestone": s.milestone, "note": s.note}
                             for c, s in self.capabilities.items()},
        }


def _caps(**overrides: CapabilityStatus) -> dict[Cap, CapabilityStatus]:
    base = {
        Cap.IDENTIFY: CapabilityStatus("implemented", 1),
        Cap.VALIDATE_STRUCTURE: CapabilityStatus("implemented", 1, "XSD; DTD in M4"),
        Cap.VALIDATE_RULES: CapabilityStatus("planned", 3),
        Cap.VALIDATE_REFERENCES: CapabilityStatus("planned", 5),
        Cap.EXTRACT_CANONICAL: CapabilityStatus("planned", 5),
        Cap.EXPORT: CapabilityStatus("planned", 6),
    }
    base.update({Cap(k.replace("_", "-")): v for k, v in overrides.items()})
    return base
