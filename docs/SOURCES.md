# Documentation sources and licences

depfix never commits scraped documentation. `src/depfix/doc_sources.yaml` lists public pages;
`depfix index build` downloads them to YOUR machine (`~/.config/depfix/raw`, never inside a repo).

| Source | Used for | Licence | How the licence was checked |
|--------|----------|---------|-----------------------------|
| NumPy 2.0 migration guide (numpy.org) | NumPy rules | BSD-3-Clause | the NumPy documentation footer states "Licensed under the 3-clause BSD License"; licence page https://numpy.org/doc/2.4/license.html |
| Python library docs: datetime, logging, threading, base64, asyncio (docs.python.org) | stdlib rules | PSF-2.0 (code examples also 0BSD) | https://docs.python.org/3/license.html and the CPython `Doc/license.rst` |

You should open both licence pages yourself before publishing. This table records what I found,
not legal advice.

## What is stored, and where it appears

Each chunk keeps its source URL, licence and attribution. `depfix explain` prints them next to every
excerpt, and a model fix stores the cited excerpts with its proposal, in your local state only.

## How fetching behaves

https only; only hosts on the allow-list (`numpy.org`, `docs.python.org`, extendable with
`--allow-host`); `robots.txt` is read first and obeyed, and an unreadable `robots.txt` means NO;
one request per second; pages over 5 MB refused; responses cached so a rebuild makes no requests.

## Test corpus

`benchmark/docs/` holds ORIGINAL text written for depfix (MIT) that imitates the structure of the
real pages. Tests and the benchmark use it so they need no network, and so recordings replay
identically on any machine. It is not a copy of anyone's documentation.

## Adding a source

Add an entry to `doc_sources.yaml` with its licence and licence URL, add its host to the allow-list
in `sources.py` (a reviewed code change, deliberately), then `make index`.
