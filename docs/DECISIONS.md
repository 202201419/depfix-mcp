# DECISIONS: every choice, the alternatives, and what would change our mind

Format: choice, why, how it is tested, and the evidence that should make us revisit it.

**D1. Parse with `ast`, not regex.** Comments, strings and docstrings can never match, and `ast`
gives exact spans. Cost: it discards formatting, so we splice text into the original source
instead of regenerating it. Tested by the comment/string/docstring cases. Revisit if we need to
rewrite code structurally (then consider LibCST).

**D2. Resolve names through imports, not type inference.** Cheap, explainable, and right for
module-level APIs. Cost: shadowing and instance methods are out of reach (benchmark known-gap
cases measure this). Revisit if the known-gap rate matters in real repos.

**D3. Rules are hand-written YAML with an executable example.** Rules are the ground truth for
the benchmark, so a model must not write them (circular evidence). `depfix rules --selftest`
runs each example on YOUR interpreter and checks before/after behaviour. NumPy rules were checked
against the official migration guide's removed-members table. Stdlib rules are checked
empirically by the self-test; confirm them against the docs page named in each rule.

**D4. The rule author decides template vs model.** Test: can you write the replacement without
reading surrounding code, with identical behaviour? `utcnow()` qualifies only because
`.replace(tzinfo=None)` keeps it naive; `asyncio.get_event_loop()` does not. A template that
fails is reported, never silently retried through a model.

**D5. Byte-offset splice.** `ast` columns are UTF-8 byte offsets. Tested with a fixed non-ASCII
case and a property test over random Unicode on both sides of the span.

**D6. Diffs are built with `difflib`, not by a model.** Small models write malformed hunks; a
diff made by code is correct by construction and its hash can be bound to an approval (drop 2).
We emit the "No newline at end of file" marker and split lines on `\n` only, to agree with git.

**D7. Finding ids hash rule + file + snippet + occurrence number (not the line).** Ids survive
edits above the usage. Occurrences are numbered after sorting by position.

**D8. Transition table for status.** Skipping verification is impossible by construction
(`PROPOSED -> APPROVED` is not in the table). Tested exhaustively.

**D9. Verify on a copy, cheap checks first, stop at the first fatal failure.** Order:
diff_scope, applies, parses, finding_gone, diff_minimal, tests. Running the project's tests is the
expensive step, so it only runs for patches that passed everything else.

**D10. "Finding gone" compares multisets of (rule, snippet), not ids.** Fixing the first of two
identical usages makes the second inherit the first one's id, which would look like "still
there". The multiset also catches a patch that swaps one deprecated call for another.

**D11. Tests are compared before vs after, not just "all green".** A failure that existed before
the patch is not blamed on it. Confidence is `high` only if the patched copy passed everything;
repos without tests get `low`.

**D12. A local runner with a launcher script, not `preexec_fn`.** `preexec_fn` is documented as
unsafe with threads and the benchmark uses a thread pool. The launcher sets CPU-time and file-size
limits, then `exec`s the command; the whole process group is killed on timeout. Known gap: no
network or filesystem isolation until drop 2, so `verify` requires `--allow-local-exec`.

**D13. Minimal child environment.** Cloud keys and tokens in your shell never reach the tests.

**D14. Refuse unsupported files instead of guessing.** BOM, bare-CR, non-UTF-8: skipped with a
reported reason. A wrong span would corrupt code silently, which is worse than a miss.

**D15. Never add imports in drop 1.** `from datetime import datetime` has no `timezone` in scope,
so STD-0001 is refused (`not_applicable`) rather than inventing a name. The benchmark counts a
correct refusal as a success (`correct_refusal_rate`). Revisit when import insertion is built.

**D16. Hidden tests judge the verifier.** The benchmark runs tests the verifier never saw.
`false_accept_rate` = verified but hidden tests fail. In drop 1 it is 0 because templates are
exact; it becomes the headline number when a model writes fixes. `oracle_discrimination` (0.79)
reports honestly that 3 of 14 cases (the multi-finding file) cannot distinguish fixed from unfixed.

**D17. Known-gap cases are reported but not gated.** They document limits without blocking CI.

**D18. One rule per qualified name.** Keeps the scanner lookup unambiguous. Enforced at load time.

**D19. Pinned environment via `uv.lock`.** Your results match mine. Python 3.12, NumPy 2.x for
the benchmark only (not a runtime dependency).

# Drop 2 decisions

**D20. A `Runner` abstraction.** Only the project's code (tests, linters) is untrusted, so only
that goes through a runner. `git apply` works on OUR diff and stays local. The checks do not know
which runner they use, so the same verifier runs in Docker, locally, and (tests) with fakes.

**D21. The whole sandbox is one pure function.** `DockerRunner.build_argv` returns the exact
`docker run` command, so a unit test can assert every flag even where Docker is unavailable. The
real isolation is then proven from inside a container by `depfix sandbox check`.

**D22. Docker by default; local needs `--allow-local-exec`.** Safe defaults beat convenient ones.

**D23. Refuse to run as root.** `--user 0:0` would make the container root, defeating `--user`.

**D24. Kill containers by name on timeout.** Killing the `docker run` client leaves the container
running. A timeout also takes priority over exit code 125 (which means "docker itself failed").

**D25. Approval is a person typing in a terminal.** `approve` refuses without a TTY and is not
exposed to the MCP server. An approval is bound to the diff hash, expires in 15 minutes, and is
single-use. Low-confidence verifications require typing the finding id, so a reflex "y" is not enough.

**D26. An HMAC key outside the repo.** Editing the database by hand cannot forge an approval
without `~/.config/depfix/approval.key`. This is defence in depth, not a boundary against a
program that can read your home folder (documented in `docs/SAFETY.md`).

**D27. Invalid approvals are revoked, not just refused.** Expired, forged or mismatched approvals
delete the record and send the finding back to VERIFIED, so a person must look at the diff again.

**D28. Apply is a dry run unless `--execute`, on a new branch, with clean-state preflight.** The
branch makes undo trivial; refusing on dirty or untracked files means we never mix our change into
your unsaved work. Failures roll back to the starting branch.

**D29. Judge a project by its own tools.** Lint and type checks run only if the project configures
ruff or mypy, and compare issues before vs after WITHOUT line numbers. Pre-existing problems are
not blamed on the patch.

**D30. Provenance: templates are re-derived, model output must cite.** A template diff must equal
what the rule produces byte for byte. A model fix must cite chunks, and every new name it
introduces must appear in them (a rule-based groundedness check, no judge model). Limit: a source
can support a wrong change.

**D31. `rule_example` re-runs the rule's claim inside the verification environment.** The
container's library versions can differ from yours.
