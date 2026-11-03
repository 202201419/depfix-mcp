"""The documentation index: one SQLite file with full-text search AND vectors.

Why one SQLite file (not Chroma/FAISS): no extra dependency, keyword search (FTS5/BM25) and vector
search live side by side, and it is trivial to inspect. Vector search is a plain dot product over
normalised vectors, which is fast enough for thousands of chunks. Revisit if the index grows past
roughly 20,000 chunks (docs/DECISIONS.md, D34).
"""

from __future__ import annotations

import re
import sqlite3
import threading
from array import array
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from .chunking import Chunk

_TERM = re.compile(r"[A-Za-z0-9_]+")


class IndexError_(Exception):
    pass


@dataclass(frozen=True)
class ChunkRow:
    chunk_id: str
    source_id: str
    section: str
    anchor: str
    text: str
    url: str
    license: str
    library: str


class Index:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One connection shared by worker threads (the benchmark runs cases in a pool). Every access
        # goes through self._lock, so queries never interleave.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        try:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources(
                    id TEXT PRIMARY KEY, library TEXT, version TEXT, url TEXT,
                    license TEXT, license_url TEXT, attribution TEXT);
                CREATE TABLE IF NOT EXISTS chunks(
                    chunk_id TEXT PRIMARY KEY, source_id TEXT, section TEXT, anchor TEXT,
                    text TEXT, vec BLOB);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED, text, tokenize="unicode61 tokenchars '_'");
                """
            )
        except sqlite3.OperationalError as exc:
            raise IndexError_(f"this Python's SQLite lacks FTS5 full-text search: {exc}") from exc

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: type[BaseException] | BaseException | TracebackType | None) -> None:
        self.db.close()

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return list(self.db.execute(sql, params).fetchall())

    # -- metadata: an index is tied to ONE chunker and ONE embedder --
    def get_meta(self, key: str) -> str | None:
        rows = self._rows("SELECT value FROM meta WHERE key=?", (key,))
        return str(rows[0][0]) if rows else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            self.db.commit()

    def check_embedder(self, name: str) -> None:
        """Vectors from different embedders are not comparable; refuse to mix them."""
        have = self.get_meta("embedder")
        if have is not None and have != name:
            raise IndexError_(
                f"this index was built with embedder '{have}', not '{name}'; "
                "rebuild it or use the same one"
            )

    # -- writing --
    def add_source(
        self,
        sid: str,
        library: str,
        version: str,
        url: str,
        license_: str,
        license_url: str,
        attribution: str,
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?)",
            (sid, library, version, url, license_, license_url, attribution),
        )

    def replace_chunks(
        self, source_id: str, chunks: list[Chunk], vectors: list[list[float]]
    ) -> None:
        old = [
            r[0]
            for r in self.db.execute("SELECT chunk_id FROM chunks WHERE source_id=?", (source_id,))
        ]
        for cid in old:
            self.db.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (cid,))
        self.db.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
        for c, v in zip(chunks, vectors, strict=True):
            self.db.execute(
                "INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                (c.chunk_id, c.source_id, c.section, c.anchor, c.text, array("f", v).tobytes()),
            )
            self.db.execute(
                "INSERT INTO chunks_fts(chunk_id, text) VALUES (?, ?)", (c.chunk_id, c.text)
            )
        self.db.commit()

    # -- reading --
    _SELECT = (
        "SELECT c.chunk_id, c.source_id, c.section, c.anchor, c.text, s.url, s.license, s.library "
        "FROM chunks c JOIN sources s ON s.id = c.source_id"
    )

    def get(self, chunk_id: str) -> ChunkRow | None:
        rows = self._rows(f"{self._SELECT} WHERE c.chunk_id=?", (chunk_id,))
        return ChunkRow(*rows[0]) if rows else None

    def chunks_on_url(self, url: str) -> list[ChunkRow]:
        base = url.split("#")[0]
        rows = self._rows(f"{self._SELECT} WHERE s.url=? ORDER BY c.rowid", (base,))
        return [ChunkRow(*r) for r in rows]

    def count(self) -> int:
        return int(self._rows("SELECT COUNT(*) FROM chunks")[0][0])

    def stats(self) -> dict[str, object]:
        per = self._rows(
            "SELECT s.id, s.library, s.license, COUNT(c.chunk_id) FROM sources s "
            "LEFT JOIN chunks c ON c.source_id = s.id GROUP BY s.id ORDER BY s.id"
        )
        return {
            "chunks": self.count(),
            "embedder": self.get_meta("embedder"),
            "chunker": self.get_meta("chunker"),
            "sources": [
                {"id": r[0], "library": r[1], "license": r[2], "chunks": r[3]} for r in per
            ],
        }

    def keyword(self, terms: list[str], library: str | None, limit: int) -> list[str]:
        """BM25 keyword search. Terms are reduced to [A-Za-z0-9_] words and quoted, because FTS5
        has its own query syntax and the terms come from rules and code, which we do not trust."""
        words = [t for term in terms for t in _TERM.findall(term)]
        if not words:
            return []
        match = " OR ".join(f'"{w}"' for w in dict.fromkeys(words))
        rows = self._rows(
            "SELECT chunks_fts.chunk_id FROM chunks_fts "
            "JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id "
            "JOIN sources s ON s.id = c.source_id "
            "WHERE chunks_fts MATCH ? AND (? IS NULL OR s.library = ?) "
            "ORDER BY bm25(chunks_fts) LIMIT ?",
            (match, library, library, limit),
        )
        return [r[0] for r in rows]

    def vector(self, query: list[float], library: str | None, limit: int) -> list[str]:
        rows = self._rows(
            "SELECT c.chunk_id, c.vec FROM chunks c JOIN sources s ON s.id = c.source_id "
            "WHERE (? IS NULL OR s.library = ?)",
            (library, library),
        )
        scored: list[tuple[float, str]] = []
        for cid, blob in rows:
            vec = array("f")
            vec.frombytes(blob)
            if len(vec) != len(query):
                raise IndexError_(
                    "vector size mismatch: the index and the query use different embedders"
                )
            scored.append((sum(a * b for a, b in zip(query, vec, strict=True)), cid))
        scored.sort(reverse=True)
        return [cid for _, cid in scored[:limit]]
