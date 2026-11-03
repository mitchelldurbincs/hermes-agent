"""Retirement lifecycle for the fork's byte-pinned session authority.

The pin predates retirement. Install once, before publishing an authority or binding a
local/API conversation. The adapter owns registration, restoration, close protection,
and the idle boundary; upstream session_retirement still owns every eligibility check.
No class/global monkeypatch: each profile gets its own adapter and original methods.
"""
from collections import UserDict

from gateway.session_contract import SessionRef


class RetirementSessions(UserDict):
    """Register lifecycle state even for LiveSessions created inside the pinned file."""

    def __setitem__(self, key, live):
        live.closed_stamp = None
        live.retire_timer = None
        live.detaching = 0
        super().__setitem__(key, live)


class RetirementLifecycle:
    """Connect the pinned scheduler to upstream's durable retirement protocol."""

    def __init__(self, authority):
        self.authority = authority
        self.original = {name: getattr(authority, name) for name in
                         ('authorize', 'detach', '_publish_pending', '_schedule', '_drain')}
        sessions = RetirementSessions()
        for sid, live in authority.sessions.items():
            sessions[sid] = live
        authority.sessions = sessions
        authority.retired_local = set()
        authority.retired_routes = {}
        for name in self.original:
            setattr(authority, name, getattr(self, name))

    def authorize(self, actor, ref, capability):
        from gateway.session_retirement import touch_restored
        result = self.original['authorize'](actor, ref, capability)
        touch_restored(self.authority, ref.session_id)
        return result

    def _publish_pending(self, ref):
        from gateway.session_retirement import restore_retired
        restore_retired(self.authority, ref.session_id)
        return self.original['_publish_pending'](ref)

    def _schedule(self, ref):
        from gateway.session_retirement import restore_retired
        restore_retired(self.authority, ref.session_id)
        return self.original['_schedule'](ref)

    async def _drain(self, ref):
        from gateway.session_retirement import retire_at_idle
        live = self.authority.sessions[ref.session_id]
        result = await self.original['_drain'](ref)
        # Only a normal exit. Upstream rechecks the ledger, workers, viewers, rescan,
        # close stamp and locks; an unknown/queued claim cannot be retired here.
        retire_at_idle(self.authority, ref, live)
        return result

    async def detach(self, actor, subscription_id, *, end_reason=None, orphaned=False):
        from gateway.session_retirement import retire_at_idle
        found = next(((sid, live) for sid, live in self.authority.sessions.items()
                      if subscription_id in live.subscribers), None)
        if found is None:
            return await self.original['detach'](actor, subscription_id,
                                                  end_reason=end_reason, orphaned=orphaned)
        sid, live = found
        # Protect the entire detach, including the off-loop editor release and close
        # stamp. A timer cannot remove the object between those two operations.
        live.detaching += 1
        try:
            result = await self.original['detach'](actor, subscription_id,
                                                   end_reason=end_reason, orphaned=orphaned)
        finally:
            live.detaching -= 1
        retire_at_idle(self.authority, SessionRef(self.authority.profile_id, sid), live)
        return result


def install_retirement(authority):
    """Idempotent owner-side installation at boot and direct local/API restore seams."""
    if '_retirement_lifecycle' not in vars(authority):
        authority._retirement_lifecycle = RetirementLifecycle(authority)
