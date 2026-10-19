from __future__ import annotations

from enum import Enum, IntEnum

from pydantic import BaseModel, Field


class Stage(IntEnum):
    INTEGRITY = 1
    PARSE = 2
    SCHEMA = 3
    BUSINESS_RULES = 4
    REFERENCES = 5          # reserved: cross-document references


class Severity(str, Enum):
    FATAL = "fatal"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Status(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not-run"               # stage exists but was not executed
    NOT_IMPLEMENTED = "not-implemented"
    UNSUPPORTED = "unsupported"       # no installed package covers this document


class Fix(BaseModel):
    """A correction that is certain to satisfy the failed constraint. Applied only by the user."""
    label: str
    kind: str                      # "attr" (set attribute value) | "text" (set element text) | "insert" (add an element)
    attribute: str | None = None
    value: str = ""
    element: str | None = None     # insert: the element to add (with its required content)
    position: str | None = None    # insert: "before" the reported element, or at the "end" of it


class Diagnostic(BaseModel):
    stage: Stage
    severity: Severity
    rule_id: str
    message: str
    source_file: str | None = None
    element_path: str | None = None
    line: int | None = None
    column: int | None = None
    suggestion: str | None = None
    reference: str | None = None
    attribute: str | None = None       # attribute the problem is about, for precise underlining
    value: str | None = None           # offending value, if any
    raw_message: str | None = None     # validator's original text
    fix: Fix | None = None
    category: str | None = None         # stable kind of problem (see validation/diagnostics.py)
    consequence_of: str | None = None   # set when this error follows from another one


class StageResult(BaseModel):
    stage: Stage
    status: Status
    note: str = ""


class ValidationReport(BaseModel):
    document_id: str | None = None
    package_id: str | None = None
    package_checksum: str | None = None
    stages: list[StageResult] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    entities: dict[str, str] = Field(default_factory=dict)     # DTD entity texts, for display

    # Four statuses are deliberately separate (spec §11): an XSD pass is not approval.
    @property
    def structural_status(self) -> Status:
        relevant = [s for s in self.stages if s.stage in (Stage.INTEGRITY, Stage.PARSE, Stage.SCHEMA)]
        if any(s.status == Status.FAILED for s in relevant):
            return Status.FAILED
        schema = next((s for s in relevant if s.stage == Stage.SCHEMA), None)
        return schema.status if schema else Status.NOT_RUN

    def _stage_status(self, st: Stage) -> Status:
        s = next((x for x in self.stages if x.stage == st), None)
        return s.status if s else Status.NOT_RUN

    @property
    def business_rule_status(self) -> Status:
        return self._stage_status(Stage.BUSINESS_RULES)

    def statuses(self) -> dict[str, str]:
        return {
            "structural": self.structural_status.value,
            "business_rules": self.business_rule_status.value,
        }

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in Severity}
        for d in self.diagnostics:
            out[d.severity.value] += 1
        return out
