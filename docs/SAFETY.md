# SAFETY: what could go wrong, and what stops it (draft; drop 4 completes the threat model)

An agent that edits code is dangerous in specific ways. This table lists each risk drop 2
addresses, the control, the test that proves the control, and what is still NOT covered.

| # | Risk | Control | Proven by | Residual risk |
|---|------|---------|-----------|---------------|
| 1 | The project's tests are untrusted code: they could read secrets, call out to the internet, fork-bomb, or fill the disk | Verification runs in a throw-away container: `--network none`, `--read-only`, `--cap-drop ALL`, non-root user, memory/CPU/PID limits, only the work copy and a scratch folder mounted, no host environment passed | `tests/test_runner.py` pins every flag; `depfix sandbox check` proves it from inside the container | Containers share the host kernel; a kernel escape would defeat them. The local runner has NO isolation |
| 2 | A patch edits a different file, renames or deletes files, or uses `../` to escape the repo | `diff_scope` checks (before anything touches disk) and again right before apply | bad-patch corpus in `tests/test_verify.py` | none known |
| 3 | A patch makes unrelated edits | `diff_minimal`: every changed line must lie inside the finding, under a size cap | `test_unrelated_edit...`, `test_oversized...` | a harmful edit INSIDE the finding's own lines is possible; `provenance` and a human review cover it |
| 4 | The fix is swapped after verification or after approval | SHA-256 of the diff is stored with the proposal, the report and the approval, and re-checked at apply time; the file's hash is re-checked too | `test_diff_swapped_in_the_database_after_approval_is_refused` | none known |
| 5 | The AI client approves its own work | `approve` needs an interactive terminal and a person's answer; it is not exposed to the MCP server (drop 4) | `test_approve_refuses_without_an_interactive_terminal` | an AI tool with a pseudo-terminal could type "y". Do not give an AI an unrestricted shell next to depfix |
| 6 | A forged or edited approval record | The record carries an HMAC made with a secret key in `~/.config/depfix` (mode 600), outside the repo; it expires in 15 minutes and works once | `tests/test_approval.py`, `test_forged_approval_row_is_refused` | a program that can read your home folder AND write your repo can forge one |
| 7 | A doctored proposal posing as the rule's fix | `provenance` re-derives the diff from the rule and requires an exact match. Model fixes must cite sources, and every new name must appear in them | `tests/test_checks.py` | a model can still cite a source that supports a WRONG change; only tests and review catch that |
| 8 | Overwriting your work or applying to the wrong place | Apply refuses unless: git repo root, on a branch, file tracked and clean, target branch not present | `tests/test_apply.py` | none known |
| 9 | A half-applied change if something fails mid-way | Everything happens on a new branch; any failure switches back and deletes it | `test_failed_apply_rolls_back_completely` | a failed commit leaves the change staged on the new branch, and says so |
| 10 | Secrets leaking into tests | The child environment is rebuilt from scratch; Docker gets only four fixed variables | `test_secrets_in_the_parent_environment...`, `test_host_environment_is_not_forwarded` | secrets stored in files inside the repo are visible to its own tests |
| 11 | A hung or runaway test | Timeout, then the container is killed by name (killing the client alone would not stop it) | `test_timeout_kills_the_container_by_name` | none known |

## Added in drop 3: the model and the documentation

| # | Risk | Control | Proven by | Residual risk |
|---|------|---------|-----------|---------------|
| 12 | Model output is untrusted text | JSON shape, citation ids, single-line expression, no eval/exec/open/dunder, no await/yield/walrus, names must already exist, file must still parse | `tests/test_llm_fixer.py` | a valid, plausible but WRONG expression; tests and a person catch that |
| 13 | A model reaches beyond the finding | it can only replace the flagged span; `diff_minimal` and `diff_scope` re-check | same | none known |
| 14 | Prompt injection through documentation or code | documentation is fenced and sanitised, the system prompt says to ignore instructions in data, and nothing the model says can run: it only proposes text that is validated and verified | `test_documentation_cannot_close_its_own_fence...` | a model may still be misled into a wrong (but validated) replacement |
| 15 | Your code leaves the machine | the model server must be local; remote needs an explicit environment variable | `test_remote_servers_are_refused...` | none by default |
| 16 | Recordings leak code or documentation | only a request hash and the answer are stored | `test_record_then_replay...stores_no_prompt_text` | none known |
| 17 | The documentation fetcher is abused | https only, host allow-list, robots obeyed (unreadable means NO), rate limit, size cap, cache | `tests/test_docs.py` | the allow-listed sites themselves could serve misleading text; fixes are still verified |
| 18 | A model fix cites sources that do not support it | `provenance`: every new name must appear in the cited text | `tests/test_checks.py` | see row 12 |

## Not covered yet

- **Model quality on real code** is unmeasured until you run `make record`.
- **Image supply chain.** The sandbox image installs packages with `pip` at build time using
  version ranges, not hashes, and the base image is not pinned by digest.
- **Windows.** The runner uses POSIX process groups and `os.getuid`.
- The local runner is for code you fully trust. Treat `--allow-local-exec` as a dangerous flag.
