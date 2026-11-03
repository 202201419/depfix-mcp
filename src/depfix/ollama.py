"""Talking to a local Ollama server over plain HTTP (standard library only).

Privacy: by default we refuse any server that is not on this machine, because the prompt contains
your source code. Set DEPFIX_ALLOW_REMOTE_OLLAMA=1 only if you understand that trade-off.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from .embed import Kind, normalize
from .llm import LLMError, LLMRequest

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:7b"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
_LOCAL = {"localhost", "127.0.0.1", "::1"}


class OllamaError(LLMError):
    pass


class OllamaClient:
    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        timeout: float = 900,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        host = urlparse(base_url).hostname or ""
        if host not in _LOCAL and os.environ.get("DEPFIX_ALLOW_REMOTE_OLLAMA") != "1":
            raise OllamaError(
                f"refusing to send your code to '{host}': only a local Ollama is allowed. "
                "(Set DEPFIX_ALLOW_REMOTE_OLLAMA=1 to override, at your own risk.)"
            )
        self.base, self.timeout, self.opener = base_url.rstrip("/"), timeout, opener

    def _call(self, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))  # type: ignore[no-any-return]
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code == 404 and "not found" in body.lower():
                raise OllamaError(f"{body.strip()} (pull it with: ollama pull <model>)") from exc
            raise OllamaError(f"Ollama returned HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise OllamaError(
                f"cannot reach Ollama at {self.base} ({exc}). Is it running? "
                "Open the Ollama app or run `ollama serve`."
            ) from exc
        except json.JSONDecodeError as exc:
            raise OllamaError(f"Ollama sent a reply that is not JSON: {exc}") from exc

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(path, payload)

    def get(self, path: str) -> dict[str, Any]:
        return self._call(path, None)


class OllamaLLM:
    def __init__(self, client: OllamaClient) -> None:
        self.client = client
        self.last_stats: dict[str, Any] = {}

    def complete(self, req: LLMRequest) -> str:
        body = self.client.post(
            "/api/chat",
            {
                "model": req.model,
                "messages": req.messages,
                "stream": False,  # one JSON object back, not a stream
                "format": req.schema,  # constrained decoding: the reply must match this schema
                "options": req.options,
            },
        )
        try:
            self.last_stats = {
                k: body[k] for k in ("eval_count", "eval_duration", "total_duration") if k in body
            }
            return str(body["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise OllamaError(f"unexpected reply shape from Ollama: {str(body)[:200]}") from exc


class OllamaEmbedder:
    # nomic-embed-text is trained with task prefixes: documents and queries differ.
    _PREFIX = {"document": "search_document: ", "query": "search_query: "}

    def __init__(
        self, client: OllamaClient, model: str = DEFAULT_EMBED_MODEL, batch: int = 16
    ) -> None:
        self.client, self.model, self.batch = client, model, batch
        self.name = f"ollama:{model}"

    def embed(self, texts: list[str], kind: Kind) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            chunk = [self._PREFIX[kind] + t for t in texts[i : i + self.batch]]
            body = self.client.post("/api/embed", {"model": self.model, "input": chunk})
            vecs = body.get("embeddings")
            if not isinstance(vecs, list) or len(vecs) != len(chunk):
                raise OllamaError("Ollama returned the wrong number of embeddings")
            out.extend(normalize([float(x) for x in v]) for v in vecs)
        return out


def check_ollama(client: OllamaClient, model: str, embed_model: str) -> list[tuple[str, bool, str]]:
    """Rows for `depfix llm check`: is the server up, are the models pulled, do they answer?"""
    rows: list[tuple[str, bool, str]] = []
    try:
        names = [m["name"] for m in client.get("/api/tags").get("models", [])]
    except OllamaError as exc:
        return [("Ollama is reachable", False, str(exc))]
    rows.append(("Ollama is reachable", True, f"{len(names)} model(s) installed"))

    def have(wanted: str) -> bool:
        return any(n == wanted or n.startswith(wanted + ":") for n in names)

    rows.append(
        (f"model '{model}' is pulled", model in names or have(model), f"ollama pull {model}")
    )
    rows.append(
        (
            f"embedding model '{embed_model}' is pulled",
            have(embed_model),
            f"ollama pull {embed_model}",
        )
    )
    if not all(ok for _, ok, _ in rows):
        return rows
    try:
        vec = OllamaEmbedder(client, embed_model).embed(["np.float_ was removed"], "query")[0]
        rows.append(("embedding works", len(vec) > 0, f"{len(vec)} dimensions"))
        llm = OllamaLLM(client)
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        start = time.time()
        reply = llm.complete(
            LLMRequest(
                model,
                [{"role": "user", "content": 'Reply with JSON: {"ok": true}'}],
                schema,
                {"temperature": 0, "seed": 0, "num_predict": 20},
            )
        )
        good = json.loads(reply).get("ok") is True
        stats = llm.last_stats
        speed = ""
        if stats.get("eval_count") and stats.get("eval_duration"):
            speed = f", {stats['eval_count'] / (stats['eval_duration'] / 1e9):.1f} tokens/s"
        rows.append(("structured output works", good, f"{time.time() - start:.1f}s{speed}"))
    except (OllamaError, json.JSONDecodeError) as exc:
        rows.append(("model call works", False, str(exc)))
    return rows
