"""The drop-2 verifier checks: provenance, citation, lint, types and the rule example."""

from pathlib import Path

from depfix.models import Proposal
from depfix.rules import build_ruleset
from depfix.runner import LocalRunner
from depfix.verify import (
    VerifyConfig,
    detect_tools,
    new_names,
    tool_issues,
    verify_proposal,
)
from tests.helpers import (
    APP,
    PYTEST_INI,
    RULES,
    TEST_APP,
    find_one,
    proposal_for,
    write_repo,
)

RUFF = '[tool.ruff.lint]\nselect = ["E", "F"]\n'
MYPY = "[tool.mypy]\ncheck_untyped_defs = true\n"
GOOD = APP.replace("logging.warn(", "logging.warning(")
LINE = "    logging.warning('hi %s', name)"


def repo_with(tmp_path: Path, pyproject: str = "") -> Path:
    files = {"src/app.py": APP, "tests/test_app.py": TEST_APP, "pytest.ini": PYTEST_INI}
    if pyproject:
        files["pyproject.toml"] = pyproject
    return write_repo(tmp_path, files)


def run(repo: Path, p: Proposal, rules=RULES, **cfg):
    f = find_one("src/app.py", APP, "STD-0003")
    v = verify_proposal(repo, f, p, rules, VerifyConfig(runner=LocalRunner(), **cfg))
    return v, {c.name: c for c in v.checks}


def model_fix(new_text: str, evidence: str, ids=("c1",)) -> Proposal:
    f = find_one("src/app.py", APP, "STD-0003")
    return proposal_for(f, APP, new_text).model_copy(
        update={"kind": "llm", "citations": list(ids), "evidence": {"c1": evidence}}
    )


# ---- provenance: template fixes --------------------------------------------------------------


def test_a_doctored_template_diff_cannot_pass_as_the_rules_fix(tmp_path):
    repo = repo_with(tmp_path)
    f = find_one("src/app.py", APP, "STD-0003")
    sneaky = GOOD.replace(LINE, LINE + "  # sneaky")
    v, checks = run(repo, proposal_for(f, APP, sneaky))
    assert checks["finding_gone"].status == "passed" and checks["diff_minimal"].status == "passed"
    assert checks["tests"].status == "passed"
    assert checks["provenance"].status == "failed" and not v.passed


# ---- provenance: model fixes (groundedness without a judge model) -----------------------------


def test_a_model_fix_that_cites_a_supporting_source_passes(tmp_path):
    v, checks = run(repo_with(tmp_path), model_fix(GOOD, "Use logging.warning instead of warn"))
    assert checks["provenance"].status == "passed" and v.passed


def test_a_model_fix_with_no_citation_fails(tmp_path):
    p = model_fix(GOOD, "x").model_copy(update={"citations": []})
    v, checks = run(repo_with(tmp_path), p)
    assert (
        checks["provenance"].status == "failed" and "cites no source" in checks["provenance"].detail
    )
    assert not v.passed


def test_citing_a_chunk_that_does_not_exist_fails(tmp_path):
    p = model_fix(GOOD, "warning").model_copy(update={"citations": ["nope"]})
    _, checks = run(repo_with(tmp_path), p)
    assert "not found" in checks["provenance"].detail


def test_a_name_the_sources_never_mention_fails(tmp_path):
    bad = APP.replace("logging.warn(", "logging.fatal(")
    v, checks = run(repo_with(tmp_path), model_fix(bad, "Use logging.warning instead of warn"))
    assert checks["provenance"].status == "failed" and "fatal" in checks["provenance"].detail
    assert not v.passed


def test_builtins_are_not_treated_as_new_unsupported_names():
    assert new_names("x = 1\n", "x = list()\n") == set()
    assert new_names("x = 1\n", "x = concat([])\n") == {"concat"}


# ---- lint and types: judged by the project's OWN configuration --------------------------------


def test_projects_that_do_not_configure_a_tool_are_not_judged_by_it(tmp_path):
    _, checks = run(repo_with(tmp_path), model_fix(GOOD, "warning"))
    assert checks["lint"].status == "skipped" and checks["types"].status == "skipped"


def test_preexisting_lint_problems_are_not_blamed_on_the_patch(tmp_path):
    repo = repo_with(tmp_path, RUFF)  # APP has an unused `import threading` (F401) already
    v, checks = run(repo, model_fix(GOOD, "warning"))
    assert checks["lint"].status == "passed", checks["lint"].detail
    assert v.passed


def test_a_patch_that_adds_a_lint_problem_is_rejected(tmp_path):
    repo = repo_with(tmp_path, RUFF)
    bad = GOOD.replace(LINE, LINE + "; import os")  # same line, so it is still 'minimal'
    v, checks = run(repo, model_fix(bad, "warning"))
    assert checks["diff_minimal"].status == "passed"
    assert checks["lint"].status == "failed" and "ruff" in checks["lint"].detail
    assert not v.passed


def test_a_patch_that_adds_a_type_error_is_rejected(tmp_path):
    repo = repo_with(tmp_path, MYPY)
    bad = GOOD.replace(LINE, LINE + "; y: int = 'a'")
    v, checks = run(repo, model_fix(bad, "warning y"))
    assert checks["types"].status == "failed" and "mypy" in checks["types"].detail
    assert not v.passed


def test_static_checks_can_be_switched_off(tmp_path):
    _, checks = run(repo_with(tmp_path, RUFF), model_fix(GOOD, "warning"), run_static=False)
    assert "lint" not in checks and "types" not in checks


def test_tool_detection(tmp_path):
    assert detect_tools(write_repo(tmp_path / "a", {"x.py": ""})) == set()
    assert detect_tools(write_repo(tmp_path / "b", {"ruff.toml": ""})) == {"ruff"}
    assert detect_tools(write_repo(tmp_path / "c", {"pyproject.toml": RUFF + MYPY})) == {
        "ruff",
        "mypy",
    }
    assert detect_tools(write_repo(tmp_path / "d", {"setup.cfg": "[mypy]\n"})) == {"mypy"}


def test_issues_are_compared_without_line_numbers():
    a = "src/app.py:6:5: F401 `os` imported but unused\nFound 1 error.\n"
    b = "src/app.py:9:5: F401 `os` imported but unused\n"
    assert tool_issues("ruff", a) == tool_issues("ruff", b)
    m = (
        "src/app.py:6: error: Incompatible types in assignment  [assignment]\n"
        "src/app.py:6: note: x\n"
    )
    assert sum(tool_issues("mypy", m).values()) == 1  # notes are not issues


# ---- the rule's own example is re-run in the verification environment -------------------------


def test_a_rule_whose_example_is_false_blocks_verification(tmp_path):
    rule = RULES.by_id["STD-0003"]
    assert rule.example is not None
    wrong = rule.model_copy(
        update={"example": rule.example.model_copy(update={"expect": "'lies'"})}
    )
    f = find_one("src/app.py", APP, "STD-0003")
    from depfix.fixer import propose_template

    repo = repo_with(tmp_path)
    res = propose_template(repo, f, wrong)
    assert res.proposal is not None
    v, checks = run(repo, res.proposal, rules=build_ruleset([wrong]))
    assert checks["rule_example"].status == "failed" and not v.passed
