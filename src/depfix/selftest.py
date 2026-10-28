"""Rule self-tests: run each rule's `example` on the interpreter you are using.

A rule is a claim ("np.float_ is removed; np.float64 works the same"). The self-test checks that
claim empirically: the 'before' must really fail or warn, and the 'after' must really work.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from .models import Rule
from .rules import RuleSet
from .verify import run_limited, sandbox_env

_SCRIPT = r"""
import json, sys, warnings
spec = json.loads(sys.argv[1])

def run(expr):
    ns = {}
    exec(spec["setup"], ns)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            value, err = repr(eval(expr, ns)), None
        except Exception as exc:
            value, err = None, type(exc).__name__
    deprecated = any(issubclass(c.category, DeprecationWarning) for c in caught)
    return {"value": value, "error": err, "deprecation": deprecated}

out = {"after": run(spec["after"])}
if spec.get("before"):
    out["before"] = run(spec["before"])
print("DEPFIX_RESULT " + json.dumps(out))
"""


def selftest_rule(rule: Rule, python: str = sys.executable) -> list[str]:
    ex = rule.example
    if ex is None:
        return []  # model-path rules have no executable example
    spec = {"setup": ex.setup, "before": ex.before, "after": ex.after}
    with tempfile.TemporaryDirectory(prefix="depfix-selftest-") as td:
        home = Path(td)
        r = run_limited([python, "-c", _SCRIPT, json.dumps(spec)], home, 60, sandbox_env(home))
    line = next((ln for ln in r.output.splitlines() if ln.startswith("DEPFIX_RESULT ")), None)
    if line is None:
        return [f"{rule.id}: example did not run: {r.output[-300:]}"]
    res = json.loads(line.removeprefix("DEPFIX_RESULT "))
    problems: list[str] = []
    after = res["after"]
    if after["error"]:
        problems.append(f"{rule.id}: 'after' raised {after['error']}")
    elif after["value"] != ex.expect:
        problems.append(f"{rule.id}: 'after' gave {after['value']!r}, expected {ex.expect!r}")
    before = res.get("before")
    if before is not None:
        if ex.before_effect == "removed" and before["error"] != "AttributeError":
            problems.append(
                f"{rule.id}: expected 'before' to be removed (AttributeError), "
                f"got error={before['error']!r}"
            )
        if ex.before_effect == "warns" and not (before["deprecation"] and before["error"] is None):
            problems.append(
                f"{rule.id}: expected 'before' to emit a DeprecationWarning "
                f"(got warning={before['deprecation']}, error={before['error']!r})"
            )
        if ex.equivalent and before["value"] != after["value"]:
            problems.append(
                f"{rule.id}: before/after differ: {before['value']!r} vs {after['value']!r}"
            )
    return problems


def selftest_all(rules: RuleSet, python: str = sys.executable) -> dict[str, list[str]]:
    return {r.id: selftest_rule(r, python) for r in rules.rules}
