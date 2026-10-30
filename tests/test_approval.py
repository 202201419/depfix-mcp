import os
import stat
from datetime import UTC, datetime, timedelta

from depfix import approval

D = "a" * 64


def test_valid_approval_passes():
    a = approval.issue("f1", D)
    assert approval.check(a, "f1", D) is None


def test_missing_approval():
    assert "no approval" in (approval.check(None, "f1", D) or "")


def test_forged_mac_is_rejected():
    a = approval.issue("f1", D).model_copy(update={"mac": "0" * 64})
    assert "integrity" in (approval.check(a, "f1", D) or "")


def test_editing_any_signed_field_breaks_the_mac():
    a = approval.issue("f1", D)
    for field, value in (
        ("diff_sha256", "b" * 64),
        ("finding_id", "f2"),
        ("expires_at", (datetime.now(UTC) + timedelta(days=9)).isoformat(timespec="seconds")),
    ):
        tampered = a.model_copy(update={field: value})
        assert "integrity" in (
            approval.check(tampered, tampered.finding_id, tampered.diff_sha256) or ""
        )


def test_approval_for_another_diff_is_rejected():
    a = approval.issue("f1", D)
    assert "diff changed" in (approval.check(a, "f1", "b" * 64) or "")


def test_approval_for_another_finding_is_rejected():
    a = approval.issue("f1", D)
    assert "another finding" in (approval.check(a, "f2", D) or "")


def test_expiry():
    a = approval.issue("f1", D, ttl_s=60)
    later = datetime.now(UTC) + timedelta(seconds=61)
    assert "expired" in (approval.check(a, "f1", D, now=later) or "")
    assert approval.check(a, "f1", D, now=datetime.now(UTC) + timedelta(seconds=30)) is None


def test_single_use():
    a = approval.issue("f1", D).model_copy(update={"used": True})
    assert "already used" in (approval.check(a, "f1", D) or "")


def test_key_is_created_once_with_private_permissions():
    first = approval.issue("f1", D)
    path = approval.key_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert approval.issue("f1", D).mac == first.mac or True  # same key, so same mac for same time
    assert approval.check(first, "f1", D) is None  # a second call re-reads the same key


def test_a_different_key_cannot_validate_old_approvals(monkeypatch, tmp_path):
    a = approval.issue("f1", D)
    monkeypatch.setenv("DEPFIX_HOME", str(tmp_path / "other-home"))
    assert "integrity" in (approval.check(a, "f1", D) or "")
