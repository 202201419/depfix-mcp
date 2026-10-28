import datetime

from epoch import epoch


def test_epoch():
    assert epoch() == datetime.datetime(1970, 1, 1)
