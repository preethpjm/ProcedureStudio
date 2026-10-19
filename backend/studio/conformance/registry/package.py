"""Schema package manifest (spec §4).

A package is a directory containing `asthra-package.json` and the schema files it
lists. Identification rules are *declared by the package*, never hardcoded, so the
application only claims support for what an installed package actually defines.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

KNOWN_STANDARDS = {"S1000D", "S2000M", "S3000L", "S4000P", "S5000F", "S6000T", "ATA2200", "ATA2300", "OEM"}


class RootMatch(BaseModel):
    """How to recognize a document of this type."""
    local_name: str
    namespace: str | None = None                 # None = no namespace
    schema_location_pattern: str | None = None   # regex against xsi:(no)SchemaLocation
    public_id_pattern: str | None = None         # regex against the DOCTYPE public identifier
    system_id_pattern: str | None = None         # regex against the DOCTYPE system identifier


class DocType(BaseModel):
    id: str                                      # e.g. "descript", "proced", "ipd", "provisioning"
    label: str
    content_family: Literal["ipd", "description", "procedure", "provisioning",
                            "product-breakdown", "maintenance-task", "other"]
    schema_file: str                             # entry XSD/DTD, relative to package root
    schema_kind: Literal["xsd", "dtd", "sgml"] = "xsd"
    match: RootMatch
    identity_xpaths: dict[str, str] = Field(default_factory=dict)  # name -> XPath (string result)
    discriminator: str | None = None             # XPath that must match for a root-only (compatible) match


class ValidationRuleRef(BaseModel):
    id: str
    kind: Literal["schematron", "xpath", "brex", "python"]
    path: str
    applies_to: list[str] = Field(default_factory=list)


class Manifest(BaseModel):
    format_version: Literal[1] = 1
    package_id: str
    name: str
    standard: str
    issue: str
    provenance: Literal["official", "oem", "synthetic"]
    licence_note: str = ""
    doc_types: list[DocType]
    files: list[str]                             # every file shipped, relative paths
    dependencies: list[str] = Field(default_factory=list)   # other package ids
    namespaces: dict[str, str] = Field(default_factory=dict)
    catalog: dict[str, str] = Field(default_factory=dict)   # URL/public id -> local file
    validation_rules: list[ValidationRuleRef] = Field(default_factory=list)
    transformations: list[str] = Field(default_factory=list)
    checksum: str | None = None                  # optional: declared by publisher
    render_roles: dict[str, str] = Field(default_factory=dict)   # element name -> display role override
    sgml: dict = Field(default_factory=dict)     # {"catalogs": [...], "declaration": file or None} for SGML packages

    @field_validator("standard")
    @classmethod
    def _std(cls, v: str) -> str:
        v = v.upper()
        if v not in KNOWN_STANDARDS:
            raise ValueError(f"unknown standard family {v!r}; use OEM for proprietary schemas")
        return v

    @property
    def key(self) -> str:
        return f"{self.standard.lower()}/{self.issue}/{self.package_id}"
