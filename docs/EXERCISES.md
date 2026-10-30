# EXERCISES: prove you own drop 1

Do these before pushing. Rule: predict first, then run. If your prediction was wrong, read the
code again until you can say why.

## A. Break-it (predict which tests fail and why)

1. In `scanner.dotted`, delete the `reversed(...)` so it returns `".".join(parts)`.
2. In `fixer.splice`, replace the byte slicing with `text[s:e]` slicing using the same numbers.
3. In `verify.check_finding_gone`, change the check to "the target finding id no longer appears".
4. In `models.ALLOWED`, add `Status.APPROVED` to the set for `Status.PROPOSED`.
5. In `diffing.make_diff`, delete the "No newline at end of file" branch.
6. In `scanner.scan_text`, remove the `ctx` (Load) check for attributes.

Run `uv run pytest -q` after each, then `git checkout -- .` to undo.

## B. Trace it

1. Pick a finding in your own project (or `/tmp/demo` from the example trace) and run
   `depfix trace <id>`. For each of the five sections, point at the function that printed it.
2. Change the file between `propose` and `verify` (add a comment line). Predict the result, run
   it, then find the code that produced it.

## C. Extend it

1. Add rule `np.cfloat -> np.complex128` (it is in the NumPy table). Steps: add YAML with an
   `example`, run `depfix rules --selftest`, add a benchmark case folder, run `make bench`, and
   explain why `baseline.json` should change only in a commit that says why.
2. Write a new bad-patch test: a patch that fixes the usage but also deletes a blank line two
   lines below. Which check should reject it?

## D. Quiz (answers at the bottom; no peeking)

1. Why do we hash (rule, file, snippet, n) for ids instead of using the line number?
2. `ast` says a span starts at column 9. Why can `line[9:]` be wrong?
3. Why is `from datetime import datetime` + `utcnow()` refused rather than fixed?
4. Why compare test failures before and after the patch instead of requiring all green?
5. What does `false_accept_rate` measure and why can it be 0 today and still not be trusted?
6. Why does `run_limited` use a launcher script instead of `preexec_fn`?
7. Which check stops a patch that swaps one deprecated call for another?
8. Why are rule examples executed on your machine?

### Answers

1. Ids survive edits above the usage. The occurrence number separates identical usages.
2. Columns are UTF-8 byte offsets; a non-ASCII character before the span is 1 character but
   2+ bytes, so character slicing lands in the wrong place.
3. The template needs `datetime.timezone`, which that file never imported. Adding imports is
   out of scope, so we refuse instead of inventing a name.
4. A failure that existed before the patch is not the patch's fault. All-green would block
   fixes in already-broken projects. Confidence is lowered unless everything passes.
5. Verified patches that hidden tests reject. It is 0 because templates are exact; it is untested
   until a model writes fixes, so today's 0 says little about the verifier.
6. `preexec_fn` is unsafe in multithreaded programs and the benchmark runs threads.
7. `finding_gone`, via the multiset comparison of (rule, snippet).
8. A rule is a claim about library behaviour on a specific version; running it checks the claim
   on the interpreter you actually use.

### Break-it answer key (verified by actually running each mutation)

| # | Mutation | Tests that fail | Why |
|---|----------|-----------------|-----|
| 1 | `dotted` without `reversed` | 27 (every scanner and fixer detection test) | `np.float_` becomes `float_.np`, so no qualified name ever matches a rule |
| 2 | splice by characters | `test_non_ascii_before_the_span...`, the Unicode property test | byte columns used as character columns land one place too far left |
| 3 | `finding_gone` checks only the target | `test_patch_that_introduces_another_deprecated_call_is_rejected` | swapping one deprecated call for another now passes |
| 4 | allow PROPOSED -> APPROVED | `test_illegal_transitions_raise[proposed-approved]` | verification could be skipped |
| 5 | drop the "No newline" marker | `test_file_without_trailing_newline` | git cannot apply the diff |
| 6 | drop the Load-context check | `test_assignment_and_delete_are_not_usages` | `np.float_ = 3` would be flagged as a usage |

Notice how few tests catch each bug. That is the point of exercise 6 in section A: a test suite
is only as good as the mutations it kills.
