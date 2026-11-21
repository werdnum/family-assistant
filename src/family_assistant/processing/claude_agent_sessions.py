"""Claude Code sessions kept in the database, and when a wake resumes one.

The Agent SDK mirrors each session's transcript to ``DatabaseSessionStore`` and
reads it back from there to resume. A turn whose whole transcript reached the
store is recorded against its session; a delegation wake resumes that session
only while it still agrees with the conversation's history, and a turn a
graceful shutdown interrupted resumes its own. See
docs/design/claude-agent-session-resume.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from claude_agent_sdk import SessionStore, project_key_for_directory

if TYPE_CHECKING:
    from claude_agent_sdk import SessionKey, SessionStoreEntry

    from family_assistant.llm.model_tiers import ClaudeAgentModel
    from family_assistant.security.taint import TaintMetadata
    from family_assistant.storage.database import Database
    from family_assistant.storage.repositories.claude_agent_sessions import (
        ClaudeAgentSessionTurn,
    )

# A session nothing has been added to for this long is deleted; a wake that
# arrives later rebuilds its turn from history.
SESSION_RETENTION = timedelta(days=7)

type WakeDecision = Literal[
    "resumed", "no_session", "conversation_moved_on", "setup_changed"
]
type SuspendedResumeDecision = Literal["resumed", "no_session", "setup_changed"]


def session_working_directory() -> str:
    """The empty directory every session runs in.

    Fixed rather than fresh per turn: the SDK derives a session's store key
    from it, and a resumed session is looked up under the key it was stored
    with.
    """
    path = Path(tempfile.gettempdir()) / "family-assistant-claude-agent"
    path.mkdir(exist_ok=True)
    return str(path)


def remove_local_transcript(session_id: str, cwd: str) -> None:
    """Delete the CLI's own on-disk copy of a session the store now holds."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    projects = (Path(config_dir) if config_dir else Path.home() / ".claude") / (
        "projects"
    )
    project = projects / project_key_for_directory(cwd)
    (project / f"{session_id}.jsonl").unlink(missing_ok=True)
    shutil.rmtree(project / session_id, ignore_errors=True)


def system_prompt_digest(system_prompt: str) -> str:
    return hashlib.sha256(system_prompt.encode()).hexdigest()


# The optional methods stay unimplemented; see database_session_store.
class DatabaseSessionStore(SessionStore):  # pylint: disable=abstract-method
    """The Agent SDK's session store, over one session's database handle.

    Entries are kept as the JSON text ``json.dumps`` produces, which is ASCII
    and round-trips exactly on every backend. ``write_failed`` is set by any
    append that raised, including one the SDK retries afterwards: a batch that
    committed and then reported failure is appended again in full, and the
    entries in it without a ``uuid`` cannot be told from their repeats.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self.write_failed = False

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        if not entries:
            return
        try:
            await self._db.claude_agent_sessions.append_entries(
                project_key=key["project_key"],
                session_id=key["session_id"],
                subpath=key.get("subpath", ""),
                entries=[json.dumps(entry) for entry in entries],
            )
        except BaseException:
            self.write_failed = True
            raise

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        entries = await self._db.claude_agent_sessions.load_entries(
            project_key=key["project_key"],
            session_id=key["session_id"],
            subpath=key.get("subpath", ""),
        )
        return [json.loads(entry) for entry in entries] if entries else None


def database_session_store(db: Database) -> DatabaseSessionStore:
    """A store for one session's transcript."""
    # The protocol's optional methods raise NotImplementedError as the SDK's
    # documented "absent" marker, and the SDK checks for an override before
    # calling one, so they are deliberately left unimplemented.
    return DatabaseSessionStore(db)  # pyright: ignore[reportAbstractUsage]


@dataclass(frozen=True)
class WakeScope:
    """Where a wake runs, and on what."""

    interface_type: str
    conversation_id: str
    subconversation_id: str | None
    profile_id: str
    turn_id: str


async def session_to_resume(
    db: Database,
    scope: WakeScope,
    *,
    model: ClaudeAgentModel,
    system_prompt: str,
) -> tuple[ClaudeAgentSessionTurn | None, WakeDecision]:
    """The stored session a wake should resume, or None and why not.

    The latest session recorded in the conversation is resumed when the
    newest row before the wake's own is from that session's last turn, and
    the session ran on the same model, effort and system prompt.
    """
    session = await db.claude_agent_sessions.latest_turn(
        interface_type=scope.interface_type,
        conversation_id=scope.conversation_id,
        subconversation_id=scope.subconversation_id,
        processing_profile_id=scope.profile_id,
    )
    if session is None:
        return None, "no_session"
    latest_turn_id = await db.message_history.latest_turn_id(
        interface_type=scope.interface_type,
        conversation_id=scope.conversation_id,
        subconversation_id=scope.subconversation_id,
        processing_profile_id=scope.profile_id,
        excluding_turn_id=scope.turn_id,
    )
    if latest_turn_id != session.turn_id:
        return None, "conversation_moved_on"
    if (
        session.model != model.model
        or session.effort != model.effort
        or session.system_prompt_sha256 != system_prompt_digest(system_prompt)
    ):
        return None, "setup_changed"
    return session, "resumed"


async def suspended_session_to_resume(
    db: Database,
    scope: WakeScope,
    *,
    model: ClaudeAgentModel,
    system_prompt: str,
) -> tuple[ClaudeAgentSessionTurn | None, SuspendedResumeDecision]:
    """The session a turn resumed after a shutdown should continue, if any.

    A turn only has a session recorded before it finishes when a graceful
    shutdown interrupted that session and recorded it for this. The resume
    handler has already established that nothing has happened in the
    conversation since, so only the setup is checked.

    The record is used up by this attempt: the resumed session writes a new
    one if it stops cleanly again, and if it is lost instead, the next attempt
    rebuilds from the rows saved since rather than going back to a checkpoint
    that predates them.
    """
    session = await db.claude_agent_sessions.take_turn(scope.turn_id)
    if session is None:
        return None, "no_session"
    if (
        session.model != model.model
        or session.effort != model.effort
        or session.system_prompt_sha256 != system_prompt_digest(system_prompt)
    ):
        return None, "setup_changed"
    return session, "resumed"


async def record_session_turn(
    db: Database,
    scope: WakeScope,
    *,
    session_id: str,
    model: ClaudeAgentModel,
    system_prompt: str,
    taint_metadata: TaintMetadata | None,
) -> None:
    """Record a turn whose whole transcript reached the store."""
    await db.claude_agent_sessions.record_turn(
        session_id=session_id,
        turn_id=scope.turn_id,
        interface_type=scope.interface_type,
        conversation_id=scope.conversation_id,
        subconversation_id=scope.subconversation_id,
        processing_profile_id=scope.profile_id,
        model=model.model,
        effort=model.effort,
        system_prompt_sha256=system_prompt_digest(system_prompt),
        taint_metadata=taint_metadata,
    )
    await db.claude_agent_sessions.delete_sessions_idle_since(
        datetime.now(UTC) - SESSION_RETENTION
    )
