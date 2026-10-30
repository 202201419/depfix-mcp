"""Stage 4: verify a proposal on an isolated COPY of the repo before a human ever sees it.

The project's own code (its tests, linters, type checker) runs through a Runner (runner.py):
a throw-away Docker container by default, or a plain local process for code you fully trust.
Only the project's code is untrusted, so only that goes through the runner; `git apply` works on
our own diff and stays a local, time-limited process.
"""

from __future__ import annotations

import ast
import builtins
import difflib
import hashlib
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .diffing import diff_paths, has_forbidden_markers, split_lines
from .fixer import propose_template
from .models import CheckResult, Finding, Proposal, Rule, Verification, now_iso
from .rules import RuleSet
from .runner import LocalRunner, Runner, run_limited, sandbox_env
from .scanner import UnsupportedFile, scan_text
from .selftest import selftest_rule

COPY_IGNORE = (
    ".git", ".venv", "venv", "__pycache__", ".depfix", "node_modules",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", ".hypothesis",
)  # fmt: skip
MAX_XML_BYTES = 5_000_000


@dataclass
class VerifyConfig:
    runner: Runner = field(default_factory=LocalRunner)
    python: str | None = None  # interpreter inside the runner; None means the runner's default
    timeout_s: int = 120
    max_changed_lines: int = 6
    run_tests: bool = True
    run_static: bool = True

    @property
    def interpreter(self) -> str:
        return self.python or self.runner.default_python()


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


# ---- cheap checks (pure Python) ----------------------------------------------------------------


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
            name=name, status="failed",
            detail=f"diff touches {sorted(paths)}, expected only {f.file}",
        )  # fmt: skip
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
            name=name, status="passed",
            detail="target usage removed, no new deprecated usage introduced",
        )  # fmt: skip
    extra = after - expected
    detail = (
        f"still or newly deprecated: {[f'{r}:{s}' for (r, s) in extra]}"
        if extra
        else "the target usage was not removed"
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
                name=name, status="failed",
                detail=f"edit at lines {i1 + 1}-{i2} is outside the finding "
                f"(lines {f.start_line}-{f.end_line})",
            )  # fmt: skip
        old_changed += i2 - i1
        new_changed += j2 - j1
    if old_changed == 0 and new_changed == 0:
        return CheckResult(name=name, status="failed", detail="the patch changes nothing")
    if max(old_changed, new_changed) > cap:
        return CheckResult(
            name=name, status="failed",
            detail=f"{max(old_changed, new_changed)} changed lines exceeds cap {cap}",
        )  # fmt: skip
    return CheckResult(
        name=name, status="passed",
        detail=f"{old_changed} line(s) replaced by {new_changed}, all inside the finding",
    )  # fmt: skip


def new_names(old: str, new: str) -> set[str]:
    """Attribute and variable names the patch introduces (builtins like `list` are exempt)."""

    def names(text: str) -> set[str]:
        tree = ast.parse(text)
        return {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }

    return names(new) - names(old) - set(dir(builtins))


def check_provenance(
    orig: Path, f: Finding, p: Proposal, rules: RuleSet, old: str, new: str
) -> CheckResult:
    """Where did this change come from, and is it backed by a source?

    Template fixes: re-derive the diff from the rule and require an EXACT match, so a doctored
    proposal cannot pose as 'the rule's fix'.
    Model fixes (drop 3): the fix must cite sources, and every new name it introduces must
    appear in the cited text (a rule-based groundedness check, no judge model).
    """
    name = "provenance"
    if p.kind == "template":
        rule = rules.by_id.get(f.rule_id)
        res = propose_template(orig, f, rule) if rule else None
        if (
            res
            and res.outcome == "proposed"
            and res.proposal
            and res.proposal.diff_sha256 == p.diff_sha256
        ):
            return CheckResult(
                name=name, status="passed", detail="diff is exactly what the rule produces"
            )
        return CheckResult(
            name=name, status="failed", detail="diff is not what the rule's template produces"
        )
    if not p.citations:
        return CheckResult(name=name, status="failed", detail="a model-written fix cites no source")
    missing = [c for c in p.citations if c not in p.evidence]
    if missing:
        return CheckResult(name=name, status="failed", detail=f"cited chunks not found: {missing}")
    cited_text = " ".join(p.evidence[c] for c in p.citations)
    unsupported = sorted(n for n in new_names(old, new) if n not in cited_text)
    if unsupported:
        return CheckResult(
            name=name, status="failed",
            detail=f"new names not found in the cited sources: {unsupported}",
        )  # fmt: skip
    return CheckResult(
        name=name, status="passed", detail=f"every new name is supported by {p.citations}"
    )


# ---- checks that run the project's code (through the runner) -----------------------------------


def check_rule_example(rule: Rule | None, cfg: VerifyConfig) -> CheckResult:
    """Re-run the rule's own example in the SAME environment that runs the tests.

    The container's library versions may differ from yours; this proves the rule's claim
    (before warns/fails, after works the same) there.
    """
    name = "rule_example"
    if rule is None or rule.example is None:
        return CheckResult(
            name=name, status="skipped", detail="this rule has no executable example"
        )
    problems = selftest_rule(rule, cfg.interpreter, cfg.runner)
    if problems:
        return CheckResult(name=name, status="failed", detail="; ".join(problems))
    return CheckResult(name=name, status="passed", detail=f"{rule.id} example holds in this runner")


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
    runner: Runner,
    python: str,
    workdir: Path,
    scratch: Path,
    timeout: int,
    label: str,
    extra: list[str] | None = None,
) -> PytestRun:
    # "{scratch}" is translated by the runner: a real path locally, /scratch in a container.
    cmd = [
        python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        f"--junitxml={{scratch}}/{label}.xml", *(extra or []),
    ]  # fmt: skip
    r = runner.run(cmd, workdir=workdir, scratch=scratch, timeout=timeout)
    return PytestRun(
        r.returncode, failed_ids(scratch / f"{label}.xml"), tail(r.output), r.timed_out
    )


def check_tests(
    orig: Path, patched: Path, scratch: Path, cfg: VerifyConfig
) -> tuple[CheckResult, bool]:
    """Compare the project's own tests before and after. Returns (result, clean).

    'clean' means the patched copy passed everything, which is what earns high confidence.
    A test that already failed before the patch is not blamed on the patch.
    """
    name = "tests"
    py = cfg.interpreter
    before = run_pytest(cfg.runner, py, orig, scratch, cfg.timeout_s, "before")
    after = run_pytest(cfg.runner, py, patched, scratch, cfg.timeout_s, "after")
    if after.timed_out:
        return CheckResult(name=name, status="failed", detail="tests timed out after patch"), False
    if "No module named pytest" in after.tail:
        return CheckResult(
            name=name, status="failed",
            detail="pytest is not installed in the interpreter/image used for verification",
        ), False  # fmt: skip
    if after.returncode == 5:
        return CheckResult(name=name, status="skipped", detail="no tests were collected"), False
    if after.returncode == 0:
        return CheckResult(
            name=name, status="passed", detail="all tests pass after the patch"
        ), True
    if after.failed is None:
        return CheckResult(
            name=name, status="failed", detail=f"could not read test results:\n{after.tail}"
        ), False  # fmt: skip
    new_failures = after.failed - (before.failed or set())
    if new_failures:
        return CheckResult(
            name=name, status="failed", detail=f"new failures: {sorted(new_failures)}\n{after.tail}"
        ), False  # fmt: skip
    return CheckResult(
        name=name, status="passed", detail=f"no new failures ({len(after.failed)} pre-existing)"
    ), False  # fmt: skip


# ---- the project's own linter / type checker ---------------------------------------------------

_TOOL_ARGS = {
    "ruff": ["-m", "ruff", "check", "--output-format=concise", "--no-cache"],
    "mypy": ["-m", "mypy", "--no-incremental", "--no-error-summary", "--no-color-output"],
}
_ISSUE = {
    "ruff": re.compile(r"^(?P<f>[^:\n]+):\d+:\d+: (?P<m>.+)$"),
    "mypy": re.compile(r"^(?P<f>[^:\n]+):\d+(?::\d+)?: (?P<m>(?:error|warning):.+)$"),
}
_CHECK_NAME = {"ruff": "lint", "mypy": "types"}


def detect_tools(root: Path) -> set[str]:
    """Which checkers does THIS project use? We only judge a project by its own rules."""

    def read(name: str) -> str:
        p = root / name
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""

    pyproject, setup_cfg = read("pyproject.toml"), read("setup.cfg")
    found: set[str] = set()
    if (
        (root / "ruff.toml").is_file()
        or (root / ".ruff.toml").is_file()
        or "[tool.ruff" in pyproject
    ):
        found.add("ruff")
    if (
        (root / "mypy.ini").is_file()
        or (root / ".mypy.ini").is_file()
        or "[tool.mypy" in pyproject
        or "[mypy" in setup_cfg
    ):
        found.add("mypy")
    return found


def tool_issues(tool: str, output: str) -> Counter[str]:
    """Issues WITHOUT line numbers, so an unrelated line shift is not mistaken for a new issue."""
    issues: Counter[str] = Counter()
    for line in output.splitlines():
        m = _ISSUE[tool].match(line.strip())
        if m:
            issues[f"{m['f']}: {m['m']}"] += 1
    return issues


def check_static(
    tool: str, orig: Path, patched: Path, scratch: Path, f: Finding, cfg: VerifyConfig
) -> CheckResult:
    name = _CHECK_NAME[tool]
    if tool not in detect_tools(orig):
        return CheckResult(name=name, status="skipped", detail=f"project does not configure {tool}")
    cmd = [cfg.interpreter, *_TOOL_ARGS[tool], f.file]
    before = cfg.runner.run(cmd, workdir=orig, scratch=scratch, timeout=cfg.timeout_s)
    after = cfg.runner.run(cmd, workdir=patched, scratch=scratch, timeout=cfg.timeout_s)
    for r in (before, after):
        if f"No module named {tool}" in r.output or r.timed_out or r.returncode not in (0, 1):
            return CheckResult(
                name=name, status="skipped", detail=f"{tool} could not run: {tail(r.output, 3)}"
            )
    new = tool_issues(tool, after.output) - tool_issues(tool, before.output)
    if new:
        return CheckResult(
            name=name, status="failed",
            detail=f"{sum(new.values())} new {tool} issue(s): {sorted(new)[:3]}",
        )  # fmt: skip
    return CheckResult(name=name, status="passed", detail=f"no new {tool} issues")


# ---- the orchestrator --------------------------------------------------------------------------


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
                )  # fmt: skip
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
        old_text, new_text = old_bytes.decode("utf-8"), new_bytes.decode("utf-8")
        add(check_finding_gone(f, old_bytes, new_bytes, rules))
        add(check_diff_minimal(f, old_text, new_text, cfg.max_changed_lines))
        if any(c.status == "failed" for c in checks):
            return finish()  # do not start containers for a patch that already failed
        # Provenance does not gate the expensive checks: a doctored proposal still gets the full
        # report, but it can never pass.
        add(check_provenance(orig, f, p, rules, old_text, new_text))
        if not cfg.run_tests:
            add(CheckResult(name="tests", status="skipped", detail="disabled by configuration"))
            return finish()
        add(check_rule_example(rules.by_id.get(f.rule_id), cfg))
        if cfg.run_static:
            for tool in ("ruff", "mypy"):
                add(check_static(tool, orig, patched, scratch, f, cfg))
        result, clean = check_tests(orig, patched, scratch, cfg)
        add(result)
        return finish(clean)
