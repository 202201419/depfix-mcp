from tag import tagged


def test_tagged():
    label, value = tagged()
    assert label.startswith("naïve")
    assert float(value) == 1.0
