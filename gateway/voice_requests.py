"""Explicit appliance compatibility extension for Hermes 81a55437.

Install as gateway/voice_requests.py only with the matching reviewed patch.
This is a request/result/cancellation seam, NOT a provider spending limiter.
There is no direct AIAgent call, new credential loader, or adapter monkeypatch.
"""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import threading
from types import SimpleNamespace
import uuid

PREFIX = "appliance_voice_"
_requests = {}
_lock = threading.RLock()
_executing = ContextVar("appliance_live_voice_request", default=None)


class LiveRequestRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class VoiceBinding:
    guild_id: str
    owner_id: str
    bot_id: str
    voice_channel_id: str
    text_channel_id: str
    profile: str
    mode: str = "general"


def _binding(value):
    try:
        result = VoiceBinding(**value)
        for field in ("guild_id", "owner_id", "bot_id", "voice_channel_id", "text_channel_id"):
            item = getattr(result, field)
            if not isinstance(item, str) or not item.isdecimal() or int(item) <= 0:
                raise ValueError()
        if (not isinstance(result.profile, str) or not result.profile
                or result.profile in {"default", "main"}
                or result.mode not in {"general", "chinese_tutor"}):
            raise ValueError()
        return result
    except (TypeError, ValueError):
        raise LiveRequestRefused("explicit_restrictive_voice_binding_required") from None


def _source(runner, adapter, binding):
    client = getattr(adapter, "_client", None)
    if (getattr(adapter, "gateway_runner", None) is not runner
            or str(getattr(getattr(client, "user", None), "id", "")) != binding.bot_id):
        raise LiveRequestRefused("receiving_bot_mismatch")
    channel = client.get_channel(int(binding.text_channel_id))
    if (channel is None or str(channel.guild.id) != binding.guild_id
            or getattr(getattr(channel, "type", None), "value", None) != 0):
        raise LiveRequestRefused("verified_guild_text_companion_required")
    member = channel.guild.get_member(int(binding.owner_id))
    if member is None or member.bot:
        raise LiveRequestRefused("verified_owner_membership_required")
    source = adapter.build_source(
        chat_id=binding.text_channel_id, chat_type="group", user_id=binding.owner_id,
        user_name=member.display_name, guild_id=binding.guild_id,
    )
    if callable(labels := getattr(adapter, "_guild_channel_labels", None)):
        source.chat_name, source.chat_topic = labels(channel)
    from gateway.session_identity import clear_identity
    clear_identity(source)
    if runner._canonicalize(source, transport_profile=getattr(adapter, "_owner_profile", None)) is None:
        raise LiveRequestRefused("voice_profile_route_unresolved")
    if getattr(adapter, "_allowed_role_ids", None):
        source.role_authorized = adapter._is_allowed_user(
            binding.owner_id, guild=channel.guild, is_dm=False) is True
    _validate_source(runner, adapter, binding, source)
    return source


def _validate_source(runner, adapter, binding, source):
    from gateway.session_identity import identity_of
    identity = identity_of(source)
    if (identity is None or not identity.multiplexed or identity.adapter() is not adapter
            or identity.runtime_profile != binding.profile
            or getattr(source.platform, "value", None) != "discord"
            or source.guild_id != binding.guild_id or source.chat_id != binding.text_channel_id
            or source.user_id != binding.owner_id or source.thread_id
            or source.profile != binding.profile or source.is_bot
            or getattr(source, "profile_route_rejected", False)
            or runner._is_user_authorized_for_source(source) is not True):
        raise LiveRequestRefused("voice_authorization_or_profile_binding_changed")
    return identity


def inspect_binding(runner, adapter, binding):
    """Read-only identity/auth check; never loads a model, secret, or tool."""
    value = _binding(binding)
    source = _source(runner, adapter, value)
    identity = _validate_source(runner, adapter, value, source)
    from gateway.session_authorities import authority_for_home
    authority = authority_for_home(runner, identity.runtime_home)
    if authority is None or getattr(authority, "retiring", False):
        raise LiveRequestRefused("live_voice_requires_serving_durable_authority")
    return {"profile": identity.runtime_profile, "authorized": True, "typed_approvals_enforced": True}


def prepare_request(runner, adapter, *, binding, context, is_current, allow_gateway_control=False):
    if allow_gateway_control is not False or not callable(is_current):
        raise LiveRequestRefused("voice_cannot_answer_gateway_controls")
    if not isinstance(context, str) or not context.strip() or len(context.encode()) > 16_384:
        raise LiveRequestRefused("invalid_or_oversized_voice_context")
    value = _binding(binding)
    inspect_binding(runner, adapter, binding)
    source = _source(runner, adapter, value)
    from gateway.platforms.event import MessageEvent, MessageType
    prompt = adapter._resolve_channel_prompt(value.text_channel_id, None)
    skills = adapter._resolve_channel_skills(value.text_channel_id, None)
    if prompt is not None and not isinstance(prompt, str):
        raise LiveRequestRefused("invalid_bound_channel_prompt")
    if skills is not None and not isinstance(skills, list):
        raise LiveRequestRefused("invalid_bound_channel_skills")
    event = MessageEvent(
        source=source, text=context, message_type=MessageType.VOICE,
        message_id=PREFIX + uuid.uuid4().hex, channel_prompt=prompt, auto_skill=skills,
        raw_message=SimpleNamespace(guild_id=int(value.guild_id), guild=None),
        internal=False, allow_gateway_control=False,
    )
    handle = LiveVoiceRequest(runner, adapter, value, event, is_current)
    with _lock:
        if len(_requests) >= 128:
            raise LiveRequestRefused("unresolved_voice_request_limit")
        _requests[handle.request_id] = handle
    return handle


def request_for_event(event):
    request_id = getattr(event, "message_id", None)
    if not isinstance(request_id, str) or not request_id.startswith(PREFIX):
        return None
    with _lock:
        handle = _requests.get(request_id)
    if handle is None:
        # Never replay a paid voice turn after restart or loss of its live audience.
        raise LiveRequestRefused("live_voice_request_lease_missing")
    handle.validate(event)
    return handle


def request_for_row(authority, ref, row):
    request_id = row.get("payload", {}).get("native_text_v1", {}).get("event", {}).get("message_id")
    if not isinstance(request_id, str) or not request_id.startswith(PREFIX):
        return None
    with _lock:
        handle = _requests.get(request_id)
    if (handle is None or handle.authority is not authority or handle.ref != ref
            or handle.admission_id != row["admission_id"]):
        raise LiveRequestRefused("live_voice_request_lease_missing")
    handle.check_current()
    return handle


def committed(authority, ref, event, row):
    # Called by the existing ordered writer BEFORE it schedules this admission.
    request_id = getattr(event, "message_id", None)
    if not isinstance(request_id, str) or not request_id.startswith(PREFIX):
        return
    with _lock:
        handle = _requests.get(request_id)
        if handle is None:
            raise LiveRequestRefused("live_voice_request_lease_missing")
        handle.authority, handle.ref, handle.admission_id = authority, ref, row["admission_id"]
        handle.actor = _actor(authority, row)
        # Cancellation can precede this shielded durable commit. The execution
        # gate already refuses it; arrange exact-row cancellation on the loop too.
        if handle.cancelled.is_set():
            handle.loop.call_soon_threadsafe(handle._schedule_cancel)


def _actor(authority, row):
    from gateway.session_contract import Principal
    # Private capability, usable only by this handle with its exact ref/receipt.
    return Principal(row["principal_id"], authority.profile_id,
                     frozenset({"session:submit", "session:control"}), "appliance-live-voice")


def executing_for(source):
    handle = _executing.get()
    if handle is None:
        return False
    b = handle.binding
    return (source.profile, source.guild_id, source.chat_id, source.user_id) == (
        b.profile, b.guild_id, b.text_channel_id, b.owner_id)


class LiveVoiceRequest:
    def __init__(self, runner, adapter, binding, event, is_current):
        self.runner, self.adapter, self.binding, self.event = runner, adapter, binding, event
        self.channel_prompt = event.channel_prompt
        self.auto_skill = tuple(event.auto_skill) if event.auto_skill is not None else None
        self.request_id, self.is_current = event.message_id, is_current
        self.authority = self.ref = self.admission_id = self.actor = None
        self.cancelled = threading.Event()
        self.loop = asyncio.get_running_loop()
        self.task = None
        self.cancel_task = None

    def check_current(self):
        if self.cancelled.is_set() or self.is_current() is not True:
            raise LiveRequestRefused("live_voice_request_revoked")

    def validate(self, event):
        self.check_current()
        if (event.message_id != self.request_id or event.internal
                or event.allow_gateway_control is not False
                or event.channel_prompt != self.channel_prompt
                or (tuple(event.auto_skill) if isinstance(event.auto_skill, list) else event.auto_skill) != self.auto_skill):
            raise LiveRequestRefused("voice_ingress_binding_mutated")
        _validate_source(self.runner, self.adapter, self.binding, event.source)

    def validate_admitted(self, event):
        self.validate(event)
        from gateway.session_authorities import active_authority, authority_for_home
        from gateway.session_identity import identity_of
        expected = authority_for_home(self.runner, identity_of(event.source).runtime_home)
        if expected is None or active_authority(self.runner) is not expected or getattr(expected, "retiring", False):
            raise LiveRequestRefused("live_voice_cannot_fall_back_to_untracked_execution")

    async def run(self):
        self.validate(self.event)
        if self.task is not None:
            raise LiveRequestRefused("live_voice_request_already_started")
        # This is the adapter's ALREADY WIRED handler, including its normal
        # native_callback + profile scope. BaseAdapter's fire-and-forget delivery
        # lifecycle is intentionally not used for a duplex request/result API.
        handler = getattr(self.adapter, "_message_handler", None)
        if not callable(handler):
            raise LiveRequestRefused("profile_scoped_message_handler_missing")
        self.task = asyncio.create_task(handler(self.event))
        self.task.add_done_callback(self._observe_outcome)
        try:
            await asyncio.shield(self.task)
            return await self.result()
        except BaseException:
            self.cancelled.set()
            self._schedule_cancel()
            raise

    @staticmethod
    def _observe_outcome(task):
        if not task.cancelled():
            task.exception()

    async def result(self):
        self.check_current()
        if self.authority is None:
            raise LiveRequestRefused("voice_request_not_durably_admitted")
        receipt = await self.authority.receipt(self.actor, self.ref, self.admission_id)
        if receipt.status != "terminal":
            raise LiveRequestRefused("voice_request_execution_unconfirmed")
        from gateway.session_results import admission_result
        saved = admission_result(self.authority.db, self.admission_id)
        if not isinstance(saved, dict) or receipt.outcome != "completed":
            raise LiveRequestRefused("voice_request_not_completed")
        # Never forward messages, thinking tokens, or the presentation response
        # that may have reasoning/footer settings appended to it.
        text = saved.get("result", {}).get("final_response")
        if not isinstance(text, str) or len(text.encode()) > 12_000:
            raise LiveRequestRefused("voice_result_missing_or_oversized")
        self.check_current()
        with _lock:
            _requests.pop(self.request_id, None)
        return {"text": text, "usage": saved.get("usage", {}), "admission_id": self.admission_id}

    def _schedule_cancel(self):
        if self.cancel_task is None or self.cancel_task.done():
            self.cancel_task = asyncio.create_task(self.cancel())
            self.cancel_task.add_done_callback(self._observe_outcome)

    async def cancel(self):
        self.cancelled.set()  # gate first, before any await or late writer commit
        if self.task is None and self.authority is None:
            with _lock:
                _requests.pop(self.request_id, None)
            return True  # prepared only: no dispatcher or writer ever started
        if self.authority is None:
            return False  # the commit callback will cancel a late admitted row
        receipt = await self.authority.receipt(self.actor, self.ref, self.admission_id)
        if receipt.status == "queued":
            claimed = await self.authority.cancel_or_claimed(self.actor, self.ref, self.admission_id)
            if claimed is None:
                with _lock:
                    _requests.pop(self.request_id, None)
                return True
            generation = claimed["generation"]
        elif receipt.status == "started":
            generation = receipt.execution_generation
        elif receipt.status == "terminal":
            with _lock:
                _requests.pop(self.request_id, None)
            return True
        else:
            return False
        if type(generation) is not int:
            return False
        # CAS generation checking in Hermes refuses if a newer turn took over.
        await self.authority.interrupt(self.actor, self.ref, generation)
        return False  # interruption requested, not proof of rollback/settlement
