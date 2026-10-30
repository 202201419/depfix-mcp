# depfix-mcp

A verification-first assistant that finds deprecated API usage in Python projects, proposes a
minimal fix, **proves the fix on an isolated copy** of your repo, and only then asks a human.

> Status: **drop 2 of 4** (deterministic core, sandboxed verification, human approval, apply; no AI yet). Start with `docs/CODEMAP.md`; a real session is in `docs/EXAMPLE_TRACE.txt`.

## What works today

- Scan a Python repo for deprecated or removed APIs (NumPy 2.0 removals and a few stdlib cases).
- Propose an exact, template-based fix as a unified diff, with a source link.
- Verify it on a temporary copy, inside a network-less container: applies cleanly, parses, the
  usage is gone, the change is minimal, it matches the rule exactly, the rule's own example holds,
  your linter and type checker (if configured) see nothing new, and your tests do not regress.
- A person approves the exact diff in a terminal; `apply` then puts it on a new git branch.
  Nothing changes without that approval, and `apply` is a dry run unless you pass `--execute`.
- A benchmark of small synthetic repos with a recorded baseline and a regression gate.

## What does not exist yet

Model-written fixes and documentation retrieval (drop 3), MCP server and CI (drop 4). Other
languages are out of scope for the MVP.

## Quickstart

```bash
uv sync
make smoke-docker                                  # once: build and prove the sandbox
uv run depfix scan path/to/project
uv run depfix propose <id> --repo path/to/project
uv run depfix verify  <id> --repo path/to/project  # runs in Docker by default
uv run depfix approve <id> --repo path/to/project  # you, in a terminal
uv run depfix apply   <id> --repo path/to/project --execute
```

See `docs/SANDBOX.md` for Docker setup and `docs/SAFETY.md` for what is and is not protected.

## Development

```bash
make check     # lint, types, tests, rule self-tests, benchmark regression gate (about 1 minute)
make smoke-docker  # build the sandbox image and prove its isolation (needs Docker running)
make bench     # run the benchmark and print scores
make baseline  # record new scores (only in a commit that explains why)
```

Docs: `docs/CODEMAP.md` (reading order), `docs/DECISIONS.md` (every choice), `docs/EXPERIMENTS.md`,
`docs/EXERCISES.md`, `docs/SAFETY.md`, `docs/SANDBOX.md`.

## Licence

MIT
