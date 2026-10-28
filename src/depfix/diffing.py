"""Unified-diff building and inspection.

We build diffs ourselves from (old text, new text) instead of asking a model for a diff: small
models produce malformed hunks, and a diff made by code is correct by construction.
"""

from __future__ import annotations

import difflib
import re

_LINE = re.compile(r"[^\n]*\n|[^\n]+")


def split_lines(text: str) -> list[str]:
    """Split on \\n only, keeping endings. str.splitlines() also splits on form feeds and Unicode
    separators, which would disagree with how ast and git count lines."""
    return _LINE.findall(text)


def make_diff(rel: str, old: str, new: str) -> str:
    out: list[str] = []
    for line in difflib.unified_diff(
        split_lines(old), split_lines(new), fromfile=f"a/{rel}", tofile=f"b/{rel}"
    ):
        out.append(line)
        if not line.endswith("\n"):  # last line of a file with no trailing newline
            out.append("\n\\ No newline at end of file\n")
    return "".join(out)


def diff_paths(diff: str) -> set[str]:
    """Paths named in ---/+++ headers, with the a/ b/ prefix removed."""
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith(("--- ", "+++ ")):
            p = line[4:].split("\t")[0]
            paths.add(p[2:] if p[:2] in ("a/", "b/") else p)
    return paths


def has_forbidden_markers(diff: str) -> str | None:
    """Git extended headers that rename, delete, create or chmod files. We never generate these."""
    for line in diff.splitlines():
        for marker in (
            "rename ",
            "copy ",
            "new file mode",
            "deleted file mode",
            "old mode",
            "new mode",
            "GIT binary patch",
            "similarity index",
        ):
            if line.startswith(marker):
                return marker
    return None
