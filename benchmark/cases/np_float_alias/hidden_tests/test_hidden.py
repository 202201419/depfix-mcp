import numpy as np
from calc import to_float


def test_type_is_float64():
    assert type(to_float(3)) is np.float64
