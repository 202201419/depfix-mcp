from codec import encode


def test_encode():
    assert encode(b"hi") == b"aGk=\n"
