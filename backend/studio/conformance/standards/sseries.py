"""S-Series adapters. S1000D, S2000M and S3000L are peers: each has its own
registry entries, identification, validation and (from M5) importer/exporter."""
from __future__ import annotations

from .base import Cap, CapabilityStatus, StandardAdapter, _caps


class S1000DAdapter(StandardAdapter):
    family = "S1000D"
    title = "S1000D — Technical publications"
    workspace = "technical-publishing"
    capabilities = {**_caps(), Cap.VALIDATE_RULES: CapabilityStatus("planned", 3, "BREX module"),
                    Cap.AUTHOR: CapabilityStatus("planned", 2)}

    def format_identity(self, f: dict[str, str]) -> str:
        order = ["modelIdentCode", "systemDiffCode", "systemCode", "subSystemCode",
                 "subSubSystemCode", "assyCode", "disassyCode", "disassyCodeVariant",
                 "infoCode", "infoCodeVariant", "itemLocationCode"]
        if all(k in f for k in order):
            dmc = "DMC-{}-{}-{}-{}{}-{}-{}{}-{}{}-{}".format(*(f[k] for k in order))
            # Never fill in values the source does not contain.
            if "issueNumber" in f:
                dmc += f" issue {f['issueNumber']}-{f['inWork'] if 'inWork' in f else '??'}"
            return dmc
        return super().format_identity(f)


class S2000MAdapter(StandardAdapter):
    family = "S2000M"
    title = "S2000M — Material management / provisioning"
    workspace = "material-management"
    capabilities = {**_caps(), Cap.VALIDATE_RULES: CapabilityStatus("planned", 3, "project rule packages")}


class S3000LAdapter(StandardAdapter):
    family = "S3000L"
    title = "S3000L — Logistics support analysis"
    workspace = "logistics-support-analysis"
    capabilities = {**_caps(), Cap.VALIDATE_RULES: CapabilityStatus("planned", 3, "project rule packages")}


class OEMAdapter(StandardAdapter):
    family = "OEM"
    title = "Proprietary OEM schema"
    workspace = "technical-publishing"
    capabilities = {**_caps(), Cap.EXTRACT_CANONICAL: CapabilityStatus(
        "planned", 5, "requires a project-defined mapping; never assumed")}
