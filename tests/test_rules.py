import pytest

from depfix.models import Rule
from depfix.rules import RuleError, build_ruleset
from depfix.selftest import selftest_all
from tests.helpers import RULES


def test_all_rule_self_tests_pass_on_this_interpreter():
    problems = {k: v for k, v in selftest_all(RULES).items() if v}
    assert problems == {}


def _rule(**over):
    base = {
        "id": "X-1",
        "library": "x",
        "match": "attribute",
        "qualified_name": "x.y",
        "summary": "s",
        "fix": {"kind": "template", "template": "{x.z}"},
        "reference": {"url": "u", "section": "s"},
        "example": {"after": "1", "expect": "1"},
    }
    base.update(over)
    return Rule.model_validate(base)


def test_template_rule_must_have_an_example():
    with pytest.raises(ValueError, match="example"):
        _rule(example=None)


def test_args_placeholder_only_for_call_rules():
    with pytest.raises(ValueError, match="args"):
        _rule(fix={"kind": "template", "template": "f({args})"})


def test_unknown_yaml_keys_are_rejected():
    with pytest.raises(ValueError, match="bogus"):
        _rule(bogus=1)


def test_duplicate_names_are_rejected():
    with pytest.raises(RuleError):
        build_ruleset([_rule(id="A"), _rule(id="B")])
