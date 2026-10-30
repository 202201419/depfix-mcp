import math

from values import missing


def test_missing_is_nan():
    assert math.isnan(missing())
