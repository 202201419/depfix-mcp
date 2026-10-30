"""Human approval records.

An approval says: "a person looked at THIS exact diff and said yes, until THIS time".

Three things make it hard to fake:
  1. It is bound to the diff's SHA-256, so a swapped patch no longer matches.
  2. It expires (15 minutes) and is single-use.
  3. It carries a MAC (a keyed fingerprint) made with a secret key stored OUTSIDE the repo
     (~/.config/depfix/approval.key, mode 600). Editing the database by hand cannot produce a
     valid MAC without that key.

Honest limit: this is defence in depth, not a wall. A program that can read your home folder AND
write your repo can still forge an approval. Do not give an AI tool unrestricted shell access
next to depfix. (See docs/SAFETY.md.)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import Approval

DEFAULT_TTL_S = 900


def key_path() -> Path:
    base = os.environ.get("DEPFIX_HOME")
    root = Path(base) if base else Path.home() / ".config" / "depfix"
    return root / "approval.key"


def _key() -> bytes:
    path = key_path()
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = secrets.token_bytes(32)
    # O_EXCL: never overwrite a key another process just created. 0o600: owner-only.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def _mac(finding_id: str, diff_sha: str, expires_at: str) -> str:
    msg = f"{finding_id}|{diff_sha}|{expires_at}".encode()
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()


def issue(finding_id: str, diff_sha: str, ttl_s: int = DEFAULT_TTL_S) -> Approval:
    now = datetime.now(UTC)
    expires = (now + timedelta(seconds=ttl_s)).isoformat(timespec="seconds")
    return Approval(
        finding_id=finding_id,
        diff_sha256=diff_sha,
        approved_at=now.isoformat(timespec="seconds"),
        expires_at=expires,
        mac=_mac(finding_id, diff_sha, expires),
    )


def check(
    a: Approval | None, finding_id: str, diff_sha: str, now: datetime | None = None
) -> str | None:
    """Return None if the approval is valid, otherwise the reason it is not."""
    if a is None:
        return "there is no approval on record"
    if a.used:
        return "this approval was already used"
    # compare_digest: a constant-time comparison, so the check leaks nothing through timing.
    if not hmac.compare_digest(a.mac, _mac(a.finding_id, a.diff_sha256, a.expires_at)):
        return "the approval record failed its integrity check"
    if a.finding_id != finding_id:
        return "the approval belongs to another finding"
    if not hmac.compare_digest(a.diff_sha256, diff_sha):
        return "the diff changed after it was approved"
    if (now or datetime.now(UTC)) > datetime.fromisoformat(a.expires_at):
        return "the approval expired"
    return None
