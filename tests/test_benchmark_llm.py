"""The model benchmark's own machinery, exercised with simulated models."""

import json

import pytest

from benchmark.llm_runner import (
    GATE,
    build_docs_index,
    check_against,
    main,
    make_llm,
    run_case,
    summarise,
)
from benchmark.policies import POLICIES
from benchmark.runner import CASES
from depfix.llm import PolicyLLM
from depfix.llm_fixer import DEFAULT_OPTIONS, ModelContext
from depfix.rules import load_rules

RULES = load_rules()
CASE = CASES / "std_logging_warn"


def go(policy: str, case=CASE, retries: int = 2, chunker: str = "entry"):
    idx, emb = build_docs_index(chunker)
    import sys

    ctx = ModelContext(idx, emb, PolicyLLM(POLICIES[policy]), "sim", dict(DEFAULT_OPTIONS))
    return run_case(case, RULES, ctx, sys.executable, retries, 0)


def test_a_perfect_model_passes_the_hidden_tests_first_try():
    (t,) = go("oracle")
    assert t.success and t.first_try and t.verified and t.hidden_pass and not t.unsafe
    assert len(t.attempts) == 1


def test_a_sloppy_model_needs_the_retry_and_is_counted_as_not_first_try():
    (t,) = go("sloppy")
    assert t.success and not t.first_try and len(t.attempts) == 2
    assert t.attempts[0]["failed_checks"] and t.attempts[1]["passed"]


def test_a_stubborn_model_fails_without_ever_being_falsely_accepted():
    (t,) = go("stubborn")
    assert not t.success and not t.verified and len(t.attempts) == 3
    s = summarise([t], 1)
    assert (
        s["fix_success_rate"] == 0.0 and s["false_accept_rate"] == 0.0 and s["consistent_fail"] == 1
    )


def test_a_refusing_model_is_scored_as_a_failure_not_a_crash():
    (t,) = go("refuser")
    assert not t.success and [a["outcome"] for a in t.attempts] == ["refused"]
    assert summarise([t], 1)["invalid_output_rate"] == 1.0


def test_the_fixed_chunker_runs_end_to_end_too():
    (t,) = go("oracle", chunker="fixed")
    assert t.success


def test_summary_metrics_and_flaky_split():
    a, b = go("oracle")[0], go("stubborn")[0]
    s = summarise([a, b], 2)  # same (case, rule) twice: one pass, one fail = flaky
    assert s["flaky"] == 1 and s["consistent_pass"] == 0 and s["fix_success_rate"] == 0.5
    assert s["citation_failure_rate"] == 0.75  # 3 of the 4 proposals (all of stubborn's) failed


def test_the_gate_catches_a_worse_model_and_ignores_noise():
    base = {
        "fix_success_rate": 0.9,
        "first_try_rate": 0.8,
        "false_accept_rate": 0.0,
        "unsafe_patch_rate": 0.0,
    }
    assert check_against(dict(base), base) == []
    assert check_against({**base, "fix_success_rate": 0.87}, base) == []  # within tolerance
    for metric, worse in (
        ("fix_success_rate", 0.5),
        ("first_try_rate", 0.5),
        ("false_accept_rate", 0.1),
        ("unsafe_patch_rate", 0.1),
    ):
        assert len(check_against({**base, metric: worse}, base)) == 1
    assert set(GATE) == set(base)


def test_make_llm_validates_its_spec(tmp_path):
    assert make_llm("policy:oracle", "http://localhost:11434")
    with pytest.raises(SystemExit):
        make_llm("bogus", "http://localhost:11434")


def test_record_then_replay_reproduces_the_same_result_without_a_model(tmp_path, capsys):
    rec = tmp_path / "r.jsonl"
    base = ["--only", "std_logging_warn", "--model", "sim"]
    # `record:` normally wraps Ollama; here we record a policy by writing through RecordingLLM directly
    from depfix.llm import RecordingLLM

    idx, emb = build_docs_index("entry")
    import sys

    ctx = ModelContext(
        idx, emb, RecordingLLM(PolicyLLM(POLICIES["oracle"]), rec), "sim", dict(DEFAULT_OPTIONS)
    )
    (t,) = run_case(CASE, RULES, ctx, sys.executable, 2, 0)
    assert t.success and rec.exists()
    base_file = tmp_path / "baseline.json"
    assert main(["--llm", f"replay:{rec}", *base, "--write-baseline", str(base_file)]) == 0
    assert json.loads(base_file.read_text())["fix_success_rate"] == 1.0
    assert main(["--llm", f"replay:{rec}", *base, "--check", str(base_file)]) == 0
    assert "regression gate: OK" in capsys.readouterr().out


def test_a_replay_with_changed_prompts_fails_loudly(tmp_path):
    from depfix.llm import ReplayMiss

    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    with pytest.raises(ReplayMiss):
        main(["--llm", f"replay:{empty}", "--only", "std_logging_warn"])
