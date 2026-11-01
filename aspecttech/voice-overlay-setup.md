# Rolling out the voice-overlay cleanup (`aspecttech-overlay-cleanup`)

A runbook for taking the cleaned-up GPT-Live voice overlay from its review branch to the running
gateway. A person can follow it, or you can hand it to your Hermes agent. This file is fork-only: it
lives under `aspecttech/`, a path upstream never has, so it can't conflict on a sync.

> **If you are a Hermes agent running this:** do the steps in order and stop at every ⛔ to get
> the operator's go-ahead. Those steps push to the fork, restart the production gateway, or change
> the GPT-Live runtime. Never run a bare `hermes update` on the gateway host (see step 5). If a check
> fails, stop and report the output. Don't work around it.

---

## 0. What you're deploying

| | |
|---|---|
| Repo | `mitchelldurbincs/hermes-agent` |
| Branch | `aspecttech-overlay-cleanup` = `aspecttech` (`17ea3f59f`) + `ff2075ce3` (the code fix) + a docs commit adding this guide |
| Upstream base | `a4994d4ea` (fork `main`, a pure mirror) |
| Dependencies | none changed (no `pyproject.toml` / `uv.lock` edits) |

What the commit changes:

1. **Voice requests run again.** The previous overlay validated the request's source at the top of
   `_handle_message`. When the drain restores a queued turn to execute it, the routing identity isn't
   attached to the source yet. In a test rig built from the real gateway code, every request was
   therefore refused at execution ("Admitted turn … failed", `voice_request_not_completed`). That
   restore code is the same at `81a55437`, so it isn't from a recent sync. If voice works in
   production today, check which code production is actually running. The overlay now looks up the
   lease first and validates after admission.
2. **The lease leak is fixed.** A lease used to stay in the registry after a refusal before commit or
   after a barge-in. After 128 of those, every voice request was refused until restart. Now a lease
   lives exactly as long as its dispatch.
3. **Less code in upstream files.** `gateway/run_turn.py` and `gateway/run_voice.py` are upstream
   files again. The chained-TTS skip is now a `DiscordAdapter._should_auto_tts_for_chat` override.
4. **The Discord stream callback runs outside the receiver lock**, so the callback may call `stop()`
   without deadlocking. Upstream's buffered speech-to-text path is unchanged.
5. **New tests:** `tests/gateway/test_voice_requests.py` and `tests/gateway/test_discord_voice_stream.py`.

---

## 1. Verify the branch in a dev checkout (not the gateway host)

```bash
git clone https://github.com/mitchelldurbincs/hermes-agent hermes-dev && cd hermes-dev
git checkout aspecttech-overlay-cleanup
git log --oneline -3          # the docs commit, then ff2075ce3, then 17ea3f59f

# Isolated home outside the checkout, so nothing touches the real ~/.hermes
DEV_HOME="$(mktemp -d)"
export HERMES_HOME="$DEV_HOME/home" HERMES_RUNTIME_DIR="$DEV_HOME/home/tools"
source ./activate

python scripts/check          # all 14 checks must report ok
scripts/run_tests.sh tests/gateway/test_voice_requests.py tests/gateway/test_discord_voice_stream.py
```

Expected: `scripts/check: 14 checks, ok` and `2 files, 12 tests passed`.

- If `scripts/check` says `no merge-base with origin/main or main`, run
  `python scripts/check --base "$(git merge-base HEAD origin/main)"`.
- The health report lists two "Allowed by comment" items in `plugins/platforms/discord/adapter.py`.
  Both are expected: upstream's own `FILE_LINES` allowance, and the overlay's
  `CC VoiceReceiver._on_packet` allowance with its reason.
- Optional wider run: `scripts/run_tests.sh tests/gateway/` (very long). One known failure is
  unrelated to the overlay and also fails on upstream:
  `tests/tools/test_session_tool_env.py::test_a_sessions_terminal_runs_with_its_clients_path`, when
  the host's shell profile sets `NVM_DIR`.

---

## 2. Update the GPT-Live runtime (breaking API change)

The two forwarding methods on the gateway runner were removed. Call the module directly. Same
arguments, same return values:

```python
# before
info   = runner.inspect_live_voice_binding(adapter, binding=binding)
handle = runner.prepare_live_voice_request(adapter, binding=binding, context=text,
                                           is_current=is_current)

# after
from gateway import voice_requests

info   = voice_requests.inspect_binding(runner, adapter, binding)
handle = voice_requests.prepare_request(runner, adapter, binding=binding, context=text,
                                        is_current=is_current)
```

Unchanged: `adapter.create_voice_stream_receiver(...)`, `handle.run()`, `handle.cancel()` and its
return values, `LiveRequestRefused`.

Behaviour the runtime should expect:

- **The 128-request cap now refuses from `run()`, not `prepare_request()`.** The reason string is
  still `unresolved_voice_request_limit`. A prepared handle that is never run holds nothing, and
  dropping it is safe.
- `run()` refuses a second call on the same handle with `live_voice_request_already_started`.
- Calling `run()` while `is_current()` is false raises `live_voice_request_revoked` before anything is
  dispatched.

⛔ **Operator:** make this runtime change **before** the gateway update (step 5). The module
functions exist in both the old and new overlay, so the updated runtime works against either gateway,
but the new gateway no longer has the runner methods the old runtime calls.

---

## 3. Gateway-side configuration the voice path requires

The overlay refuses a request unless all of these hold. Check them once on the gateway host.

| Requirement | Where | Refusal you'd see |
|---|---|---|
| The voice bot runs under a **named profile**, not `default` or `main` | `~/.hermes/profiles/<voice-profile>/` | `explicit_restrictive_voice_binding_required` |
| Gateway multiplexing is on (on by default when safe; set it explicitly to be sure) | default profile `config.yaml`: `gateway:` → `multiplex_profiles: true` | `voice_authorization_or_profile_binding_changed` |
| The speaker (`owner_id`) is allowlisted **in the voice profile's own `.env`** | `profiles/<voice-profile>/.env`: `DISCORD_ALLOWED_USERS=<owner id>` (or a role allowlist) | `voice_authorization_or_profile_binding_changed` |
| A home channel is set for that profile (otherwise the first turn posts upstream's "📬 No home channel" notice into the text channel) | `profiles/<voice-profile>/.env`: `DISCORD_HOME_CHANNEL=<channel id>` | (a notice in the text channel, not a refusal) |
| `binding.bot_id` is that profile's own Discord bot, and the runtime passes that profile's adapter | runtime | `receiving_bot_mismatch` |
| `text_channel_id` is a plain guild text channel (not a thread or forum) in `guild_id`; the owner is a member and not a bot | Discord | `verified_guild_text_companion_required` / `verified_owner_membership_required` |
| The session is not on managed workers | `gateway.managed_workers` | `live_voice_managed_worker_not_supported` |
| `context` ≤ 16 KB; the answer ≤ 12,000 bytes | runtime | `invalid_or_oversized_voice_context` / `voice_result_missing_or_oversized` |

All binding IDs are positive decimal strings. `mode` is `general` or `chinese_tutor`.

---

## 4. Merge into `aspecttech` and refresh the audit artifacts

⛔ **Operator:** this pushes to the deployed branch.

```bash
git checkout aspecttech
git pull --ff-only origin aspecttech
git merge --ff-only origin/aspecttech-overlay-cleanup   # fast-forward: two commits, no merge commit
git push origin aspecttech
```

Then regenerate what the audit tripwires compare against. **The pinned set shrinks from 7 files to
5.** `gateway/run_turn.py` and `gateway/run_voice.py` are byte-identical to upstream again, so drop
their pins or re-pin them as upstream.

```bash
UP=$(git merge-base aspecttech origin/main)    # the upstream commit the overlay sits on (a4994d4ea today)
PATCHED="gateway/run_inbound.py gateway/run_turn_persistence.py gateway/session_authority.py \
gateway/session_ingress.py plugins/platforms/discord/adapter.py"

# The reviewable overlay patch (the new files are included automatically)
git diff "$UP" aspecttech > voice-overlay.patch
git diff --stat "$UP" aspecttech                # expect 9 files: 5 patched + voice_requests.py + 2 tests + this guide

# Upstream-side bytes of the patched files (if your pins guard "upstream changed under us")
for f in $PATCHED; do printf '%s  %s\n' "$(git show "$UP:$f" | sha256sum | cut -d' ' -f1)" "$f"; done

# Patched bytes (if your pins guard "the deployed file is exactly the reviewed one")
sha256sum $PATCHED gateway/voice_requests.py
```

Write those values into your pin manifest in whatever format it already uses. The header of
`gateway/voice_requests.py` still says "Hermes 81a55437". If your audit reads that header, update it
to `$UP` or have the audit stop reading it.

---

## 5. Deploy to the gateway host

⛔ **Operator:** this restarts the production gateway. In-flight chat turns are interrupted.

**Do not run a bare `hermes update` here.** On a fork the update channel defaults to `main`, and the
updater may switch a checkout on another branch over to `main` before pulling. That silently drops
the whole overlay. Either update the checkout with git directly:

```bash
cd <gateway checkout>
git fetch origin
git checkout aspecttech
git merge --ff-only origin/aspecttech
hermes gateway restart          # the default profile's restart targets the multiplexing host process
```

…or pin the branch every time you use the updater (check the plan first):

```bash
hermes update --branch aspecttech --plan
hermes update --branch aspecttech
```

Confirm what's running:

```bash
git -C <gateway checkout> merge-base --is-ancestor ff2075ce3 HEAD && echo "running the fix"
hermes gateway status
```

---

## 6. Smoke-test the live path

1. `voice_requests.inspect_binding(runner, adapter, binding)` returns
   `{"profile": "<voice-profile>", "authorized": True, ...}`.
2. Join the voice channel and ask something. The answer is spoken, and **nothing** is posted in the
   bound text channel.
3. Talk over a long answer (barge-in). The turn stops, and the next request works normally.
4. Do a few barge-ins and refusals in a row, then keep using voice. You should never see
   `unresolved_voice_request_limit` (that was the leak).
5. Type a message in the bound text channel. It's answered in text, and auto-TTS still applies to
   typed turns if it's on for that chat.
6. Watch the logs while you test:

```bash
# Logs are per profile even under multiplexing: <home>/profiles/<voice-profile>/logs/
hermes -p <voice-profile> logs gateway -f
hermes -p <voice-profile> logs errors -n 100
```

Should **not** appear during normal use: `Admitted turn … failed` with `LiveRequestRefused`,
`voice_authorization_or_profile_binding_changed`, `committed without its lease`.
Expected only after a gateway restart: a queued voice row refused as `live_voice_request_lease_missing`
(by design, a voice turn is never replayed with nobody listening).

---

## 7. Rollback

⛔ **Operator.**

```bash
git checkout aspecttech
git revert --no-edit ff2075ce3      # reverts the code only (this guide stays); no force-push needed
git push origin aspecttech
# on the gateway host: fetch + ff-merge as in step 5, then
hermes gateway restart
```

The runtime's step-2 change can stay: the module functions it calls exist in the old overlay too.
The old overlay has the execution-time refusal described in step 0, so a rollback is only a
stopgap.

---

## 8. Every future upstream sync

1. Sync fork `main` with upstream, as you do now.
2. Bring `aspecttech` onto it. Rebasing keeps the overlay a single reviewable commit stack:
   `git checkout aspecttech && git rebase origin/main && git push --force-with-lease origin aspecttech`.
   Merging also works, but conflict resolutions then live inside merge commits.
3. **Run the overlay tests and checks before deploying** (step 1 commands). The byte-pins tell you
   *that* a patched file changed. The tests tell you whether the overlay still *works*, including
   when upstream changes one of the ~9 unpinned modules `voice_requests.py` depends on.
4. Regenerate the patch and pins (step 4), then deploy (step 5).

---

## 9. Still open (not in this branch)

- `voice_requests.py` tidy-up: stale `81a55437` header; the `allow_gateway_control` parameter only
  accepts `False`; `"typed_approvals_enforced": True` is hard-coded; app-specific `mode` /
  `chinese_tutor` validation lives in the gateway; each prepare builds the source twice;
  mixed naming (appliance / live voice / GPT-Live).
- The voice context is stored as the user's message (up to 16 KB per request, carried in history).
  Upstream's `voice-live` surface keeps it out of the transcript. Check what the runtime sends.
- Evaluate upstream's `SessionAuthority.submit(..., surface="voice-live")` and the `hermes-talk`
  catalog plugin as a way to drop more of the overlay.
- Prune the fork's ~2,800 mirrored branches if they're leftovers from forking.
