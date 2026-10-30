import threading

from who import current_name


def test_name():
    assert current_name() == threading.current_thread().name
