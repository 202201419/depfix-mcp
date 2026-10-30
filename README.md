# depfix-mcp

A verification-first assistant that finds deprecated API usage in Python projects, proposes a
minimal fix, **proves the fix on an isolated copy** of your repo, and only then asks a human.

> Status: **drop 1 of 4** (deterministic core, no AI yet). Start with `docs/CODEMAP.md`; a real session is in `docs/EXAMPLE_TRACE.txt`.

## What works today

- Scan a Python repo for deprecated or removed APIs (NumPy 2.0 removals and a few stdlib cases).
- Propose an exact, template-based fix as a unified diff, with a source link.
- Verify it on a temporary copy: applies cleanly, still parses, the deprecated usage is gone,
  nothing else changed, and the project's own tests do not regress.
- A benchmark of small synthetic repos with a recorded baseline and a regression gate.

## What does not exist yet

Model-written fixes and documentation retrieval (drop 3), container sandbox and approval/apply
(drop 2), MCP server (drop 4). Other languages are out of scope for the MVP.

## Quickstart

```bash
uv sync
uv run depfix scan path/to/project
uv run depfix propose <id> --repo path/to/project
uv run depfix verify <id> --repo path/to/project --allow-local-exec
```

`verify` runs the project's tests as a local process (no network isolation until drop 2), so only
use it on code you trust.

## Development

```bash
make check     # lint, types, tests, rule self-tests, benchmark regression gate (about 1 minute)
make bench     # run the benchmark and print scores
make baseline  # record new scores (only in a commit that explains why)
```

Docs: `docs/CODEMAP.md` (reading order), `docs/DECISIONS.md` (every choice), `docs/EXPERIMENTS.md`,
`docs/EXERCISES.md`.

## Licence

MIT
