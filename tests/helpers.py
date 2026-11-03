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


# ---- drop 2 helpers ----------------------------------------------------------------------------

import subprocess  # noqa: E402

from depfix import pipeline  # noqa: E402
from depfix.runner import LocalRunner  # noqa: E402
from depfix.verify import VerifyConfig  # noqa: E402


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def git_init(repo: Path) -> None:
    (repo / ".gitignore").write_text(".depfix/\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        assert git(repo, *args).returncode == 0, args


def make_verified(repo: Path, run_tests: bool = False) -> str:
    """scan -> propose -> verify the logging.warn finding; returns its id."""
    findings, _ = pipeline.scan(repo)
    fid = next(f.id for f in findings if f.rule_id == "STD-0003")
    assert pipeline.propose(repo, fid).outcome == "proposed"
    v = pipeline.verify(repo, fid, VerifyConfig(runner=LocalRunner(), run_tests=run_tests))
    assert v.passed, [(c.name, c.status, c.detail) for c in v.checks]
    return fid


# ---- drop 3 helpers ----------------------------------------------------------------------------

import io  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import urllib.error  # noqa: E402
from collections.abc import Callable  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402
from typing import Any  # noqa: E402

from depfix.embed import Embedder, HashEmbedder  # noqa: E402
from depfix.index import Index  # noqa: E402
from depfix.indexer import build_index  # noqa: E402
from depfix.sources import load_sources, read_source  # noqa: E402

DOCS = Path(__file__).parent.parent / "benchmark" / "docs"


def fixture_index(
    chunker: str = "entry", embedder: Embedder | None = None
) -> tuple[Index, Embedder]:
    """An in-memory index of the original-text documentation fixtures (no network, no model)."""
    emb = embedder or HashEmbedder()
    idx = Index(":memory:")
    build_index(
        idx, load_sources(DOCS / "sources.yaml"), emb, chunker,
        lambda s: read_source(s, Path("/nonexistent"), DOCS),
    )  # fmt: skip
    return idx, emb


class FakeOllama:
    """A real HTTP server on 127.0.0.1 that speaks just enough of Ollama's API for tests."""

    def __init__(self, handler: Callable[[str, str, dict[str, Any]], tuple[int, Any]]) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self, method: str) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                outer.requests.append((method, self.path, body))
                code, payload = handler(method, self.path, body)
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:  # noqa: N802
                self._do("POST")

            def do_GET(self) -> None:  # noqa: N802
                self._do("GET")

            def log_message(self, *a: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body.encode()))  # type: ignore[arg-type]
