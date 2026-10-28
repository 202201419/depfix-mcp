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

## Planned (drops 3 and 4)

| ID | Question | Hypothesis | Variable | Metric | Result |
|----|----------|------------|----------|--------|--------|
| E1 | Chunking | entry-level chunks beat fixed windows | entry-level vs fixed 800/150 | retrieval hit@4, fix success | |
| E2 | Retrieval layers | anchor + keyword + vector beats any one | layers on/off | hit@4, MRR | |
| E3 | Docs help? | docs raise fix success over no-docs | with/without retrieval | fix success | |
| E4 | Model size | 14B beats 7B (same family) | qwen2.5-coder 7b vs 14b | fix success, false_accept | |
| E5 | Retry loop | 1-2 retries help, more does not | max retries 0/1/2/3 | first-try vs final success | |
| E6 | Hybrid | templates first, model for the rest beats model-only | routing | fix success, false_accept | |
| E7 | Flakiness | most model failures are consistent, not random | 3-5 repeats, temp 0 | consistent-fail vs flaky share | |

Each experiment changes ONE variable. If a result surprises us, that goes in the README honestly.
