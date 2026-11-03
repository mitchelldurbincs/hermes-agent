# Crew-owned nightly merge planner

`scripts/nightly_crew.py` remains the immutable-input tree assembler: normal three-way
merge, refusal of unknown conflicts, exact restoration and SHA-256 verification of all
seven pins. Its output alone is never approval.

The implemented controller, receipt consumer, queue, systemd units and cutover commands
are in [the appliance runbook](../../appliance/nightly/RUNBOOK.md).
The controller installs this planner under `appliance/lib/` and the unchanged manifest
under `appliance/nightly/`; there is one implementation of pin-preserving assembly.

```sh
python3 scripts/nightly_crew.py --repo . \
  --fork 622d41a6bce667a36885d704a6a46723b3b671c5 \
  --upstream d70686448812f62dbd6630cd83d0565f709f097b \
  --pins scripts/nightly_crew/pins.json --output "${TMPDIR:-/tmp}/new-nightly-rehearsal"
```

The raw November 3 tree remains `99c5335e581cec01d42b0b886ce6d5c7fc8188fe`.
Its retirement incompatibility is fixed by the reviewed non-pinned adapter and binding
hooks in `appliance/nightly/retirement-compat.patch`. The preflight verifies that patch's
configured hash, applies it in a private index, and checks all pins again. If the patch
is already integrated, reverse-application checking proves that before skipping it.
Unexpected upstream context changes HOLD for review instead of guessing.

No pin revisions are authorized. Matching the 14 baseline failures does not approve a
candidate: the retirement tests must pass and remaining failures still hold promotion.
