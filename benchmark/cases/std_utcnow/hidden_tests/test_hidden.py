import datetime

from clock import stamp


def test_recent_and_naive():
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    assert stamp().tzinfo is None
    assert abs((now - stamp()).total_seconds()) < 5
