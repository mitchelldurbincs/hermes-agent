"""The fork's streaming PCM tap on Discord's ``VoiceReceiver`` (``pcm_callback`` mode).

A live voice runtime receives each allowlisted speaker's decoded frames as they arrive instead of
upstream's buffered chained STT. Real receiver and RTP parsing; NaCl and opus are stand-ins.
"""
import struct
import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("discord")

from plugins.platforms.discord.adapter import VoiceReceiver

OWNER, GUEST = 42, 43
PCM = b"\x01\x02" * 960


class _Aead:
    decrypts = 0

    def __init__(self, key):
        pass

    def decrypt(self, encrypted, header, nonce):
        _Aead.decrypts += 1
        return b"\xfc\xff\xfe"


@pytest.fixture
def codec(monkeypatch):
    """NaCl transport decrypt and the opus decoder, counted."""
    _Aead.decrypts = 0
    nacl, secret = types.ModuleType("nacl"), types.ModuleType("nacl.secret")
    secret.Aead, nacl.secret = _Aead, secret
    monkeypatch.setitem(sys.modules, "nacl", nacl)
    monkeypatch.setitem(sys.modules, "nacl.secret", secret)
    with patch("discord.opus.Decoder") as decoder:
        decoder.return_value.decode.return_value = PCM
        yield _Aead


def _receiver(on_pcm, allowed=(str(OWNER),)):
    conn = SimpleNamespace(secret_key=bytes(32), dave_session=None, dave_protocol_version=0, dave_downgraded=False,
                           dave_pending_transitions={}, ssrc=999, hook=None, ws=None,
                           add_socket_listener=lambda cb: None, remove_socket_listener=lambda cb: None)
    vc = SimpleNamespace(_connection=conn, channel=SimpleNamespace(members=[]), user=SimpleNamespace(id=1))
    receiver = VoiceReceiver(vc, allowed_user_ids=set(allowed), pcm_callback=on_pcm)
    receiver.start()
    return receiver


def _packet(ssrc):
    return struct.pack(">BBHII", 0x80, 0x78, 1, 1000, ssrc) + b"e" * 8 + b"nonc"


def test_only_a_mapped_allowlisted_speaker_is_streamed(codec):
    frames = []
    receiver = _receiver(lambda user, pcm, at: frames.append((user, pcm)))
    receiver.map_ssrc(1, OWNER)
    receiver.map_ssrc(2, GUEST)  # in the channel, not on the speaker allowlist

    for ssrc in (1, 2, 3):  # 3: no SPEAKING mapping yet
        receiver._on_packet(_packet(ssrc))

    assert frames == [(OWNER, PCM)]
    assert codec.decrypts == 1  # an unknown sender is dropped before any decrypt
    assert not receiver._buffers  # the stream never feeds upstream's buffered chained STT


def test_the_consumer_may_stop_the_receiver_from_its_callback(codec):
    """The callback runs outside the receiver lock: revoking capture from inside it must not
    deadlock the receive thread."""
    receiver = _receiver(lambda user, pcm, at: receiver.stop())
    receiver.map_ssrc(1, OWNER)
    reader = threading.Thread(target=receiver._on_packet, args=(_packet(1),), daemon=True)

    reader.start()
    reader.join(5)

    assert not reader.is_alive(), "the receive thread deadlocked on the receiver lock"
    assert receiver._running is False


def test_a_frame_decoded_after_capture_paused_is_not_streamed(codec):
    frames = []
    receiver = _receiver(lambda user, pcm, at: frames.append(user))
    receiver.map_ssrc(1, OWNER)

    def decode_while_paused(_data):
        receiver.pause()  # capture revoked while this frame was decoding
        return PCM
    with patch("discord.opus.Decoder") as decoder:
        decoder.return_value.decode.side_effect = decode_while_paused
        receiver._on_packet(_packet(1))

    assert frames == []


@pytest.mark.parametrize("allowed", [set(), {"not-a-snowflake"}, {OWNER}])
def test_a_stream_receiver_needs_an_explicit_snowflake_allowlist(allowed):
    from plugins.platforms.discord.adapter import DiscordAdapter
    adapter = DiscordAdapter.__new__(DiscordAdapter)
    adapter._client = object()
    with pytest.raises(ValueError):
        adapter.create_voice_stream_receiver(SimpleNamespace(client=adapter._client),
                                             allowed_user_ids=allowed, on_pcm=lambda *a: None)
