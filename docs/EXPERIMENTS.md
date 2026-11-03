# EXPERIMENTS: hypotheses we will test, and where results go

Fill the Result column from `benchmark/results/latest.json` after each run. Record the model,
quantization and commit hash next to every number. Never edit a result after the fact; add a row.

## E0. Drop 1 baseline (no AI)

| Metric | Value | Meaning |
|--------|-------|---------|
| precision / recall | 1.0 / 1.0 | on the 16 gated cases, excluding known gaps |
| fix_success_rate | 1.0 | verified AND hidden tests pass AND only the target file changed |
| correct_refusal_rate | 1.0 | not_applicable / needs_llm routed correctly |
| false_accept_rate | 0.0 | verifier approved, hidden tests disagreed |
| unsafe_patch_rate | 0.0 | verified patch changed anything besides the target file |
| oracle_discrimination | 0.7857 | hidden tests fail on the UNFIXED repo (3 cases cannot tell) |
| known gaps | 1 miss, 1 false positive | `getattr`, shadowed parameter |

A perfect score on cases we wrote ourselves says the pipeline is consistent, not that it is
good on real code. Treat this as a floor that later drops must not fall below.

## Drop 3: experiments you can run now

Record the model, quantization, date and commit hash with every number. Add rows; never edit one.

| ID | Question | How to run | Metric | Result |
|----|----------|------------|--------|--------|
| E1a | Which chunker retrieves better on REAL docs? | `make exp-retrieval` (needs Ollama + one download) | hit@4, MRR | |
| E1b | Does chunking change fix success? | `python -m benchmark.llm_runner --llm ollama --chunker entry`, then `--chunker fixed` | fix_success_rate | |
| E2 | What does each retrieval layer add? | `depfix index eval` prints keyword, vector and keyword+vector (anchor excluded: it is the answer key) | hit@4, MRR | |
| E4 | Does a bigger model help? | `make record MODEL=qwen2.5-coder:7b`, then `MODEL=qwen2.5-coder:14b` | fix_success_rate, first_try_rate, false_accept_rate | |
| E5 | Do retries help? | `python -m benchmark.llm_runner --llm ollama --retries 0`, then 1, 2, 3 | first_try_rate vs fix_success_rate | |
| E6 | Template-first beats model-only? | `make bench` (templates) vs `make record` (model, same cases) | fix_success_rate, false_accept_rate | |
| E7 | Are model failures consistent or flaky? | `python -m benchmark.llm_runner --llm ollama --repeats 3` | consistent_fail vs flaky | |

**E3 (do docs help?) is not possible yet.** It needs a mode that calls the model WITHOUT
documentation, and our safety rules forbid that (no evidence, no call; citations required). It
would have to be a clearly labelled ablation that bypasses those checks. Deferred to drop 4.

How to read the model benchmark output: `fix_success_rate` is final, after retries;
`first_try_rate` is without help; `false_accept_rate` is verified-but-wrong (the number that
matters most); `invalid_output_rate` counts unusable or refused answers; `citation_failure_rate`
counts proposals whose new names are not in their cited sources.

Each experiment changes ONE variable. A surprising result goes in the README as it is.
