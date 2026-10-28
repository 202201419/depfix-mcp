"""Mentions np.float_ in a docstring."""


class _NS:
    float_ = float


np = _NS()


def build():
    # np.NaN in a comment
    return np.float_(1)
