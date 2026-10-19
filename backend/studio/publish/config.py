"""Publication identity: DMC / CMM numbering and company data, set once per project."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass
class PublicationConfig:
    # S1000D data module code
    model_ident_code: str = "TECHPUB"
    system_diff_code: str = "A"
    system_code: str = "32"
    sub_system_code: str = "4"
    sub_sub_system_code: str = "2"
    assy_code: str = "11"
    disassy_code: str = "00"
    disassy_code_variant: str = "A"
    item_location_code: str = "C"
    language: str = "en"
    country: str = "US"
    issue_number: str = "001"
    in_work: str = "01"
    security_classification: str = "01"
    enterprise_code: str = ""
    enterprise_name: str = ""
    applicability: str = ""
    # the BREX the data modules name (brexDmRef): the S1000D default, or the project's own BREX
    brex_dmc: str = "S1000D-G-04-10-0301-00A-022A-D"
    # ATA iSpec 2200 CMM
    ata_chapter: str = "32"
    ata_section: str = "42"
    ata_subject: str = "11"
    cmm_doc_number: str = ""
    figure_number: str = "1"
    issue_date: str = field(default_factory=lambda: date.today().isoformat())

    def sns(self) -> dict:
        return {"systemCode": self.system_code, "subSystemCode": self.sub_system_code,
                "subSubSystemCode": self.sub_sub_system_code, "assyCode": self.assy_code}

    def dmc(self, info_code: str, variant: str = "A") -> dict:
        return {"modelIdentCode": self.model_ident_code, "systemDiffCode": self.system_diff_code, **self.sns(),
                "disassyCode": self.disassy_code, "disassyCodeVariant": self.disassy_code_variant,
                "infoCode": info_code, "infoCodeVariant": variant, "itemLocationCode": self.item_location_code}

    def brex_dmcode(self) -> dict:
        """brex_dmc ("MIC-SDC-SYS-SUBSUBSUB-ASSY-DISVAR-INFOVAR-LOC", with or without "DMC-") -> dmCode attributes."""
        import re
        m = re.fullmatch(r"(?:DMC-)?([A-Z0-9]{2,14})-([A-Z0-9]{1,4})-([A-Z0-9]{2,3})-([A-Z0-9])([A-Z0-9])-([A-Z0-9]{2,4})-"
                         r"([A-Z0-9]{2})([A-Z0-9]{1,3})-([A-Z0-9]{3})([A-Z0-9])-([A-Z])", (self.brex_dmc or "").strip().upper())
        if not m:
            raise ValueError(f"brex_dmc '{self.brex_dmc}' is not a data module code like S1000D-G-04-10-0301-00A-022A-D")
        keys = ["modelIdentCode", "systemDiffCode", "systemCode", "subSystemCode", "subSubSystemCode", "assyCode",
                "disassyCode", "disassyCodeVariant", "infoCode", "infoCodeVariant", "itemLocationCode"]
        return dict(zip(keys, m.groups()))

    def dmc_string(self, info_code: str, variant: str = "A") -> str:
        d = self.dmc(info_code, variant)
        return (f"DMC-{d['modelIdentCode']}-{d['systemDiffCode']}-{d['systemCode']}-{d['subSystemCode']}"
                f"{d['subSubSystemCode']}-{d['assyCode']}-{d['disassyCode']}{d['disassyCodeVariant']}-"
                f"{d['infoCode']}{d['infoCodeVariant']}-{d['itemLocationCode']}")

    def file_name(self, info_code: str) -> str:
        return (f"{self.dmc_string(info_code)}_{self.issue_number}-{self.in_work}_"
                f"{self.language.upper()}-{self.country.upper()}.XML")

    @classmethod
    def from_dict(cls, d: dict | None) -> "PublicationConfig":
        d = d or {}
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__ and v not in (None, "")}
        return cls(**known)

    def as_dict(self) -> dict:
        return asdict(self)
