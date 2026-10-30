"""The core: every stage as a plain function.

The CLI (and the MCP server in drop 4) only call these."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from .fixer import ProposeResult, propose_template, render_name
from .models import Finding, Status, Verification
from .rules import RuleSet, load_rules
from .scanner import collect_aliases, parse_source, scan_repo
from .store import Store
from .verify import VerifyConfig, verify_proposal


class DepfixError(Exception):
    pass


def _finding(store: Store, fid: str) -> Finding:
    f = store.get_finding(fid)
    if f is None:
        raise DepfixError(f"no finding '{fid}'; run `depfix scan` first")
    return f


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


def verify(
    repo: Path, fid: str, cfg: VerifyConfig | None = None, rules: RuleSet | None = None
) -> Verification:
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
