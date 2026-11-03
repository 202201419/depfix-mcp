"""Stage 2: find the documentation that explains one finding.

Three layers, most trustworthy first:
  anchor   chunks on the very page the rule cites (rule.reference.url) that mention the symbol.
  keyword  BM25 over all docs of the library for the exact name. Embeddings are weak at exact
           identifiers like `float_`; keyword search nails them.
  vector   semantic search, for paraphrases the keyword layer cannot match.
Keyword and vector scores are not comparable, so they are merged by RANK (reciprocal rank fusion).
Every layer can be switched off, which is how the experiments measure what each one contributes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .embed import Embedder
from .index import Index
from .models import Rule

LAYERS = ("anchor", "keyword", "vector")


@dataclass(frozen=True)
class Evidence:
    chunk_id: str
    text: str
    url: str
    section: str
    license: str
    layers: tuple[str, ...]


def symbol_of(rule: Rule) -> str:
    return rule.qualified_name.split(".")[-1]


def mentions(text: str, symbol: str) -> bool:
    """True if `symbol` appears as a whole name. A plain substring test would let `warn` match
    `warning` and `product` match `cumproduct`, ranking the wrong entry first."""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])", text) is not None


def rrf(*ranked: list[str], k: int = 60) -> list[str]:
    score: dict[str, float] = {}
    for lst in ranked:
        for rank, cid in enumerate(lst):
            score[cid] = score.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=lambda c: (-score[c], c))


def retrieve(
    index: Index,
    embedder: Embedder,
    rule: Rule,
    snippet: str,
    k: int = 4,
    budget_chars: int = 6000,
    layers: tuple[str, ...] = LAYERS,
) -> list[Evidence]:
    sym = symbol_of(rule)
    anchored: list[str] = []
    if "anchor" in layers:
        anchored = [
            c.chunk_id for c in index.chunks_on_url(rule.reference.url) if mentions(c.text, sym)
        ]
    keyword = (
        index.keyword([rule.qualified_name, sym], rule.library, k * 3)
        if "keyword" in layers
        else []
    )
    vector: list[str] = []
    if "vector" in layers and index.count():
        query = f"{rule.qualified_name} deprecated replacement {snippet}"
        vector = index.vector(embedder.embed([query], "query")[0], rule.library, k * 3)
    ranked = anchored + [c for c in rrf(keyword, vector) if c not in anchored]
    out: list[Evidence] = []
    used = 0
    for cid in ranked[:k]:
        row = index.get(cid)
        if row is None or used + len(row.text) > budget_chars:
            continue  # the budget protects a small model's context window
        used += len(row.text)
        found_in = tuple(
            name
            for name, ids in (("anchor", anchored), ("keyword", keyword), ("vector", vector))
            if cid in ids
        )
        out.append(Evidence(cid, row.text, row.url, row.section, row.license, found_in))
    return out


def gold_ids(index: Index, rule: Rule) -> set[str]:
    """Answer key for retrieval experiments: chunks on the rule's own page naming the symbol."""
    sym = symbol_of(rule)
    return {c.chunk_id for c in index.chunks_on_url(rule.reference.url) if mentions(c.text, sym)}


def evaluate(
    index: Index, embedder: Embedder, rules: list[Rule], k: int, layers: tuple[str, ...]
) -> dict[str, float]:
    """hit@k and mean reciprocal rank over every rule that has a gold chunk in the index."""
    hits, rr, n = 0, 0.0, 0
    for rule in rules:
        gold = gold_ids(index, rule)
        if not gold:
            continue
        n += 1
        got = [
            e.chunk_id
            for e in retrieve(index, embedder, rule, rule.qualified_name, k, 10**9, layers)
        ]
        for rank, cid in enumerate(got, 1):
            if cid in gold:
                hits += 1
                rr += 1.0 / rank
                break
    return {
        "rules": n,
        f"hit@{k}": round(hits / n, 4) if n else 0.0,
        "mrr": round(rr / n, 4) if n else 0.0,
    }
