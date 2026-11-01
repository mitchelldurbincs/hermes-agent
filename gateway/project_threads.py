"""Config-only Discord forum workspace resolution and gateway turn binding.

No Discord or gateway runtime imports at import time. The resolver consumes channel
facts; the admission seam recovers those facts again for durable native replays.
"""

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import logging
from pathlib import PurePosixPath
import re

logger = logging.getLogger(__name__)
GUIDANCE = "Post in a forum thread with [project:<slug>] in its title or starter, using a configured project slug."
_TAG = re.compile(r"\[project:([a-zA-Z0-9][a-zA-Z0-9_-]*)\]")


@dataclass(frozen=True)
class ProjectThread:
    forum_id: str
    slug: str = ""
    cwd: str = ""


def project_forums(config):
    """Read the profile/session map without assuming valid YAML shapes."""
    discord = config.get("discord") if isinstance(config, Mapping) else None
    forums = discord.get("project_threads") if isinstance(discord, Mapping) else None
    return forums if isinstance(forums, Mapping) else {}


def _channel_type(channel):
    kind = getattr(channel, "type", None)
    return getattr(kind, "value", kind)


def resolve_project_thread(channel, config, *, starter="", parent=None):
    """Pure resolution: None is unmanaged; an empty cwd is a managed forum refusal.

    A title tag takes precedence over the starter. Multiple different tags in the
    selected field are ambiguous and refuse; reply text never selects a project.
    """
    direct = _channel_type(channel) == 15
    parent = parent if parent is not None else getattr(channel, "parent", None)
    forum = channel if direct else parent
    if _channel_type(forum) != 15:
        return None
    forum_id = str(getattr(forum, "id", ""))
    forums = project_forums(config)
    if forum_id not in forums:
        return None
    refused = ProjectThread(forum_id)
    if direct or _channel_type(channel) not in (10, 11, 12):
        return refused
    title = getattr(channel, "name", "")
    selected = title if isinstance(title, str) and "[project:" in title else starter
    if not isinstance(selected, str):
        return refused
    matches = _TAG.findall(selected)
    slugs = set(matches)
    if len(slugs) != 1 or selected.count("[project:") != len(matches):
        return refused
    slug = slugs.pop()
    projects = forums[forum_id]
    project = projects.get(slug) if isinstance(projects, Mapping) else None
    cwd = project.get("cwd") if isinstance(project, Mapping) else None
    if not isinstance(cwd, str) or "\x00" in cwd or not PurePosixPath(cwd).is_absolute():
        return refused
    return ProjectThread(forum_id, slug, cwd)


async def _resolve_event_channel(runner, event, config):
    """Use native channel facts or the receiving adapter's existing fetch path."""
    import discord

    source = event.source
    channel = getattr(event.raw_message, "channel", None)
    adapter = None
    if channel is None:
        adapter = runner._delivery_adapter_for(source)
        resolve_channel = getattr(adapter, "_resolve_channel", None)
        if not callable(resolve_channel):
            return _unavailable_channel(source, config)
        channel = await resolve_channel(source.thread_id or source.chat_id)
    if channel is None:
        return _unavailable_channel(source, config)
    parent = getattr(channel, "parent", None)
    parent_id = getattr(channel, "parent_id", None)
    if parent is None and parent_id and str(parent_id) in project_forums(config):
        adapter = adapter or runner._delivery_adapter_for(source)
        resolve_channel = getattr(adapter, "_resolve_channel", None)
        if callable(resolve_channel):
            parent = await resolve_channel(parent_id)
        if parent is None:
            return ProjectThread(str(parent_id))
    resolution = resolve_project_thread(channel, config, parent=parent)
    if resolution is None or resolution.cwd or _channel_type(channel) == 15:
        return resolution
    title = getattr(channel, "name", "")
    if isinstance(title, str) and "[project:" in title:
        return resolution
    # A forum starter has the thread's ID. Fetch that exact message, never a reply
    # or the oldest surviving history item (which could silently rebind the cwd).
    starter = event.raw_message
    if getattr(starter, "id", None) != channel.id:
        try:
            starter = await channel.fetch_message(channel.id)
        except discord.HTTPException:
            logger.warning("Project thread starter unavailable: thread=%s", channel.id, exc_info=True)
            return resolution
    return resolve_project_thread(channel, config, parent=parent, starter=getattr(starter, "content", ""))


def _unavailable_channel(source, config):
    # A replay of a known managed route cannot run unbound after a failed fetch.
    forum_id = source.parent_chat_id if source.thread_id else source.chat_id
    return ProjectThread(forum_id) if forum_id in project_forums(config) else None


async def prepare_event(runner, event):
    """After normal authorization, refuse before commands, admission or any LLM."""
    source = event.source
    source._project_thread = None
    if getattr(source.platform, "value", None) != "discord" or source.chat_type == "dm":
        return None
    from gateway.run import _load_gateway_config
    config = _load_gateway_config()
    forums = project_forums(config)
    if not forums:
        return None
    import discord
    try:
        resolution = await _resolve_event_channel(runner, event, config)
    except discord.HTTPException:
        logger.warning("Project thread channel unavailable: chat=%s", source.chat_id, exc_info=True)
        resolution = _unavailable_channel(source, config)
    source._project_thread = resolution
    if resolution is not None and not resolution.cwd:
        logger.warning("Project thread refused: forum=%s thread=%s", resolution.forum_id, source.thread_id)
        return GUIDANCE
    return None


@contextmanager
def turn_workspace(source, task_id):
    """Bind context discovery and local tools; re-register only on workspace change."""
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd
    from tools.terminal_scope import terminal_env
    from tools.terminal_tool import (
        _qualify_task_key, _task_env_overrides, register_task_env_overrides,
        resolve_task_overrides, workspace_key_for,
    )

    project = getattr(source, "_project_thread", None)
    if not task_id:
        yield
        return
    managed = isinstance(project, ProjectThread) and bool(project.cwd)
    # The effective override lookup consults terminal policy for its fallback key.
    # Unmanaged turns only need to drop a binding this hook previously owned.
    current = (resolve_task_overrides(task_id) if managed
               else _task_env_overrides.get(_qualify_task_key(task_id), {}))
    local = managed and terminal_env("TERMINAL_ENV", "local").strip().lower() == "local"
    if not local:
        if current.get("cwd_source") == "project_threads":
            from gateway.session_policy import unbind_turn_workspace
            from tools.terminal_tool import clear_session_cwd
            unbind_turn_workspace(task_id)
            clear_session_cwd(task_id)
        yield
        return
    if current.get("cwd") != project.cwd or current.get("cwd_source") != "project_threads":
        register_task_env_overrides(task_id, {
            **current, "cwd": project.cwd, "cwd_source": "project_threads",
            "workspace_key": workspace_key_for(project.cwd),
        })
    token = set_session_cwd(project.cwd)
    try:
        yield
    finally:
        reset_session_cwd(token)
