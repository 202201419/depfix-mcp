import a
import b


def test_values():
    assert a.LIMIT == float("inf")
    assert "np.Inf" in b.NOTE
