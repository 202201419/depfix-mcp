"""Documentation side: HTML parsing, chunking, the index, and polite fetching."""

import io
import urllib.error
from pathlib import Path

import pytest

from depfix.chunking import chunk_entries, chunk_fixed, section_of
from depfix.embed import HashEmbedder
from depfix.htmlblocks import parse_html
from depfix.index import Index, IndexError_
from depfix.indexer import build_index
from depfix.sources import FetchError, Source, fetch_html, load_sources, read_source
from tests.helpers import DOCS, fixture_index, http_error

NUMPY = (DOCS / "numpy_removals.html").read_text()
LOGGING = (DOCS / "python_logging.html").read_text()

# ---- html -> blocks ----------------------------------------------------------------------------


def test_navigation_footer_and_scripts_are_ignored():
    blocks = parse_html(
        "<nav>MENU</nav><script>evil()</script><p>kept text</p><footer>FOOT</footer>"
    )
    assert [b.text for b in blocks] == ["kept text"]


def test_headings_form_a_trail_and_deeper_headings_nest():
    html = "<h1>A</h1><p>one</p><h2>B</h2><p>two</p><h2>C</h2><p>three</p><h1>D</h1><p>four</p>"
    paths = {b.text: b.path for b in parse_html(html)}
    assert paths == {"one": ("A",), "two": ("A", "B"), "three": ("A", "C"), "four": ("D",)}


def test_table_rows_become_one_block_each_and_header_rows_are_skipped():
    blocks = [b for b in parse_html(NUMPY) if b.kind == "row"]
    assert len(blocks) == 16
    assert blocks[0].text.startswith("np.float_ | Removed in 2.0")
    assert not any("Removed name" in b.text for b in blocks)


def test_definition_entries_keep_their_title_and_anchor():
    entries = [b for b in parse_html(LOGGING) if b.kind == "entry"]
    warn = next(b for b in entries if b.anchor == "logging.warn")
    assert "logging.warn(msg" in warn.text and "logging.warning" in warn.text


def test_permalink_marks_are_removed_and_code_blocks_keep_newlines():
    blocks = parse_html("<h1>T&para;</h1><pre>a = 1\nb = 2</pre>")
    assert (
        blocks[0].kind == "code" and blocks[0].path == ("T",) and blocks[0].text == "a = 1\nb = 2"
    )


def test_sloppy_html_does_not_crash():
    assert parse_html("<p>unclosed <b>bold <li>item <td>cell") is not None
    assert parse_html("") == []


# ---- chunking ----------------------------------------------------------------------------------


def test_entry_chunks_one_per_row_and_never_mix_rows():
    chunks = chunk_entries(parse_html(NUMPY), "n")
    float_chunk = next(c for c in chunks if "np.float_" in c.text)
    assert "np.complex_" not in float_chunk.text  # the old name and ITS answer, nothing else
    assert "np.float64" in float_chunk.text
    assert float_chunk.text.startswith("Migrating to the 2.0 series > Main namespace\n")


def test_entry_chunks_keep_old_and_new_name_together_for_definition_lists():
    chunks = chunk_entries(parse_html(LOGGING), "l")
    warn = next(c for c in chunks if "logging.warn(" in c.text)
    assert "logging.warning" in warn.text


def test_fixed_chunks_respect_size_and_overlap():
    text = "<p>" + " ".join(f"word{i}" for i in range(600)) + "</p>"
    chunks = chunk_fixed(parse_html(text), "f", size=300, overlap=60)
    assert len(chunks) > 3 and all(len(c.text) <= 300 for c in chunks)
    assert chunks[0].text[-40:] in chunks[1].text  # consecutive windows overlap


def test_long_paragraphs_are_split_on_sentences():
    para = " ".join(f"Sentence number {i} is here." for i in range(100))
    chunks = chunk_entries(parse_html(f"<h1>H</h1><p>{para}</p>"), "p", max_chars=400)
    assert len(chunks) > 3 and all(len(c.text) <= 400 for c in chunks)
    assert all(c.text.startswith("H\n") for c in chunks)


def test_section_of_joins_the_trail():
    assert section_of(("A", "B")) == "A > B" and section_of(()) == ""


# ---- index -------------------------------------------------------------------------------------


def test_keyword_search_matches_names_with_trailing_underscores():
    idx, _ = fixture_index()
    hits = idx.keyword(["np.float_", "float_"], "numpy", 3)
    assert "np.float_" in idx.get(hits[0]).text  # type: ignore[union-attr]


def test_vector_search_puts_the_row_that_shares_words_in_the_top_four():
    # Top 4 is what matters: the model is shown four excerpts. (Top 1 is ~86%; see EXPERIMENTS.)
    idx, emb = fixture_index()
    top = idx.vector(emb.embed(["np.float_ removed use np.float64"], "query")[0], "numpy", 4)
    assert any("np.float_" in idx.get(c).text for c in top)  # type: ignore[union-attr]


def test_library_filter_separates_numpy_from_stdlib():
    idx, _ = fixture_index()
    for cid in idx.keyword(["utcnow"], "numpy", 5):
        raise AssertionError(f"numpy filter leaked stdlib chunk {cid}")
    assert idx.keyword(["utcnow"], "stdlib", 5)


def test_hostile_search_terms_cannot_break_the_query():
    idx, _ = fixture_index()
    assert idx.keyword(['"; DROP TABLE chunks; --', "a* OR b", "NEAR("], None, 5) is not None
    assert idx.count() > 0
    assert idx.keyword(["!!!", ""], None, 5) == []


def test_rebuilding_replaces_instead_of_duplicating():
    idx, emb = fixture_index()
    before = idx.count()
    build_index(idx, load_sources(DOCS / "sources.yaml"), emb, "entry",
                lambda s: read_source(s, Path("/x"), DOCS))  # fmt: skip
    assert idx.count() == before


def test_an_index_refuses_a_different_embedder_or_chunker():
    idx, _ = fixture_index()
    with pytest.raises(IndexError_, match="embedder"):
        build_index(idx, [], HashEmbedder(128), "entry", lambda s: "")
    with pytest.raises(IndexError_, match="chunker"):
        build_index(idx, [], HashEmbedder(), "fixed", lambda s: "")
    with pytest.raises(IndexError_, match="unknown chunker"):
        build_index(idx, [], HashEmbedder(), "nope", lambda s: "")


def test_stats_report_sources_licences_and_counts():
    idx, _ = fixture_index()
    stats = idx.stats()
    assert stats["chunker"] == "entry" and stats["embedder"] == "hash-256"
    sources = stats["sources"]
    assert isinstance(sources, list) and len(sources) == 6 and all(s["chunks"] > 0 for s in sources)


def test_fixed_chunker_builds_too():
    idx, _ = fixture_index("fixed")
    assert idx.count() > 0 and idx.get_meta("chunker") == "fixed"


# ---- sources and polite fetching -----------------------------------------------------------------


class Resp:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self, n: int = -1) -> bytes:
        return self.data[:n] if n >= 0 else self.data

    def __enter__(self) -> "Resp":
        return self

    def __exit__(self, *a: object) -> None:
        pass


def opener_for(
    robots: bytes | Exception, page: bytes = b"<p>hello</p>", log: list[str] | None = None
):
    def opener(req, timeout=None):
        url = req.full_url
        if log is not None:
            log.append(url)
        if url.endswith("/robots.txt"):
            if isinstance(robots, Exception):
                raise robots
            return Resp(robots)
        return Resp(page)

    return opener


OK = "https://docs.python.org/3/library/logging.html"
NOSLEEP = lambda s: None  # noqa: E731


def test_fetch_downloads_checks_robots_sleeps_and_caches(tmp_path):
    log: list[str] = []
    slept: list[float] = []
    op = opener_for(b"User-agent: *\nAllow: /\n", log=log)
    html = fetch_html(OK, tmp_path, opener=op, sleep=slept.append)
    assert html == "<p>hello</p>" and slept == [1.0]
    assert [u.rsplit("/", 1)[-1] for u in log] == ["robots.txt", "logging.html"]
    log.clear()
    assert fetch_html(OK, tmp_path, opener=op, sleep=slept.append) == "<p>hello</p>"
    assert log == [] and slept == [1.0]  # the cache means no second request and no second delay


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.python.org/x",
        "https://evil.example/x",
        "file:///etc/passwd",
        "ftp://docs.python.org/x",
    ],
)
def test_unsafe_urls_are_refused_before_any_request(tmp_path, url):
    log: list[str] = []
    with pytest.raises(FetchError):
        fetch_html(url, tmp_path, opener=opener_for(b"", log=log), sleep=NOSLEEP)
    assert log == []


def test_extra_hosts_must_be_added_explicitly(tmp_path):
    op = opener_for(b"User-agent: *\nAllow: /\n")
    got = fetch_html(
        "https://example.org/p", tmp_path, frozenset({"example.org"}), opener=op, sleep=NOSLEEP
    )
    assert got == "<p>hello</p>"


def test_robots_disallow_is_obeyed(tmp_path):
    op = opener_for(b"User-agent: *\nDisallow: /3/\n")
    with pytest.raises(FetchError, match="robots.txt"):
        fetch_html(OK, tmp_path, opener=op, sleep=NOSLEEP)


def test_missing_robots_means_no_restrictions_but_forbidden_and_errors_fail_closed(tmp_path):
    assert fetch_html(OK, tmp_path / "a", opener=opener_for(http_error(404)), sleep=NOSLEEP)
    with pytest.raises(FetchError):
        fetch_html(OK, tmp_path / "b", opener=opener_for(http_error(403)), sleep=NOSLEEP)
    with pytest.raises(FetchError, match="robots"):
        fetch_html(OK, tmp_path / "c", opener=opener_for(http_error(500)), sleep=NOSLEEP)
    with pytest.raises(FetchError, match="robots"):
        fetch_html(
            OK, tmp_path / "d", opener=opener_for(urllib.error.URLError("down")), sleep=NOSLEEP
        )


def test_oversized_pages_are_refused(tmp_path):
    op = opener_for(b"User-agent: *\nAllow: /\n", page=b"x" * 5_000_001)
    with pytest.raises(FetchError, match="larger"):
        fetch_html(OK, tmp_path, opener=op, sleep=NOSLEEP)


def test_source_list_has_licences_for_every_entry():
    sources = load_sources()
    assert len(sources) == 6
    assert all(s.license and s.license_url.startswith("https://") for s in sources)
    assert {s.license for s in sources} == {"BSD-3-Clause", "PSF-2.0"}


def test_local_sources_read_from_disk_not_the_network(tmp_path):
    (tmp_path / "p.html").write_text("<p>local</p>")
    src = Source(
        id="s",
        library="x",
        version="1",
        url="https://docs.python.org/x",
        license="MIT",
        path="p.html",
    )
    assert read_source(src, tmp_path / "cache", tmp_path) == "<p>local</p>"


def test_every_rule_page_is_covered_by_a_source():
    from tests.helpers import RULES

    urls = {s.url for s in load_sources()}
    assert {r.reference.url.split("#")[0] for r in RULES.rules} <= urls
    fixture_urls = {s.url for s in load_sources(DOCS / "sources.yaml")}
    assert {r.reference.url.split("#")[0] for r in RULES.rules} <= fixture_urls


def test_index_object_is_a_context_manager():
    with Index(":memory:") as idx:
        assert idx.count() == 0
    assert isinstance(io.StringIO(), io.StringIO)


def test_the_index_can_be_queried_from_many_threads_at_once():
    from concurrent.futures import ThreadPoolExecutor

    idx, emb = fixture_index()
    q = emb.embed(["np.float_ removed"], "query")[0]

    def work(i: int) -> tuple[int, int]:
        return len(idx.keyword(["float_"], "numpy", 3)), len(idx.vector(q, "numpy", 3))

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(work, range(64)))
    assert set(results) == {work(0)} and work(0)[0] >= 1  # same answers as one thread
