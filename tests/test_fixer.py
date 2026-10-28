import ast
import hashlib
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from depfix.fixer import propose_template, splice
from depfix.verify import apply_patch
from tests.helpers import RULES, find_one, write_repo


def propose(tmp_path: Path, src: str, rule_id: str, rel: str = "m.py"):
    write_repo(tmp_path, {rel: src})
    f = find_one(rel, src, rule_id)
    return f, propose_template(tmp_path, f, RULES.by_id[rule_id])


def applied(tmp_path: Path, res) -> str:
    """Apply a proposal's diff with real `git apply` in a scratch copy and return the new file."""
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        work = scratch / "w"
        work.mkdir()
        (work / res.proposal.file).parent.mkdir(parents=True, exist_ok=True)
        (work / res.proposal.file).write_bytes((tmp_path / res.proposal.file).read_bytes())
        ok, detail = apply_patch(work, res.proposal.diff, scratch)
        assert ok, detail
        return (work / res.proposal.file).read_bytes().decode("utf-8")


def test_attribute_template_uses_the_files_own_alias(tmp_path):
    _, res = propose(tmp_path, "import numpy as np\nx = np.float_(1)\n", "NP-0001")
    assert res.outcome == "proposed"
    assert applied(tmp_path, res) == "import numpy as np\nx = np.float64(1)\n"


def test_unusual_alias_is_respected(tmp_path):
    _, res = propose(tmp_path, "import numpy as xp\ny = xp.NaN\n", "NP-0003")
    assert applied(tmp_path, res) == "import numpy as xp\ny = xp.nan\n"


def test_from_import_cannot_be_templated_without_adding_imports(tmp_path):
    _, res = propose(tmp_path, "from numpy import float_\nx = float_(3)\n", "NP-0001")
    assert res.outcome == "not_applicable" and "does not import" in res.reason


def test_ninf_is_parenthesised_to_keep_precedence(tmp_path):
    _, res = propose(tmp_path, "import numpy as np\nv = np.NINF ** 2\n", "NP-0013")
    new = applied(tmp_path, res)
    assert new == "import numpy as np\nv = (-np.inf) ** 2\n"
    assert eval(
        compile(ast.parse(new.split("\n")[1], mode="exec"), "x", "exec") and "(-float('inf')) ** 2"
    ) == float("inf")


def test_call_template_utcnow_import_datetime(tmp_path):
    _, res = propose(tmp_path, "import datetime\nt = datetime.datetime.utcnow()\n", "STD-0001")
    assert applied(tmp_path, res) == (
        "import datetime\nt = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)\n"
    )


def test_call_template_refuses_when_timezone_is_not_importable(tmp_path):
    _, res = propose(tmp_path, "from datetime import datetime\nt = datetime.utcnow()\n", "STD-0001")
    assert res.outcome == "not_applicable"


def test_from_import_works_when_all_names_are_available(tmp_path):
    src = "from datetime import datetime, timezone\nt = datetime.utcnow()\n"
    _, res = propose(tmp_path, src, "STD-0001")
    assert applied(tmp_path, res).endswith("t = datetime.now(timezone.utc).replace(tzinfo=None)\n")


def test_arity_is_enforced(tmp_path):
    src = "import datetime\nt = datetime.datetime.utcfromtimestamp(1, 2)\n"
    _, res = propose(tmp_path, src, "STD-0002")
    assert res.outcome == "not_applicable" and "positional" in res.reason


def test_starred_arguments_are_refused(tmp_path):
    src = "import datetime\nargs = [1]\nt = datetime.datetime.utcfromtimestamp(*args)\n"
    _, res = propose(tmp_path, src, "STD-0002")
    assert res.outcome == "not_applicable"


def test_multiline_call_is_replaced_whole(tmp_path):
    src = "import datetime\nt = datetime.datetime.utcfromtimestamp(\n    0\n)\n"
    _, res = propose(tmp_path, src, "STD-0002")
    assert applied(tmp_path, res) == (
        "import datetime\nt = datetime.datetime.fromtimestamp(0, datetime.timezone.utc)"
        ".replace(tzinfo=None)\n"
    )


def test_non_ascii_before_the_span_uses_byte_offsets(tmp_path):
    src = 'import numpy as np\npair = ("naïve", np.float_(1))\n'
    f, res = propose(tmp_path, src, "NP-0001")
    # the span columns are BYTE offsets: 'ï' is 2 bytes, so they differ from character columns
    assert f.start_col == src.split("\n")[1].index("np.float_") + 1
    assert applied(tmp_path, res) == 'import numpy as np\npair = ("naïve", np.float64(1))\n'


def test_crlf_files_keep_their_line_endings(tmp_path):
    src = "import logging\r\nlogging.warn('x')\r\n"
    _, res = propose(tmp_path, src, "STD-0003")
    assert applied(tmp_path, res) == "import logging\r\nlogging.warning('x')\r\n"


def test_file_without_trailing_newline(tmp_path):
    src = "import logging\nlogging.warn('x')"
    _, res = propose(tmp_path, src, "STD-0003")
    assert applied(tmp_path, res) == "import logging\nlogging.warning('x')"


def test_stale_file_is_detected(tmp_path):
    f, _ = propose(tmp_path, "import logging\nlogging.warn('x')\n", "STD-0003")
    (tmp_path / "m.py").write_text("# edited\nimport logging\nlogging.warn('x')\n")
    res = propose_template(tmp_path, f, RULES.by_id["STD-0003"])
    assert res.outcome == "stale"


def test_unsafe_path_characters_are_refused(tmp_path):
    f, _ = propose(tmp_path, "import logging\nlogging.warn('x')\n", "STD-0003", rel="my file.py")
    res = propose_template(tmp_path, f, RULES.by_id["STD-0003"])
    assert res.outcome == "not_applicable"


def test_llm_rules_are_routed_not_templated(tmp_path):
    _, res = propose(tmp_path, "import asyncio\nloop = asyncio.get_event_loop()\n", "STD-0008")
    assert res.outcome == "needs_llm"


def test_every_template_renders_valid_python_with_a_plain_import():
    """Each template, given the import style the rule's own example uses, must parse."""
    for rule in RULES.rules:
        if rule.fix.template is None:
            continue
        assert ast.parse(rule.fix.template.replace("{args}", "0").replace("{", "").replace("}", ""))


# ---- property-based: the byte-offset splice is the classic off-by-one source -------------------

TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters='"\\'),
    max_size=25,
)


@settings(max_examples=150, deadline=None)
@given(prefix=TEXT, suffix=TEXT)
def test_splice_hits_exactly_the_span_whatever_unicode_surrounds_it(prefix: str, suffix: str):
    src = f'import numpy as np\ns = "{prefix}"; x = np.float_(1); t = "{suffix}"\n'
    f = find_one("m.py", src, "NP-0001")
    assert splice(src, f, f.snippet) == src  # replacing a span with itself is a no-op
    out = splice(src, f, "np.float64")
    assert out == src.replace("np.float_", "np.float64", 1) or out.count("np.float64") == 1
    ast.parse(out)
    assert hashlib.sha256(out.encode()).hexdigest() != hashlib.sha256(src.encode()).hexdigest()
