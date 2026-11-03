"""The verify-and-retry loop, and the CLI around it, using simulated models."""

import json

import pytest

import depfix.cli as cli_mod
from benchmark.policies import garbage, oracle, refuser, sloppy, stubborn
from depfix import pipeline
from depfix.cli import main
from depfix.embed import HashEmbedder
from depfix.index import Index
from depfix.llm import PolicyLLM, ReplayLLM, ReplayMiss
from depfix.llm_fixer import ModelContext
from depfix.models import Status
from depfix.runner import LocalRunner
from depfix.store import Store
from depfix.verify import VerifyConfig
from tests.helpers import DOCS, FakeOllama, app_repo, fixture_index

FAST = VerifyConfig(runner=LocalRunner(), run_tests=False)  # logic of the loop, no containers/tests
FULL = VerifyConfig(runner=LocalRunner(), run_tests=True)


@pytest.fixture(scope="module")
def docs():
    return fixture_index()


def setup(tmp_path, docs, policy):
    repo = app_repo(tmp_path)
    findings, _ = pipeline.scan(repo)
    fid = next(f.id for f in findings if f.rule_id == "STD-0003")
    idx, emb = docs
    llm = PolicyLLM(policy)
    return repo, fid, ModelContext(idx, emb, llm, "m"), llm


def status(repo, fid):
    with Store(repo) as s:
        f = s.get_finding(fid)
        assert f is not None
        return f.status


def test_a_perfect_model_is_verified_on_the_first_attempt_with_the_real_tests(tmp_path, docs):
    repo, fid, ctx, _ = setup(tmp_path, docs, oracle)
    attempts = pipeline.fix_with_model(repo, fid, ctx, FULL)
    assert [(a.n, a.outcome, a.passed) for a in attempts] == [(0, "proposed", True)]
    assert status(repo, fid) is Status.VERIFIED
    with Store(repo) as s:
        v = s.get_verification(fid)
        assert v is not None and v.confidence == "high"
        assert {c.name for c in v.checks if c.status == "passed"} >= {"provenance", "tests"}


def test_a_hallucinated_name_is_caught_and_the_retry_prompt_explains_why(tmp_path, docs):
    repo, fid, ctx, llm = setup(tmp_path, docs, sloppy)
    attempts = pipeline.fix_with_model(repo, fid, ctx, FAST)
    assert [(a.outcome, a.passed) for a in attempts] == [("proposed", False), ("proposed", True)]
    assert "provenance" in attempts[0].failed_checks
    second = llm.calls[1].messages[-1]["content"]
    assert "was rejected" in second and "provenance" in second and "warning99" in second
    assert status(repo, fid) is Status.VERIFIED


def test_a_model_that_never_improves_stops_after_the_retry_limit(tmp_path, docs):
    repo, fid, ctx, llm = setup(tmp_path, docs, stubborn)
    attempts = pipeline.fix_with_model(repo, fid, ctx, FAST, max_retries=2)
    assert len(attempts) == 3 and not any(a.passed for a in attempts) and len(llm.calls) == 3
    assert status(repo, fid) is Status.FAILED
    assert (repo / "src/app.py").read_text().count("logging.warn(") == 1  # nothing was applied


def test_zero_retries_means_one_attempt(tmp_path, docs):
    repo, fid, ctx, llm = setup(tmp_path, docs, stubborn)
    assert len(pipeline.fix_with_model(repo, fid, ctx, FAST, max_retries=0)) == 1


def test_a_refusal_ends_the_loop_immediately(tmp_path, docs):
    repo, fid, ctx, llm = setup(tmp_path, docs, refuser)
    attempts = pipeline.fix_with_model(repo, fid, ctx, FAST)
    assert [(a.outcome, a.passed) for a in attempts] == [("refused", None)] and len(llm.calls) == 1


def test_unusable_output_is_retried_with_the_reason(tmp_path, docs):
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        return garbage(req) if calls["n"] == 1 else oracle(req)

    repo, fid, ctx, llm = setup(tmp_path, docs, flaky)
    attempts = pipeline.fix_with_model(repo, fid, ctx, FAST)
    assert [a.outcome for a in attempts] == ["invalid", "proposed"] and attempts[-1].passed
    assert "not valid JSON" in llm.calls[1].messages[-1]["content"]


def test_no_documentation_ends_the_loop_without_calling_the_model(tmp_path):
    repo = app_repo(tmp_path)
    findings, _ = pipeline.scan(repo)
    fid = next(f.id for f in findings if f.rule_id == "STD-0003")
    llm = PolicyLLM(oracle)
    attempts = pipeline.fix_with_model(
        repo, fid, ModelContext(Index(":memory:"), HashEmbedder(), llm, "m"), FAST
    )
    assert [a.outcome for a in attempts] == ["no_evidence"] and llm.calls == []


def test_a_missing_recording_is_a_clear_error_not_a_silent_skip(tmp_path, docs):
    repo = app_repo(tmp_path)
    findings, _ = pipeline.scan(repo)
    fid = findings[0].id
    idx, emb = docs
    with pytest.raises(ReplayMiss):
        pipeline.fix_with_model(
            repo, fid, ModelContext(idx, emb, ReplayLLM(tmp_path / "x"), "m"), FAST
        )


def test_a_model_fix_is_a_normal_proposal_for_approval_and_apply(tmp_path, docs):
    """Nothing about the human gate depends on WHO wrote the fix."""
    from tests.helpers import git_init

    repo, fid, ctx, _ = setup(tmp_path / "r", docs, oracle)
    git_init(repo)
    pipeline.fix_with_model(repo, fid, ctx, FAST)
    assert pipeline.apply_fix  # imported
    with pytest.raises(pipeline.DepfixError, match="must be approved"):
        pipeline.apply_fix(repo, fid, execute=True)
    assert pipeline.approve(repo, fid, lambda req: req.proposal.kind == "llm") is not None
    assert pipeline.apply_fix(repo, fid, execute=True).applied


def test_explain_cites_sources_with_licences_when_an_index_exists(tmp_path, docs):
    repo, fid, _, _ = setup(tmp_path, docs, oracle)
    idx, emb = docs
    text = pipeline.explain_text(repo, fid, index=idx, embedder=emb)
    assert "Retrieved documentation" in text and "[fixture-python-logging#entry:" in text
    assert (
        "MIT (original text written for depfix)" in text
        and "source: https://docs.python.org" in text
    )
    assert "no documentation index" in pipeline.explain_text(repo, fid)


# ---- the CLI around it ---------------------------------------------------------------------------------


@pytest.fixture
def cli_index(tmp_path, capsys):
    path = tmp_path / "idx.db"
    args = [
        "index",
        "build",
        "--docs",
        str(DOCS / "sources.yaml"),
        "--embedder",
        "hash",
        "--index",
        str(path),
    ]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "indexed" in out and "licence MIT" in out
    return str(path)


def cli_setup(tmp_path, capsys):
    repo = app_repo(tmp_path / "repo")
    main(["scan", str(repo), "--json"])
    return repo, json.loads(capsys.readouterr().out)[0]["id"]


def test_cli_index_stats_and_eval(cli_index, capsys):
    assert main(["index", "stats", "--index", cli_index]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["chunker"] == "entry" and stats["chunks"] > 30
    assert main(["index", "eval", "--index", cli_index]) == 0
    out = capsys.readouterr().out
    assert (
        "keyword" in out
        and "vector" in out
        and "hit@4" in out
        and "anchor layer is excluded" in out
    )


def test_cli_index_refuses_to_mix_chunkers_unless_rebuilding(cli_index, capsys):
    args = [
        "index",
        "build",
        "--docs",
        str(DOCS / "sources.yaml"),
        "--embedder",
        "hash",
        "--index",
        cli_index,
    ]
    assert main([*args, "--chunker", "fixed"]) == 2
    assert "rebuild" in capsys.readouterr().err
    assert main([*args, "--chunker", "fixed", "--rebuild"]) == 0


def test_cli_propose_with_a_model_then_fix_then_explain(tmp_path, cli_index, capsys, monkeypatch):
    repo, fid = cli_setup(tmp_path, capsys)
    monkeypatch.setattr(cli_mod, "_llm", lambda a: PolicyLLM(oracle))
    assert main(["propose", fid, "--repo", str(repo), "--mode", "llm", "--index", cli_index]) == 0
    out = capsys.readouterr().out
    assert (
        "outcome: proposed" in out and "evidence [fixture-python-logging" in out and "cites:" in out
    )
    fix = [
        "fix",
        fid,
        "--repo",
        str(repo),
        "--index",
        cli_index,
        "--runner",
        "local",
        "--allow-local-exec",
    ]
    assert main(fix) == 0
    out = capsys.readouterr().out
    assert "attempt 1: proposed" in out and "VERIFIED" in out and "next: depfix approve" in out
    assert main(["explain", fid, "--repo", str(repo), "--index", cli_index]) == 0
    assert "Retrieved documentation" in capsys.readouterr().out


def test_cli_fix_reports_a_model_that_cannot_be_fixed(tmp_path, cli_index, capsys, monkeypatch):
    repo, fid = cli_setup(tmp_path, capsys)
    monkeypatch.setattr(cli_mod, "_llm", lambda a: PolicyLLM(stubborn))
    args = ["fix", fid, "--repo", str(repo), "--index", cli_index, "--retries", "1",
            "--runner", "local", "--allow-local-exec"]  # fmt: skip
    assert main(args) == 1
    out = capsys.readouterr().out
    assert out.count("attempt ") == 2 and "NOT FIXED" in out


def test_cli_auto_mode_uses_the_template_for_template_rules(tmp_path, capsys, monkeypatch):
    repo, fid = cli_setup(tmp_path / "x", capsys)
    monkeypatch.setattr(
        cli_mod, "_llm", lambda a: (_ for _ in ()).throw(AssertionError("model used"))
    )
    assert main(["propose", fid, "--repo", str(repo)]) == 0
    assert "logging.warning" in capsys.readouterr().out


def test_cli_without_an_index_explains_how_to_build_one(tmp_path, capsys):
    repo, fid = cli_setup(tmp_path / "y", capsys)
    assert (
        main(
            [
                "propose",
                fid,
                "--repo",
                str(repo),
                "--mode",
                "llm",
                "--index",
                str(tmp_path / "none.db"),
            ]
        )
        == 2
    )
    assert "depfix index build" in capsys.readouterr().err


def test_cli_llm_check_reports_status_and_the_fix_command(capsys):
    def handler(method, path, body):
        if path == "/api/tags":
            return 200, {"models": [{"name": "nomic-embed-text:latest"}]}
        return 404, {}

    srv = FakeOllama(handler)
    try:
        assert main(["llm", "check", "--ollama-url", srv.url]) == 1
        out = capsys.readouterr().out
        assert "[ok  ] Ollama is reachable" in out and "ollama pull qwen2.5-coder:7b" in out
    finally:
        srv.stop()


def test_cli_sandbox_commands_explain_when_docker_is_not_running(capsys, monkeypatch):
    monkeypatch.setattr(cli_mod, "docker_available", lambda: False)
    for action in ("build", "check"):
        assert main(["sandbox", action]) == 2
        assert "Docker is not running" in capsys.readouterr().err
