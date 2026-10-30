"""Stage 1: find deprecated usages by resolving what each name really refers to.

The idea: `np.float_` is only interesting if `np` is NumPy. We parse with `ast` (so comments and
strings can never match), record what every import binds, and rewrite each attribute chain into
its fully qualified name before looking it up in the rule table.
"""

from __future__ import annotations

import ast
import hashlib
import io
import os
import re
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .models import Finding, Rule
from .rules import RuleSet

SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".depfix",
        "node_modules",
        ".tox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".hypothesis",
        "build",
        "dist",
    }
)
MAX_FILE_BYTES = 2_000_000
_LONE_CR = re.compile(rb"\r(?!\n)")


class UnsupportedFile(Exception):
    """A file this tool refuses to touch. Refusing is safer than guessing byte offsets."""


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Aliases(ast.NodeVisitor):
    """Maps a local name to the fully qualified thing it was imported as."""

    def __init__(self) -> None:
        self.map: dict[str, str] = {}
        # If one name is bound to two different things anywhere in the file we cannot tell which
        # one a usage means, so we refuse to resolve it rather than guess.
        self.ambiguous: set[str] = set()

    def _bind(self, local: str, target: str) -> None:
        if local in self.map and self.map[local] != target:
            self.ambiguous.add(local)
        self.map[local] = target

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.asname:  # import numpy as np -> np = numpy
                self._bind(a.asname, a.name)
            else:  # import os.path binds only the head name: os
                head = a.name.split(".")[0]
                self._bind(head, head)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # level > 0 is a relative import: we cannot know the absolute module, so skip it.
        if node.level == 0 and node.module:
            for a in node.names:
                if a.name != "*":
                    self._bind(a.asname or a.name, f"{node.module}.{a.name}")


def collect_aliases(tree: ast.AST) -> Aliases:
    al = Aliases()
    al.visit(tree)
    return al


def dotted(node: ast.AST) -> str | None:
    """Turn nested Attribute nodes back into text: Attribute(Attribute(Name a, b), c) -> 'a.b.c'.

    The outermost node holds the LAST segment, so we peel from the outside in and reverse.
    """
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None  # e.g. foo().bar: the root is a call, so we cannot name the library


def qualify(node: ast.AST, aliases: Aliases) -> str | None:
    d = dotted(node)
    if d is None:
        return None
    head, _, rest = d.partition(".")
    if head in aliases.ambiguous:
        return None
    base = aliases.map.get(head)
    if base is None:
        return None  # not imported: a local variable that merely looks like a library
    return f"{base}.{rest}" if rest else base


def decode_source(data: bytes) -> str:
    """Decode a file or refuse.

    ast reports columns as byte offsets into the UTF-8 re-encoding of the text. Supporting other
    encodings, BOMs or bare-CR line endings would need extra translation, so we skip those files.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        raise UnsupportedFile("UTF-8 BOM not supported")
    if _LONE_CR.search(data):
        raise UnsupportedFile("bare CR line endings not supported")
    try:
        enc, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    except SyntaxError as exc:
        raise UnsupportedFile(f"bad encoding declaration: {exc}") from exc
    if enc != "utf-8":
        raise UnsupportedFile(f"encoding {enc} not supported (UTF-8 only)")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedFile(f"not valid UTF-8: {exc}") from exc


def parse_source(data: bytes) -> tuple[str, ast.Module]:
    text = decode_source(data)
    try:
        return text, ast.parse(text)
    except SyntaxError as exc:
        raise UnsupportedFile(f"syntax error: {exc.msg} (line {exc.lineno})") from exc


def scan_text(relpath: str, data: bytes, rules: RuleSet) -> list[Finding]:
    text, tree = parse_source(data)
    aliases = collect_aliases(tree)
    candidates: list[tuple[Rule, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # Call rules own the whole call expression, so the fix can rewrite its parentheses.
            q = qualify(node.func, aliases)
            rule = rules.by_name.get(q) if q else None
            if rule is not None and rule.match == "call":
                candidates.append((rule, node))
        elif isinstance(node, ast.Attribute | ast.Name) and isinstance(node.ctx, ast.Load):
            # Load only: `np.float_ = 3` or `del np.float_` are not usages we should rewrite.
            q = qualify(node, aliases)
            rule = rules.by_name.get(q) if q else None
            if rule is not None and rule.match == "attribute":
                candidates.append((rule, node))

    sha = hashlib.sha256(data).hexdigest()
    # Sort by position BEFORE numbering so the id of "the 2nd identical usage" does not depend on
    # the order ast.walk happens to visit nodes.
    candidates.sort(key=lambda c: (c[1].lineno, c[1].col_offset, c[0].id))
    seen: dict[str, int] = {}
    out: list[Finding] = []
    for rule, node in candidates:
        snippet = ast.get_source_segment(text, node) or ""
        key = f"{rule.id}|{relpath}|{snippet}"
        seen[key] = seen.get(key, 0) + 1
        # The id hashes WHAT and WHERE-by-name, not the line number, so it survives edits above.
        fid = hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()[:10]
        out.append(
            Finding(
                id=fid,
                rule_id=rule.id,
                library=rule.library,
                file=relpath,
                start_line=node.lineno,
                start_col=node.col_offset,
                end_line=node.end_lineno or node.lineno,
                end_col=node.end_col_offset if node.end_col_offset is not None else node.col_offset,
                snippet=snippet,
                file_sha256=sha,
            )
        )
    return out


def locate_node(tree: ast.AST, f: Finding, match: str) -> ast.expr | None:
    """Find the AST node a finding points at (used again when building the fix)."""
    want = ast.Call if match == "call" else (ast.Attribute, ast.Name)
    for node in ast.walk(tree):
        if (
            isinstance(node, want)
            and node.lineno == f.start_line
            and node.col_offset == f.start_col
            and node.end_lineno == f.end_line
            and node.end_col_offset == f.end_col
        ):
            return node
    return None


def iter_python_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Never follow symlinks: one could point outside the repo.
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not (Path(dirpath) / d).is_symlink()
        )
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if name.endswith(".py") and not p.is_symlink():
                yield p


def scan_repo(root: Path, rules: RuleSet, libraries: set[str] | None = None) -> ScanResult:
    active = rules.for_libraries(libraries)
    result = ScanResult()
    for path in iter_python_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise UnsupportedFile("file larger than 2 MB")
            result.findings.extend(scan_text(rel, path.read_bytes(), active))
        except (UnsupportedFile, OSError) as exc:
            result.errors.append(f"{rel}: {exc}")
    return result
