"""The Docker runner cannot run in every environment, so its exact command line is tested.

Every safety property of the sandbox is a flag in `build_argv`; these tests pin each one. The real
isolation is proven on a machine with Docker by `depfix sandbox check` (make smoke-docker).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import depfix.runner as runner_mod
from depfix.runner import (
    DockerRunner,
    LocalRunner,
    Proc,
    RunnerError,
    docker_available,
    image_present,
    make_docker_runner,
    sandbox_probe,
)


@pytest.fixture
def not_root(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.setattr(os, "getgid", lambda: 1000)


def argv(tmp_path: Path, cmd=None) -> list[str]:
    (tmp_path / "w").mkdir(exist_ok=True)
    (tmp_path / "s").mkdir(exist_ok=True)
    return DockerRunner().build_argv(
        cmd or ["python", "-m", "pytest", "--junitxml={scratch}/a.xml"],
        tmp_path / "w", tmp_path / "s", "depfix-test",
    )  # fmt: skip


def pair(a: list[str], flag: str) -> str:
    return a[a.index(flag) + 1]


def test_every_isolation_flag_is_present(tmp_path, not_root):
    a = argv(tmp_path)
    assert a[:3] == ["docker", "run", "--rm"]
    assert pair(a, "--network") == "none"
    assert "--read-only" in a
    assert pair(a, "--cap-drop") == "ALL"
    assert pair(a, "--security-opt") == "no-new-privileges"
    assert pair(a, "--user") == "1000:1000"
    assert pair(a, "--memory") == pair(a, "--memory-swap") == "1g"
    assert pair(a, "--cpus") == "1"
    assert pair(a, "--pids-limit") == "256"
    assert pair(a, "--tmpfs").startswith("/tmp:")


def test_only_the_work_copy_and_scratch_are_mounted(tmp_path, not_root):
    a = argv(tmp_path)
    mounts = [a[i + 1] for i, x in enumerate(a) if x == "-v"]
    assert len(mounts) == 2
    assert mounts[0].endswith(":/work:rw") and mounts[1].endswith(":/scratch:rw")
    assert str(Path.home()) not in " ".join(mounts)


def test_command_is_a_plain_argument_list_with_scratch_translated(tmp_path, not_root):
    a = argv(tmp_path)
    image_at = a.index(runner_mod.DEFAULT_IMAGE)
    assert a[image_at + 1 :] == ["python", "-m", "pytest", "--junitxml=/scratch/a.xml"]
    assert "sh" not in a and "-c" not in a  # never a shell string


def test_host_environment_is_not_forwarded(tmp_path, not_root, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    a = argv(tmp_path)
    assert "secret" not in " ".join(a)
    envs = [a[i + 1] for i, x in enumerate(a) if x == "-e"]
    assert all(
        "=" in e
        and e.split("=")[0] in {"HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED", "PYTHONUTF8"}
        for e in envs
    )


def test_running_as_root_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 0)
    with pytest.raises(RunnerError, match="root"):
        argv(tmp_path)


def test_paths_that_break_the_mount_syntax_are_refused(tmp_path, not_root):
    bad = tmp_path / "a:b"
    bad.mkdir()
    with pytest.raises(RunnerError, match="cannot mount"):
        DockerRunner().build_argv(["x"], bad, tmp_path, "n")


# ---- behaviour of run() with a faked docker ----------------------------------------------------


class FakePopen:
    def __init__(self, argv, stdout, **kw):
        self.argv, self.pid, self.returncode = argv, 1, kw.get("rc", 125)
        self.calls = FakePopen.registry
        FakePopen.registry.append(self)
        stdout.write(FakePopen.output)

    registry: list = []
    output = b""
    hang = False

    def wait(self, timeout=None):
        if FakePopen.hang and timeout is not None and not getattr(self, "killed", False):
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.fixture
def fake_docker(monkeypatch, not_root):
    FakePopen.registry, FakePopen.output, FakePopen.hang = [], b"", False
    monkeypatch.setattr(runner_mod.subprocess, "Popen", FakePopen)
    killed: list[list[str]] = []
    monkeypatch.setattr(
        runner_mod.subprocess,
        "run",
        lambda cmd, **kw: killed.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    return killed


def test_docker_exit_125_is_reported_as_a_sandbox_problem_not_a_test_failure(tmp_path, fake_docker):
    FakePopen.output = b"Unable to find image 'x'"
    with pytest.raises(RunnerError, match="could not start"):
        DockerRunner().run(["python"], workdir=tmp_path, scratch=tmp_path, timeout=5)


def test_timeout_kills_the_container_by_name(tmp_path, fake_docker):
    FakePopen.hang = True
    r = DockerRunner().run(["python"], workdir=tmp_path, scratch=tmp_path, timeout=1)
    assert r.timed_out
    started = FakePopen.registry[0].argv
    name = started[started.index("--name") + 1]
    assert ["docker", "kill", name] in fake_docker  # killing the client alone would not stop it


# ---- local runner and error messages -----------------------------------------------------------


def test_local_runner_translates_scratch(tmp_path):
    r = LocalRunner().run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", "{scratch}/x"],
        workdir=tmp_path, scratch=tmp_path, timeout=10,
    )  # fmt: skip
    assert r.output.strip() == f"{tmp_path}/x"


def test_missing_docker_gives_actionable_messages(monkeypatch):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda _: None)
    with pytest.raises(RunnerError, match="Install Docker"):
        make_docker_runner()
    monkeypatch.setattr(runner_mod.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(runner_mod, "docker_available", lambda: False)
    with pytest.raises(RunnerError, match="not running"):
        make_docker_runner()
    monkeypatch.setattr(runner_mod, "docker_available", lambda: True)
    monkeypatch.setattr(runner_mod, "image_present", lambda _: False)
    with pytest.raises(RunnerError, match="depfix sandbox build"):
        make_docker_runner()


class CannedRunner:
    name = "canned"

    def __init__(self, probe: str, timed_out: bool = True):
        self.probe, self.timed_out = probe, timed_out

    def default_python(self) -> str:
        return "python"

    def run(self, cmd, *, workdir, scratch, timeout):
        if "time.sleep" in cmd[-1]:
            return Proc(-9, "", self.timed_out)
        return Proc(0, self.probe, False)


GOOD_PROBE = "DEPFIX_PROBE " + json.dumps(
    {
        "network_blocked": True,
        "root_readonly": True,
        "secrets_visible": False,
        "host_home_visible": False,
        "uid": 1000,
    }
)


def test_probe_reports_all_properties_ok():
    rows = sandbox_probe(CannedRunner(GOOD_PROBE))
    assert len(rows) == 6 and all(ok for _, ok, _ in rows)


def test_probe_flags_each_broken_property():
    data = json.loads(GOOD_PROBE.removeprefix("DEPFIX_PROBE "))
    data.update(network_blocked=False, uid=0)
    leaky = "DEPFIX_PROBE " + json.dumps(data)
    failed = {name for name, ok, _ in sandbox_probe(CannedRunner(leaky, timed_out=False)) if not ok}
    assert failed == {
        "network is blocked",
        "runs as a non-root user",
        "a runaway process is killed on timeout",
    }


def test_probe_that_did_not_run_fails_loudly():
    rows = sandbox_probe(CannedRunner("garbage"))
    assert rows[0][0] == "probe ran" and rows[0][1] is False


# ---- real Docker: runs only where Docker and the image exist (e.g. your Mac) -------------------

DOCKER_READY = docker_available() and image_present(runner_mod.DEFAULT_IMAGE) and os.getuid() != 0


@pytest.mark.skipif(not DOCKER_READY, reason="Docker or the depfix sandbox image is not available")
def test_real_sandbox_isolation_probe():
    rows = sandbox_probe(make_docker_runner())
    assert all(ok for _, ok, _ in rows), rows
