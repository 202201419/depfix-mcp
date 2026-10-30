.PHONY: setup check lint types test bench baseline selftest smoke-docker

setup:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

types:
	uv run mypy

test:
	uv run pytest

bench:
	uv run python -m benchmark.runner

baseline:
	uv run python -m benchmark.runner --write-baseline

selftest:
	uv run depfix rules --selftest

# Needs Docker running. Builds the sandbox image (the one step that uses the network), then proves
# from INSIDE the container that the network is blocked, the filesystem is read-only, and so on.
smoke-docker:
	uv run depfix sandbox build
	uv run depfix sandbox check

# One command, one summary. Paste the tail of this output when reporting a problem.
check: lint types test selftest
	uv run python -m benchmark.runner --check benchmark/baseline.json
	@echo "ALL CHECKS PASSED"
