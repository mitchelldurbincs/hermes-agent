Configured Discord forum threads now select a project folder from a tag in the thread title or starter message.
Missing, unknown, or malformed tags receive one guidance line before any model work.
Project instructions, terminal commands, and relative file reads use the selected folder on local turns.
An agent's directory changes survive later turns, and different threads keep separate working directories.
Docker's existing workspace policy remains intact.
The 38 new tests pass, including real terminal commands and file reads during a gateway turn.
The full test run finished with 23,865 passes, 274 failures, five errors, 231 skips, two expected failures, and one timed-out file.
All 274 failures and five errors reproduced on the starting commit; the timed-out file remains unverified.

## Configuration

```yaml
discord:
  project_threads:
    "123456789012345678":
      xunning:
        cwd: /home/aspect/source/repos/xunning
```

Post `[project:xunning]` in the thread title or starter. A title tag takes precedence. Conflicting tags in the selected field refuse. Forums absent from this map, DMs, text channels, and non-forum threads keep their existing behavior. Direct posts to a configured forum receive the same guidance.

## Changed files

Branch: `claude/project-threads`. Base: `17ea3f59f225ecd642aa63721d932646aba6ceb7` (`aspecttech`). Branch-only delivery; no PR or merge.

| Path | Added | Removed |
| --- | ---: | ---: |
| `gateway/project_threads.py` | 182 | 0 |
| `gateway/run_inbound.py` | 15 | 5 |
| `gateway/run_turn_runner.py` | 2 | 0 |
| `tests/gateway/test_project_threads.py` | 300 | 0 |
| `report-sol.md` | 102 | 0 |

The existing adapter already supplies raw Discord channels and a channel resolver, so no adapter changes are needed. The pre-dispatch extraction keeps the existing voice checks and metrics in order and stays within the repository's complexity limit.

## Test matrix

| Check | Result |
| --- | --- |
| `python3 -m py_compile` on all four changed Python files | Passed |
| New feature tests, canonical runner without extra harness | 38 passed, 0 failed |
| Existing Docker workspace tests, with bootstrap preload | 24 passed, 0 failed |
| Existing profile-binding check, with bootstrap preload | 1 passed, 0 failed |
| Four environment-failing files rerun with application preload | 50 passed, 0 failed |
| Final integration tests with the base gateway seams restored | 4 expected failures, 34 deselected |
| `scripts/check --staged --base 17ea3f59f225ecd642aa63721d932646aba6ceb7` | 14 checks passed; 0 health findings |
| Gateway: all 1,726 files | 13,044 passed, 166 failed, 4 errors, 79 skipped, 1 xfailed; 1 file timed out |
| Tools: all 857 files | 10,771 passed, 108 failed, 1 collection error, 151 skipped |
| Six additional Discord adapter/platform/contract files | 50 passed, 0 failed, 1 skipped, 1 xfailed |
| Complete 2,589-file selection | 23,865 passed, 274 failed, 5 errors, 231 skipped, 2 xfailed; exit 1 |
| Baseline rerun of 75 failed files, excluding timeout | 629 passed, 274 failed, 5 errors; every per-file count matches the patched run |

Base seam verification temporarily restored the two gateway files from the starting commit, ran the four integration cases, and restored the patch. No production runtime state or live checkout files were changed.

The same restoration was used for all 75 baseline files. Both existing modified production files were restored to `17ea3f5`; the new module remained present but had no gateway callers. Every completed failure/error count reproduced, with zero per-file mismatches. The four new integration failures on the base cover three missing refusals and missing project context binding. Restoring the patch returns the new suite to 38 passes.

The canonical `scripts/run_tests.sh` runner is used throughout. Python 3.14.9 test environments were built through `pm.build_env`, with the messaging extra for real Discord imports. Test homes are temporary.

Full suite command (64 minutes, six workers):

```sh
# no-tmp: ok — records the actual command in the user-designated worktree.
HERMES_PYTHON=/tmp/wt-project-threads/.venv-tests/bin/python scripts/run_tests.sh \
  tests/gateway tests/tools \
  tests/plugins/platforms/test_discord_gate_isolation.py \
  tests/plugins/platforms/test_discord_reauthorize_allowed_roles.py \
  tests/plugins/test_discord_runtime_failure.py \
  tests/e2e/test_discord_adapter.py \
  tests/e2e/core/platforms/test_discord_contract.py \
  tests/e2e/core/platforms/test_discord_thread_rename_ordering.py \
  -j 6 -p project_threads_test_bootstrap -q
```

A preliminary broad run was interrupted after existing tests failed or stalled on the home-I/O guard: the worktree's Git metadata lives under the live checkout's `.git/worktrees/`. The final broad run uses a temporary, uncommitted `project_threads_test_bootstrap` pytest plugin. Its only action is importing `hermes_bootstrap` in `pytest_configure`, after conftest installs the temporary Hermes home and before per-test guards activate. With that setup, the existing Docker workspace suite passes all 24 cases and the profile-binding check passes its one case. No guard is disabled. Some files also import `run_agent` during a test; its separate update-recovery probe hits the same guard. A second temporary plugin that preloads `run_agent` made four representative failing files pass all 50 tests. Those reruns are reported separately, not substituted into the broad-run totals.

The temporary bootstrap plugin contains:

```python
def pytest_configure(config):
    import hermes_bootstrap
```

<!-- no-tmp: ok — records existing local test evidence, not runtime path guidance. -->
Local evidence is retained in `/tmp/project-threads-suites-final.log`, `/tmp/project-threads-baseline-failures.log`, `/tmp/project-threads-base-red-final.log`, `/tmp/project-threads-feature-final.log`, `/tmp/project-threads-check-final.log`, `/tmp/project-threads-harness.log`, and `/tmp/project-threads-env-diagnostic.log`. These logs and temporary harnesses are excluded from the commit.

## Limits

No live Discord server or paid model request was used. Discord retrieval is exercised with fake transports; the complete turn runner uses a model stub, with real terminal and file-tool execution. Production configuration was not changed. Cached prompts retain their existing lifetime; changing a running thread's project does not rebuild its past prompt context.

`tests/gateway/test_session_mutation_compress_managed.py` exceeded the runner's 300-second cap and was killed; it was not rerun on the base. `tests/tools/test_fal_sdk.py` could not collect because optional `fal_client` is absent. The four remaining errors are fixtures in `tests/gateway/test_timestamp_sidecar_replay.py`. `tests/gateway/test_session_listener_lazy_dashboard.py` failed its first attempt and passed the runner's automatic retry; the totals use its final result, and that flake was not baseline-tested. Skipped and expected-failure cases are not counted as passes. This Linux run does not verify host-gated Windows or macOS behavior.

The first unstaged lint run scanned disposable Python runtime files under `.build-runtime` and reported irrelevant vendored-code findings, plus one real complexity increase. The ingress pre-dispatch extraction removed that increase. The staged patch passes all 14 repository checks.

Read-only `git status --porcelain` checks of `/home/aspect/.hermes/hermes-agent` returned no output. Only this worktree was edited. Its pre-existing untracked `astra-run.log` and `build-brief.md` are preserved and excluded from the commit.

## PR body draft

Bind configured Discord forum threads to project workspaces using `[project:<slug>]` in the title or starter. Refuse unresolved projects before commands, durable admission, or model work. Bind both project-instruction discovery and local tool working directories; preserve `cd` between turns and keep Docker workspace gating unchanged.

Use the existing Discord channel resolver for replay recovery and the existing terminal override registry for workspace changes. No new persisted state, model tool, or environment variable.

Validation: 38 new tests pass, including real local `pwd`, relative `read_file`, preserved `cd`, and pre-model refusals. The four integration cases fail on the base. Compilation and all 14 repository checks pass. The 2,589-file gateway/tools/Discord run reports 23,865 passed, 274 failed, five errors, 231 skipped, and two expected failures; one file timed out. All failures and errors reproduced on the base with matching per-file counts. No live Discord/model smoke test; timeout remains unverified.
