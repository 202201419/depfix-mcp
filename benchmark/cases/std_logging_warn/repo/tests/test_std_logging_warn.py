from notifier import notify


def test_notify():
    assert notify("a") == 1
