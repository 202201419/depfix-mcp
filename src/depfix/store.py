"""SQLite state in <repo>/.depfix/state.db. Findings, proposals and verification reports."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Self

from .models import Finding, Proposal, Status, Verification, check_transition


class Store:
    def __init__(self, repo: Path) -> None:
        folder = repo / ".depfix"
        folder.mkdir(exist_ok=True)
        self.db = sqlite3.connect(folder / "state.db")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS findings(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS proposals(finding_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS verifications(
                finding_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            """
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: type[BaseException] | BaseException | TracebackType | None) -> None:
        self.db.close()

    def upsert_scan(self, found: list[Finding], libraries: set[str] | None) -> None:
        """Merge a scan into the database.

        Same id AND same file hash keeps its status (so re-scanning does not erase your progress).
        A changed file resets the finding. Findings that vanished are dropped, but only within the
        libraries this scan covered.
        """
        existing = {f.id: f for f in self.list_findings()}
        new_ids = {f.id for f in found}
        for f in found:
            old = existing.get(f.id)
            if old is not None and old.file_sha256 == f.file_sha256:
                continue
            self.db.execute(
                "INSERT OR REPLACE INTO findings VALUES (?, ?)", (f.id, f.model_dump_json())
            )
            self.db.execute("DELETE FROM proposals WHERE finding_id=?", (f.id,))
            self.db.execute("DELETE FROM verifications WHERE finding_id=?", (f.id,))
        for fid, old in existing.items():
            if fid not in new_ids and (not libraries or old.library in libraries):
                for table in ("findings", "proposals", "verifications"):
                    col = "id" if table == "findings" else "finding_id"
                    self.db.execute(f"DELETE FROM {table} WHERE {col}=?", (fid,))
        self.db.commit()

    def list_findings(self) -> list[Finding]:
        rows = self.db.execute("SELECT data FROM findings").fetchall()
        found = [Finding.model_validate_json(r[0]) for r in rows]
        return sorted(found, key=lambda f: (f.file, f.start_line, f.start_col))

    def get_finding(self, fid: str) -> Finding | None:
        row = self.db.execute("SELECT data FROM findings WHERE id=?", (fid,)).fetchone()
        return Finding.model_validate_json(row[0]) if row else None

    def set_status(self, fid: str, new: Status, note: str | None = None) -> None:
        f = self.get_finding(fid)
        if f is None:
            raise KeyError(fid)
        check_transition(f.status, new)  # raises InvalidTransition on an illegal move
        f.status = new
        if note is not None:
            f.note = note
        self.db.execute("UPDATE findings SET data=? WHERE id=?", (f.model_dump_json(), fid))
        self.db.commit()

    def set_note(self, fid: str, note: str) -> None:
        f = self.get_finding(fid)
        if f is not None:
            f.note = note
            self.db.execute("UPDATE findings SET data=? WHERE id=?", (f.model_dump_json(), fid))
            self.db.commit()

    def save_proposal(self, p: Proposal) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO proposals VALUES (?, ?)", (p.finding_id, p.model_dump_json())
        )
        self.db.execute("DELETE FROM verifications WHERE finding_id=?", (p.finding_id,))
        self.db.commit()

    def get_proposal(self, fid: str) -> Proposal | None:
        row = self.db.execute("SELECT data FROM proposals WHERE finding_id=?", (fid,)).fetchone()
        return Proposal.model_validate_json(row[0]) if row else None

    def save_verification(self, v: Verification) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO verifications VALUES (?, ?)",
            (v.finding_id, v.model_dump_json()),
        )
        self.db.commit()

    def get_verification(self, fid: str) -> Verification | None:
        row = self.db.execute(
            "SELECT data FROM verifications WHERE finding_id=?", (fid,)
        ).fetchone()
        return Verification.model_validate_json(row[0]) if row else None
