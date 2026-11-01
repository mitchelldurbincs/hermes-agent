"""The fork's live-voice request seam (``gateway/voice_requests.py``) against the real gateway.

A GPT-Live runtime hands Hermes one spoken request at a time: ``prepare_request`` binds it to a
Discord guild text channel, ``run`` executes it as an ordinary durable turn, ``result`` reads the
committed answer, ``cancel`` withdraws it. Real GatewayRunner (multiplexed, the ``voice`` profile
owning the Discord bot), DiscordAdapter, SessionAuthority, drain and SessionDB; only the Discord
client objects and the model run are stand-ins.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.waiting import async_wait_until

GUILD, OWNER, BOT, TEXT = '9001', '42', '777', '5002'
BINDING = {'guild_id': GUILD, 'owner_id': OWNER, 'bot_id': BOT, 'voice_channel_id': '5001',
           'text_channel_id': TEXT, 'profile': 'voice'}


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Each test starts with no leases, so a leak fails the test that caused it."""
    from gateway import voice_requests
    voice_requests._requests.clear()


def _discord_client():
    member = SimpleNamespace(id=int(OWNER), bot=False, display_name='Owner')
    guild = SimpleNamespace(id=int(GUILD), name='Guild', get_member=lambda uid: member if uid == int(OWNER) else None)
    channel = SimpleNamespace(id=int(TEXT), name='general', guild=guild, type=SimpleNamespace(value=0), topic=None)
    return SimpleNamespace(user=SimpleNamespace(id=int(BOT)),
                           get_channel=lambda cid: channel if cid == int(TEXT) else None)


async def _gateway(tmp_path, monkeypatch):
    import hermes_state
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_authority import initialize_session_authority
    from gateway.session_results import execution_result
    from hermes_state import SessionDB
    from plugins.platforms.discord.adapter import DiscordAdapter

    root = tmp_path / '.hermes'
    home = root / 'profiles' / 'voice'
    home.mkdir(parents=True)
    (home / 'config.yaml').write_text('{}\n', encoding='utf-8')
    # The profile's own allowlist; a home channel, so no first-turn onboarding notice is posted.
    (home / '.env').write_text(f'DISCORD_ALLOWED_USERS={OWNER}\nDISCORD_HOME_CHANNEL={TEXT}\n', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)  # profile homes are HOME-anchored
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=True))
    runner.config._runtime_profile_homes = (('default', root.resolve()), ('voice', home.resolve()))  # served homes
    launch = await initialize_session_authority(runner, profile_id=str(root), instance_id='owner',
                                                db=SessionDB(root / 'state.db'))
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id='owner',
                                                   db=SessionDB(home / 'state.db'), register=False)
    runner.session_authorities = SessionAuthorities(root, multiplexed=True)
    runner.session_authorities.add(root, launch)
    runner.session_authorities.add(home, authority, name='voice')
    monkeypatch.setattr(runner, '_read_user_config', lambda: {'approvals': {'destructive_slash_confirm': False}})

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    adapter.gateway_runner = runner
    adapter._client = _discord_client()
    sent = []

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)
        return SendResult(success=True, message_id='s')
    monkeypatch.setattr(adapter, 'send', send)
    runner._profile_adapters = {'voice': {Platform.DISCORD: adapter}}
    runner._configure_profile_adapter(adapter, 'voice', Platform.DISCORD)

    ran, gates = [], {}

    async def run_agent(*, message, session_id, **_kwargs):
        ran.append((message, session_id))
        if message in gates:
            await gates[message].wait()
        reply = f'spoken answer to {message!r}'
        result = {'final_response': reply, 'completed': True, 'api_calls': 1, 'failed': False,
                  'messages': [{'role': 'user', 'content': message}, {'role': 'assistant', 'content': reply}]}
        execution_result.get()['result'] = result  # what TurnRunner.run_sync records for a real turn
        return result
    monkeypatch.setattr(runner, '_run_agent', run_agent)
    return SimpleNamespace(runner=runner, adapter=adapter, authority=authority, sent=sent, ran=ran, gates=gates)


def _registry():
    from gateway import voice_requests
    return dict(voice_requests._requests)


def _admissions(authority):
    from hermes_state_runtime import list_session_admissions
    return [row for sid in authority.sessions
            for row in list_session_admissions(authority.db, session_id=sid, pending_only=False)]


def _prepare(gw, context, is_current=lambda: True):
    from gateway import voice_requests
    return voice_requests.prepare_request(gw.runner, gw.adapter, binding=BINDING, context=context, is_current=is_current)


async def _type_in_channel(gw, text):
    """The owner types in the bound text channel: an ordinary turn on the same conversation."""
    from gateway.platforms.event import MessageEvent
    source = gw.adapter.build_source(chat_id=TEXT, chat_type='group', user_id=OWNER, guild_id=GUILD)
    return asyncio.create_task(gw.adapter.handle_message(MessageEvent(text=text, source=source, message_id=text)))


async def _settled(handle):
    """The request's dispatch (its handler) has returned: the row is settled or was never committed.
    Awaiting the task (not polling ``done()``) resumes after its earlier-registered done-callbacks."""
    await async_wait_until(lambda: handle.task is not None, what='the dispatch')
    await asyncio.wait([handle.task], timeout=10)


@pytest.mark.asyncio
async def test_a_spoken_request_runs_as_a_durable_turn_and_returns_only_its_answer(tmp_path, monkeypatch):
    from gateway import voice_requests
    gw = await _gateway(tmp_path, monkeypatch)
    assert voice_requests.inspect_binding(gw.runner, gw.adapter, BINDING)['profile'] == 'voice'

    reply = await _prepare(gw, 'what is on my calendar').run()

    assert reply['text'] == "spoken answer to 'what is on my calendar'"
    assert [message for message, _ in gw.ran] == ['what is on my calendar']
    assert [(row['status'], row['outcome']) for row in _admissions(gw.authority)] == [('terminal', 'completed')]
    assert gw.sent == []  # the answer goes back through the durable receipt, never into the text channel
    assert _registry() == {}


@pytest.mark.asyncio
async def test_a_request_revoked_before_it_runs_never_dispatches_or_holds_a_lease(tmp_path, monkeypatch):
    from gateway import voice_requests
    gw = await _gateway(tmp_path, monkeypatch)
    handle = _prepare(gw, 'never mind', is_current=lambda: False)  # the speaker left before dispatch

    with pytest.raises(voice_requests.LiveRequestRefused, match='revoked'):
        await handle.run()

    assert gw.ran == [] and _admissions(gw.authority) == []
    assert _registry() == {}
    assert await handle.cancel() is True  # nothing was dispatched, so nothing is owed


@pytest.mark.asyncio
async def test_a_request_refused_before_its_commit_releases_its_lease(tmp_path, monkeypatch):
    """Revoked after authorization but before the durable write: no row, no turn, and the lease is
    gone (a leaked lease per refusal fills the 128-request cap and refuses all voice until restart)."""
    from gateway import voice_requests
    gw = await _gateway(tmp_path, monkeypatch)
    current = [True]
    admit = gw.runner._hm_admit_event

    async def admit_then_speaker_leaves(event):
        admitted = await admit(event)
        current[0] = False
        return admitted
    monkeypatch.setattr(gw.runner, '_hm_admit_event', admit_then_speaker_leaves)
    handle = _prepare(gw, 'turn the lights off', is_current=lambda: current[0])

    with pytest.raises(voice_requests.LiveRequestRefused, match='revoked'):
        await handle.run()
    await _settled(handle)

    assert gw.ran == [] and _admissions(gw.authority) == []
    assert _registry() == {}


@pytest.mark.asyncio
async def test_barge_in_interrupts_the_running_turn_and_releases_its_lease(tmp_path, monkeypatch):
    gw = await _gateway(tmp_path, monkeypatch)
    interrupted = []
    interrupt = gw.authority.interrupt

    async def recorded_interrupt(actor, ref, generation):
        interrupted.append(generation)
        return await interrupt(actor, ref, generation)
    monkeypatch.setattr(gw.authority, 'interrupt', recorded_interrupt)
    gw.gates['tell me a long story'] = asyncio.Event()
    handle = _prepare(gw, 'tell me a long story')
    caller = asyncio.create_task(handle.run())
    await async_wait_until(lambda: gw.ran, what='the turn to start')

    caller.cancel()  # the speaker talks over the answer
    with pytest.raises(asyncio.CancelledError):
        await caller
    await async_wait_until(lambda: interrupted, what='the interrupt')
    gw.gates['tell me a long story'].set()
    await _settled(handle)

    assert [row['status'] for row in _admissions(gw.authority)] == ['terminal']
    assert _registry() == {}


@pytest.mark.asyncio
async def test_a_request_whose_lease_is_gone_is_never_executed(tmp_path, monkeypatch):
    """A restart drops the in-memory lease while the row waits behind a typed turn: the drain
    refuses the row instead of replaying a paid voice turn nobody is listening to."""
    from gateway import voice_requests
    gw = await _gateway(tmp_path, monkeypatch)
    gw.gates['typed first'] = asyncio.Event()
    typed = await _type_in_channel(gw, 'typed first')
    await async_wait_until(lambda: gw.ran, what='the typed turn to start')
    handle = _prepare(gw, 'spoken second')
    caller = asyncio.create_task(handle.run())
    await async_wait_until(lambda: len(_admissions(gw.authority)) == 2, what='the spoken row queued')

    voice_requests._requests.clear()  # what a restart leaves: the row, without its lease
    gw.gates['typed first'].set()
    await typed

    with pytest.raises(voice_requests.LiveRequestRefused):
        await caller
    await async_wait_until(lambda: gw.sent, what='the typed reply')
    assert [message for message, _ in gw.ran] == ['typed first']
    assert sorted(row['outcome'] for row in _admissions(gw.authority)) == ['completed', 'failed']
    assert gw.sent == ["spoken answer to 'typed first'"]


@pytest.mark.asyncio
async def test_a_live_request_switches_off_chained_auto_tts_for_its_own_turn_only(tmp_path, monkeypatch):
    """GPT-Live supplies the speech: the chat's auto-TTS gate (what streaming TTS asks) reads off
    inside the voice turn and stays on for a typed turn in the same channel."""
    gw = await _gateway(tmp_path, monkeypatch)
    gw.adapter._auto_tts_enabled_chats.add(TEXT)
    asked = {}
    run_agent = gw.runner._run_agent

    async def agent_asking_the_gate(*, message, **kwargs):
        asked[message] = gw.adapter._should_auto_tts_for_chat(TEXT)
        return await run_agent(message=message, **kwargs)
    monkeypatch.setattr(gw.runner, '_run_agent', agent_asking_the_gate)

    await _prepare(gw, 'spoken').run()
    await _type_in_channel(gw, 'typed')
    await async_wait_until(lambda: 'typed' in asked, what='the typed turn')

    assert asked == {'spoken': False, 'typed': True}
