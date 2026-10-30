# CODEMAP: how drop 1 fits together

Read the files in this order. Each entry says where the file sits in the flow, what goes in and
out, and the one thing to understand before moving on. A real session is in
`docs/EXAMPLE_TRACE.txt`; run `depfix trace <id>` on your own finding to see the same thing live.

```
repo --scan--> Finding --propose--> Proposal(diff) --verify--> Verification --approve--> Approval --apply--> git branch
         ^           ^                    ^                        ^
      rules.yaml   aliases of          template or             checks on a COPY
                   that file           (drop 3: model)         of the repo
```

## Reading order

| # | File | Role in the flow | In -> Out | Understand this |
|---|------|------------------|-----------|-----------------|
| 1 | `models.py` | Shapes + state machine | none | `ALLOWED` is the only place that says which status changes are legal |
| 2 | `rule_data/*.yaml`, `rules.py` | The knowledge base | YAML -> `RuleSet` | rules are DATA; one rule per qualified name |
| 3 | `scanner.py` | Stage 1: find usages | bytes -> `Finding`s | alias map -> `dotted` -> `qualify` -> rule lookup |
| 4 | `diffing.py` | Diff made by code | old/new text -> diff | why we never ask a model for a diff |
| 5 | `fixer.py` | Stage 3: template fix | `Finding` -> `Proposal` | `render_name` spells names the way THIS file does; `splice` works on bytes |
| 6 | `store.py` | Memory between commands | SQLite | rescans keep progress only if the file hash is unchanged |
| 7 | `verify.py` | Stage 4: the checks | `Proposal` -> `Verification` | every check runs on a temp copy; order is cheap first, short-circuit |
| 8 | `selftest.py` | Rules prove themselves | rule -> problems | a rule is a claim; this runs it |
| 9 | `pipeline.py` | Stages as plain functions | ids -> results | CLI and (drop 4) MCP only call these |
| 10 | `cli.py` | Thin wrapper | argv -> pipeline | no logic here by design |
| 11 | `tests/test_verify.py` | The bad-patch corpus | | each test is a way a patch could be wrong |
| 12 | `benchmark/runner.py` | Measures everything | cases -> metrics | hidden tests judge the verifier |

## Where each safety property lives

| Property | Where | Test |
|----------|-------|------|
| Nothing is applied in drop 1 | no apply code exists | `test_cli` end-to-end checks source untouched |
| Verification never touches the real repo | `verify.copy_repo` | `test_verification_never_modifies_the_real_repo` |
| Diff can only touch the finding's file | `check_diff_scope` | traversal / rename / other-file tests |
| Patch is minimal | `check_diff_minimal` | unrelated-edit and oversized tests |
| Tests cannot see your secrets | `sandbox_env` | `test_secrets_in_the_parent_environment...` |
| Runaway tests are killed | `run_limited` (process group) | `test_timeouts_kill_the_process_group` |
| No shell strings | all commands are argument lists | by construction |
| Symlinks never followed | `scanner.iter_python_files`, `copytree(symlinks=True)` | `test_scan_repo_skips...` |

**Changed in drop 2:** the Docker runner provides network and filesystem isolation (see
`docs/SANDBOX.md`, `docs/SAFETY.md`). The local runner still has none, and says so.

## Known limits of the scanner (measured by the benchmark's known-gap cases)

- Shadowing: a parameter named `np` is treated as NumPy (false positive).
- Dynamic access (`getattr(np, "float_")`) is invisible (miss).
- Instance-method deprecations (`self.assertEquals`) cannot be resolved from imports alone.
- Import scope is flat: an import inside one function counts for the whole file.
- Fixes never add imports; such findings are refused as `not_applicable` instead of guessed.
- UTF-8 files only; BOM and bare-CR line endings are refused.

## Counts (for orientation)

21 rules (13 NumPy, 8 stdlib), about 1,500 lines of source, 103 tests, 18 benchmark cases.


## Drop 2 additions (read after the table above)

| File | Role | In -> Out | Understand this |
|------|------|-----------|-----------------|
| `runner.py` | WHERE project code runs | command -> `Proc` | `build_argv` is the whole sandbox: every safety flag is one list item |
| `approval.py` | Human yes, made unforgeable | diff hash -> signed `Approval` | the MAC key lives outside the repo; approvals expire and are single-use |
| `apply.py` | Changing the real repo, safely | diff -> git branch + commit | refuse first, then branch, then apply, and roll back on any failure |
| `verify.py` (changed) | Four new checks | | `provenance`, `rule_example`, `lint`, `types`; tools judge a project by ITS OWN config |
| `pipeline.py` (changed) | `approve` and `apply_fix` | | `confirm` is the human; there is no way to approve without calling it |
| `cli.py` (changed) | `approve`, `apply`, `sandbox` | | `approve` refuses without a terminal; `apply` is a dry run unless `--execute` |

### Life of a finding, now ending in git

```
scan -> propose -> verify (container) -> approve (a person, in a terminal) -> apply --execute
        FOUND      PROPOSED   VERIFIED          APPROVED                          APPLIED
```

A real session is in `docs/EXAMPLE_APPLY.txt`.

Counts: 21 rules, about 2,100 lines of source, 143 tests, 18 benchmark cases.
