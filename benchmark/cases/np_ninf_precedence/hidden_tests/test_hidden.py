import math

from inf import squared_neg_inf


def test_positive_infinity():
    v = squared_neg_inf()
    assert math.isinf(v) and v > 0
