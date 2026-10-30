import io
import json

import pytest

import depfix.cli as cli_mod
from depfix.cli import main
from depfix.runner import RunnerError
from tests.helpers import app_repo, git_init


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def scan_id(tmp_path, capsys) -> str:
    assert main(["scan", str(tmp_path), "--json"]) == 0
    return str(json.loads(capsys.readouterr().out)[0]["id"])


def test_end_to_end_scan_propose_verify_report_trace(tmp_path, capsys):
    app_repo(tmp_path)
    fid = scan_id(tmp_path, capsys)

    assert main(["propose", fid, "--repo", str(tmp_path)]) == 0
    assert "logging.warning" in capsys.readouterr().out

    local = ["verify", fid, "--repo", str(tmp_path), "--runner", "local"]
    assert main(local) == 2  # local needs the explicit acknowledgement
    assert "WITHOUT network" in capsys.readouterr().err

    assert main([*local, "--allow-local-exec"]) == 0
    out = capsys.readouterr().out
    assert "VERIFIED" in out and "runner: local" in out

    assert main(["report", "--repo", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["by_status"] == {"verified": 1}

    assert main(["trace", fid, "--repo", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    for stage in (
        "1. SCAN",
        "2. RULE",
        "3. NAME RESOLUTION",
        "4. PROPOSE",
        "5. VERIFY",
        "6. APPROVAL",
    ):
        assert stage in out
    assert (tmp_path / "src/app.py").read_text().count("logging.warn(") == 1  # nothing applied


def test_docker_is_the_default_runner_and_failures_are_clean(tmp_path, capsys, monkeypatch):
    app_repo(tmp_path)
    fid = scan_id(tmp_path, capsys)
    main(["propose", fid, "--repo", str(tmp_path)])
    capsys.readouterr()

    def no_docker(image=None):
        raise RunnerError("Docker was not found. Install Docker Desktop")

    monkeypatch.setattr(cli_mod, "make_docker_runner", no_docker)
    assert main(["verify", fid, "--repo", str(tmp_path)]) == 2
    assert "Docker was not found" in capsys.readouterr().err


def test_cannot_verify_before_proposing(tmp_path, capsys):
    app_repo(tmp_path)
    fid = scan_id(tmp_path, capsys)
    args = ["verify", fid, "--repo", str(tmp_path), "--runner", "local", "--allow-local-exec"]
    assert main(args) == 2
    assert "propose a fix first" in capsys.readouterr().err


def test_unknown_finding_is_a_clean_error(tmp_path, capsys):
    app_repo(tmp_path)
    assert main(["propose", "nope", "--repo", str(tmp_path)]) == 2
    assert "no finding" in capsys.readouterr().err


def verified_repo(tmp_path, capsys, with_tests=True):
    repo = app_repo(tmp_path / "repo", with_tests=with_tests)
    git_init(repo)
    fid = scan_id(repo, capsys)
    main(["propose", fid, "--repo", str(repo)])
    assert (
        main(["verify", fid, "--repo", str(repo), "--runner", "local", "--allow-local-exec"]) == 0
    )
    capsys.readouterr()
    return repo, fid


def test_approve_refuses_without_an_interactive_terminal(tmp_path, capsys, monkeypatch):
    repo, fid = verified_repo(tmp_path, capsys)
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))  # a script piping 'y' must not work
    assert main(["approve", fid, "--repo", str(repo)]) == 2
    assert "interactive terminal" in capsys.readouterr().err


def test_full_human_flow_approve_then_dry_run_then_apply(tmp_path, capsys, monkeypatch):
    repo, fid = verified_repo(tmp_path, capsys)
    monkeypatch.setattr("sys.stdin", FakeTTY("y\n"))
    assert main(["approve", fid, "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "logging.warning" in out and "confidence high" in out and "approved until" in out

    assert main(["apply", fid, "--repo", str(repo)]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert "logging.warn(" in (repo / "src/app.py").read_text()

    assert main(["apply", fid, "--repo", str(repo), "--execute"]) == 0
    out = capsys.readouterr().out
    assert f"depfix/{fid}" in out and "To undo" in out
    assert "logging.warning(" in (repo / "src/app.py").read_text()


def test_declining_changes_nothing(tmp_path, capsys, monkeypatch):
    repo, fid = verified_repo(tmp_path, capsys)
    monkeypatch.setattr("sys.stdin", FakeTTY("n\n"))
    assert main(["approve", fid, "--repo", str(repo)]) == 1
    assert "nothing was changed" in capsys.readouterr().out
    assert main(["apply", fid, "--repo", str(repo), "--execute"]) == 2  # not approved
    assert "logging.warn(" in (repo / "src/app.py").read_text()


def test_low_confidence_requires_typing_the_finding_id(tmp_path, capsys, monkeypatch):
    repo, fid = verified_repo(tmp_path, capsys, with_tests=False)
    monkeypatch.setattr("sys.stdin", FakeTTY("y\n"))  # a casual 'y' is not enough
    assert main(["approve", fid, "--repo", str(repo)]) == 1
    assert "low confidence" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", FakeTTY(f"{fid}\n"))
    assert main(["approve", fid, "--repo", str(repo)]) == 0


@pytest.mark.parametrize("action", ["build", "check"])
def test_sandbox_commands_fail_cleanly_without_docker(action, monkeypatch, capsys):
    def no_docker(image=None):
        raise RunnerError("Docker was not found")

    monkeypatch.setattr(cli_mod, "make_docker_runner", no_docker)
    monkeypatch.setattr(
        cli_mod,
        "sandbox_build",
        lambda image=None: (
            cli_mod.Proc(1, "docker: command not found", False)
            if hasattr(cli_mod, "Proc")
            else None
        ),
    )
    if action == "check":
        assert main(["sandbox", "check"]) == 2
        assert "Docker was not found" in capsys.readouterr().err
