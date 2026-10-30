from clock import stamp


def test_stamp():
    assert stamp().tzinfo is None
