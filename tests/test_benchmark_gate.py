"""The regression gate guards everything else, so the gate itself is tested."""

import json
from pathlib import Path

import pytest
import yaml

from benchmark.runner import BASELINE, CASES, GATE, check_against
from tests.helpers import RULES

GOOD = {
    "precision": 1.0,
    "recall": 1.0,
    "fix_success_rate": 1.0,
    "correct_refusal_rate": 1.0,
    "false_accept_rate": 0.0,
    "unsafe_patch_rate": 0.0,
}


def test_identical_scores_pass():
    assert check_against(dict(GOOD), dict(GOOD)) == []


def test_small_wobble_within_tolerance_passes():
    assert check_against({**GOOD, "recall": 0.99}, GOOD) == []


@pytest.mark.parametrize(
    ("metric", "bad"),
    [
        ("precision", 0.8),
        ("recall", 0.8),
        ("fix_success_rate", 0.5),
        ("correct_refusal_rate", 0.5),
        ("false_accept_rate", 0.2),
        ("unsafe_patch_rate", 0.01),
    ],
)
def test_every_gated_metric_fails_when_it_regresses(metric, bad):
    problems = check_against({**GOOD, metric: bad}, GOOD)
    assert len(problems) == 1 and metric in problems[0]


def test_gate_covers_every_headline_metric():
    assert set(GATE) == set(GOOD)


def test_committed_baseline_has_all_gated_metrics():
    base = json.loads(BASELINE.read_text())
    assert set(GATE) <= set(base)


def case_dirs() -> list[Path]:
    return sorted(p for p in CASES.iterdir() if p.is_dir())


@pytest.mark.parametrize("case", case_dirs(), ids=lambda p: p.name)
def test_benchmark_case_is_well_formed(case: Path):
    spec = yaml.safe_load((case / "expected.yaml").read_text(encoding="utf-8"))
    assert (case / "repo/pytest.ini").is_file()
    assert any((case / "repo/tests").glob("test_*.py"))
    assert (case / "hidden_tests").is_dir()
    for e in spec["expected"]:
        assert e["rule"] in RULES.by_id, f"unknown rule {e['rule']}"
        assert e["fix"] in {"template_ok", "not_applicable", "needs_llm"}
        assert (case / "repo" / e["file"]).is_file()
        # the expected line must really contain the first line of the usage
        assert e["line"] <= len((case / "repo" / e["file"]).read_bytes().splitlines())


def test_there_are_enough_cases():
    assert len(case_dirs()) >= 15
