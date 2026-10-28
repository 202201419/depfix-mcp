"""Stage 3 (template path): rewrite one finding with a rule's template. No model involved.

A template is exact and repeatable, so it is also our no-AI baseline for the benchmark.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .diffing import make_diff
from .models import Arity, Finding, Proposal, Rule, now_iso
from .scanner import Aliases, UnsupportedFile, collect_aliases, locate_node, parse_source

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")


@dataclass
class ProposeResult:
    outcome: Literal["proposed", "not_applicable", "needs_llm", "stale"]
    proposal: Proposal | None
    reason: str = ""


class NoAlias(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def render_name(full: str, aliases: Aliases) -> str | None:
    """Spell `full` the way THIS file spells it.

    numpy.float64 becomes np.float64 if the file did `import numpy as np`. Returns None if the
    file imports nothing that provides the name; adding imports is out of scope for drop 1, and
    returning None is how we say so instead of inventing a name the file does not have.
    """
    best: tuple[str, str] | None = None
    for local, target in aliases.map.items():
        if local in aliases.ambiguous:
            continue
        if (full == target or full.startswith(target + ".")) and (
            best is None or len(target) > len(best[1])
        ):
            best = (local, target)
    if best is None:
        return None
    local, target = best
    return local + full[len(target) :]


def render_template(template: str, aliases: Aliases, args_text: str) -> str:
    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name == "args":
            return args_text
        rendered = render_name(name, aliases)
        if rendered is None:
            raise NoAlias(name)
        return rendered

    return _PLACEHOLDER.sub(sub, template)


def line_starts(b: bytes) -> list[int]:
    return [0] + [i + 1 for i, c in enumerate(b) if c == 10]


def splice(text: str, f: Finding, replacement: str) -> str:
    """Replace the finding's span.

    ast columns are UTF-8 BYTE offsets, not character positions: in 'naïve = np.float_(1)' the
    'ï' is one character but two bytes, so character slicing would land one place too far left.
    So we encode, slice bytes, and decode.
    """
    b = text.encode("utf-8")
    starts = line_starts(b)
    s = starts[f.start_line - 1] + f.start_col
    e = starts[f.end_line - 1] + f.end_col
    if b[s:e].decode("utf-8", errors="replace") != f.snippet:
        raise UnsupportedFile("span no longer matches the scanned snippet")
    return (b[:s] + replacement.encode("utf-8") + b[e:]).decode("utf-8")


def check_arity(node: ast.Call, arity: Arity | None) -> str | None:
    want = arity or Arity()
    if any(isinstance(a, ast.Starred) for a in node.args) or any(
        k.arg is None for k in node.keywords
    ):
        return "call uses *args/**kwargs, so the template cannot be applied safely"
    if len(node.args) != want.positional or len(node.keywords) != want.keywords:
        return (
            f"call has {len(node.args)} positional and {len(node.keywords)} keyword arguments; "
            f"the template expects {want.positional} and {want.keywords}"
        )
    return None


def propose_template(repo: Path, f: Finding, rule: Rule) -> ProposeResult:
    if rule.fix.kind != "template" or rule.fix.template is None:
        return ProposeResult("needs_llm", None, "this rule needs a model-written fix (drop 3)")
    if not _SAFE_PATH.match(f.file):
        return ProposeResult("not_applicable", None, "path has characters the diff builder rejects")
    path = repo / f.file
    if path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
        return ProposeResult("not_applicable", None, "file is a symlink or outside the repo")
    data = path.read_bytes()
    # A finding describes one exact version of one file. If it changed, the span is meaningless.
    if hashlib.sha256(data).hexdigest() != f.file_sha256:
        return ProposeResult("stale", None, "file changed since the scan; run scan again")
    try:
        text, tree = parse_source(data)
    except UnsupportedFile as exc:
        return ProposeResult("stale", None, str(exc))
    node = locate_node(tree, f, rule.match)
    if node is None:
        return ProposeResult("stale", None, "could not find the finding in the file")

    args_text = ""
    if rule.match == "call":
        assert isinstance(node, ast.Call)
        problem = check_arity(node, rule.fix.arity)
        if problem:
            return ProposeResult("not_applicable", None, problem)
        args_text = ", ".join(ast.get_source_segment(text, a) or "" for a in node.args)
    try:
        replacement = render_template(rule.fix.template, collect_aliases(tree), args_text)
    except NoAlias as exc:
        return ProposeResult(
            "not_applicable",
            None,
            f"this file does not import '{exc.name}'; adding imports is not supported yet",
        )
    new_text = splice(text, f, replacement)
    try:
        ast.parse(new_text)  # a template that yields invalid Python is a rule bug: refuse
    except SyntaxError:
        return ProposeResult("not_applicable", None, "template produced invalid syntax")
    diff = make_diff(f.file, text, new_text)
    return ProposeResult(
        "proposed",
        Proposal(
            finding_id=f.id,
            file=f.file,
            replacement=replacement,
            diff=diff,
            diff_sha256=hashlib.sha256(diff.encode()).hexdigest(),
            file_sha256=f.file_sha256,
            rationale=f"{rule.summary} (rule {rule.id})",
            reference_url=rule.reference.url,
            created_at=now_iso(),
        ),
    )
