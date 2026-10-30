"""Shared builders for tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

from depfix.diffing import make_diff
from depfix.models import Finding, Proposal
from depfix.rules import load_rules
from depfix.scanner import scan_text

RULES = load_rules()

# A tiny, numpy-free project whose only deprecated usage is logging.warn (rule STD-0003).
APP = (
    "import logging\n"
    "import threading\n"
    "\n"
    "\n"
    "def greet(name):\n"
    "    logging.warn('hi %s', name)\n"
    "    return f'hello {name}'\n"
    "\n"
    "\n"
    "def other():\n"
    "    return 1\n"
)
TEST_APP = (
    "from app import greet, other\n"
    "\n"
    "\n"
    "def test_greet():\n"
    "    assert greet('a') == 'hello a'\n"
    "\n"
    "\n"
    "def test_other():\n"
    "    assert other() == 1\n"
)
PYTEST_INI = "[pytest]\npythonpath = src\ntestpaths = tests\n"


def write_repo(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))
    return root


def app_repo(root: Path, with_tests: bool = True) -> Path:
    files = {"src/app.py": APP, "pytest.ini": PYTEST_INI}
    if with_tests:
        files["tests/test_app.py"] = TEST_APP
    return write_repo(root, files)


def find_one(rel: str, text: str, rule_id: str) -> Finding:
    found = [f for f in scan_text(rel, text.encode("utf-8"), RULES) if f.rule_id == rule_id]
    assert len(found) == 1, found
    return found[0]


def proposal_for(f: Finding, old: str, new: str, file: str | None = None) -> Proposal:
    """Build a Proposal from raw old/new text so tests can craft deliberately BAD patches."""
    diff = make_diff(file or f.file, old, new)
    return Proposal(
        finding_id=f.id,
        file=f.file,
        replacement="",
        diff=diff,
        diff_sha256=hashlib.sha256(diff.encode()).hexdigest(),
        file_sha256=f.file_sha256,
        rationale="test",
        reference_url="https://example.invalid",
    )
