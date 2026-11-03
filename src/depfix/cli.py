"""Thin command-line wrapper over pipeline.py. No logic lives here."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .embed import Embedder, HashEmbedder
from .index import Index, IndexError_
from .indexer import build_index
from .llm import LLM, LLMError, RecordingLLM, ReplayLLM
from .llm_fixer import ModelContext
from .models import InvalidTransition
from .ollama import (
    DEFAULT_EMBED_MODEL,
    DEFAULT_MODEL,
    DEFAULT_URL,
    OllamaClient,
    OllamaEmbedder,
    OllamaLLM,
    check_ollama,
)
from .paths import home, index_path
from .pipeline import (
    ApprovalRequest,
    DepfixError,
    apply_fix,
    approve,
    explain_text,
    fix_with_model,
    propose,
    propose_with_model,
    report,
    rule_for,
    scan,
    trace,
    verify,
)
from .retrieve import evaluate
from .rules import load_rules
from .runner import (
    DEFAULT_IMAGE,
    LocalRunner,
    Runner,
    RunnerError,
    docker_available,
    make_docker_runner,
    sandbox_build,
    sandbox_probe,
)
from .selftest import selftest_all
from .sources import FetchError, load_sources, read_source
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


# ---- wiring for the documentation index and the model --------------------------------------------


def _open_index(a: argparse.Namespace) -> Index:
    path = Path(a.index) if a.index else index_path()
    if not path.exists():
        raise DepfixError("no documentation index yet; build one with: depfix index build")
    return Index(path)


def _embedder_for(index: Index, a: argparse.Namespace) -> Embedder:
    """The embedder is read from the index itself, so a query can never use a different one."""
    name = index.get_meta("embedder") or ""
    if name.startswith("hash-"):
        return HashEmbedder(int(name.split("-")[1]))
    if name.startswith("ollama:"):
        return OllamaEmbedder(OllamaClient(a.ollama_url), name.split(":", 1)[1])
    raise DepfixError(f"the index has an unknown embedder '{name}'")


def _llm(a: argparse.Namespace) -> LLM:
    spec: str = a.llm
    if spec == "ollama":
        return OllamaLLM(OllamaClient(a.ollama_url))
    if spec.startswith("replay:"):
        return ReplayLLM(Path(spec[len("replay:") :]))
    if spec.startswith("record:"):
        return RecordingLLM(OllamaLLM(OllamaClient(a.ollama_url)), Path(spec[len("record:") :]))
    raise DepfixError("--llm must be 'ollama', 'replay:FILE' or 'record:FILE'")


def _ctx(a: argparse.Namespace) -> ModelContext:
    index = _open_index(a)
    return ModelContext(index, _embedder_for(index, a), _llm(a), a.model)


# ---- commands ------------------------------------------------------------------------------------


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
    repo = _repo(a.repo)
    use_model = a.mode == "llm" or (a.mode == "auto" and rule_for(repo, a.id).fix.kind == "llm")
    if use_model:
        res = propose_with_model(repo, a.id, _ctx(a))
        print(f"outcome: {res.outcome}" + (f" ({res.reason})" if res.reason else ""))
        for e in res.evidence:
            print(
                f"  evidence [{e.chunk_id}] {e.section or '(page)'} ({e.license})"
                f" via {','.join(e.layers)}"
            )
        if res.proposal:
            p = res.proposal
            print(
                f"\n{p.rationale}\ncites: {', '.join(p.citations)}\n"
                f"source: {p.reference_url}\n\n{p.diff}"
            )
        return 0
    res_t = propose(repo, a.id)
    print(f"outcome: {res_t.outcome}" + (f" ({res_t.reason})" if res_t.reason else ""))
    if res_t.proposal:
        p = res_t.proposal
        print(f"\n{p.rationale}\nsource: {p.reference_url}\n\n{p.diff}")
    return 0


def _print_verification(runner_name: str, v_checks: list[tuple[str, str, str]]) -> None:
    print(f"runner: {runner_name}")
    for status, name, first in v_checks:
        print(f"[{status.upper():7}] {name:<13} {first}")


def cmd_verify(a: argparse.Namespace) -> int:
    runner = _runner(a)
    cfg = VerifyConfig(runner=runner, python=a.python, timeout_s=a.timeout)
    v = verify(_repo(a.repo), a.id, cfg)
    _print_verification(
        runner.name,
        [(c.status, c.name, c.detail.splitlines()[0] if c.detail else "") for c in v.checks],
    )
    print(f"\nresult: {'VERIFIED' if v.passed else 'FAILED'}  confidence: {v.confidence}")
    return 0 if v.passed else 1


def cmd_fix(a: argparse.Namespace) -> int:
    runner = _runner(a)
    cfg = VerifyConfig(runner=runner, python=a.python, timeout_s=a.timeout)
    attempts = fix_with_model(_repo(a.repo), a.id, _ctx(a), cfg, max_retries=a.retries)
    print(f"runner: {runner.name}   model: {a.model}   retries allowed: {a.retries}")
    for t in attempts:
        verdict = {True: "VERIFIED", False: "FAILED", None: "-"}[t.passed]
        extra = ", ".join(t.failed_checks) or (t.reason.splitlines()[0] if t.reason else "")
        print(f"attempt {t.n + 1}: {t.outcome:<12} {verdict:<9} {extra}")
    ok = bool(attempts) and attempts[-1].passed is True
    print(f"\nresult: {'VERIFIED' if ok else 'NOT FIXED'} after {len(attempts)} attempt(s)")
    if ok:
        print(f"next: depfix approve {a.id} --repo {a.repo}")
    return 0 if ok else 1


def cmd_approve(a: argparse.Namespace) -> int:
    # An approval is a person's decision. Refuse when no person can be typing: piped input from a
    # script or an AI tool must not be able to say "y".
    if not sys.stdin.isatty():
        raise DepfixError("approval needs an interactive terminal: a person must read and answer")

    def confirm(req: ApprovalRequest) -> bool:
        f, p, v = req.finding, req.proposal, req.verification
        print(f"\nFinding {f.id}   {f.file}:{f.start_line}   rule {req.rule.id}   fix by: {p.kind}")
        print(f"{p.rationale}\nsource: {p.reference_url}")
        if p.citations:
            print(f"the model cited: {', '.join(p.citations)}")
        print(f"\n{p.diff}")
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
    path = Path(a.index) if a.index else index_path()
    if path.exists():
        with Index(path) as idx:
            print(explain_text(_repo(a.repo), a.id, index=idx, embedder=_embedder_for(idx, a)))
    else:
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
    if not docker_available():
        raise RunnerError(
            "Docker is not running (or not installed). Start Docker Desktop, or `colima start`."
        )
    if a.action == "build":
        print(f"Building {a.image or DEFAULT_IMAGE}{' (minimal)' if a.minimal else ''}; "
              "this one step uses the network and streams its progress below.")  # fmt: skip
        rc = sandbox_build(a.image, a.minimal)
        if rc:
            print(
                "\nThe build failed. On a slow connection try the small image first:\n"
                "  depfix sandbox build --minimal\n"
                "A pytest-only image is enough for projects that need no other libraries.",
                file=sys.stderr,
            )
        return rc
    rows = sandbox_probe(make_docker_runner(a.image))
    for name, ok, detail in rows:
        print(f"[{'ok' if ok else 'FAIL':<4}] {name} {detail}".rstrip())
    bad = sum(not ok for _, ok, _ in rows)
    print("\nsandbox isolation verified" if not bad else f"\n{bad} isolation check(s) FAILED")
    return 1 if bad else 0


def cmd_index(a: argparse.Namespace) -> int:
    path = Path(a.index) if a.index else index_path()
    if a.action == "build":
        if a.rebuild and path.exists():
            path.unlink()
        embedder: Embedder = (
            HashEmbedder()
            if a.embedder == "hash"
            else OllamaEmbedder(OllamaClient(a.ollama_url), a.embed_model)
        )
        base = None if a.docs == "real" else Path(a.docs).resolve().parent
        sources = load_sources(None if a.docs == "real" else Path(a.docs))
        cache = home() / "raw"
        extra = frozenset(a.allow_host or [])
        with Index(path) as idx:
            rep = build_index(
                idx, sources, embedder, a.chunker,
                lambda s: read_source(s, cache, base, extra_hosts=extra, refresh=a.refresh),
            )  # fmt: skip
        print(
            f"indexed {rep.chunks} chunks into {path} "
            f"(chunker {a.chunker}, embedder {embedder.name})"
        )
        for s in sources:
            print(f"  - {s.id}: {rep.per_source[s.id]} chunks; licence {s.license} {s.license_url}")
        print("Attribution and licences are stored with every chunk and shown by `depfix explain`.")
        return 0
    if not path.exists():
        raise DepfixError("no documentation index yet; build one with: depfix index build")
    with Index(path) as idx:
        if a.action == "stats":
            print(json.dumps(idx.stats(), indent=2))
            return 0
        embedder = _embedder_for(idx, a)
        rules = list(load_rules().rules)
        print(f"retrieval quality on {path.name} (chunker {idx.get_meta('chunker')}, "
              f"embedder {idx.get_meta('embedder')}); the anchor layer is excluded because it is "
              "the answer key itself")  # fmt: skip
        for layers in (("keyword",), ("vector",), ("keyword", "vector")):
            print(f"  {'+'.join(layers):<15}{evaluate(idx, embedder, rules, a.k, layers)}")
    return 0


def cmd_llm(a: argparse.Namespace) -> int:
    rows = check_ollama(OllamaClient(a.ollama_url), a.model, a.embed_model)
    for name, ok, detail in rows:
        print(f"[{'ok' if ok else 'FAIL':<4}] {name}" + (f"   ({detail})" if detail else ""))
    return 0 if all(ok for _, ok, _ in rows) else 1


# ---- argument parser -----------------------------------------------------------------------------


def _add_runner_args(x: argparse.ArgumentParser) -> None:
    x.add_argument("--runner", choices=["docker", "local"], default="docker")
    x.add_argument("--image", help=f"sandbox image (default {DEFAULT_IMAGE})")
    x.add_argument(
        "--allow-local-exec", action="store_true",
        help="required for --runner local: you accept there is no isolation",
    )  # fmt: skip
    x.add_argument("--python", help="interpreter inside the runner (default: its own)")
    x.add_argument("--timeout", type=int, default=120)


def _add_model_args(x: argparse.ArgumentParser) -> None:
    x.add_argument("--model", default=os.environ.get("DEPFIX_MODEL", DEFAULT_MODEL))
    x.add_argument("--ollama-url", default=DEFAULT_URL)
    x.add_argument("--llm", default="ollama", help="ollama | replay:FILE | record:FILE")
    x.add_argument("--index", help="documentation index file (default: in ~/.config/depfix)")


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
        ("propose", cmd_propose, "build a fix for one finding (template or model)"),
        ("verify", cmd_verify, "verify a proposed fix on a copy of the repo"),
        ("fix", cmd_fix, "model fix + verify, retrying with the failure message"),
        ("approve", cmd_approve, "a person approves a verified fix (interactive)"),
        ("apply", cmd_apply, "apply an approved fix on a new git branch (dry run by default)"),
        ("explain", cmd_explain, "the rule and the cited documentation behind a finding"),
        ("trace", cmd_trace, "show every stage's input and output for one finding"),
    ):
        x = sub.add_parser(name, help=helptext)
        x.add_argument("id")
        x.add_argument("--repo", default=".")
        x.set_defaults(fn=fn)
        if name == "verify":
            _add_runner_args(x)
        if name == "fix":
            _add_runner_args(x)
            _add_model_args(x)
            x.add_argument("--retries", type=int, default=2)
        if name == "propose":
            _add_model_args(x)
            x.add_argument("--mode", choices=["auto", "template", "llm"], default="auto")
        if name == "explain":
            x.add_argument("--index")
            x.add_argument("--ollama-url", default=DEFAULT_URL)
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
    sb.add_argument("--minimal", action="store_true", help="pytest-only image (small, fast)")
    sb.set_defaults(fn=cmd_sandbox)

    ix = sub.add_parser("index", help="build, inspect or evaluate the documentation index")
    ix.add_argument("action", choices=["build", "stats", "eval"])
    ix.add_argument(
        "--docs", default="real", help="'real' or a sources.yaml file (e.g. a test corpus)"
    )
    ix.add_argument("--chunker", choices=["entry", "fixed"], default="entry")
    ix.add_argument("--embedder", choices=["ollama", "hash"], default="ollama")
    ix.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    ix.add_argument("--ollama-url", default=DEFAULT_URL)
    ix.add_argument("--index")
    ix.add_argument("--allow-host", action="append", help="extra host allowed for fetching")
    ix.add_argument("--refresh", action="store_true", help="ignore the download cache")
    ix.add_argument("--rebuild", action="store_true", help="delete the index first")
    ix.add_argument("--k", type=int, default=4)
    ix.set_defaults(fn=cmd_index)

    lm = sub.add_parser("llm", help="check that Ollama and the models work")
    lm.add_argument("action", choices=["check"])
    lm.add_argument("--model", default=os.environ.get("DEPFIX_MODEL", DEFAULT_MODEL))
    lm.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    lm.add_argument("--ollama-url", default=DEFAULT_URL)
    lm.set_defaults(fn=cmd_llm)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (DepfixError, InvalidTransition, RunnerError, LLMError, FetchError, IndexError_) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
