"""Where untrusted code runs.

verify.py never starts a process for the project's code itself. It asks a Runner. Two runners:

  LocalRunner   a child process on this machine: timeout and CPU/file-size limits, stripped
                environment, but NO network or filesystem isolation. Only for code you trust.
  DockerRunner  a throw-away container: no network, read-only root filesystem, no capabilities,
                non-root user, memory/CPU/process limits, and only the work copy mounted.

Both honour the same call, so the checks do not care which one they are using.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import IO, Protocol

DEFAULT_IMAGE = "depfix-sandbox:py312"


class RunnerError(Exception):
    """The sandbox itself is unavailable or misconfigured (not a test failure)."""


@dataclass
class Proc:
    returncode: int
    output: str
    timed_out: bool


# Runs inside the child process before exec: set CPU-time and file-size limits, then become the
# real command. A launcher script (not subprocess's preexec_fn) because preexec_fn is documented
# as unsafe in programs that use threads, and the benchmark runs cases in a thread pool.
_LAUNCHER = (
    "import os, resource, sys\n"
    "t = int(sys.argv[1])\n"
    "for lim, val in ((resource.RLIMIT_CPU, t), (resource.RLIMIT_FSIZE, 200 * 1024 * 1024)):\n"
    "    try:\n"
    "        resource.setrlimit(lim, (val, val))\n"
    "    except (ValueError, OSError):\n"
    "        pass\n"
    "os.execvp(sys.argv[2], sys.argv[2:])\n"
)


def sandbox_env(home: Path) -> dict[str, str]:
    """A minimal environment: no inherited secrets (cloud keys, tokens) reach the tests."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONUTF8": "1",
    }


def _read_tail(out: IO[bytes], limit: int = 20_000) -> str:
    out.seek(0, os.SEEK_END)
    size = out.tell()
    out.seek(max(0, size - limit))
    return out.read().decode("utf-8", errors="replace")


def run_limited(cmd: list[str], cwd: Path, timeout: int, env: dict[str, str]) -> Proc:
    """Run a fixed argument list (never a shell string) with a timeout and resource limits."""
    with tempfile.TemporaryFile() as out:  # a file, so a chatty process cannot fill our memory
        proc = subprocess.Popen(
            [sys.executable, "-c", _LAUNCHER, str(timeout + 5), *cmd],
            cwd=cwd,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)  # whole group, so child processes die too
            proc.wait()
        text = _read_tail(out)
    return Proc(proc.returncode, text, timed_out)


class Runner(Protocol):
    name: str

    def default_python(self) -> str: ...

    def run(self, cmd: list[str], *, workdir: Path, scratch: Path, timeout: int) -> Proc:
        """Run `cmd` with `workdir` as the current directory.

        Any "{scratch}" inside an argument is replaced by the runner's view of the scratch
        folder: the real path locally, /scratch inside a container. Scratch is where result files
        (like the junit XML) are written, so they never appear inside the repo copy.
        """
        ...


class LocalRunner:
    name = "local"

    def default_python(self) -> str:
        return sys.executable

    def run(self, cmd: list[str], *, workdir: Path, scratch: Path, timeout: int) -> Proc:
        argv = [c.replace("{scratch}", str(scratch)) for c in cmd]
        return run_limited(argv, workdir, timeout, sandbox_env(scratch))


@dataclass
class DockerRunner:
    image: str = DEFAULT_IMAGE
    memory: str = "1g"
    cpus: str = "1"
    pids: int = 256
    name: str = "docker"

    def default_python(self) -> str:
        return "python"  # the interpreter INSIDE the image

    def build_argv(self, cmd: list[str], workdir: Path, scratch: Path, container: str) -> list[str]:
        """The exact `docker run` command line. Pure, so tests can assert every safety flag."""
        uid = os.getuid()
        if uid == 0:
            # `--user 0:0` would make the container run as root, defeating the point of --user.
            raise RunnerError("do not run depfix as root: the sandbox must not run as root either")
        # resolve(): on macOS temp dirs live under /var -> /private/var; Docker needs the real path.
        w, s = str(workdir.resolve()), str(scratch.resolve())
        for p in (w, s):
            if ":" in p or "," in p:
                raise RunnerError(f"path {p!r} contains ':' or ',', which docker -v cannot mount")
        return [
            "docker", "run", "--rm", "--name", container,
            "--network", "none",  # no network at all
            "--read-only",  # the image's filesystem cannot be modified
            "--tmpfs", "/tmp:rw,size=256m",  # the only other writable place besides the mounts
            "--memory", self.memory, "--memory-swap", self.memory,  # hard memory cap, no swap
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids),  # blocks fork bombs
            "--cap-drop", "ALL",  # no Linux capabilities
            "--security-opt", "no-new-privileges",
            "--user", f"{os.getuid()}:{os.getgid()}",  # never root
            "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "PYTHONHASHSEED=0", "-e", "PYTHONUTF8=1",
            "-v", f"{w}:/work:rw", "-v", f"{s}:/scratch:rw",  # ONLY these two folders are visible
            "-w", "/work",
            self.image,
            *[c.replace("{scratch}", "/scratch") for c in cmd],
        ]  # fmt: skip

    def run(self, cmd: list[str], *, workdir: Path, scratch: Path, timeout: int) -> Proc:
        container = f"depfix-{secrets.token_hex(6)}"
        argv = self.build_argv(cmd, workdir, scratch, container)
        with tempfile.TemporaryFile() as out:
            proc = subprocess.Popen(
                argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL
            )
            timed_out = False
            try:
                proc.wait(timeout=timeout + 10)  # +10 s: container start-up is not the tests' time
            except subprocess.TimeoutExpired:
                timed_out = True
                # Killing the `docker run` client does NOT stop the container, so kill it by name.
                subprocess.run(["docker", "kill", container], capture_output=True, timeout=30)
                proc.kill()
                proc.wait()
            text = _read_tail(out)
        # 125 means docker itself failed (bad image, daemon error), not the tests. A timeout wins:
        # we killed it ourselves, so its exit code says nothing about docker.
        if proc.returncode == 125 and not timed_out:
            raise RunnerError(f"docker could not start the container:\n{text.strip()[-600:]}")
        return Proc(proc.returncode, text, timed_out)


def docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, timeout=20, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def image_present(image: str) -> bool:
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def make_docker_runner(image: str | None = None) -> DockerRunner:
    img = image or DEFAULT_IMAGE
    if shutil.which("docker") is None:
        raise RunnerError(
            "Docker was not found. Install Docker Desktop (or Colima) and start it. "
            "For code you fully trust you can use: --runner local --allow-local-exec"
        )
    if not docker_available():
        raise RunnerError("Docker is installed but not running. Start Docker Desktop and retry.")
    if not image_present(img):
        raise RunnerError(
            f"The sandbox image '{img}' does not exist yet. Build it: depfix sandbox build"
        )
    return DockerRunner(image=img)


def sandbox_build(image: str | None = None) -> Proc:
    """Build the default image. This is the ONE step that uses the network (pip install)."""
    dockerfile = (resources.files("depfix") / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
    r = subprocess.run(
        ["docker", "build", "-t", image or DEFAULT_IMAGE, "-"],
        input=dockerfile, text=True, capture_output=True, check=False,
    )  # fmt: skip
    return Proc(r.returncode, (r.stdout + r.stderr)[-2000:], False)


_PROBE = r"""
import json, os, socket, sys
res = {}
try:
    socket.create_connection(("1.1.1.1", 80), timeout=3).close()
    res["network_blocked"] = False
except OSError:
    res["network_blocked"] = True
try:
    open("/depfix_probe", "w").write("x")
    res["root_readonly"] = False
except OSError:
    res["root_readonly"] = True
SECRETS = ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "DEPFIX_SECRET")
res["secrets_visible"] = any(k in os.environ for k in SECRETS)
res["host_home_visible"] = os.path.exists(sys.argv[1])
res["uid"] = os.getuid()
print("DEPFIX_PROBE " + json.dumps(res))
"""


def sandbox_probe(runner: Runner) -> list[tuple[str, bool, str]]:
    """Prove the isolation empirically from the inside. Returns (property, ok, detail) rows."""
    rows: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="depfix-probe-") as td:
        d = Path(td)
        py = runner.default_python()
        r = runner.run([py, "-c", _PROBE, str(Path.home())], workdir=d, scratch=d, timeout=60)
        line = next((x for x in r.output.splitlines() if x.startswith("DEPFIX_PROBE ")), None)
        if line is None:
            return [("probe ran", False, r.output[-300:])]
        res = json.loads(line.removeprefix("DEPFIX_PROBE "))
        rows.append(("network is blocked", bool(res["network_blocked"]), ""))
        rows.append(("root filesystem is read-only", bool(res["root_readonly"]), ""))
        rows.append(("host secrets are not visible", not res["secrets_visible"], ""))
        rows.append(("your home folder is not visible", not res["host_home_visible"], ""))
        rows.append(("runs as a non-root user", res["uid"] != 0, f"uid={res['uid']}"))
        t = runner.run([py, "-c", "import time; time.sleep(30)"], workdir=d, scratch=d, timeout=3)
        rows.append(("a runaway process is killed on timeout", t.timed_out, ""))
    return rows
