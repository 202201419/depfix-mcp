"""Applying an approved fix to the real repo, on its own git branch.

Nothing here decides WHETHER to apply (pipeline.apply_fix does that, after checking the approval).
This file only knows how to do it safely with git: refuse unless the repo is in a clean, simple
state, work on a new branch so the change is trivially undone, and clean up if anything fails.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .models import Finding, Proposal


class ApplyRefused(Exception):
    pass


def git(repo: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin, capture_output=True, text=True, env=env, check=False,
    )  # fmt: skip


def branch_name(f: Finding) -> str:
    return f"depfix/{f.id}"


def preflight(repo: Path, f: Finding) -> str:
    """Refuse unless applying is simple and safe. Returns the branch we start from."""
    top = git(repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise ApplyRefused("this folder is not a git repository")
    if Path(top.stdout.strip()).resolve() != repo.resolve():
        raise ApplyRefused("run depfix at the root of the git repository (paths must line up)")
    start = git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    if start.returncode != 0:
        raise ApplyRefused("HEAD is detached; switch to a branch first")
    if git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode != 0:
        raise ApplyRefused("the repository has no commits yet")
    if git(repo, "ls-files", "--error-unmatch", "--", f.file).returncode != 0:
        raise ApplyRefused(f"{f.file} is not tracked by git")
    if git(repo, "status", "--porcelain", "--", f.file).stdout.strip():
        raise ApplyRefused(f"{f.file} has uncommitted changes; commit or stash them first")
    exists = git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch_name(f)}")
    if exists.returncode == 0:
        raise ApplyRefused(f"branch {branch_name(f)} already exists")
    return start.stdout.strip()


def apply_on_branch(repo: Path, f: Finding, p: Proposal, message: str, start: str) -> str:
    """Create the branch, apply the diff, commit. On any failure, undo and return to `start`."""
    branch = branch_name(f)
    made = git(repo, "switch", "-c", branch)
    if made.returncode != 0:
        raise ApplyRefused(f"could not create branch {branch}: {made.stderr.strip()}")

    def rollback() -> None:
        git(repo, "checkout", "--", f.file)
        git(repo, "reset", "-q", "HEAD", "--", f.file)
        git(repo, "switch", start)
        git(repo, "branch", "-D", branch)

    for extra in (["--check"], []):
        r = git(repo, "apply", "--whitespace=nowarn", *extra, "-", stdin=p.diff)
        if r.returncode != 0:
            rollback()
            raise ApplyRefused(f"git could not apply the diff: {r.stderr.strip()}")
    if git(repo, "add", "--", f.file).returncode != 0:
        rollback()
        raise ApplyRefused("git add failed")
    done = git(repo, "commit", "-q", "-m", message)
    if done.returncode != 0:
        # The change is applied and staged on the new branch; say so instead of hiding it.
        raise ApplyRefused(
            f"the change is staged on {branch} but git could not commit it: {done.stderr.strip()}"
        )
    return git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
