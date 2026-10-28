import a
import b


def test_other_file_untouched():
    assert a.LIMIT == float("inf")
    assert b.NOTE == "np.Inf is gone"
