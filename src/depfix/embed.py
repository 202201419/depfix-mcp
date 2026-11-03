"""Turning text into vectors. The Embedder interface keeps retrieval independent of the model."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Literal, Protocol

Kind = Literal["document", "query"]


class Embedder(Protocol):
    name: str  # recorded in the index so a query is never compared with vectors from another model

    def embed(self, texts: list[str], kind: Kind) -> list[list[float]]: ...


def normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class HashEmbedder:
    """A deterministic, model-free embedder: hashed bag of words with signs.

    It is NOT semantically smart (similar texts must share words), but it is exact, instant and
    identical on every machine, which is what tests and the CI benchmark need. Real retrieval
    quality is measured with the Ollama embedder (docs/EXPERIMENTS.md, E1 and E2).
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.name = f"hash-{dim}"

    def _tokens(self, text: str) -> list[str]:
        words = re.findall(r"[a-z0-9_]+", text.lower())
        parts = [p for w in words for p in w.split("_") if p and p != w]
        return words + parts  # `float_` also contributes `float`

    def embed(self, texts: list[str], kind: Kind) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in self._tokens(text):
                h = int.from_bytes(hashlib.sha1(tok.encode()).digest()[:8], "big")
                vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
            out.append(normalize(vec))
        return out
