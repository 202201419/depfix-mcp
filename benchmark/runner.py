"""Benchmark: run the deterministic pipeline on synthetic repos that have known answers.

For each case we scan, propose and verify, then judge the result with HIDDEN tests the verifier
never saw. The gap between "the verifier approved it" and "the hidden tests agree" is the most
important number here: it measures how often the harness itself is fooled.

Usage:
    python -m benchmark.runner                       # run and print
    python -m benchmark.runner --write-baseline      # record benchmark/baseline.json
    python -m benchmark.runner --check benchmark/baseline.json   # regression gate (exit 1 on drop)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from depfix import pipeline
from depfix.rules import RuleSet, load_rules
from depfix.store import Store
from depfix.verify import VerifyConfig, apply_patch, copy_repo, run_pytest

CASES = Path(__file__).parent / "cases"
BASELINE = Path(__file__).parent / "baseline.json"
RESULTS = Path(__file__).parent / "results"


@dataclass
class FixOutcome:
    expected: str  # template_ok | not_applicable | needs_llm
    outcome: str  # what the pipeline did: proposed | not_applicable | needs_llm | missed
    verified: bool = False
    hidden_pass: bool | None = None
    unsafe: bool = False
    oracle_discriminates: bool | None = None  # do hidden tests fail on the UNFIXED repo?
    success: bool = False
    note: str = ""


@dataclass
class CaseResult:
    name: str
    tags: list[str]
    tp: int = 0
    fp: int = 0
    fn: int = 0
    fixes: list[FixOutcome] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def gated(self) -> bool:
        return "known_gap" not in self.tags


def tree_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if (
            p.is_file()
            and not p.is_symlink()
            and not {".depfix", "__pycache__", ".pytest_cache"} & set(rel.parts)
        ):
            out[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def hidden_run(
    base: Path, case: Path, strict: bool, python: str, scratch: Path, label: str
) -> bool:
    shutil.copytree(case / "hidden_tests", base / "hidden_tests", dirs_exist_ok=True)
    extra = ["hidden_tests"] + (["-W", "error::DeprecationWarning"] if strict else [])
    run = run_pytest(python, base, scratch, 120, label, extra)
    return run.returncode == 0


def run_case(case: Path, rules: RuleSet, python: str) -> CaseResult:
    start = time.time()
    spec: dict[str, Any] = yaml.safe_load((case / "expected.yaml").read_text(encoding="utf-8"))
    res = CaseResult(name=case.name, tags=list(spec.get("tags") or []))
    strict = bool(spec.get("strict_warnings"))
    with tempfile.TemporaryDirectory(prefix="depfix-bench-") as td:
        root = Path(td)
        repo = root / "repo"
        shutil.copytree(case / "repo", repo)
        findings, _ = pipeline.scan(repo, rules=rules)
        expected = {(e["rule"], e["file"], e["line"]): e for e in spec["expected"]}
        got = {(f.rule_id, f.file, f.start_line): f for f in findings}
        res.tp = len(expected.keys() & got.keys())
        res.fp = len(got.keys() - expected.keys())
        res.fn = len(expected.keys() - got.keys())
        cfg = VerifyConfig(python=python, timeout_s=120)

        for key, e in expected.items():
            fo = FixOutcome(expected=e["fix"], outcome="missed")
            res.fixes.append(fo)
            f = got.get(key)
            if f is None:
                continue
            pr = pipeline.propose(repo, f.id, rules)
            fo.outcome = pr.outcome
            fo.note = pr.reason
            if e["fix"] != "template_ok":
                fo.success = pr.outcome == e["fix"]  # correct refusal / routing
                continue
            if pr.outcome != "proposed" or pr.proposal is None:
                continue
            v = pipeline.verify(repo, f.id, cfg, rules)
            fo.verified = v.passed
            scratch = root / f"scratch-{f.id}"
            scratch.mkdir()
            before = root / f"before-{f.id}"
            copy_repo(repo, before)
            fo.oracle_discriminates = not hidden_run(before, case, strict, python, scratch, "pre")
            if not v.passed:
                continue
            work = root / f"after-{f.id}"
            copy_repo(repo, work)
            with Store(repo) as store:
                proposal = store.get_proposal(f.id)
            assert proposal is not None
            ok, detail = apply_patch(work, proposal.diff, scratch)
            if not ok:
                fo.note = detail
                continue
            # Independent of the verifier's own checks: did ANY file other than the target change?
            changed = {p for p, h in tree_hashes(work).items() if tree_hashes(repo).get(p) != h}
            fo.unsafe = changed != {f.file}
            fo.hidden_pass = hidden_run(work, case, strict, python, scratch, "post")
            fo.success = bool(fo.hidden_pass) and not fo.unsafe
    res.seconds = round(time.time() - start, 1)
    return res


def ratio(num: int, den: int, empty: float) -> float:
    return round(num / den, 4) if den else empty


def summarise(results: list[CaseResult]) -> dict[str, Any]:
    gated = [r for r in results if r.gated]
    tp, fp, fn = (sum(getattr(r, k) for r in gated) for k in ("tp", "fp", "fn"))
    fixes = [(r, f) for r in gated for f in r.fixes]
    tmpl = [f for _, f in fixes if f.expected == "template_ok"]
    refuse = [f for _, f in fixes if f.expected != "template_ok"]
    verified = [f for f in tmpl if f.verified]
    gap = [r for r in results if not r.gated]
    return {
        "cases": len(gated),
        "expected_findings": tp + fn,
        "precision": ratio(tp, tp + fp, 1.0),
        "recall": ratio(tp, tp + fn, 1.0),
        "fix_success_rate": ratio(sum(f.success for f in tmpl), len(tmpl), 1.0),
        "correct_refusal_rate": ratio(sum(f.success for f in refuse), len(refuse), 1.0),
        "false_accept_rate": ratio(
            sum(1 for f in verified if not f.hidden_pass), len(verified), 0.0
        ),
        "unsafe_patch_rate": ratio(sum(f.unsafe for f in verified), len(verified), 0.0),
        "oracle_discrimination": ratio(
            sum(1 for f in tmpl if f.oracle_discriminates), len(tmpl), 1.0
        ),
        "known_gap_cases": {r.name: {"tp": r.tp, "fp": r.fp, "fn": r.fn} for r in gap},
    }


# metric -> (direction, tolerance). Deterministic pipeline, so tolerances are tight; the LLM drops
# will use the same gate on recorded model outputs.
GATE: dict[str, tuple[str, float]] = {
    "precision": ("min", 0.02),
    "recall": ("min", 0.02),
    "fix_success_rate": ("min", 0.02),
    "correct_refusal_rate": ("min", 0.02),
    "false_accept_rate": ("max", 0.01),
    "unsafe_patch_rate": ("max", 0.0),
}


def check_against(summary: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for metric, (direction, tol) in GATE.items():
        now, base = summary[metric], baseline[metric]
        if direction == "min" and now < base - tol:
            problems.append(f"{metric} dropped: {now} < baseline {base} (tolerance {tol})")
        if direction == "max" and now > base + tol:
            problems.append(f"{metric} rose: {now} > baseline {base} (tolerance {tol})")
    return problems


def print_report(results: list[CaseResult], summary: dict[str, Any]) -> None:
    print(f"{'CASE':<26}{'DET tp/fp/fn':<14}FIXES (expected -> result)")
    for r in sorted(results, key=lambda x: x.name):
        marks = []
        for f in r.fixes:
            mark = "ok" if f.success else "FAIL"
            marks.append(f"{f.expected}->{f.outcome}:{mark}")
        tag = " [known gap]" if not r.gated else ""
        print(f"{r.name:<26}{f'{r.tp}/{r.fp}/{r.fn}':<14}{', '.join(marks) or '-'}{tag}")
    print()
    for k, v in summary.items():
        if k != "known_gap_cases":
            print(f"{k:<24}{v}")
    print(f"known gaps (not gated): {summary['known_gap_cases']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--check", type=Path)
    ap.add_argument("--only", help="run a single case by name")
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args(argv)

    rules = load_rules()
    cases = sorted(
        p for p in CASES.iterdir() if p.is_dir() and (a.only is None or p.name == a.only)
    )
    workers = min(4, os.cpu_count() or 1)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda c: run_case(c, rules, a.python), cases))
    summary = summarise(results)
    print_report(results, summary)

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "latest.json").write_text(
        json.dumps({"summary": summary, "cases": [asdict(r) for r in results]}, indent=2)
    )
    if a.write_baseline:
        BASELINE.write_text(json.dumps(summary, indent=2) + "\n")
        print(f"\nbaseline written to {BASELINE}")
    if a.check:
        problems = check_against(summary, json.loads(a.check.read_text()))
        if problems:
            print("\nREGRESSION:\n  " + "\n  ".join(problems))
            return 1
        print("\nregression gate: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
