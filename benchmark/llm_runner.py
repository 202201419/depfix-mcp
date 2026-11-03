"""Benchmark the MODEL path: force a model to write each fix, verify it, retry, then judge.

For every expected fixable finding the model gets the same documentation and the same retry budget
a user would. We then judge the final patch with the HIDDEN tests, exactly as the template
benchmark does. Because the template rule answers these cases perfectly, any gap is the model's.

    python -m benchmark.llm_runner --llm policy:oracle         # self-test with a simulated model
    python -m benchmark.llm_runner --llm record:FILE --model qwen2.5-coder:7b
    python -m benchmark.llm_runner --llm replay:FILE --model qwen2.5-coder:7b --check BASELINE
    --chunker fixed    # experiment E1: does the chunking strategy change fix success?
    --repeats 3        # experiment E7: consistent failures vs flaky ones (temperature 0.3)

`replay:` is what CI does: no model needed, identical answers every time.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from benchmark.policies import POLICIES
from benchmark.runner import CASES, RESULTS, hidden_run, ratio, tree_hashes
from depfix import pipeline
from depfix.embed import HashEmbedder
from depfix.index import Index
from depfix.indexer import build_index
from depfix.llm import LLM, PolicyLLM, RecordingLLM, ReplayLLM
from depfix.llm_fixer import DEFAULT_OPTIONS, ModelContext
from depfix.ollama import DEFAULT_MODEL, DEFAULT_URL, OllamaClient, OllamaLLM
from depfix.rules import RuleSet, load_rules
from depfix.runner import LocalRunner
from depfix.sources import load_sources, read_source
from depfix.store import Store
from depfix.verify import VerifyConfig, apply_patch, copy_repo

DOCS = Path(__file__).parent / "docs"


@dataclass
class Target:
    case: str
    rule: str
    repeat: int
    attempts: list[dict[str, Any]] = field(default_factory=list)
    verified: bool = False
    hidden_pass: bool | None = None
    unsafe: bool = False
    success: bool = False
    first_try: bool = False


def build_docs_index(chunker: str) -> tuple[Index, HashEmbedder]:
    """Retrieval for benchmarks uses OUR original-text docs and a deterministic embedder, so a
    recording made on your Mac replays identically in CI (real-docs retrieval is measured
    separately by `depfix index eval`)."""
    emb = HashEmbedder()
    idx = Index(":memory:")
    build_index(
        idx, load_sources(DOCS / "sources.yaml"), emb, chunker,
        lambda s: read_source(s, Path("/nonexistent"), DOCS),
    )  # fmt: skip
    return idx, emb


def make_llm(spec: str, ollama_url: str) -> LLM:
    if spec.startswith("policy:"):
        return PolicyLLM(POLICIES[spec.split(":", 1)[1]])
    if spec.startswith("replay:"):
        return ReplayLLM(Path(spec[len("replay:") :]))
    if spec.startswith("record:"):
        return RecordingLLM(OllamaLLM(OllamaClient(ollama_url)), Path(spec[len("record:") :]))
    if spec == "ollama":
        return OllamaLLM(OllamaClient(ollama_url))
    raise SystemExit("--llm must be policy:NAME, replay:FILE, record:FILE or ollama")


def run_case(
    case: Path, rules: RuleSet, ctx: ModelContext, python: str, retries: int, repeat: int
) -> list[Target]:
    spec: dict[str, Any] = yaml.safe_load((case / "expected.yaml").read_text(encoding="utf-8"))
    strict = bool(spec.get("strict_warnings"))
    out: list[Target] = []
    with tempfile.TemporaryDirectory(prefix="depfix-llmbench-") as td:
        root = Path(td)
        repo = root / "repo"
        shutil.copytree(case / "repo", repo)
        findings, _ = pipeline.scan(repo, rules=rules)
        got = {(f.rule_id, f.file, f.start_line): f for f in findings}
        cfg = VerifyConfig(runner=LocalRunner(), python=python, timeout_s=120)
        for e in spec["expected"]:
            if e["fix"] != "template_ok":
                continue  # only fixes that have a hidden-test oracle are scored
            t = Target(case.name, e["rule"], repeat)
            out.append(t)
            f = got.get((e["rule"], e["file"], e["line"]))
            if f is None:
                continue  # the scanner missed it; that is the scanner's metric, not the model's
            attempts = pipeline.fix_with_model(repo, f.id, ctx, cfg, rules, retries)
            t.attempts = [asdict(a) for a in attempts]
            final = attempts[-1] if attempts else None
            t.verified = bool(final and final.passed)
            if not t.verified:
                continue
            with Store(repo) as store:
                proposal = store.get_proposal(f.id)
            assert proposal is not None
            scratch = root / f"scratch-{f.id}"
            scratch.mkdir()
            work = root / f"after-{f.id}"
            copy_repo(repo, work)
            ok, _ = apply_patch(work, proposal.diff, scratch)
            if not ok:
                continue
            after = tree_hashes(work)
            before = tree_hashes(repo)
            t.unsafe = {p for p, h in after.items() if before.get(p) != h} != {f.file}
            t.hidden_pass = hidden_run(work, case, strict, python, scratch, "post")
            t.success = bool(t.hidden_pass) and not t.unsafe
            t.first_try = t.success and len(attempts) == 1
    return out


def summarise(targets: list[Target], repeats: int) -> dict[str, Any]:
    verified = [t for t in targets if t.verified]
    attempts = [a for t in targets for a in t.attempts]
    proposed = [a for a in attempts if a["outcome"] == "proposed"]
    by_key: dict[tuple[str, str], list[bool]] = {}
    for t in targets:
        by_key.setdefault((t.case, t.rule), []).append(t.success)
    consistent_pass = sum(all(v) for v in by_key.values())
    consistent_fail = sum(not any(v) for v in by_key.values())
    return {
        "targets": len(by_key),
        "repeats": repeats,
        "fix_success_rate": ratio(sum(t.success for t in targets), len(targets), 1.0),
        "first_try_rate": ratio(sum(t.first_try for t in targets), len(targets), 1.0),
        "false_accept_rate": ratio(
            sum(1 for t in verified if not t.hidden_pass), len(verified), 0.0
        ),
        "unsafe_patch_rate": ratio(sum(t.unsafe for t in verified), len(verified), 0.0),
        "invalid_output_rate": ratio(
            sum(1 for a in attempts if a["outcome"] in ("invalid", "refused")), len(attempts), 0.0
        ),
        "citation_failure_rate": ratio(
            sum(1 for a in proposed if "provenance" in a["failed_checks"]), len(proposed), 0.0
        ),
        "mean_attempts": round(len(attempts) / len(targets), 2) if targets else 0.0,
        "consistent_pass": consistent_pass,
        "consistent_fail": consistent_fail,
        "flaky": len(by_key) - consistent_pass - consistent_fail,
    }


GATE = {
    "fix_success_rate": ("min", 0.05),
    "first_try_rate": ("min", 0.05),
    "false_accept_rate": ("max", 0.0),
    "unsafe_patch_rate": ("max", 0.0),
}


def check_against(summary: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    problems = []
    for metric, (direction, tol) in GATE.items():
        now, base = summary[metric], baseline[metric]
        if direction == "min" and now < base - tol:
            problems.append(f"{metric} dropped: {now} < baseline {base} (tolerance {tol})")
        if direction == "max" and now > base + tol:
            problems.append(f"{metric} rose: {now} > baseline {base} (tolerance {tol})")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--ollama-url", default=DEFAULT_URL)
    ap.add_argument("--chunker", choices=["entry", "fixed"], default="entry")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--only", help="run a single case")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--write-baseline", type=Path)
    ap.add_argument("--check", type=Path)
    a = ap.parse_args(argv)

    rules = load_rules()
    idx, emb = build_docs_index(a.chunker)
    llm = make_llm(a.llm, a.ollama_url)
    cases = sorted(
        p for p in CASES.iterdir() if p.is_dir() and (a.only is None or p.name == a.only)
    )
    live = a.llm.startswith(("record:", "ollama"))
    workers = 1 if live else 4  # one local model, one request at a time
    targets: list[Target] = []
    start = time.time()
    for rep in range(a.repeats):
        options = dict(DEFAULT_OPTIONS)
        if a.repeats > 1:  # repeats only differ if sampling is random
            options.update(temperature=0.3, seed=rep + 1)
        ctx = ModelContext(idx, emb, llm, a.model, options)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for res in ex.map(
                lambda c, r=rep, x=ctx: run_case(c, rules, x, a.python, a.retries, r), cases
            ):
                targets.extend(res)
    summary = summarise(targets, a.repeats)
    summary.update(
        model=a.model,
        chunker=a.chunker,
        llm=a.llm.split(":")[0],
        seconds=round(time.time() - start),
    )

    print(f"{'CASE':<26}{'RULE':<10}{'ATTEMPTS':<10}RESULT")
    for t in sorted(targets, key=lambda x: (x.case, x.rule, x.repeat)):
        flow = (
            "->".join(f"{x['outcome'][:4]}{'+' if x['passed'] else ''}" for x in t.attempts) or "-"
        )
        print(f"{t.case:<26}{t.rule:<10}{flow:<10}{'ok' if t.success else 'FAIL'}")
    print()
    for k, v in summary.items():
        print(f"{k:<24}{v}")
    kinds = Counter(x["outcome"] for t in targets for x in t.attempts)
    print(f"attempt outcomes: {dict(kinds)}")

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "latest-llm.json").write_text(
        json.dumps({"summary": summary, "targets": [asdict(t) for t in targets]}, indent=2)
    )
    if a.write_baseline:
        a.write_baseline.write_text(json.dumps(summary, indent=2) + "\n")
        print(f"\nbaseline written to {a.write_baseline}")
    if a.check:
        problems = check_against(summary, json.loads(a.check.read_text()))
        if problems:
            print("\nREGRESSION:\n  " + "\n  ".join(problems))
            return 1
        print("\nregression gate: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
