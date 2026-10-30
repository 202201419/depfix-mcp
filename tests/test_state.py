import pytest

from depfix.models import ALLOWED, InvalidTransition, Status, check_transition
from depfix.store import Store
from tests.helpers import RULES, find_one, write_repo


def test_every_status_has_a_transition_entry():
    assert set(ALLOWED) == set(Status)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (Status.FOUND, Status.VERIFIED),  # cannot skip proposing
        (Status.FOUND, Status.APPLIED),
        (Status.PROPOSED, Status.APPROVED),  # cannot skip verification
        (Status.PROPOSED, Status.APPLIED),
        (Status.FAILED, Status.APPROVED),
        (Status.VERIFIED, Status.APPLIED),  # cannot skip approval
        (Status.SKIPPED, Status.PROPOSED),
        (Status.APPLIED, Status.FOUND),
    ],
)
def test_illegal_transitions_raise(old, new):
    with pytest.raises(InvalidTransition):
        check_transition(old, new)


def test_legal_path_to_applied():
    path = [Status.FOUND, Status.PROPOSED, Status.VERIFIED, Status.APPROVED, Status.APPLIED]
    for a, b in zip(path, path[1:], strict=False):
        check_transition(a, b)


def test_store_keeps_progress_on_rescan_and_resets_when_file_changes(tmp_path):
    src = "import logging\nlogging.warn('x')\n"
    write_repo(tmp_path, {"m.py": src})
    f = find_one("m.py", src, "STD-0003")
    with Store(tmp_path) as s:
        s.upsert_scan([f], None)
        s.set_status(f.id, Status.PROPOSED)
        s.upsert_scan([f], None)  # same file hash: status kept
        assert s.get_finding(f.id).status is Status.PROPOSED
        changed = f.model_copy(update={"file_sha256": "different"})
        s.upsert_scan([changed], None)  # file changed: reset to FOUND
        assert s.get_finding(f.id).status is Status.FOUND


def test_store_enforces_the_state_machine(tmp_path):
    src = "import logging\nlogging.warn('x')\n"
    write_repo(tmp_path, {"m.py": src})
    f = find_one("m.py", src, "STD-0003")
    with Store(tmp_path) as s:
        s.upsert_scan([f], None)
        with pytest.raises(InvalidTransition):
            s.set_status(f.id, Status.APPLIED)


def test_rescan_drops_vanished_findings_only_within_scanned_libraries(tmp_path):
    src = "import logging\nlogging.warn('x')\n"
    write_repo(tmp_path, {"m.py": src})
    f = find_one("m.py", src, "STD-0003")
    with Store(tmp_path) as s:
        s.upsert_scan([f], None)
        s.upsert_scan([], {"numpy"})  # a numpy-only scan must not delete stdlib findings
        assert s.get_finding(f.id) is not None
        s.upsert_scan([], None)
        assert s.get_finding(f.id) is None


def test_rules_have_unique_ids_and_names():
    assert len({r.id for r in RULES.rules}) == len(RULES.rules)
    assert len({r.qualified_name for r in RULES.rules}) == len(RULES.rules)
