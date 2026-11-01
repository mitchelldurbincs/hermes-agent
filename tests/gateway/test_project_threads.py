"""Forum tags bind real local tools and refuse unresolved projects before model work."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.project_threads import GUIDANCE, ProjectThread, prepare_event, resolve_project_thread
from gateway.session import SessionSource, build_session_key


def _channel(title="[project:xunning] work", *, forum_type=15, forum_id=100, thread_id=200):
    parent = SimpleNamespace(id=forum_id, type=SimpleNamespace(value=forum_type))
    return SimpleNamespace(id=thread_id, type=SimpleNamespace(value=11), parent=parent,
                           parent_id=forum_id, name=title, fetch_message=AsyncMock())


def _config(cwd="/projects/xunning"):
    return {"discord": {"project_threads": {"100": {"xunning": {"cwd": cwd}}}}}


def _event(channel, *, raw=True):
    thread = channel.type.value == 11
    source = SessionSource(platform=Platform.DISCORD, chat_id=str(channel.id),
                           chat_type="thread" if thread else "group", user_id="42",
                           thread_id=str(channel.id) if thread else None,
                           parent_chat_id=str(channel.parent_id) if thread else None)
    message = SimpleNamespace(channel=channel, id=201, content="[project:other] reply") if raw else None
    return MessageEvent(source=source, text="pwd", raw_message=message)


@pytest.mark.parametrize("title,starter", [
    ("[project:xunning] work", ""), ("work", "Please use [project:xunning]"),
    ("[project:xunning]", "[project:other]"),
])
def test_resolve_title_or_starter(title, starter):
    assert resolve_project_thread(_channel(title), _config(), starter=starter) == ProjectThread(
        "100", "xunning", "/projects/xunning")


@pytest.mark.parametrize("title", ["work", "[project:missing]", "[project:]", "[project:../xunning]",
                                   "[project:xunning", "[project:xunning] [project:other]", "[project:xunning] [project:]"])
def test_resolve_unbound_or_malformed_tag_refuses(title):
    assert resolve_project_thread(_channel(title), _config()) == ProjectThread("100")


@pytest.mark.parametrize("config", [None, [], {"discord": []}, {"discord": {"project_threads": []}}])
def test_resolve_malformed_outer_config_is_unmanaged(config):
    assert resolve_project_thread(_channel(), config) is None


@pytest.mark.parametrize("project", [None, [], {}, {"cwd": 42}, {"cwd": "relative"}, {"cwd": ""}])
def test_resolve_malformed_project_refuses(project):
    config = {"discord": {"project_threads": {"100": {"xunning": project}}}}
    assert resolve_project_thread(_channel(), config) == ProjectThread("100")


@pytest.mark.parametrize("channel", [None, _channel(forum_type=0), _channel(forum_id=101),
                                     SimpleNamespace(id=200, type=SimpleNamespace(value=1))])
def test_resolve_non_forum_and_unconfigured_forum_are_unmanaged(channel):
    assert resolve_project_thread(channel, _config()) is None


def test_resolve_forum_parent_itself_refuses():
    assert resolve_project_thread(_channel().parent, _config()) == ProjectThread("100")


@pytest.fixture
def configured_home(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    workspace = tmp_path / "xunning"
    workspace.mkdir()
    (workspace / "marker.txt").write_text("project file\n")
    (workspace / "AGENTS.md").write_text("Project instructions live here.\n")
    (workspace / "child").mkdir()
    (home / "config.yaml").write_text(json.dumps(_config(str(workspace))))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    return workspace


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", [_channel("work"), _channel("[project:missing]"), _channel().parent])
async def test_handle_message_refuses_before_any_model_or_command(configured_home, channel, caplog):
    from gateway.run import GatewayRunner
    event = _event(channel)
    runner = object.__new__(GatewayRunner)
    runner._hm_admit_event = AsyncMock(return_value=(event, event.source, False))
    runner._run_agent = AsyncMock()
    runner._hm_authority_admission = AsyncMock(return_value=(True, "unexpected admission"))
    runner._hm_dispatch_idle_commands = AsyncMock()
    assert await runner._handle_message(event) == GUIDANCE
    runner._run_agent.assert_not_called()
    runner._hm_authority_admission.assert_not_called()
    runner._hm_dispatch_idle_commands.assert_not_called()
    assert "Project thread refused" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [True, False])
async def test_starter_resolution_including_replay(configured_home, raw):
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.config import PlatformConfig
    channel = _channel("work")
    channel.fetch_message.return_value = SimpleNamespace(content="[project:xunning]")
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._client = SimpleNamespace(get_channel=lambda channel_id: channel)
    runner = SimpleNamespace(_delivery_adapter_for=lambda source: adapter)
    event = _event(channel, raw=raw)
    assert await prepare_event(runner, event) is None
    assert event.source._project_thread.cwd == str(configured_home)
    channel.fetch_message.assert_awaited_once_with(200)


@pytest.mark.asyncio
async def test_non_forum_thread_never_refused_even_if_parent_id_is_configured(configured_home):
    event = _event(_channel(forum_type=0))
    assert await prepare_event(SimpleNamespace(), event) is None
    assert event.source._project_thread is None


@pytest.mark.asyncio
async def test_local_turn_binds_context_pwd_relative_read_and_preserves_cd(configured_home, monkeypatch):
    from agent.runtime_cwd import resolve_context_cwd, scoped_session_cwd
    from agent.prompt_builder import build_context_files_prompt
    from gateway.run_turn_runner import TurnRunner
    from gateway.session_context import clear_session_vars, set_session_vars
    from gateway.turn_context import TurnContext
    from tools import terminal_tool
    from tools.file_tools import read_file_tool
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope

    event = _event(_channel())
    assert await prepare_event(SimpleNamespace(), event) is None
    class ToolAgent:
        def __init__(self, **kwargs):
            self.model, self.session_id = kwargs["model"], kwargs["session_id"]
            self.tools = []
            self.context_compressor = SimpleNamespace(last_prompt_tokens=0, context_length=200_000)
            self.session_prompt_tokens = self.session_completion_tokens = 0

        def run_conversation(self, _message, **_kwargs):
            return {**model_turn(_kwargs["task_id"]), "messages": []}

    ctx = TurnContext(
        source=event.source, session_id="project-turn", session_key=build_session_key(event.source),
        user_config=_config(str(configured_home)), message="inspect the workspace", history=[],
        AIAgent=ToolAgent, resolve_display_setting=lambda *_args: False,
        _run_still_current=lambda: True, _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )
    runner = TurnRunner(_gateway_runner(), ctx)
    registered = Mock(wraps=terminal_tool.register_task_env_overrides)
    monkeypatch.setattr(terminal_tool, "register_task_env_overrides", registered)
    scope = set_terminal_scope({"TERMINAL_ENV": "local", "TERMINAL_CWD": str(configured_home.parent)})
    tokens = set_session_vars(session_key=build_session_key(event.source), session_id=ctx.session_id)
    observations = []

    def model_turn(task_id):
        assert "Project instructions live here." in build_context_files_prompt(str(resolve_context_cwd()))
        observations.append((resolve_context_cwd(), json.loads(terminal_tool.terminal_tool(
            "pwd", task_id=task_id)), json.loads(read_file_tool("marker.txt", task_id=task_id))))
        return {"final_response": "done"}

    try:
        assert runner.run_sync()["final_response"] == "done"
        cwd, pwd, contents = observations[0]
        assert cwd == configured_home
        assert pwd["output"].strip() == str(configured_home)
        assert "project file" in contents["content"]
        changed = json.loads(terminal_tool.terminal_tool("cd child", task_id=ctx.session_id))
        assert changed["exit_code"] == 0
        runner.run_sync()
        assert observations[1][1]["output"].strip() == str(configured_home / "child")
        registered.assert_called_once()
        assert scoped_session_cwd() == ""
    finally:
        terminal_tool.cleanup_all_environments()
        terminal_tool.clear_task_env_overrides(ctx.session_id)
        clear_session_vars(tokens)
        reset_terminal_scope(scope)


def test_project_binding_does_not_override_docker_policy(configured_home):
    from gateway.project_threads import turn_workspace
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope
    from tools.terminal_tool import resolve_task_overrides
    source = SimpleNamespace(_project_thread=ProjectThread("100", "xunning", str(configured_home)))
    token = set_terminal_scope({"TERMINAL_ENV": "docker"})
    try:
        with turn_workspace(source, "docker-turn"):
            assert resolve_task_overrides("docker-turn") == {}
    finally:
        reset_terminal_scope(token)


def test_local_binding_is_removed_on_backend_change(configured_home):
    from gateway.project_threads import turn_workspace
    from gateway.session_context import scoped_session_key
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope
    from tools.terminal_tool import clear_task_env_overrides, get_session_cwd, record_session_cwd, resolve_task_overrides
    source = SimpleNamespace(_project_thread=ProjectThread("100", "xunning", str(configured_home)))
    local_token = set_terminal_scope({"TERMINAL_ENV": "local"})
    try:
        with scoped_session_key("backend-key"), turn_workspace(source, "backend-change"):
            assert resolve_task_overrides("backend-change")["cwd"] == str(configured_home)
            record_session_cwd("backend-change", str(configured_home / "child"))
            record_session_cwd("backend-key", str(configured_home / "child"))
        docker_token = set_terminal_scope({"TERMINAL_ENV": "docker"})
        try:
            with scoped_session_key("backend-key"), turn_workspace(source, "backend-change"):
                assert resolve_task_overrides("backend-change") == {}
                assert get_session_cwd("backend-change") is None
                assert get_session_cwd("backend-key") is None
        finally:
            reset_terminal_scope(docker_token)
    finally:
        reset_terminal_scope(local_token)
        clear_task_env_overrides("backend-change")


@pytest.mark.asyncio
async def test_replay_without_channel_access_refuses_known_forum(configured_home):
    event = _event(_channel(), raw=False)
    runner = SimpleNamespace(_delivery_adapter_for=lambda source: None)
    assert await prepare_event(runner, event) == GUIDANCE


@pytest.mark.asyncio
async def test_starter_permission_failure_refuses_before_model(configured_home):
    import discord
    channel = _channel("work")
    channel.fetch_message.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
    assert await prepare_event(SimpleNamespace(), _event(channel)) == GUIDANCE


def test_workspace_change_rebinds_only_own_thread(configured_home, tmp_path):
    from gateway.project_threads import turn_workspace
    from gateway.session_context import scoped_session_key
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope
    from tools.terminal_tool import clear_task_env_overrides, terminal_tool, cleanup_all_environments
    first, second = _event(_channel()), _event(_channel(thread_id=300))
    first.source._project_thread = ProjectThread("100", "xunning", str(configured_home))
    second.source._project_thread = ProjectThread("100", "second", str(tmp_path))
    first_key, second_key = build_session_key(first.source), build_session_key(second.source)
    assert first_key != second_key
    token = set_terminal_scope({"TERMINAL_ENV": "local"})
    try:
        with scoped_session_key("first-session-key"), turn_workspace(first.source, first_key):
            assert json.loads(terminal_tool("pwd", task_id=first_key))["output"].strip() == str(configured_home)
        with scoped_session_key("second-session-key"), turn_workspace(second.source, second_key):
            assert json.loads(terminal_tool("pwd", task_id=second_key))["output"].strip() == str(tmp_path)
        first.source._project_thread = ProjectThread("100", "xunning", str(configured_home / "child"))
        with scoped_session_key("first-session-key"), turn_workspace(first.source, first_key):
            assert json.loads(terminal_tool("pwd", task_id=first_key))["output"].strip() == str(configured_home / "child")
        with scoped_session_key("second-session-key"), turn_workspace(second.source, second_key):
            assert json.loads(terminal_tool("pwd", task_id=second_key))["output"].strip() == str(tmp_path)
    finally:
        cleanup_all_environments()
        clear_task_env_overrides(first_key)
        clear_task_env_overrides(second_key)
        reset_terminal_scope(token)


def _gateway_runner():
    """Stub provider/delivery services; the complete TurnRunner and tools run unchanged."""
    runner = MagicMock()
    runner.config = SimpleNamespace(streaming=None)
    runner._provider_routing = {}
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._session_db = None
    runner._prefill_messages = None
    runner._pending_model_notes = {}
    runner._pending_skills_reload_notes = {}
    runner.session_store._entries = {}
    runner._get_system_prompt_for_channel.return_value = None
    runner._resolve_session_agent_runtime.return_value = ("test-model", {})
    runner._resolve_session_reasoning_config.return_value = None
    runner._resolve_session_service_tier.return_value = None
    runner._resolve_turn_agent_config.return_value = {"model": "test-model", "runtime": {}}
    runner._agent_config_signature.return_value = ("test-signature",)
    runner._extract_cache_busting_config.return_value = {}
    runner._refresh_fallback_model.return_value = None
    runner._consume_pending_native_image_paths.return_value = []
    runner._consume_pending_turn_sidecar_notes.return_value = []
    runner._is_telegram_topic_lane.return_value = False
    runner._is_discord_auto_thread_lane.return_value = False
    runner._is_relay_discord_channel_lane.return_value = False
    return runner


def test_unmanaged_turn_does_not_read_terminal_policy():
    from gateway.project_threads import turn_workspace
    from tools.terminal_scope import TerminalPolicyRefusal, reset_terminal_scope, set_terminal_scope
    token = set_terminal_scope(TerminalPolicyRefusal("unrelated profile policy"))
    try:
        with turn_workspace(SimpleNamespace(), "unmanaged"):
            pass
    finally:
        reset_terminal_scope(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("title,expected", [("[project:xunning]", None), ("[project:missing]", GUIDANCE)])
async def test_integer_yaml_forum_key_binds_and_refuses(configured_home, title, expected):
    import hermes_yaml as yaml

    config = {"discord": {"project_threads": {100: {"xunning": {"cwd": str(configured_home)}}}}}
    (configured_home.parent / "profile" / "config.yaml").write_text(yaml.safe_dump(config))
    event = _event(_channel(title))
    assert await prepare_event(SimpleNamespace(), event) == expected
    assert event.source._project_thread.forum_id == "100"


@pytest.fixture
def local_project_tools(configured_home):
    from tools import terminal_tool
    from tools.file_tools import clear_file_ops_cache
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope

    token = set_terminal_scope({"TERMINAL_ENV": "local", "TERMINAL_CWD": str(configured_home.parent)})
    try:
        yield
    finally:
        terminal_tool.cleanup_all_environments()
        for task_id in ("followup-turn", "background-turn"):
            terminal_tool.clear_task_env_overrides(task_id)
            clear_file_ops_cache(task_id)
        reset_terminal_scope(token)


def _assert_project_tools(task_id, workspace):
    from agent.runtime_cwd import resolve_context_cwd
    from tools.file_tools import read_file_tool
    from tools.terminal_tool import terminal_tool

    assert resolve_context_cwd() == workspace
    assert json.loads(terminal_tool("pwd", task_id=task_id))["output"].strip() == str(workspace)
    contents = json.loads(read_file_tool("marker.txt", task_id=task_id))
    assert "project file" in contents.get("content", ""), contents


def _followup_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._run_agent_deliver_first_response = AsyncMock()
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="pwd")
    runner._persist_prompt_pins = AsyncMock()
    runner._pinned_channel_inputs = lambda key, prompt, source, **kw: (prompt, source)
    runner._session_key_for_source = build_session_key
    runner._is_goal_continuation_event = lambda event: False
    runner._delivery_adapter_for = lambda source: None
    runner._intake_adapter_for = lambda source: None
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted,initial_commands", [(False, ["pwd"]), (True, [])])
async def test_drained_followup_rechecks_fresh_source_and_binds(
    local_project_tools, configured_home, interrupted, initial_commands,
):
    from gateway.project_threads import turn_workspace
    from gateway.session_context import scoped_session_key
    from gateway.turn_context import TurnContext
    from tools.terminal_tool import terminal_tool

    first, pending = _event(_channel()), _event(_channel())
    runner = _followup_runner()
    assert await prepare_event(runner, first) is None
    key = build_session_key(first.source)
    ctx = TurnContext(source=first.source, session_id="followup-turn", session_key=key, history=[])

    async def run_followup(**kwargs):
        with turn_workspace(kwargs["source"], kwargs["session_id"]):
            _assert_project_tools(kwargs["session_id"], configured_home)
        return {"final_response": "follow-up complete", "messages": []}

    runner._run_agent = AsyncMock(side_effect=run_followup)
    with scoped_session_key(key):
        with turn_workspace(first.source, ctx.session_id):
            for command in initial_commands:
                assert json.loads(terminal_tool(command, task_id=ctx.session_id))["output"].strip() == str(configured_home)
        result = await runner._run_agent_queued_followup(
            ctx, None, pending.text, pending, {}, {"messages": [], "interrupted": interrupted}, None)
    assert result["final_response"] == "follow-up complete"
    runner._run_agent.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_drained_followup_refuses_changed_tag_before_model(local_project_tools, configured_home, interrupted):
    from gateway.project_threads import turn_workspace
    from gateway.session_context import scoped_session_key
    from gateway.turn_context import TurnContext

    first, pending = _event(_channel()), _event(_channel("[project:missing]"))
    pending.message_id = "refused-followup"
    runner = _followup_runner()
    runner._run_agent = AsyncMock()
    assert await prepare_event(runner, first) is None
    key = build_session_key(first.source)
    ctx = TurnContext(source=first.source, session_id="followup-turn", session_key=key, history=[])
    with scoped_session_key(key):
        with turn_workspace(first.source, ctx.session_id):
            _assert_project_tools(ctx.session_id, configured_home)
        result = await runner._run_agent_queued_followup(
            ctx, None, pending.text, pending, {},
            {"messages": [], "interrupted": interrupted, "already_sent": True}, None)
    assert result["final_response"] == GUIDANCE
    assert not result.get("already_sent")
    assert result["queued_terminal_inbound_id"] == pending.message_id
    runner._run_agent.assert_not_called()
    runner._prepare_profile_scoped_inbound_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_source_without_project_metadata_keeps_registered_binding(local_project_tools, configured_home):
    from gateway.project_threads import turn_workspace
    from gateway.session_context import scoped_session_key

    event = _event(_channel())
    assert await prepare_event(SimpleNamespace(), event) is None
    with scoped_session_key(build_session_key(event.source)):
        with turn_workspace(event.source, "followup-turn"):
            pass
        with turn_workspace(_event(_channel()).source, "followup-turn"):
            _assert_project_tools("followup-turn", configured_home)


@pytest.mark.asyncio
async def test_background_agent_binds_project_before_construction_and_tools(
    local_project_tools, configured_home, monkeypatch,
):
    from agent.runtime_cwd import resolve_context_cwd
    from gateway.run import GatewayRunner
    from gateway.session_context import scoped_session_key
    from tools.terminal_tool import get_session_cwd, record_session_cwd, resolve_task_overrides
    import sys

    event = _event(_channel())
    assert await prepare_event(SimpleNamespace(), event) is None
    runner = _gateway_runner()
    adapter = AsyncMock()
    adapter.extract_media = lambda response: ([], response)
    adapter.extract_images = lambda response: ([], response)
    runner._delivery_adapter_for.return_value = adapter
    runner._resolve_session_agent_runtime.return_value = ("test-model", {"api_key": "fake"})
    runner._resolve_turn_toolsets.return_value = ([], None)
    runner._get_executor.return_value = None
    constructed = []

    class BackgroundAgent:
        def __init__(self, **kwargs):
            constructed.append(resolve_context_cwd())

        def run_conversation(self, *, user_message, task_id):
            _assert_project_tools(task_id, configured_home)
            return {"final_response": "background complete", "messages": []}

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=BackgroundAgent))
    runner._run_in_executor_with_context = GatewayRunner._run_in_executor_with_context.__get__(runner)
    key = build_session_key(event.source)
    record_session_cwd(key, str(configured_home / "child"))
    with scoped_session_key(key):
        await GatewayRunner._run_background_task_inner(runner, "fix the failing test", event.source, "background-turn")
    assert constructed == [configured_home]
    assert "background complete" in adapter.send.call_args.kwargs["content"]
    assert get_session_cwd(key) == str(configured_home / "child")
    assert resolve_task_overrides("background-turn") == {}
