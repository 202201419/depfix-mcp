"""Where documentation comes from, and how we fetch it politely.

We never commit scraped documentation. A source list (rule_data/sources.yaml) names public pages,
their licences and the attribution to show; `depfix index build` downloads them to YOUR machine.
Fetching is deliberately conservative: https only, an allow-list of hosts, robots.txt is obeyed
(and an unreadable robots.txt means NO), a delay between requests, a size cap, and a cache so a
rebuild does not hit the site again.
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request
import urllib.robotparser
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict

from . import __version__

ALLOWED_HOSTS = frozenset({"numpy.org", "docs.python.org"})
USER_AGENT = f"depfix-mcp/{__version__} (documentation indexer; respects robots.txt)"
MAX_BYTES = 5_000_000


class FetchError(Exception):
    pass


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    library: str
    version: str
    url: str  # the public page; rules point at this URL, which is how "anchor" retrieval finds it
    license: str
    license_url: str = ""
    attribution: str = ""
    path: str | None = None  # a local HTML file to read instead of the URL (used by test corpora)


def load_sources(path: Path | None = None) -> list[Source]:
    text = (
        path.read_text(encoding="utf-8")
        if path
        else (resources.files("depfix") / "doc_sources.yaml").read_text(encoding="utf-8")
    )
    return [Source.model_validate(s) for s in yaml.safe_load(text)["sources"]]


Opener = Callable[..., Any]


def _robots_allows(host: str, url: str, opener: Opener) -> bool:
    rp = urllib.robotparser.RobotFileParser()
    req = urllib.request.Request(f"https://{host}/robots.txt", headers={"User-Agent": USER_AGENT})
    try:
        with opener(req, timeout=30) as resp:
            rp.parse(resp.read(200_000).decode("utf-8", errors="replace").splitlines())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False
        if 400 <= exc.code < 500:
            return True  # no robots.txt: no restrictions published
        raise FetchError(f"could not read robots.txt for {host}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"could not read robots.txt for {host}: {exc}") from exc  # fail closed
    return rp.can_fetch(USER_AGENT, url)


def fetch_html(
    url: str,
    cache_dir: Path,
    extra_hosts: frozenset[str] = frozenset(),
    refresh: bool = False,
    opener: Opener = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    parts = urlparse(url)
    if parts.scheme != "https":
        raise FetchError(f"refusing non-https URL: {url}")
    host = parts.hostname or ""
    if host not in ALLOWED_HOSTS | extra_hosts:
        raise FetchError(f"host '{host}' is not on the allow-list (use --allow-host to add it)")
    cache = cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".html")
    if cache.exists() and not refresh:
        return cache.read_text(encoding="utf-8")
    if not _robots_allows(host, url, opener):
        raise FetchError(f"robots.txt on {host} does not allow fetching {url}")
    sleep(1.0)  # be polite: never hammer a documentation site
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(req, timeout=30) as resp:
            data = resp.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"could not fetch {url}: {exc}") from exc
    if len(data) > MAX_BYTES:
        raise FetchError(f"{url} is larger than {MAX_BYTES} bytes; refusing")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return str(data.decode("utf-8", errors="replace"))


def read_source(
    src: Source, cache_dir: Path, base_dir: Path | None = None, **fetch_kwargs: Any
) -> str:
    if src.path:
        return ((base_dir or Path.cwd()) / src.path).read_text(encoding="utf-8")
    return fetch_html(src.url, cache_dir, **fetch_kwargs)
