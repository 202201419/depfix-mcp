from calc import to_float


def test_to_float():
    assert float(to_float(2)) == 2.0
