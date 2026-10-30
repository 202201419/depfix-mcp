"""Data shapes and the finding state machine.

Every stage passes these objects around, so the other modules only have to agree on
shapes here, never on each other's internals.
"""

from __future__ import annotations

import enum
import re
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator


class Status(enum.StrEnum):
    FOUND = "found"
    PROPOSED = "proposed"
    VERIFIED = "verified"
    FAILED = "failed"  # a verification check failed
    SKIPPED = "skipped"  # a fix cannot be made safely by this tool
    APPROVED = "approved"  # drop 2
    APPLIED = "applied"  # drop 2


# Why a table: every legal move is listed in one place, so "can a finding skip
# verification?" is answered by reading six lines instead of tracing the whole program.
ALLOWED: dict[Status, frozenset[Status]] = {
    Status.FOUND: frozenset({Status.PROPOSED, Status.SKIPPED}),
    Status.PROPOSED: frozenset({Status.VERIFIED, Status.FAILED}),
    Status.FAILED: frozenset({Status.PROPOSED}),
    Status.VERIFIED: frozenset({Status.APPROVED, Status.PROPOSED}),
    Status.APPROVED: frozenset({Status.APPLIED}),
    Status.SKIPPED: frozenset(),
    Status.APPLIED: frozenset(),
}


class InvalidTransition(Exception):
    pass


def check_transition(old: Status, new: Status) -> None:
    if new not in ALLOWED[old]:
        raise InvalidTransition(f"cannot move a finding from {old.value} to {new.value}")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    section: str


class Arity(BaseModel):
    """How many arguments a call may have for a template to be safe."""

    model_config = ConfigDict(extra="forbid")
    positional: int = 0
    keywords: int = 0


class Fix(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["template", "llm"]
    template: str | None = None
    arity: Arity | None = None


class Example(BaseModel):
    """A rule's self-test. Rules are data, so this is what proves a rule is true."""

    model_config = ConfigDict(extra="forbid")
    setup: str = ""
    before: str | None = None
    before_effect: Literal["removed", "warns"] | None = None
    after: str
    expect: str  # repr() of the value of `after`
    equivalent: bool = False  # if true, repr(before) must equal repr(after)


_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    library: str
    match: Literal["attribute", "call"]
    qualified_name: str
    deprecated_in: str | None = None
    removed_in: str | None = None
    summary: str
    fix: Fix
    reference: Reference
    example: Example | None = None
    needs_review: bool = False

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.fix.kind == "template":
            if not self.fix.template:
                raise ValueError(f"{self.id}: template rules need fix.template")
            if self.example is None:
                raise ValueError(f"{self.id}: template rules need an example (the self-test)")
            if self.match == "attribute" and "{args}" in self.fix.template:
                raise ValueError(f"{self.id}: {{args}} is only valid for call rules")
            if self.match == "attribute" and self.fix.arity is not None:
                raise ValueError(f"{self.id}: arity is only valid for call rules")
        return self


class Finding(BaseModel):
    id: str
    rule_id: str
    library: str
    file: str  # posix path relative to the repo root
    start_line: int  # 1-based
    start_col: int  # 0-based UTF-8 BYTE offset within the line (this is what ast reports)
    end_line: int
    end_col: int
    snippet: str
    file_sha256: str  # a fix is only valid for the exact file content that was scanned
    status: Status = Status.FOUND
    note: str = ""


class Proposal(BaseModel):
    finding_id: str
    file: str
    kind: Literal["template"] = "template"
    replacement: str
    diff: str
    diff_sha256: str
    file_sha256: str
    rationale: str
    reference_url: str
    created_at: str = ""


class CheckResult(BaseModel):
    name: str
    status: Literal["passed", "failed", "skipped"]
    detail: str = ""


class Verification(BaseModel):
    finding_id: str
    diff_sha256: str  # binds this report to one exact diff (drop 2 approval checks it)
    passed: bool
    confidence: Literal["high", "low"]
    checks: list[CheckResult]
    ran_at: str = ""
