"""Cut parsed documentation blocks into retrievable chunks. Two strategies, so we can MEASURE.

entry  one chunk per table row / list item / definition, each prefixed with its heading trail
       ("NumPy 2.0 migration guide > Main namespace"). Plain paragraphs are merged per section.
fixed  the control: ignore structure, slide a fixed window (800 chars, 150 overlap) over the text.

Hypothesis (docs/EXPERIMENTS.md, E1): entry chunks keep "old name" and "new name" together, so
they retrieve better. The benchmark decides; nothing here assumes the answer.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable
from dataclasses import dataclass

from .htmlblocks import Block

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    source_id: str
    section: str
    anchor: str
    text: str


def section_of(path: tuple[str, ...]) -> str:
    return " > ".join(path)


def _split(text: str, limit: int) -> list[str]:
    """Split on sentence boundaries into pieces of at most `limit` characters."""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE.split(text):
        while len(sentence) > limit:  # a single enormous sentence: hard split
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def chunk_entries(blocks: list[Block], source_id: str, max_chars: int = 1200) -> list[Chunk]:
    out: list[Chunk] = []
    pending: list[Block] = []

    def emit(path: tuple[str, ...], anchor: str, text: str) -> None:
        section = section_of(path)
        prefix = f"{section}\n" if section else ""
        for part in _split(text, max(200, max_chars - len(prefix))):
            out.append(
                Chunk(f"{source_id}#entry:{len(out)}", source_id, section, anchor, prefix + part)
            )

    def flush() -> None:
        if pending:
            emit(pending[0].path, pending[0].anchor, "\n".join(b.text for b in pending))
            pending.clear()

    for b in blocks:
        if b.kind in ("row", "item", "entry"):
            flush()
            emit(b.path, b.anchor, b.text)  # one entry, one chunk: old and new name stay together
            continue
        size = sum(len(x.text) + 1 for x in pending) + len(b.text)
        if pending and (pending[-1].path != b.path or size > max_chars):
            flush()
        pending.append(b)
    flush()
    return out


def chunk_fixed(
    blocks: list[Block], source_id: str, size: int = 800, overlap: int = 150
) -> list[Chunk]:
    stream = ""
    starts: list[int] = []
    owners: list[Block] = []
    for b in blocks:
        starts.append(len(stream))
        owners.append(b)
        stream += b.text + "\n\n"
    out: list[Chunk] = []
    step = max(1, size - overlap)
    for i in range(0, max(len(stream), 1), step):
        window = stream[i : i + size]
        if not window.strip():
            continue
        owner = owners[max(0, bisect.bisect_right(starts, i) - 1)] if owners else None
        out.append(
            Chunk(
                f"{source_id}#fixed:{len(out)}",
                source_id,
                section_of(owner.path) if owner else "",
                owner.anchor if owner else "",
                window,
            )
        )
        if i + size >= len(stream):
            break
    return out


CHUNKERS: dict[str, Callable[..., list[Chunk]]] = {"entry": chunk_entries, "fixed": chunk_fixed}
