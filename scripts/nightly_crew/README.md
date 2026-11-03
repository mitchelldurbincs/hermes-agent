# Crew-owned nightly update prototype

This is an offline merge planner for the aspecttech fork.
It produces a reviewable diff and a receipt, never a deployment or a branch merge.
The seven hashes in `pins.json` are the standing pins verified at fork commit
`622d41a6bce667a36885d704a6a46723b3b671c5`; changing them requires Mitchell's explicit authorization.
They match the prior nightly lane's `fork-pin-hashes.json`.

## Rehearse the frozen November 3 input

Run from the new `codex/nightly-crew-v1` worktree, using a fresh evidence directory:

```sh
python3 scripts/nightly_crew.py \
  --repo . \
  --fork 622d41a6bce667a36885d704a6a46723b3b671c5 \
  --upstream d70686448812f62dbd6630cd83d0565f709f097b \
  --pins scripts/nightly_crew/pins.json \
  --output ./nightly-review
```

Resolve/fetch commits before invocation, then freeze the full IDs.
The planner does not fetch refs, switch branches, edit tracked files, alter the caller's
index, commit, push, install dependencies, contact Discord, or restart services.
`merge-tree` writes unreachable Git objects; a partial clone can lazily retrieve missing
objects from its promisor remote, so the rehearsal is not guaranteed network-free.
Keep `candidate.patch` and its SHA-256 receipt: an unreachable tree can be garbage-collected.

Exit 0 means a candidate tree was assembled and still needs validation.
Exit 2 means unresolved conflicts; exit 3 means an input/Git/pin error.
Every receipt has `deployable: false`.
An existing output directory is refused so evidence cannot be silently overwritten.

The assembler runs Git's ordinary three-way tree merge, records the conflicts, then
restores the exact fork blob and mode for **every** pinned path, including clean merges.
Unknown conflicts, including rename endpoints outside the pins, stop assembly.
There is no blanket `ours` strategy for other files.
Outputs include `candidate.patch`, `fork-overlay.patch`, `withheld-upstream.patch`, raw
merge evidence, a diffstat and a JSON receipt.
Review the candidate against both parents, and validate its materialized tree, not the
branch containing this planner.

To materialize in a fresh disposable checkout at the receipt's fork ID, inspect the diff,
run `git apply --check /absolute/path/candidate.patch`, then apply it with `git apply --index`.
Require `git write-tree` to equal `candidate_tree` before testing.
Do not apply it over the prototype commit, whose added scripts would change that tree.
Applying the patch alone does not record upstream ancestry.
A later authorized integration must be a reviewed two-parent merge whose parents are the
receipt's fork and upstream IDs; otherwise the next nightly repeats the same history.
Creating that integration commit and promoting it are outside this prototype.

## Intended operating flow — not installed

The existing job is a systemd user timer, not a Hermes cron record.
It specifies 03:30 America/New_York plus up to 300 seconds of randomized delay.
Treat “3:31” as the deployment window, not an exact ordering guarantee.

| When, America/New_York | Owner | Action and durable output |
| --- | --- | --- |
| 02:55 | Preflight controller | Acquire one stable lock shared with promotion, snapshot live fork and last integrated upstream, resolve only upstream `main`, and freeze the full SHA. |
| 02:55–03:15 | Astra-authored planner | Run this merge attempt in a disposable worktree; preserve pins, review overlays and withheld fixes, materialize the tree, run targeted contracts and `scripts/check`. |
| By 03:20 | Referee | Review both-parent diffs and exact-tree test receipts. Seal a candidate only if every gate passes; otherwise atomically enqueue one incident. |
| 03:30–03:35 | Deployment controller | Consume an explicitly approved, exact candidate SHA whose first parent still equals live `aspecttech`. No approved candidate means HOLD, with no install/restart. |
| After promotion | Deployment owner | Install through PM, preserve deployed Google policy, restart externally, check code SHA/service health and offline profile/voice readiness, then record success. |
| 08:00 or next crew dispatch | Fix lane + referee | Claim the oldest blocked incident, work from its frozen input, resolve and retest on a new branch, and return a new review receipt. |

Production preflight must use an isolated `HERMES_HOME` and `HERMES_RUNTIME_DIR`, no
production credentials, and explicit profiles for gateway/voice probes.
The lock must be a stable separate lockfile, not the inode of a replaced updater script.
Use a fresh directory per attempt and atomic result publication; interrupted runs never
publish a ready pointer.
The 03:31 consumer cannot treat an earlier day's receipt, a nonzero test exit, a missing
report, or `needs_validation` as success.

Fetch only `refs/heads/main` into a run-specific namespace; do not refresh every upstream
feature branch or chase commits arriving after the snapshot.
Require the previous integrated upstream to be an ancestor of the fetched snapshot.
If upstream main was rewritten, queue an ancestry-review incident; never force-reset the fork.
Force updates to unrelated feature branches are not themselves a merge conflict.
If the fork changes before promotion, mark the candidate stale and defer regeneration to
the next budgeted attempt; do not keep rebasing inside tonight's job.
Keep mirror-main maintenance separate from live deployment, in its own checkout.

## Fallback queue and limits

The planner's receipt is an input to a future queue controller; it does not install or
dispatch that controller.
Persist an incident keyed by repository, target branch and unresolved conflict family,
not by upstream SHA (a new push must not erase its age).
Record first-seen time, distinct scheduled nights attempted, frozen fork/upstream/base,
pin manifest hash, affected paths, test failures, artifact locations, owner, lease expiry,
last progress, and status: `blocked`, `claimed`, `review`, `approved`, `stale`, or
`needs_mitchell`.
Use atomic replacement under the shared lock, and expire a crashed worker's claim without
discarding its evidence.
Identical inputs reuse their receipt; retrying in one night never increments the night count.
An open incident keeps its frozen lane; newer upstream becomes next-run backlog, not a reset.
After a fixed snapshot is promoted, only close its incident when validation and deployment
receipts pass; merging alone is not resolution.

Proposed aggregate cap per scheduled night: **25 minutes and 60,000 metered input + output
tokens across all crew roles**, including retries and the associated morning fix dispatch.
Cached input counts toward the conservative ceiling too.
Reserve 15 minutes for deterministic assembly/tests, 6 for fixes and 4 for review; a stage
can finish early but cannot extend the aggregate deadline.
Use an outer process-group timeout and the model runner's enforced token budget, not just
a prompt instruction; snapshot evidence and queue a timeout if either limit is reached.
Ordinary clean merges need no author-model call beyond review policy.
The Python prototype has per-Git-call timeouts only; aggregate metering, process-group
cancellation, the persistent queue, scheduling and promotion are still to be implemented.

Maximum **three unresolved scheduled nights**, then stop automatic fix attempts and route
one evidence packet to Mitchell; retain HOLD until he gives a disposition.
The ceiling across one incident is therefore 75 minutes / 180,000 tokens.
Seed tonight's incident with the two conflict loops reported in the request; do not reset
the count at cutover or on a new upstream SHA.
If its next scheduled attempt still fails, escalate that morning, not on a fourth night.
Pin changes require Mitchell immediately; a third night never authorizes unpinning.
Quiet success remains log-only; duplicate conflict alerts coalesce into the incident.

## Required cutover after Mitchell's go

Replace the active `/home/aspect/appliance/scripts/hermes-nightly-update.sh` merge phase
with the approved-candidate consumer, and update its documentation/unit description.
Do not modify the older, inactive same-named script under the Hermes scripts directory.
The current active script fetches all upstream branches, switches the live checkout to
main, pushes the mirror, merges upstream into aspecttech, pushes it, then installs/restarts.
That path must no longer run for aspecttech; adding preflight alongside it leaves the loop.

Install the shared lock and preflight schedule, convert the existing timer's consumer,
and exercise HOLD, stale-candidate, failed-test and restart-failure cases before enabling
promotion. Preserve the existing deployed Google source policy checks and PM workflow.
Do this between runs, accounting for the timer's `Persistent=true` catch-up behavior.
No approved artifact means a quiet logged HOLD plus the queue record; no fallback to the
naive merge.
Until this cutover is authorized and installed, the old updater remains active and can
repeat its conflict; this prototype cannot stop it by itself.

For future promotion, keep an exact source/dependency rollback receipt and compare the
live head under lock before any update. Do not force-rewrite remote history on rollback.
If a promoted revision is unhealthy, the deployment owner restores the known-good local
revision/environment and records the mismatch for a reviewed forward repair.
Restart failure never marks a candidate deployed, and repair never happens inside the
gateway process being restarted.

## Tonight's expected result

At the frozen inputs above there is one text conflict, in pinned `gateway/run_turn.py`.
Pin restoration resolves the text conflict and yields tree
`99c5335e581cec01d42b0b886ce6d5c7fc8188fe`.
This is **not a deployable fix**: upstream session-retirement siblings require new state
on the pinned authority, and the previous pin-induced regressions remain.
The correct overnight action is HOLD plus a morning compatibility lane, with the patch
available for referee rehearsal.
Do not silently add lazy attributes, bypass retirement, or unpin the authority to manufacture
a green result. Review a complete compatibility solution and its lifecycle contracts.
