from run import run


def test_run():
    n, t = run()
    assert n >= 1 and t.tzinfo is None
