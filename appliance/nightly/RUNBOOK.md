# Crew nightly cutover

The pinned authority keeps its exact bytes. A per-profile adapter supplies the missing
retirement lifecycle at boot and local/API binding: every live session receives close/timer
state, admission restores retired sessions, detach protects the close across awaits, and
the drain checks retirement on normal exit. Upstream retains all eligibility and durable
receipt checks. `UserDict` registration also covers native `setdefault` and `update` paths.
No module/class monkeypatch, pin revision, or retirement bypass is used.

## Review and promotion

Preflight runs at **02:55 America/New_York**. It shares the stable
`/home/aspect/appliance/state/hermes-nightly/nightly.lock` with promotion, approval and queue
claims. It freezes the clean live `aspecttech` HEAD and fetches only upstream main into a
unique ref. The integrated upstream frontier must be an ancestor of both inputs.
It merges normally, restores all seven pins, applies the reviewed non-pinned compatibility
patch (or verifies it is already present), and retains both-parent patches and an exact
candidate commit with first parent equal to the frozen fork.

The gate process builds fresh PM test environments for the two lockfiles in isolated homes, including the messaging extra needed by Discord contracts.
It runs PR #1, Discord isolation, completion folding, the four pin-lag test files, and the
candidate retirement tests using `scripts/run_tests.sh`. It compares individual test
identities/outcomes and runs `scripts/check`. A changed failure identity, absent test,
collection error, failed runner, or missing evidence refuses the candidate.
Matching the baseline is diagnostic success; **any remaining failure prevents approval**.
The known 87-pass/14-fail baseline therefore still HOLDs pending its disposition.

The **03:33 America/New_York** consumer requires a same-night, unexpired, explicitly
referee-approved receipt, matching evidence/pin hashes, exact candidate tree and parents,
and an unchanged, clean live fork. Otherwise it only records HOLD. There is no fallback
fetch/merge, installation, restart, or Google-policy repair on a HOLD.
After approval it fast-forwards to the already-tested commit and pushes without force.
It records `promoted_awaiting_deployment_owner`; PM installation, deployed Google policy
verification, external service restart and health/rollback checks remain the deployment
owner's separate operation. This cutover never deploys the gateway.

Referee approval, only after reviewing the exact candidate and both-parent diffs:

```sh
python3 /home/aspect/appliance/scripts/nightly_crew_promote.py \
  --config /home/aspect/appliance/nightly/config.json \
  --approve REFEREE_NAME --expires YYYY-MM-DDTHH:MM:SS+00:00
```

Approval cannot override failing gates. The existing 14 baseline failures need a separately
reviewed resolution before this command can approve anything.

## Install order and commands

Run between nightly invocations. Keep a timestamped rollback directory and JSON receipt.
The executed commands and hashes for the actual installation live in the report's
`cutover/commands.log` and `cutover/receipt.json`. User changes elsewhere in appliance are
out of scope. Install from the reviewed appliance worktree, not an uncommitted live copy.

1. Run disposable-repository HOLD/stale/failure/queue tests and keep their log in this run.
   Record the live fork SHA and timer/service state. Refuse if the updater or preflight
   service is running. Copy the old script, all affected source/installed unit files, enabled
   state and candidate pointer into the rollback directory. Record missing files too.
2. Stop only updater/crew timers (never gateway/dashboard services):

```sh
systemctl --user stop hermes-nightly-update.timer
systemctl --user stop hermes-nightly-crew-preflight.timer hermes-nightly-crew-escalate.timer
```

The second command is unnecessary on the first installation when the units do not exist.

3. Copy `appliance/scripts/nightly_crew_*.py`, `hermes-nightly-update.sh`, `nightly/config.json`,
   `nightly/retirement-compat.patch`, `nightly/RUNBOOK.md` into `/home/aspect/appliance/`.
   Install the original planner `scripts/nightly_crew.py` as `appliance/lib/nightly_crew.py`
   and `scripts/nightly_crew/pins.json` as `appliance/nightly/pins.json`.
   Install the six service/timer files in both `appliance/systemd/` and
   `/home/aspect/.config/systemd/user/`. Do not touch any gateway unit.
4. Publish a HOLD pointer before enabling timers. Seed the unresolved `baseline_pin_lag`
   incident with **2026-11-02 and 2026-11-03**, preserving the two recorded blocked nights;
   include the historical log and this run's diagnostic receipt. Run the installed consumer
   once in HOLD-only mode and compare the live Git SHA before/after:

```sh
/home/aspect/appliance/scripts/hermes-nightly-update.sh --hold-only
systemctl --user daemon-reload
systemd-analyze --user verify /home/aspect/.config/systemd/user/hermes-nightly-update.service \
  /home/aspect/.config/systemd/user/hermes-nightly-crew-preflight.service \
  /home/aspect/.config/systemd/user/hermes-nightly-crew-escalate.service
```

5. Timer activation is the last step, after the preceding receipts exist:

```sh
systemctl --user enable --now hermes-nightly-crew-preflight.timer
systemctl --user enable --now hermes-nightly-crew-escalate.timer
systemctl --user enable --now hermes-nightly-update.timer
systemctl --user list-timers --all hermes-nightly-update.timer \
  hermes-nightly-crew-preflight.timer hermes-nightly-crew-escalate.timer
```

All timers use `Persistent=false` and zero random delay, so installation does not catch up
an old invocation. The original updater used 03:30 plus up to five minutes of jitter.

## Rollback

The rollback receipt records the original contents and enabled/active states. Stop the
three timers above, restore each backed-up file to its recorded destination (remove only
newly installed files recorded absent), then `systemctl --user daemon-reload`.
Leave the old updater timer disabled by default: enabling it restores the naive merge,
install and gateway-restart behavior. Only after an explicit decision to restore that
behavior, run `systemctl --user enable --now hermes-nightly-update.timer`.
No source reset, dependency reinstall, gateway restart, or remote rewrite is part of this
scheduler rollback. Preserve receipts and queue history for investigation.

## Queue, budget, escalation and mirror

Incidents are keyed by `(repo, branch, conflict-family)`, not changing upstream SHAs.
Distinct Eastern calendar nights age an incident; repeated attempts that night do not.
Leases last 900 seconds and may be reclaimed after expiry:

```sh
python3 /home/aspect/appliance/scripts/nightly_crew_queue.py \
  --config /home/aspect/appliance/nightly/config.json --claim INCIDENT_ID --owner CREW_ROLE
```

Night three changes status to `needs_mitchell`, writes one escalation packet, and stops
further preflight/fix attempts until disposition. An active lease also holds preflight.
The 08:00 dispatcher sends the packet to the existing `discord:aspectcisco` Mitchell
route via `hermes send`, then records delivery; successes are quiet. Delivery failures
retain the packet. A crash after sending but before recording can duplicate a message;
incident IDs make that delivery ambiguity visible. No paid model or voice calls run in
this lane: **zero automatic model tokens**. Preflight has a 25-minute systemd cgroup
limit; `ExecStopPost` queues timeout/crash evidence. Individual gates have shorter limits.
The claimed fix lane is a durable work queue, not an installed autonomous model runner.

Mirror-main maintenance is a separate manual command and checkout, never a phase of the
live updater. Provision `config.mirror_repo` with its own origin/upstream remotes, then:

```sh
python3 /home/aspect/appliance/scripts/nightly_crew_mirror.py \
  --config /home/aspect/appliance/nightly/config.json
```

It requires a clean `main`, verifies ancestry, fast-forwards and pushes without force.
No mirror timer or mirror push is installed/executed by this cutover.
