"""The model interface, and the record/replay doubles that make model runs reproducible.

LLM        anything that turns a request into a JSON string (a real model, a recording, a script).
ReplayLLM  answers from a recording, keyed by a hash of the whole request. CI uses this: no model,
           no GPU, same answers every time.
RecordingLLM  wraps a real model and appends each answer to a recording file. Only the HASH and
           the answer are stored, never the prompt, so documentation text is not written out.
PolicyLLM  answers with a Python function: how tests and the benchmark self-test simulate models.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class LLMError(Exception):
    pass


class ReplayMiss(LLMError):
    pass


@dataclass(frozen=True)
class LLMRequest:
    model: str
    messages: list[dict[str, str]]
    schema: dict[str, Any]
    options: dict[str, Any]

    def key(self) -> str:
        """Identical requests give identical keys, on every machine."""
        blob = json.dumps(
            {"m": self.model, "msgs": self.messages, "s": self.schema, "o": self.options},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode()).hexdigest()


class LLM(Protocol):
    def complete(self, req: LLMRequest) -> str: ...


class ReplayLLM:
    def __init__(self, path: Path) -> None:
        self.answers: dict[str, str] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.answers[rec["key"]] = rec["response"]

    def complete(self, req: LLMRequest) -> str:
        key = req.key()
        if key not in self.answers:
            raise ReplayMiss(
                f"no recording for this request (key {key[:12]}, model {req.model}). "
                "The prompt, documentation or options changed since recording; "
                "re-record with: make record"
            )
        return self.answers[key]


class RecordingLLM:
    def __init__(self, inner: LLM, path: Path) -> None:
        self.inner, self.path = inner, path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.seen = ReplayLLM(path).answers.keys() if path.exists() else set()

    def complete(self, req: LLMRequest) -> str:
        out = self.inner.complete(req)
        key = req.key()
        if key not in self.seen:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"key": key, "model": req.model, "response": out}) + "\n")
            self.seen = {*self.seen, key}
        return out


@dataclass
class PolicyLLM:
    policy: Callable[[LLMRequest], str]
    calls: list[LLMRequest] = field(default_factory=list)

    def complete(self, req: LLMRequest) -> str:
        self.calls.append(req)
        return self.policy(req)
