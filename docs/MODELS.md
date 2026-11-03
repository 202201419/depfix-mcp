# Models: setup, choices, and what to expect (M3 Pro, 18 GB)

## One-time setup

1. Install Ollama (https://ollama.com) and open the app. It runs a server on `localhost:11434`.
2. Pull the models. **Start small**, because downloads go over your connection:

```bash
ollama pull nomic-embed-text        # about 270 MB: turns documentation into vectors
ollama pull qwen2.5-coder:7b        # about 4.7 GB: writes the fixes
```

3. `make smoke-llm`. It checks that the server is up, both models are pulled, an embedding and a
   structured answer work (with tokens per second), then runs ONE real model fix end to end.

The 14 GB class models are optional. Only pull `qwen2.5-coder:14b` (about 9 GB) after the 7B works,
for experiment E4 (does a bigger model help?).

## Why these models

| Role | Model | Why |
|------|-------|-----|
| Embeddings | `nomic-embed-text` | small, runs fast, needs the `search_document:` / `search_query:` prefixes we add |
| Fixes | `qwen2.5-coder:7b` | fits comfortably in memory next to the container and your editor |
| Comparison | `qwen2.5-coder:14b` | same family, so size is the only variable (E4) |

Check `ollama.com/library` for newer coder models before pinning; swap with `--model` or the
`DEPFIX_MODEL` environment variable. The benchmark records the model name with every result.

## Privacy

Your code goes only to `localhost`. depfix refuses any other Ollama address unless you set
`DEPFIX_ALLOW_REMOTE_OLLAMA=1`. Recordings contain a hash of each request and the model's answer,
never the prompt, so they do not contain your code or documentation text.

## The commands

```bash
make index                    # download the public docs (politely) and build the index, once
depfix explain <id>           # the rule plus the cited documentation excerpts
depfix propose <id> --mode llm      # one model attempt, nothing verified
depfix fix <id>               # model fix, verify, and retry with the failure message (max 2)
depfix approve <id>           # a person decides, exactly as for template fixes
```

`depfix propose` in `auto` mode uses the template when the rule has one, and the model otherwise.

## What to expect honestly

A 7B model writes small replacements well when the documentation names the new spelling, and
badly when the right answer depends on code it cannot see. That is why the model may only replace
one expression, why every answer is checked, and why a refusal is a valid outcome. The benchmark
measures all of this; do not trust a number until you have run `make record` yourself.

## On a slow connection

- Downloads happen only in `ollama pull`, `depfix index build` (6 small pages, cached), and
  `depfix sandbox build`. Everything else works offline.
- Interrupted `ollama pull` resumes where it stopped; re-run the same command.
