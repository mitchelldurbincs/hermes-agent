"""Pinned-authority registration covers native setdefault paths and profile isolation."""
from types import SimpleNamespace

from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_retirement_compat import install_retirement


def authority():
    return SessionAuthority(SimpleNamespace(), profile_id='fixture', instance_id='fixture', db=None, epoch=1)


def test_native_registration_preserves_lifecycle_state_on_repeated_bind():
    owner = authority()
    install_retirement(owner)
    live = owner.sessions.setdefault('chat', LiveSession(None, 'route'))
    live.detaching = 1
    live.closed_stamp = ('closed',)
    assert owner.sessions.setdefault('chat', LiveSession(None, 'other')) is live
    install_retirement(owner)
    assert live.detaching == 1 and live.closed_stamp == ('closed',)
    assert live.retire_timer is None


def test_each_profile_owns_its_own_retirement_and_native_session_registration():
    first, second = authority(), authority()
    install_retirement(first)
    install_retirement(second)
    first.retired_local.add('chat')
    first.retired_routes['chat'] = 'first-route'
    second.sessions.update({'chat': LiveSession(None, 'second-route')})
    assert second.retired_local == set() and second.retired_routes == {}
    assert second.sessions['chat'].detaching == 0
    assert first.sessions == {}
