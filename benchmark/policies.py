"""Simulated models, so the whole model pipeline can be tested and benchmarked without Ollama.

Each policy reads the machine-readable lines of depfix's own prompt (RULE, EXPRESSION, IMPORTS,
the DOC ids) and answers like a particular kind of model:

  oracle    a perfect, honest model (applies the rule's template, cites what it was given)
  sloppy    makes a typical mistake first (an invented name), then corrects itself once it is shown
            why it was rejected
  stubborn  makes the same mistake every time
  refuser   always declines
  garbage   answers in prose instead of JSON
  uncited   right answer, but cites nothing
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable
from typing import Any

from depfix.fixer import NoAlias, render_template
from depfix.llm import LLMRequest
from depfix.rules import load_rules
from depfix.scanner import Aliases

_RULES = load_rules()


def prompt_info(req: LLMRequest) -> dict[str, Any]:
    user = next(m["content"] for m in req.messages if m["role"] == "user")
    return {
        "rule": re.search(r"^RULE: (.+)$", user, re.M).group(1),  # type: ignore[union-attr]
        "expression": json.loads(re.search(r"^EXPRESSION: (.+)$", user, re.M).group(1)),  # type: ignore[union-attr]
        "imports": json.loads(re.search(r"^IMPORTS: (.+)$", user, re.M).group(1)),  # type: ignore[union-attr]
        "doc_ids": re.findall(r"<<<DOC id=(\S+) source=", user),
        "rejected": any(
            "was rejected" in m["content"] for m in req.messages if m["role"] == "user"
        ),
        "last_user": req.messages[-1]["content"],
    }


def reply(replacement: str, ids: list[str], why: str = "") -> str:
    return json.dumps({"replacement": replacement, "citation_ids": ids, "rationale": why})


def oracle_replacement(info: dict[str, Any]) -> str | None:
    rule = _RULES.by_id[info["rule"]]
    if rule.fix.template is None:
        return None
    aliases = Aliases()
    aliases.map = dict(info["imports"])
    args = ""
    if rule.match == "call":
        node = ast.parse(info["expression"].strip(), mode="eval").body
        if isinstance(node, ast.Call):
            args = ", ".join(ast.unparse(a) for a in node.args)
    try:
        return render_template(rule.fix.template, aliases, args)
    except NoAlias:
        return None


def oracle(req: LLMRequest) -> str:
    info = prompt_info(req)
    repl = oracle_replacement(info)
    if repl is None:
        return reply("", [], "The documentation does not support a change here.")
    return reply(repl, info["doc_ids"], f"Applies the documented replacement ({info['rule']}).")


def _wrong(info: dict[str, Any]) -> str:
    repl = oracle_replacement(info)
    if repl is None:
        return reply("", [], "no change")
    # Invent a name the documentation never mentions: the classic hallucination.
    mutated = re.sub(r"\.(\w+)", lambda m: f".{m.group(1)}99", repl, count=1)
    return reply(mutated, info["doc_ids"], "Use the new spelling.")


def sloppy(req: LLMRequest) -> str:
    info = prompt_info(req)
    return oracle(req) if info["rejected"] else _wrong(info)


def stubborn(req: LLMRequest) -> str:
    return _wrong(prompt_info(req))


def refuser(req: LLMRequest) -> str:
    return reply("", [], "I cannot tell from the documentation.")


def garbage(req: LLMRequest) -> str:
    return "You should probably switch to the newer function."


def uncited(req: LLMRequest) -> str:
    info = prompt_info(req)
    repl = oracle_replacement(info)
    return reply(repl or "", [], "Looks right to me.")


POLICIES: dict[str, Callable[[LLMRequest], str]] = {
    "oracle": oracle, "sloppy": sloppy, "stubborn": stubborn,
    "refuser": refuser, "garbage": garbage, "uncited": uncited,
}  # fmt: skip
