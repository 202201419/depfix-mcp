from clock import stamp


def test_naive():
    assert stamp().tzinfo is None
