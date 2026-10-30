"""Approval and apply: the only path by which a file in your real repo can change."""

import sqlite3

import pytest

from depfix import approval as approvals
from depfix import pipeline
from depfix.apply import ApplyRefused, apply_on_branch
from depfix.models import Approval, Status
from depfix.pipeline import DepfixError
from depfix.store import Store
from tests.helpers import APP, app_repo, git, git_init, make_verified, proposal_for


def yes(_req) -> bool:
    return True


def no(_req) -> bool:
    return False


@pytest.fixture
def repo(tmp_path):
    r = app_repo(tmp_path / "repo")
    git_init(r)
    return r


@pytest.fixture
def approved(repo):
    fid = make_verified(repo)
    assert pipeline.approve(repo, fid, yes) is not None
    return repo, fid


def status(repo, fid) -> Status:
    with Store(repo) as s:
        f = s.get_finding(fid)
        assert f is not None
        return f.status


def current_branch(repo) -> str:
    return git(repo, "branch", "--show-current").stdout.strip()


# ---- approve ----------------------------------------------------------------------------------


def test_declined_approval_changes_nothing(repo):
    fid = make_verified(repo)
    assert pipeline.approve(repo, fid, no) is None
    assert status(repo, fid) is Status.VERIFIED
    with Store(repo) as s:
        assert s.get_approval(fid) is None


def test_only_verified_findings_can_be_approved(repo):
    findings, _ = pipeline.scan(repo)
    fid = next(f.id for f in findings if f.rule_id == "STD-0003")
    with pytest.raises(DepfixError, match="only a verified fix"):
        pipeline.approve(repo, fid, yes)
    pipeline.propose(repo, fid)
    with pytest.raises(DepfixError, match="only a verified fix"):
        pipeline.approve(repo, fid, yes)


def test_approval_is_refused_if_the_file_changed_after_verification(repo):
    fid = make_verified(repo)
    (repo / "src/app.py").write_text(APP + "# edited\n")
    with pytest.raises(DepfixError, match="changed since"):
        pipeline.approve(repo, fid, yes)


def test_approve_shows_the_confirm_callback_the_evidence(repo):
    fid = make_verified(repo)
    seen = []
    pipeline.approve(repo, fid, lambda req: seen.append(req) or False)
    req = seen[0]
    assert req.finding.id == fid and "logging.warning" in req.proposal.diff
    assert req.verification.passed and req.rule.id == "STD-0003"


# ---- apply: the happy path and the dry run ----------------------------------------------------


def test_dry_run_is_the_default_and_changes_nothing(approved):
    repo, fid = approved
    before = (repo / "src/app.py").read_bytes()
    out = pipeline.apply_fix(repo, fid)
    assert out.applied is False and out.steps
    assert (repo / "src/app.py").read_bytes() == before
    assert current_branch(repo) == "main"
    assert git(repo, "branch", "--list", out.branch).stdout.strip() == ""
    assert status(repo, fid) is Status.APPROVED


def test_execute_applies_on_a_new_branch_and_leaves_main_alone(approved):
    repo, fid = approved
    out = pipeline.apply_fix(repo, fid, execute=True)
    assert out.applied and out.branch == f"depfix/{fid}"
    assert current_branch(repo) == out.branch
    assert "logging.warning(" in (repo / "src/app.py").read_text()
    assert "logging.warn(" in git(repo, "show", "main:src/app.py").stdout  # main is untouched
    msg = git(repo, "log", "-1", "--format=%B").stdout
    assert "STD-0003" in msg and "docs.python.org" in msg and "Approved by a person" in msg
    assert git(repo, "status", "--porcelain").stdout.strip() == ""
    assert status(repo, fid) is Status.APPLIED
    with Store(repo) as s:
        a = s.get_approval(fid)
        assert a is not None and a.used


def test_changes_are_trivially_undone(approved):
    repo, fid = approved
    out = pipeline.apply_fix(repo, fid, execute=True)
    assert git(repo, "switch", out.start_branch).returncode == 0
    assert git(repo, "branch", "-D", out.branch).returncode == 0
    assert "logging.warn(" in (repo / "src/app.py").read_text()


def test_an_approval_cannot_be_used_twice(approved):
    repo, fid = approved
    pipeline.apply_fix(repo, fid, execute=True)
    with pytest.raises(DepfixError, match="must be approved"):
        pipeline.apply_fix(repo, fid, execute=True)


# ---- apply: refusing without a valid approval --------------------------------------------------


def test_apply_without_approval_is_refused(repo):
    fid = make_verified(repo)  # verified, but no human said yes
    with pytest.raises(DepfixError, match="must be approved"):
        pipeline.apply_fix(repo, fid, execute=True)
    assert "logging.warn(" in (repo / "src/app.py").read_text()


def test_expired_approval_is_revoked(approved):
    repo, fid = approved
    with Store(repo) as s:
        s.save_approval(approvals.issue(fid, pipeline._sha(s.get_proposal(fid).diff), ttl_s=-5))  # type: ignore[union-attr]
    with pytest.raises(DepfixError, match="expired"):
        pipeline.apply_fix(repo, fid, execute=True)
    assert status(repo, fid) is Status.VERIFIED  # a person must look again
    with Store(repo) as s:
        assert s.get_approval(fid) is None


def test_forged_approval_row_is_refused(approved):
    repo, fid = approved
    with Store(repo) as s:
        real = s.get_approval(fid)
        assert real is not None
        s.save_approval(Approval(**{**real.model_dump(), "mac": "f" * 64}))
    with pytest.raises(DepfixError, match="integrity"):
        pipeline.apply_fix(repo, fid, execute=True)
    assert "logging.warn(" in (repo / "src/app.py").read_text()


def test_diff_swapped_in_the_database_after_approval_is_refused(approved):
    repo, fid = approved
    db = sqlite3.connect(repo / ".depfix" / "state.db")
    (blob,) = db.execute("SELECT data FROM proposals WHERE finding_id=?", (fid,)).fetchone()
    db.execute(
        "UPDATE proposals SET data=? WHERE finding_id=?",
        (blob.replace("logging.warning", "logging.warnin"), fid),
    )
    db.commit()
    db.close()
    with pytest.raises(DepfixError, match="diff"):
        pipeline.apply_fix(repo, fid, execute=True)
    assert "logging.warn(" in (repo / "src/app.py").read_text()


def test_file_edited_after_approval_is_refused(approved):
    repo, fid = approved
    (repo / "src/app.py").write_text(APP + "# edited\n")
    with pytest.raises(DepfixError, match="changed since"):
        pipeline.apply_fix(repo, fid, execute=True)


def test_re_proposing_after_approval_is_not_possible(approved):
    repo, fid = approved
    with pytest.raises(DepfixError, match="cannot be proposed again"):
        pipeline.propose(repo, fid)


# ---- apply: git preconditions ------------------------------------------------------------------


def refused(repo, fid, match):
    with pytest.raises(DepfixError, match=match):
        pipeline.apply_fix(repo, fid, execute=True)
    assert "logging.warn(" in (repo / "src/app.py").read_text()


def test_uncommitted_changes_to_the_file_are_refused(tmp_path):
    repo = app_repo(tmp_path / "repo")
    git_init(repo)
    (repo / "src/app.py").write_text("# local edit\n" + APP)  # dirty BEFORE scanning
    fid = make_verified(repo)
    pipeline.approve(repo, fid, yes)
    refused(repo, fid, "uncommitted")


def test_untracked_file_is_refused(tmp_path):
    repo = app_repo(tmp_path / "repo")
    git_init(repo)
    git(repo, "rm", "-q", "--cached", "src/app.py")
    git(repo, "commit", "-q", "-m", "untrack")
    fid = make_verified(repo)
    pipeline.approve(repo, fid, yes)
    refused(repo, fid, "not tracked")


def test_existing_branch_is_refused(approved):
    repo, fid = approved
    git(repo, "branch", f"depfix/{fid}")
    refused(repo, fid, "already exists")


def test_detached_head_is_refused(approved):
    repo, fid = approved
    git(repo, "checkout", "-q", "--detach")
    refused(repo, fid, "detached")


def test_non_git_folder_is_refused(tmp_path):
    repo = app_repo(tmp_path / "repo")
    fid = make_verified(repo)
    pipeline.approve(repo, fid, yes)
    refused(repo, fid, "not a git repository")


def test_project_in_a_subfolder_of_a_larger_repo_is_refused(tmp_path):
    top = tmp_path / "mono"
    proj = app_repo(top / "proj")
    git_init(top)
    fid = make_verified(proj)
    pipeline.approve(proj, fid, yes)
    refused(proj, fid, "root of the git repository")


# ---- apply_on_branch: cleanup on failure -------------------------------------------------------


def test_failed_apply_rolls_back_completely(repo):
    from tests.helpers import find_one

    f = find_one("src/app.py", APP, "STD-0003")
    wrong = "return f'hello {name}'"
    bad = proposal_for(
        f,
        APP.replace(wrong, "return 'zzz'"),
        APP.replace(wrong, "return 'zzz'").replace("logging.warn(", "logging.warning("),
    )
    with pytest.raises(ApplyRefused, match="could not apply"):
        apply_on_branch(repo, f, bad, "msg", "main")
    assert current_branch(repo) == "main"
    assert git(repo, "branch", "--list", f"depfix/{f.id}").stdout.strip() == ""
    assert (repo / "src/app.py").read_text() == APP
    assert git(repo, "status", "--porcelain", "--", "src/app.py").stdout.strip() == ""


def test_failed_commit_is_reported_and_leaves_the_change_staged(repo):
    from depfix.fixer import propose_template
    from tests.helpers import RULES, find_one

    hooks = repo / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    git(repo, "config", "core.hooksPath", str(hooks))
    f = find_one("src/app.py", APP, "STD-0003")
    res = propose_template(repo, f, RULES.by_id["STD-0003"])
    assert res.proposal is not None
    with pytest.raises(ApplyRefused, match="staged on"):
        apply_on_branch(repo, f, res.proposal, "msg", "main")
    assert current_branch(repo) == f"depfix/{f.id}"
    assert "logging.warning(" in (repo / "src/app.py").read_text()
