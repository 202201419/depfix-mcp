"""Thin command-line wrapper over pipeline.py. No logic lives here."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .models import InvalidTransition
from .pipeline import DepfixError, explain_text, propose, report, scan, trace, verify
from .rules import load_rules
from .selftest import selftest_all
from .verify import VerifyConfig

NO_SANDBOX = (
    "Drop 1 runs your project's tests as a local process on THIS machine, with a timeout and a\n"
    "stripped environment but WITHOUT network or filesystem isolation. Re-run with\n"
    "--allow-local-exec only for code you trust. The container sandbox arrives in drop 2."
)


def _repo(s: str) -> Path:
    p = Path(s).resolve()
    if not p.is_dir():
        raise DepfixError(f"{s} is not a directory")
    return p


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
    if not a.allow_local_exec:
        print(NO_SANDBOX, file=sys.stderr)
        return 2
    cfg = VerifyConfig(python=a.python or sys.executable, timeout_s=a.timeout)
    v = verify(_repo(a.repo), a.id, cfg)
    for c in v.checks:
        first = c.detail.splitlines()[0] if c.detail else ""
        print(f"[{c.status.upper():7}] {c.name:<13} {first}")
    print(f"\nresult: {'VERIFIED' if v.passed else 'FAILED'}  confidence: {v.confidence}")
    return 0 if v.passed else 1


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
        bad = 0
        for rid, problems in selftest_all(rules, a.python or sys.executable).items():
            print(f"{'FAIL' if problems else 'ok  '} {rid}")
            for p in problems:
                print(f"     {p}")
            bad += bool(problems)
        print(f"\n{len(rules.rules) - bad}/{len(rules.rules)} rules passed their self-test")
        return 1 if bad else 0
    for r in rules.rules:
        print(f"{r.id:<10}{r.library:<8}{r.fix.kind:<9}{r.qualified_name}")
    return 0


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
        ("explain", cmd_explain, "show the rule and source behind a finding"),
        ("trace", cmd_trace, "show every stage's input and output for one finding"),
    ):
        x = sub.add_parser(name, help=helptext)
        x.add_argument("id")
        x.add_argument("--repo", default=".")
        x.set_defaults(fn=fn)
        if name == "verify":
            x.add_argument(
                "--allow-local-exec",
                action="store_true",
                help="acknowledge there is no sandbox yet (drop 1)",
            )
            x.add_argument("--python", help="interpreter that runs the project's tests")
            x.add_argument("--timeout", type=int, default=120)

    r = sub.add_parser("report", help="summary of all findings")
    r.add_argument("--repo", default=".")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_report)

    ru = sub.add_parser("rules", help="list rules, or --selftest them")
    ru.add_argument("--selftest", action="store_true")
    ru.add_argument("--python")
    ru.set_defaults(fn=cmd_rules)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (DepfixError, InvalidTransition) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
