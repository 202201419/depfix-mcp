"""Thin command-line wrapper over pipeline.py. No logic lives here."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .models import InvalidTransition
from .pipeline import (
    ApprovalRequest,
    DepfixError,
    apply_fix,
    approve,
    explain_text,
    propose,
    report,
    scan,
    trace,
    verify,
)
from .rules import load_rules
from .runner import (
    DEFAULT_IMAGE,
    LocalRunner,
    Runner,
    RunnerError,
    make_docker_runner,
    sandbox_build,
    sandbox_probe,
)
from .selftest import selftest_all
from .verify import VerifyConfig

NO_SANDBOX = (
    "The local runner executes your project's tests on THIS machine WITHOUT network or filesystem\n"
    "isolation (only a timeout, resource limits and a stripped environment). Prefer the default\n"
    "Docker runner. Use --runner local --allow-local-exec only for code you fully trust."
)


def _repo(s: str) -> Path:
    p = Path(s).resolve()
    if not p.is_dir():
        raise DepfixError(f"{s} is not a directory")
    return p


def _runner(a: argparse.Namespace) -> Runner:
    if a.runner == "local":
        if not getattr(a, "allow_local_exec", False):
            raise DepfixError(NO_SANDBOX)
        return LocalRunner()
    return make_docker_runner(a.image)


def cmd_scan(a: argparse.Namespace) -> int:
    findings, errors = scan(_repo(a.repo), set(a.library) if a.library else None)
    if a.json:
        print(json.dumps([f.model_dump(mode="json") for f in findings], indent=2))
    else:
        print(f"{'ID':<12}{'RULE':<10}{'STATUS':<10}{'LOCATION':<34}SNIPPET")
        for f in findings:
            loc = f"{f.file}:{f.start_line}"
            print(f"{f.id:<12}{f.rule_id:<10}{f.status.value:<10}{loc:<34}{f.snippet[:40]}")
        print(f"\n{len(findings)} finding(s). State saved in .depfix/state.db")
    for e in errors:
        print(f"skipped {e}", file=sys.stderr)
    return 0


def cmd_propose(a: argparse.Namespace) -> int:
    res = propose(_repo(a.repo), a.id)
    print(f"outcome: {res.outcome}" + (f" ({res.reason})" if res.reason else ""))
    if res.proposal:
        p = res.proposal
        print(f"\n{p.rationale}\nsource: {p.reference_url}\n\n{p.diff}")
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    runner = _runner(a)
    cfg = VerifyConfig(runner=runner, python=a.python, timeout_s=a.timeout)
    v = verify(_repo(a.repo), a.id, cfg)
    print(f"runner: {runner.name}")
    for c in v.checks:
        first = c.detail.splitlines()[0] if c.detail else ""
        print(f"[{c.status.upper():7}] {c.name:<13} {first}")
    print(f"\nresult: {'VERIFIED' if v.passed else 'FAILED'}  confidence: {v.confidence}")
    return 0 if v.passed else 1


def cmd_approve(a: argparse.Namespace) -> int:
    # An approval is a person's decision. Refuse when no person can be typing: piped input from a
    # script or an AI tool must not be able to say "y".
    if not sys.stdin.isatty():
        raise DepfixError("approval needs an interactive terminal: a person must read and answer")

    def confirm(req: ApprovalRequest) -> bool:
        f, p, v = req.finding, req.proposal, req.verification
        print(f"\nFinding {f.id}   {f.file}:{f.start_line}   rule {req.rule.id}")
        print(f"{p.rationale}\nsource: {p.reference_url}\n\n{p.diff}")
        for c in v.checks:
            print(
                f"[{c.status.upper():7}] {c.name}: {c.detail.splitlines()[0] if c.detail else ''}"
            )
        print(f"\nverification: PASSED, confidence {v.confidence}")
        if v.confidence == "low":
            print("WARNING: low confidence (no tests ran, or some tests already failed).")
            return input(f"Type the finding id ({f.id}) to approve: ").strip() == f.id
        return input("Approve this exact change? [y/N] ").strip().lower() == "y"

    approval = approve(_repo(a.repo), a.id, confirm)
    if approval is None:
        print("not approved; nothing was changed")
        return 1
    print(f"approved until {approval.expires_at}. Next: depfix apply {a.id} --execute")
    return 0


def cmd_apply(a: argparse.Namespace) -> int:
    out = apply_fix(_repo(a.repo), a.id, execute=a.execute)
    print("Plan:" if not out.applied else "Done:")
    for s in out.steps:
        print(f"  - {s}")
    if not out.applied:
        print("\nDRY RUN: nothing was changed. Re-run with --execute to apply.")
        return 0
    print(f"\nApplied on branch {out.branch} (commit {out.commit}).")
    print(f"To undo:  git switch {out.start_branch} && git branch -D {out.branch}")
    print(f"To keep:  review it, then merge {out.branch} the way you normally would.")
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    data = report(_repo(a.repo))
    if a.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"{data['total']} finding(s): {data['by_status']}")
    return 0


def cmd_explain(a: argparse.Namespace) -> int:
    print(explain_text(_repo(a.repo), a.id))
    return 0


def cmd_trace(a: argparse.Namespace) -> int:
    print(trace(_repo(a.repo), a.id))
    return 0


def cmd_rules(a: argparse.Namespace) -> int:
    rules = load_rules()
    if a.selftest:
        runner: Runner = make_docker_runner(a.image) if a.runner == "docker" else LocalRunner()
        bad = 0
        for rid, problems in selftest_all(rules, a.python, runner).items():
            print(f"{'FAIL' if problems else 'ok  '} {rid}")
            for p in problems:
                print(f"     {p}")
            bad += bool(problems)
        print(f"\n{len(rules.rules) - bad}/{len(rules.rules)} rules passed their self-test")
        return 1 if bad else 0
    for r in rules.rules:
        print(f"{r.id:<10}{r.library:<8}{r.fix.kind:<9}{r.qualified_name}")
    return 0


def cmd_sandbox(a: argparse.Namespace) -> int:
    if a.action == "build":
        print(f"Building {a.image or DEFAULT_IMAGE} (this one step uses the network)...")
        r = sandbox_build(a.image)
        print(r.output if r.returncode else "built.")
        return r.returncode
    rows = sandbox_probe(make_docker_runner(a.image))
    for name, ok, detail in rows:
        print(f"[{'ok' if ok else 'FAIL':<4}] {name} {detail}".rstrip())
    bad = sum(not ok for _, ok, _ in rows)
    print("\nsandbox isolation verified" if not bad else f"\n{bad} isolation check(s) FAILED")
    return 1 if bad else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="depfix", description="Verification-first deprecation fixer")
    p.add_argument("--version", action="version", version=f"depfix {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="find deprecated usages")
    s.add_argument("repo", nargs="?", default=".")
    s.add_argument("--library", action="append", help="limit to a library (repeatable)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_scan)

    for name, fn, helptext in (
        ("propose", cmd_propose, "build a fix for one finding"),
        ("verify", cmd_verify, "verify a proposed fix on a copy of the repo"),
        ("approve", cmd_approve, "a person approves a verified fix (interactive)"),
        ("apply", cmd_apply, "apply an approved fix on a new git branch (dry run by default)"),
        ("explain", cmd_explain, "show the rule and source behind a finding"),
        ("trace", cmd_trace, "show every stage's input and output for one finding"),
    ):
        x = sub.add_parser(name, help=helptext)
        x.add_argument("id")
        x.add_argument("--repo", default=".")
        x.set_defaults(fn=fn)
        if name == "verify":
            x.add_argument("--runner", choices=["docker", "local"], default="docker")
            x.add_argument("--image", help=f"sandbox image (default {DEFAULT_IMAGE})")
            x.add_argument(
                "--allow-local-exec", action="store_true",
                help="required for --runner local: you accept there is no isolation",
            )  # fmt: skip
            x.add_argument("--python", help="interpreter inside the runner (default: its own)")
            x.add_argument("--timeout", type=int, default=120)
        if name == "apply":
            x.add_argument("--execute", action="store_true", help="really apply (default: dry run)")

    r = sub.add_parser("report", help="summary of all findings")
    r.add_argument("--repo", default=".")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_report)

    ru = sub.add_parser("rules", help="list rules, or --selftest them")
    ru.add_argument("--selftest", action="store_true")
    ru.add_argument("--python")
    ru.add_argument("--runner", choices=["docker", "local"], default="local")
    ru.add_argument("--image")
    ru.set_defaults(fn=cmd_rules)

    sb = sub.add_parser("sandbox", help="build or check the Docker sandbox image")
    sb.add_argument("action", choices=["build", "check"])
    sb.add_argument("--image")
    sb.set_defaults(fn=cmd_sandbox)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (DepfixError, InvalidTransition, RunnerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
