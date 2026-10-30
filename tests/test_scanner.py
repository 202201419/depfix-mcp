from pathlib import Path

import pytest

from depfix.scanner import UnsupportedFile, decode_source, scan_repo, scan_text
from tests.helpers import RULES, write_repo


def scan(src: str, rel: str = "m.py"):
    return scan_text(rel, src.encode("utf-8"), RULES)


def test_resolves_import_alias():
    f = scan("import numpy as np\nx = np.float_(1)\n")
    assert [(x.rule_id, x.snippet, x.start_line) for x in f] == [("NP-0001", "np.float_", 2)]


def test_resolves_unusual_alias_and_from_import():
    f = scan("import numpy as numpy_lib\nfrom numpy import NaN as n\na = numpy_lib.NaN\nb = n\n")
    assert sorted(x.snippet for x in f) == ["n", "numpy_lib.NaN"]


def test_comments_and_strings_never_match():
    src = '"""np.float_ in a docstring"""\nimport numpy as np\n# np.float_ here\ns = "np.float_"\n'
    assert scan(src) == []


def test_name_that_is_not_imported_is_not_flagged():
    src = "class NS:\n    float_ = float\nnp = NS()\nx = np.float_(1)\n"
    assert scan(src) == []


def test_conflicting_bindings_are_refused_not_guessed():
    src = "import numpy as np\nimport other as np\nx = np.float_\n"
    assert scan(src) == []


def test_assignment_and_delete_are_not_usages():
    assert scan("import numpy as np\nnp.float_ = 3\ndel np.float_\n") == []


def test_call_rule_spans_the_whole_call():
    f = scan("import datetime\nx = datetime.datetime.utcnow()\n")
    assert len(f) == 1 and f[0].snippet == "datetime.datetime.utcnow()"
    assert (f[0].start_col, f[0].end_col) == (4, 30)


def test_bare_reference_to_call_rule_is_ignored():
    assert scan("import datetime\nf = datetime.datetime.utcnow\n") == []


def test_ids_survive_line_shifts_and_differ_for_identical_usages():
    one = "import numpy as np\na = np.float_(1)\nb = np.float_(1)\n"
    shifted = "import numpy as np\n\n\n# comment\na = np.float_(1)\nb = np.float_(1)\n"
    ids1 = [f.id for f in scan(one)]
    ids2 = [f.id for f in scan(shifted)]
    assert len(set(ids1)) == 2
    assert ids1 == ids2


def test_nested_function_imports_are_found():
    src = "def f():\n    import numpy as np\n    return np.Inf\n"
    assert [x.rule_id for x in scan(src)] == ["NP-0004"]


@pytest.mark.parametrize(
    "data",
    [b"\xef\xbb\xbfx = 1\n", b"x = 1\ry = 2\r", b"# -*- coding: latin-1 -*-\nx = 1\n", b"\xff\xfe"],
)
def test_unsupported_encodings_are_refused(data):
    with pytest.raises(UnsupportedFile):
        decode_source(data)


def test_scan_repo_skips_bad_files_symlinks_and_ignored_dirs(tmp_path: Path):
    write_repo(
        tmp_path,
        {
            "ok.py": "import numpy as np\nx = np.NaN\n",
            "broken.py": "def (:\n",
            ".venv/lib.py": "import numpy as np\nx = np.NaN\n",
            "build_tool/__pycache__/c.py": "import numpy as np\nx = np.NaN\n",
        },
    )
    outside = tmp_path.parent / "outside_target.py"
    outside.write_text("import numpy as np\nx = np.NaN\n")
    (tmp_path / "link.py").symlink_to(outside)
    res = scan_repo(tmp_path, RULES)
    assert [f.file for f in res.findings] == ["ok.py"]
    assert any("broken.py" in e for e in res.errors)


def test_library_filter():
    src = "import numpy as np\nimport logging\nnp.NaN\nlogging.warn('x')\n"
    only = RULES.for_libraries({"numpy"})
    assert [f.library for f in scan_text("m.py", src.encode(), only)] == ["numpy"]
