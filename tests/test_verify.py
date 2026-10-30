"""The verifier is tested like production code: a corpus of deliberately BAD patches, each of
which it must reject, plus one good patch it must accept."""

import hashlib
import os
import sys
from pathlib import Path

import pytest

from depfix.fixer import propose_template
from depfix.verify import VerifyConfig, run_limited, sandbox_env, verify_proposal
from tests.helpers import APP, RULES, app_repo, find_one, proposal_for


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return app_repo(tmp_path)


@pytest.fixture
def finding():
    return find_one("src/app.py", APP, "STD-0003")


def verdict(repo, finding, proposal, tests=True):
    v = verify_proposal(repo, finding, proposal, RULES, VerifyConfig(run_tests=tests, timeout_s=60))
    return v, {c.name: c for c in v.checks}


GOOD = APP.replace("logging.warn(", "logging.warning(")


def test_good_patch_is_accepted_with_high_confidence(repo, finding):
    res = propose_template(repo, finding, RULES.by_id["STD-0003"])
    v, checks = verdict(repo, finding, res.proposal)
    assert v.passed and v.confidence == "high", [(c.name, c.status, c.detail) for c in v.checks]
    assert [c.name for c in v.checks] == [
        "diff_scope",
        "applies",
        "parses",
        "finding_gone",
        "diff_minimal",
        "tests",
    ]


def test_edit_to_another_file_is_rejected(repo, finding):
    p = proposal_for(finding, "x\n", "y\n", file="tests/test_app.py")
    v, checks = verdict(repo, finding, p)
    assert not v.passed and checks["diff_scope"].status == "failed"


def test_unrelated_edit_in_the_same_file_is_rejected(repo, finding):
    p = proposal_for(finding, APP, GOOD.replace("return 1", "return 2"))
    v, checks = verdict(repo, finding, p)
    assert not v.passed and checks["diff_minimal"].status == "failed"
    assert "outside the finding" in checks["diff_minimal"].detail


def test_patch_that_leaves_the_deprecation_in_place_is_rejected(repo, finding):
    p = proposal_for(finding, APP, APP.replace("hi %s", "hello %s"))
    v, checks = verdict(repo, finding, p)
    assert not v.passed and checks["finding_gone"].status == "failed"


def test_syntax_error_is_rejected(repo, finding):
    p = proposal_for(
        finding, APP, APP.replace("logging.warn('hi %s', name)", "logging.warning('hi %s', name")
    )
    v, checks = verdict(repo, finding, p)
    assert not v.passed and checks["parses"].status == "failed"
    assert "tests" not in checks  # short-circuit: no point running tests


def test_patch_that_breaks_the_tests_is_rejected(repo, finding):
    p = proposal_for(finding, APP, APP.replace("logging.warn(", "(lambda *a: 1 / 0)("))
    v, checks = verdict(repo, finding, p)
    assert not v.passed and checks["tests"].status == "failed"
    assert checks["finding_gone"].status == "passed"


def test_patch_that_introduces_another_deprecated_call_is_rejected(repo, finding):
    p = proposal_for(finding, APP, APP.replace("logging.warn(", "threading.activeCount("))
    v, checks = verdict(repo, finding, p)
    assert not v.passed and checks["finding_gone"].status == "failed"
    assert "STD-0005" in checks["finding_gone"].detail


def test_oversized_replacement_is_rejected(repo, finding):
    big = "\n".join(f"    x{i} = {i}" for i in range(10))
    p = proposal_for(finding, APP, GOOD.replace("    logging.warning('hi %s', name)", big))
    v, checks = verdict(repo, finding, p)
    assert not v.passed and checks["diff_minimal"].status == "failed"


def test_diff_with_wrong_context_does_not_apply(repo, finding):
    # the line right below the fix is shown as diff context; make it disagree with the real file
    wrong = "return f'hello {name}'"
    p = proposal_for(
        finding, APP.replace(wrong, "return 'zzz'"), GOOD.replace(wrong, "return 'zzz'")
    )
    v, checks = verdict(repo, finding, p)
    assert not v.passed and checks["applies"].status == "failed"


def test_path_traversal_header_is_rejected(repo, finding):
    p = proposal_for(finding, APP, GOOD)
    bad = p.diff.replace("--- a/src/app.py", "--- a/../evil.py").replace(
        "+++ b/src/app.py", "+++ b/../evil.py"
    )
    p = p.model_copy(update={"diff": bad, "diff_sha256": hashlib.sha256(bad.encode()).hexdigest()})
    v, checks = verdict(repo, finding, p)
    assert not v.passed and checks["diff_scope"].status == "failed"


def test_rename_marker_is_rejected(repo, finding):
    p = proposal_for(finding, APP, GOOD)
    bad = "rename from src/app.py\nrename to src/evil.py\n" + p.diff
    p = p.model_copy(update={"diff": bad, "diff_sha256": hashlib.sha256(bad.encode()).hexdigest()})
    v, checks = verdict(repo, finding, p)
    assert not v.passed and checks["diff_scope"].status == "failed"


def test_tampered_diff_is_rejected(repo, finding):
    p = proposal_for(finding, APP, GOOD)
    p = p.model_copy(update={"diff": p.diff.replace("warning", "warnin")})
    v, checks = verdict(repo, finding, p)
    assert not v.passed and "hash" in checks["diff_scope"].detail


def test_empty_diff_is_rejected(repo, finding):
    p = proposal_for(finding, APP, APP)
    v, _ = verdict(repo, finding, p)
    assert not v.passed


def test_file_edited_after_proposal_is_rejected(repo, finding):
    res = propose_template(repo, finding, RULES.by_id["STD-0003"])
    (repo / "src/app.py").write_text(APP + "# edited later\n")
    v, checks = verdict(repo, finding, res.proposal)
    assert not v.passed and checks["applies"].status == "failed"


def test_repo_without_tests_gets_low_confidence(tmp_path):
    repo = app_repo(tmp_path, with_tests=False)
    finding = find_one("src/app.py", APP, "STD-0003")
    res = propose_template(repo, finding, RULES.by_id["STD-0003"])
    v, checks = verdict(repo, finding, res.proposal)
    assert v.passed and v.confidence == "low" and checks["tests"].status == "skipped"


def test_verification_never_modifies_the_real_repo(repo, finding):
    before = (repo / "src/app.py").read_bytes()
    res = propose_template(repo, finding, RULES.by_id["STD-0003"])
    verdict(repo, finding, res.proposal)
    assert (repo / "src/app.py").read_bytes() == before
    assert not (repo / ".pytest_cache").exists()


# ---- runner safety -----------------------------------------------------------------------------


def test_timeouts_kill_the_process_group(tmp_path):
    r = run_limited(
        [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 1, sandbox_env(tmp_path)
    )
    assert r.timed_out


def test_secrets_in_the_parent_environment_do_not_reach_tests(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPFIX_SECRET", "hunter2")
    r = run_limited(
        [sys.executable, "-c", "import os; print(os.environ.get('DEPFIX_SECRET'))"],
        tmp_path,
        10,
        sandbox_env(tmp_path),
    )
    assert r.output.strip() == "None" and "DEPFIX_SECRET" in os.environ
