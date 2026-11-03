"""Turn a documentation web page into a flat list of text blocks, keeping the heading trail.

Migration guides are mostly tables ("removed name | use this instead") and Sphinx definition lists
(one entry per function). A generic "split every 800 characters" would cut those entries in half,
so we first recover their structure: every table row, list item and definition becomes one Block
that remembers which headings it sat under.

Only the standard library is used (html.parser), so there is nothing extra to install.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

# Page furniture that is never documentation content.
_SKIP = frozenset(
    {
        "script",
        "style",
        "nav",
        "footer",
        "header",
        "aside",
        "form",
        "button",
        "noscript",
        "svg",
        "head",
    }
)
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# Elements that end the current block, so text from neighbouring elements never gets glued together.
_BREAKS = frozenset({"p", "div", "ul", "ol", "dl", "table", "blockquote", "section", "article"})
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Block:
    path: tuple[str, ...]  # heading trail, outermost first
    kind: str  # "para" | "item" | "row" | "entry" | "code"
    text: str
    anchor: str  # id of the nearest heading / section / definition


def _clean(text: str) -> str:
    return _WS.sub(" ", text.replace("\u00b6", "")).strip()  # \u00b6 is Sphinx's permalink mark


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.levels: list[tuple[int, str]] = []
        self.anchor = ""
        self.skip = 0
        self.buf: list[str] = []
        self.kind = "para"
        self.in_pre = False
        self.heading: tuple[int, list[str]] | None = None
        self.row: list[str] | None = None
        self.row_all_th = True
        self.cell: list[str] | None = None
        self.cell_is_th = False
        self.entry_title = ""
        self.entry_parts: list[str] = []
        self.dd_depth = 0

    # -- helpers --
    def _path(self) -> tuple[str, ...]:
        return tuple(t for _, t in self.levels)

    def _emit(self, kind: str, text: str) -> None:
        if len(text) >= 3:
            self.blocks.append(Block(self._path(), kind, text, self.anchor))

    def _flush(self) -> None:
        text = _clean("".join(self.buf))
        self.buf = []
        kind, self.kind = self.kind, "para"
        if not text:
            return
        if self.dd_depth > 0 and self.entry_title:
            self.entry_parts.append(text)  # emitted as ONE block when the definition ends
        else:
            self._emit("item" if kind == "item" else "para", text)

    # -- HTMLParser hooks --
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        ident = dict(attrs).get("id") or ""
        if tag in _HEADINGS:
            self._flush()
            self.heading = (int(tag[1]), [])
            self.anchor = ident or self.anchor
        elif tag == "pre":
            self._flush()
            self.in_pre = True
        elif tag == "tr":
            self._flush()
            self.row, self.row_all_th = [], True
        elif tag in ("td", "th"):
            self.cell, self.cell_is_th = [], tag == "th"
        elif tag == "dt":
            self._flush()
            self.anchor = ident or self.anchor
            self.kind = "dt"
        elif tag == "dd":
            self._flush()
            self.dd_depth += 1
        elif tag == "li":
            self._flush()
            self.kind = "item"
        elif tag in _BREAKS:
            self._flush()
            if tag == "section" and ident:
                self.anchor = ident
        elif tag == "br" and not self.in_pre:
            self.buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in _HEADINGS and self.heading is not None:
            level, parts = self.heading
            text = _clean("".join(parts))
            self.heading = None
            if text:
                self.levels = [(lv, t) for lv, t in self.levels if lv < level] + [(level, text)]
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(_clean("".join(self.cell)))
            self.row_all_th = self.row_all_th and self.cell_is_th
            self.cell = None
        elif tag == "tr" and self.row is not None:
            cells = [c for c in self.row if c]
            if cells and not self.row_all_th:
                self._emit("row", " | ".join(cells))
            self.row = None
        elif tag == "pre":
            code = "".join(self.buf).strip("\n")
            self.buf, self.in_pre = [], False
            self._emit("code", code)
        elif tag == "dt":
            self.entry_title = _clean("".join(self.buf))
            self.buf, self.kind = [], "para"
        elif tag == "dd":
            self._flush()
            self.dd_depth = max(0, self.dd_depth - 1)
            if self.dd_depth == 0:
                if self.entry_parts:
                    # one definition = one block: the signature, the description and the
                    # "use X instead" note stay together
                    self._emit("entry", f"{self.entry_title}: {' '.join(self.entry_parts)}")
                self.entry_parts, self.entry_title = [], ""
        elif tag in ("li", "p", *_BREAKS):
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.heading is not None:
            self.heading[1].append(data)
        elif self.cell is not None:
            self.cell.append(data)
        else:
            self.buf.append(data)


def parse_html(html: str) -> list[Block]:
    parser = _Parser()
    parser.feed(html)
    parser.close()
    parser._flush()
    return parser.blocks
