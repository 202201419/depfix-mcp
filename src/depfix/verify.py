"""Stage 4: verify a proposal on an isolated COPY of the repo before a human ever sees it.

DROP 1 LIMIT (read this): the project's tests run as a local subprocess. We copy the repo, strip
the environment, apply a timeout and CPU/file-size limits, and kill the whole process group on
timeout, but there is NO network or filesystem isolation yet. That arrives with the container
runner in drop 2. Until then, only verify code you trust.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .diffing import diff_paths, has_forbidden_markers, split_lines
from .models import CheckResult, Finding, Proposal, Verification, now_iso
from .rules import RuleSet
from .scanner import UnsupportedFile, scan_text

COPY_IGNORE = (
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".depfix",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".hypothesis",
)
MAX_XML_BYTES = 5_000_000

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


@dataclass
class VerifyConfig:
    python: str = sys.executable  # interpreter that runs the PROJECT's tests (needs pytest)
    timeout_s: int = 120
    max_changed_lines: int = 6
    run_tests: bool = True


@dataclass
class Proc:
    returncode: int
    output: str
    timed_out: bool


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
        out.seek(0, os.SEEK_END)
        size = out.tell()
        out.seek(max(0, size - 20_000))
        text = out.read().decode("utf-8", errors="replace")
    return Proc(proc.returncode, text, timed_out)


def copy_repo(src: Path, dst: Path) -> None:
    # symlinks=True copies links AS links instead of following them out of the repo.
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(*COPY_IGNORE))


def tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def apply_patch(workdir: Path, diff: str, scratch: Path) -> tuple[bool, str]:
    """`git apply --check` first, then apply. The patch file lives OUTSIDE the work copy."""
    patch = scratch / "proposal.patch"
    patch.write_bytes(diff.encode("utf-8"))
    env = sandbox_env(scratch)
    for extra in (["--check"], []):
        r = run_limited(
            ["git", "apply", "--whitespace=nowarn", *extra, str(patch)], workdir, 30, env
        )
        if r.returncode != 0:
            return False, tail(r.output) or "git apply failed"
    return True, "patch applies cleanly"


# ---- individual checks -------------------------------------------------------------------------


def check_diff_scope(f: Finding, p: Proposal) -> CheckResult:
    """Cheap, pure checks on the diff text itself. Run first so a bad diff never touches disk."""
    name = "diff_scope"
    if hashlib.sha256(p.diff.encode()).hexdigest() != p.diff_sha256:
        return CheckResult(
            name=name, status="failed", detail="diff does not match its recorded hash"
        )
    if p.file != f.file or p.finding_id != f.id:
        return CheckResult(name=name, status="failed", detail="proposal belongs to another finding")
    marker = has_forbidden_markers(p.diff)
    if marker:
        return CheckResult(name=name, status="failed", detail=f"diff uses '{marker}' (not allowed)")
    paths = diff_paths(p.diff)
    if paths != {f.file}:
        return CheckResult(
            name=name,
            status="failed",
            detail=f"diff touches {sorted(paths)}, expected only {f.file}",
        )
    if any(".." in Path(x).parts or Path(x).is_absolute() for x in paths):
        return CheckResult(name=name, status="failed", detail="diff path escapes the repo")
    if "\n@@" not in "\n" + p.diff:
        return CheckResult(name=name, status="failed", detail="diff has no hunks")
    return CheckResult(name=name, status="passed", detail=f"touches only {f.file}")


def check_parses(path: Path) -> CheckResult:
    try:
        ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
        return CheckResult(name="parses", status="failed", detail=f"patched file is invalid: {exc}")
    return CheckResult(name="parses", status="passed", detail="patched file parses")


def check_finding_gone(f: Finding, orig: bytes, patched: bytes, rules: RuleSet) -> CheckResult:
    """The target usage must be gone AND nothing deprecated may have appeared.

    We compare multisets of (rule, snippet) instead of finding ids: fixing the first of two
    identical usages makes the second inherit the same id, which would look like 'still there'.
    """
    name = "finding_gone"
    try:
        before = Counter((x.rule_id, x.snippet) for x in scan_text(f.file, orig, rules))
        after = Counter((x.rule_id, x.snippet) for x in scan_text(f.file, patched, rules))
    except UnsupportedFile as exc:
        return CheckResult(name=name, status="failed", detail=str(exc))
    expected = before.copy()
    expected[(f.rule_id, f.snippet)] -= 1
    expected += Counter()  # drop zero/negative counts
    if after == expected:
        return CheckResult(
            name=name,
            status="passed",
            detail="target usage removed, no new deprecated usage introduced",
        )
    extra = after - expected
    detail = (
        f"still or newly deprecated: {[f'{r}:{s}' for (r, s) in extra]}"
        if extra
        else ("the target usage was not removed")
    )
    return CheckResult(name=name, status="failed", detail=detail)


def check_diff_minimal(f: Finding, old: str, new: str, cap: int) -> CheckResult:
    name = "diff_minimal"
    a, b = split_lines(old), split_lines(new)
    lo, hi = f.start_line - 1, f.end_line - 1  # 0-based inclusive range of the finding's lines
    old_changed = new_changed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        inside = (lo <= i1 <= hi + 1) if i1 == i2 else (i1 >= lo and i2 - 1 <= hi)
        if not inside:
            return CheckResult(
                name=name,
                status="failed",
                detail=f"edit at lines {i1 + 1}-{i2} is outside the finding "
                f"(lines {f.start_line}-{f.end_line})",
            )
        old_changed += i2 - i1
        new_changed += j2 - j1
    if old_changed == 0 and new_changed == 0:
        return CheckResult(name=name, status="failed", detail="the patch changes nothing")
    if max(old_changed, new_changed) > cap:
        return CheckResult(
            name=name,
            status="failed",
            detail=f"{max(old_changed, new_changed)} changed lines exceeds cap {cap}",
        )
    return CheckResult(
        name=name,
        status="passed",
        detail=f"{old_changed} line(s) replaced by {new_changed}, all inside the finding",
    )


@dataclass
class PytestRun:
    returncode: int
    failed: set[str] | None  # None: results file missing or unreadable
    tail: str
    timed_out: bool


def failed_ids(xml_path: Path) -> set[str] | None:
    try:
        if xml_path.stat().st_size > MAX_XML_BYTES:
            return None
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError):
        return None
    return {
        f"{tc.get('classname', '')}::{tc.get('name', '')}"
        for tc in root.iter("testcase")
        if tc.find("failure") is not None or tc.find("error") is not None
    }


def run_pytest(
    python: str,
    workdir: Path,
    scratch: Path,
    timeout: int,
    label: str,
    extra: list[str] | None = None,
) -> PytestRun:
    xml = scratch / f"{label}.xml"
    cmd = [
        python,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={xml}",
        *(extra or []),
    ]
    r = run_limited(cmd, workdir, timeout, sandbox_env(scratch))
    return PytestRun(r.returncode, failed_ids(xml), tail(r.output), r.timed_out)


def check_tests(
    orig: Path, patched: Path, scratch: Path, cfg: VerifyConfig
) -> tuple[CheckResult, bool]:
    """Compare the project's own tests before and after. Returns (result, clean).

    'clean' means the patched copy passed everything, which is what earns high confidence.
    A test that already failed before the patch is not blamed on the patch.
    """
    name = "tests"
    before = run_pytest(cfg.python, orig, scratch, cfg.timeout_s, "before")
    after = run_pytest(cfg.python, patched, scratch, cfg.timeout_s, "after")
    if after.timed_out:
        return CheckResult(name=name, status="failed", detail="tests timed out after patch"), False
    if "No module named pytest" in after.tail:
        return CheckResult(
            name=name,
            status="failed",
            detail="pytest is not installed for the interpreter given by --python",
        ), False
    if after.returncode == 5:
        return CheckResult(name=name, status="skipped", detail="no tests were collected"), False
    if after.returncode == 0:
        return CheckResult(
            name=name, status="passed", detail="all tests pass after the patch"
        ), True
    if after.failed is None:
        return CheckResult(
            name=name, status="failed", detail=f"could not read test results:\n{after.tail}"
        ), False
    new_failures = after.failed - (before.failed or set())
    if new_failures:
        return CheckResult(
            name=name, status="failed", detail=f"new failures: {sorted(new_failures)}\n{after.tail}"
        ), False
    return CheckResult(
        name=name, status="passed", detail=f"no new failures ({len(after.failed)} pre-existing)"
    ), False


def verify_proposal(
    repo: Path, f: Finding, p: Proposal, rules: RuleSet, cfg: VerifyConfig | None = None
) -> Verification:
    cfg = cfg or VerifyConfig()
    checks: list[CheckResult] = []

    def finish(clean: bool = False) -> Verification:
        ok = all(c.status != "failed" for c in checks)
        tests_passed = any(c.name == "tests" and c.status == "passed" for c in checks)
        return Verification(
            finding_id=f.id,
            diff_sha256=p.diff_sha256,
            passed=ok,
            confidence="high" if ok and tests_passed and clean else "low",
            checks=checks,
            ran_at=now_iso(),
        )

    def add(c: CheckResult) -> bool:
        checks.append(c)
        return c.status != "failed"

    if not add(check_diff_scope(f, p)):
        return finish()
    with tempfile.TemporaryDirectory(prefix="depfix-") as td:
        root = Path(td)
        orig, patched, scratch = root / "orig", root / "patched", root / "scratch"
        scratch.mkdir()
        copy_repo(repo, orig)
        target = orig / f.file
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != p.file_sha256:
            add(
                CheckResult(
                    name="applies",
                    status="failed",
                    detail="file changed since the proposal was made; propose again",
                )
            )
            return finish()
        shutil.copytree(orig, patched, symlinks=True)
        ok, detail = apply_patch(patched, p.diff, scratch)
        if not add(CheckResult(name="applies", status="passed" if ok else "failed", detail=detail)):
            return finish()
        if not add(check_parses(patched / f.file)):
            return finish()
        old_bytes = (orig / f.file).read_bytes()
        new_bytes = (patched / f.file).read_bytes()
        add(check_finding_gone(f, old_bytes, new_bytes, rules))
        add(
            check_diff_minimal(
                f, old_bytes.decode("utf-8"), new_bytes.decode("utf-8"), cfg.max_changed_lines
            )
        )
        if any(c.status == "failed" for c in checks):
            return finish()  # do not spend time running tests on a patch that already failed
        if not cfg.run_tests:
            add(CheckResult(name="tests", status="skipped", detail="disabled by configuration"))
            return finish()
        result, clean = check_tests(orig, patched, scratch, cfg)
        add(result)
        return finish(clean)
