"""Rule loading. Rules are YAML data shipped inside the package (src/depfix/rule_data)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources

import yaml
from pydantic import ValidationError

from .models import Rule


class RuleError(Exception):
    pass


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...]
    by_id: dict[str, Rule]
    by_name: dict[str, Rule]

    def for_libraries(self, libraries: set[str] | None) -> RuleSet:
        if not libraries:
            return self
        return build_ruleset(r for r in self.rules if r.library in libraries)


def build_ruleset(rules: Iterable[Rule]) -> RuleSet:
    items = tuple(rules)
    by_id: dict[str, Rule] = {}
    by_name: dict[str, Rule] = {}
    for r in items:
        if r.id in by_id:
            raise RuleError(f"duplicate rule id {r.id}")
        # One rule per qualified name keeps the scanner's lookup unambiguous.
        if r.qualified_name in by_name:
            raise RuleError(
                f"{r.id} and {by_name[r.qualified_name].id} both match {r.qualified_name}"
            )
        by_id[r.id] = r
        by_name[r.qualified_name] = r
    return RuleSet(items, by_id, by_name)


def load_rules() -> RuleSet:
    folder = resources.files("depfix") / "rule_data"
    loaded: list[Rule] = []
    for entry in sorted(folder.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".yaml"):
            continue
        data = yaml.safe_load(entry.read_text(encoding="utf-8"))
        for item in data["rules"]:
            try:
                loaded.append(Rule.model_validate(item))
            except ValidationError as exc:
                raise RuleError(f"{entry.name}: {exc}") from exc
    return build_ruleset(loaded)
