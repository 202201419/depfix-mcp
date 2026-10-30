from pick import pick


def test_pick():
    assert pick(type("X", (), {"float_": 5})) == 5
