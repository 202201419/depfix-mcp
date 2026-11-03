"""Build the documentation index: fetch pages, parse, chunk, embed, store."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .chunking import CHUNKERS
from .embed import Embedder
from .htmlblocks import parse_html
from .index import Index, IndexError_
from .sources import Source


@dataclass
class BuildReport:
    per_source: dict[str, int] = field(default_factory=dict)

    @property
    def chunks(self) -> int:
        return sum(self.per_source.values())


def build_index(
    index: Index,
    sources: list[Source],
    embedder: Embedder,
    chunker: str,
    read: Callable[[Source], str],
    batch: int = 32,
) -> BuildReport:
    if chunker not in CHUNKERS:
        raise IndexError_(f"unknown chunker '{chunker}' (choose from {sorted(CHUNKERS)})")
    index.check_embedder(embedder.name)
    prior = index.get_meta("chunker")
    if prior not in (None, chunker):
        raise IndexError_(
            f"this index uses the '{prior}' chunker; rebuild from scratch "
            f"to switch to '{chunker}' (--rebuild)"
        )
    report = BuildReport()
    for src in sources:
        chunks = CHUNKERS[chunker](parse_html(read(src)), src.id)
        vectors: list[list[float]] = []
        for i in range(0, len(chunks), batch):
            vectors.extend(embedder.embed([c.text for c in chunks[i : i + batch]], "document"))
        index.add_source(
            src.id, src.library, src.version, src.url.split("#")[0], src.license,
            src.license_url, src.attribution,
        )  # fmt: skip
        index.replace_chunks(src.id, chunks, vectors)
        report.per_source[src.id] = len(chunks)
    index.set_meta("embedder", embedder.name)
    index.set_meta("chunker", chunker)
    return report
