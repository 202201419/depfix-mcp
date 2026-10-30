"""The core: every stage as a plain function.

The CLI (and the MCP server in drop 4) only call these.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import approval as approvals
from .apply import ApplyRefused, apply_on_branch, branch_name, preflight
from .fixer import ProposeResult, propose_template, render_name
from .models import Approval, Finding, Proposal, Rule, Status, Verification
from .rules import RuleSet, load_rules
from .scanner import collect_aliases, parse_source, scan_repo
from .store import Store
from .verify import VerifyConfig, check_diff_scope, verify_proposal


class DepfixError(Exception):
    pass


def _finding(store: Store, fid: str) -> Finding:
    f = store.get_finding(fid)
    if f is None:
        raise DepfixError(f"no finding '{fid}'; run `depfix scan` first")
    return f


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _require_file_unchanged(repo: Path, f: Finding, p: Proposal) -> None:
    """A diff is only valid for the exact file content it was made from."""
    path = repo / f.file
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != p.file_sha256:
        raise DepfixError(f"{f.file} changed since the fix was verified; scan and propose again")


# ---- stages 1-4 --------------------------------------------------------------------------------


def scan(
    repo: Path, libraries: set[str] | None = None, rules: RuleSet | None = None
) -> tuple[list[Finding], list[str]]:
    rules = rules or load_rules()
    result = scan_repo(repo, rules, libraries)
    with Store(repo) as store:
        store.upsert_scan(result.findings, libraries)
        findings = [f for f in store.list_findings() if not libraries or f.library in libraries]
    return findings, result.errors


def propose(repo: Path, fid: str, rules: RuleSet | None = None) -> ProposeResult:
    rules = rules or load_rules()
    with Store(repo) as store:
        f = _finding(store, fid)
        if f.status in (Status.SKIPPED, Status.APPROVED, Status.APPLIED):
            raise DepfixError(f"finding is {f.status.value}; it cannot be proposed again")
        res = propose_template(repo, f, rules.by_id[f.rule_id])
        if res.outcome == "proposed" and res.proposal is not None:
            store.save_proposal(res.proposal)
            if f.status != Status.PROPOSED:
                store.set_status(fid, Status.PROPOSED, note="")
        elif res.outcome == "not_applicable" and f.status == Status.FOUND:
            store.set_status(fid, Status.SKIPPED, note=res.reason)
        else:
            store.set_note(fid, res.reason)
        return res


def verify(repo: Path, fid: str, cfg: VerifyConfig, rules: RuleSet | None = None) -> Verification:
    """`cfg` is required on purpose: the caller must choose WHERE the project's code runs."""
    rules = rules or load_rules()
    with Store(repo) as store:
        f = _finding(store, fid)
        proposal = store.get_proposal(fid)
        if f.status != Status.PROPOSED or proposal is None:
            raise DepfixError(f"finding is {f.status.value}; propose a fix first")
        v = verify_proposal(repo, f, proposal, rules, cfg)
        store.save_verification(v)
        store.set_status(fid, Status.VERIFIED if v.passed else Status.FAILED)
        return v


# ---- stage 5: human approval, then apply -------------------------------------------------------


@dataclass
class ApprovalRequest:
    """Everything a person needs to see before saying yes."""

    finding: Finding
    proposal: Proposal
    verification: Verification
    rule: Rule


def approve(
    repo: Path,
    fid: str,
    confirm: Callable[[ApprovalRequest], bool],
    rules: RuleSet | None = None,
) -> Approval | None:
    """Record a human's yes. `confirm` is the human: it receives the evidence and answers.

    There is deliberately no way to approve without calling `confirm`. The CLI's `confirm` reads
    the answer from an interactive terminal; the MCP server (drop 4) will not expose this function.
    """
    rules = rules or load_rules()
    with Store(repo) as store:
        f = _finding(store, fid)
        if f.status != Status.VERIFIED:
            raise DepfixError(f"finding is {f.status.value}; only a verified fix can be approved")
        p, v = store.get_proposal(fid), store.get_verification(fid)
        if p is None or v is None or not v.passed:
            raise DepfixError("there is no passing verification report for this finding")
        digest = _sha(p.diff)
        if not (digest == p.diff_sha256 == v.diff_sha256):
            raise DepfixError("the diff no longer matches what was verified")
        _require_file_unchanged(repo, f, p)
        if not confirm(ApprovalRequest(f, p, v, rules.by_id[f.rule_id])):
            return None
        a = approvals.issue(fid, digest)
        store.save_approval(a)
        store.set_status(fid, Status.APPROVED)
        return a


@dataclass
class ApplyOutcome:
    applied: bool
    branch: str
    start_branch: str
    commit: str = ""
    steps: list[str] = field(default_factory=list)


def _commit_message(f: Finding, p: Proposal, v: Verification, r: Rule) -> str:
    checks = ", ".join(f"{c.name}:{c.status}" for c in v.checks)
    return (
        f"depfix: {r.summary} ({r.id})\n\n"
        f"Finding: {f.id}\nFile: {f.file}:{f.start_line}\nSource: {p.reference_url}\n"
        f"Verification: passed, confidence {v.confidence} ({checks})\n"
        "Approved by a person via `depfix approve`."
    )


def apply_fix(
    repo: Path, fid: str, execute: bool = False, rules: RuleSet | None = None
) -> ApplyOutcome:
    """Apply an APPROVED fix on a new git branch. Dry run unless execute=True."""
    rules = rules or load_rules()
    with Store(repo) as store:
        f = _finding(store, fid)
        if f.status != Status.APPROVED:
            raise DepfixError(
                f"finding is {f.status.value}; it must be approved first (run `depfix approve`)"
            )
        p, v, a = store.get_proposal(fid), store.get_verification(fid), store.get_approval(fid)
        if p is None or v is None:
            raise DepfixError("the stored proposal or verification report is missing")
        digest = _sha(p.diff)
        reason = approvals.check(a, fid, digest)
        if reason is None and v.diff_sha256 != digest:
            reason = "the diff differs from the one that was verified"
        if reason is not None:
            # A bad or expired approval is revoked: the person must look at the diff again.
            store.delete_approval(fid)
            store.set_status(fid, Status.VERIFIED)
            raise DepfixError(f"{reason}; approve again")
        scope = check_diff_scope(f, p)
        if scope.status == "failed":
            raise DepfixError(f"refusing to apply: {scope.detail}")
        _require_file_unchanged(repo, f, p)
        try:
            start = preflight(repo, f)
        except ApplyRefused as exc:
            raise DepfixError(f"cannot apply: {exc}") from exc
        branch = branch_name(f)
        steps = [
            f"create branch {branch} from {start}",
            f"apply the approved diff to {f.file} (checked first with git apply --check)",
            "commit it with the rule, source and verification summary in the message",
            f"your current branch ({start}) is not touched",
        ]
        if not execute:
            return ApplyOutcome(False, branch, start, steps=steps)
        try:
            commit = apply_on_branch(
                repo, f, p, _commit_message(f, p, v, rules.by_id[f.rule_id]), start
            )
        except ApplyRefused as exc:
            raise DepfixError(str(exc)) from exc
        store.mark_approval_used(fid)
        store.set_status(fid, Status.APPLIED)
        return ApplyOutcome(True, branch, start, commit, steps)


# ---- reporting ---------------------------------------------------------------------------------


def report(repo: Path) -> dict[str, object]:
    with Store(repo) as store:
        findings = store.list_findings()
    counts = Counter(f.status.value for f in findings)
    return {
        "total": len(findings),
        "by_status": dict(sorted(counts.items())),
        "findings": [f.model_dump(mode="json") for f in findings],
    }


def trace(repo: Path, fid: str, rules: RuleSet | None = None) -> str:
    """Print what every stage saw and produced for ONE finding (the fastest way to learn)."""
    rules = rules or load_rules()
    out: list[str] = []
    with Store(repo) as store:
        f = _finding(store, fid)
        rule = rules.by_id[f.rule_id]
        out.append("== 1. SCAN: what was found ==")
        out.append(json.dumps(f.model_dump(mode="json"), indent=2))
        out.append("\n== 2. RULE: why it matched ==")
        out.append(f"{rule.id} [{rule.match}] {rule.qualified_name}")
        out.append(f"{rule.summary}\nsource: {rule.reference.url}")
        out.append(f"fix kind: {rule.fix.kind}  template: {rule.fix.template}")
        try:
            _, tree = parse_source((repo / f.file).read_bytes())
            al = collect_aliases(tree)
            out.append("\n== 3. NAME RESOLUTION: how this file spells things ==")
            out.append(f"imports seen: {al.map}   ambiguous: {sorted(al.ambiguous)}")
            if rule.fix.template:
                for token in re.findall(r"\{([A-Za-z_][\w.]*)\}", rule.fix.template):
                    if token != "args":
                        out.append(f"  {{{token}}} -> {render_name(token, al)}")
        except Exception as exc:  # trace must never crash while teaching
            out.append(f"(could not re-parse file: {exc})")
        p = store.get_proposal(fid)
        out.append("\n== 4. PROPOSE: the change ==")
        out.append(p.diff if p else f"(no proposal yet; status={f.status.value}; note={f.note})")
        v = store.get_verification(fid)
        out.append("== 5. VERIFY: the checks ==")
        if v:
            for c in v.checks:
                first = c.detail.splitlines()[0] if c.detail else ""
                out.append(f"[{c.status.upper():7}] {c.name}: {first}")
            out.append(f"overall: {'PASSED' if v.passed else 'FAILED'}  confidence: {v.confidence}")
        else:
            out.append("(not verified yet)")
        a = store.get_approval(fid)
        out.append("\n== 6. APPROVAL ==")
        out.append(
            f"approved until {a.expires_at} (used={a.used})"
            if a
            else f"(none; status={f.status.value})"
        )
    return "\n".join(out)


def explain_text(repo: Path, fid: str, rules: RuleSet | None = None) -> str:
    """Offline explanation from the rule itself (retrieved docs with citations: drop 3)."""
    rules = rules or load_rules()
    with Store(repo) as store:
        f = _finding(store, fid)
    r = rules.by_id[f.rule_id]
    lines = [f"{f.file}:{f.start_line}  {f.snippet}", "", r.summary, ""]
    lines.append(f"source: {r.reference.url}  ({r.reference.section})")
    if r.deprecated_in:
        lines.append(f"deprecated in: {r.deprecated_in}")
    if r.removed_in:
        lines.append(f"removed in: {r.removed_in}")
    if r.needs_review:
        lines.append("note: the right fix depends on context; a person must review it.")
    lines.append("(documentation retrieval with citations arrives in drop 3)")
    return "\n".join(lines)
