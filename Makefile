.PHONY: setup check lint types test bench baseline selftest smoke-docker smoke-llm index exp-retrieval record bench-llm bench-llm-selftest

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

# ---- drop 3: documentation index and models (need Ollama running; see docs/MODELS.md) ----------
MODEL ?= qwen2.5-coder:7b
SLUG  := $(shell echo $(MODEL) | tr ':/.' '---')

# Is Ollama up, are the models pulled, do they answer? Then one real model fix end to end.
smoke-llm:
	uv run depfix llm check --model $(MODEL)
	uv run python -m benchmark.llm_runner --llm ollama --model $(MODEL) --only std_logging_warn

# Download the real documentation (politely, to YOUR machine only) and build the index.
index:
	uv run depfix index build --rebuild

# Experiments E1 and E2: which chunking strategy and which retrieval layers find the right entry?
exp-retrieval:
	uv run depfix index build --rebuild --chunker entry --index $(HOME)/.config/depfix/exp-entry.db
	uv run depfix index eval --index $(HOME)/.config/depfix/exp-entry.db
	uv run depfix index build --rebuild --chunker fixed --index $(HOME)/.config/depfix/exp-fixed.db
	uv run depfix index eval --index $(HOME)/.config/depfix/exp-fixed.db

# Run the real model on every benchmark case, record its answers, and write a baseline.
# Commit benchmark/recordings/ and the baseline so CI can replay them without a model.
record:
	uv run python -m benchmark.llm_runner --llm record:benchmark/recordings/$(SLUG).jsonl --model $(MODEL) --write-baseline benchmark/baseline-llm-$(SLUG).json

# What CI does: replay the recording (no model needed) and fail if scores dropped.
bench-llm:
	uv run python -m benchmark.llm_runner --llm replay:benchmark/recordings/$(SLUG).jsonl --model $(MODEL) --check benchmark/baseline-llm-$(SLUG).json

# Machinery self-test with a simulated perfect model (no Ollama needed).
bench-llm-selftest:
	uv run python -m benchmark.llm_runner --llm policy:oracle

# One command, one summary. Paste the tail of this output when reporting a problem.
check: lint types test selftest
	uv run python -m benchmark.runner --check benchmark/baseline.json
	@echo "ALL CHECKS PASSED"
