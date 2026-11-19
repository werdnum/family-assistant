"""Which conversation is a user's Home.

A Home is keyed on the user's canonical identity, so one person reaches the same
Home however they signed in on a surface. Message rows carry whatever id the
login produced, so every lookup canonicalizes first. Web and voice share a Home:
voice sessions are saved as web conversations.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from family_assistant.services.user_identity import UserIdentityResolver
from family_assistant.threads.listing import Viewer

if TYPE_CHECKING:
    from datetime import datetime

    from family_assistant.config_models import AppConfig
    from family_assistant.storage.database import DatabaseExecutor


def home_interface_type(interface_type: str) -> str:
    """The surface whose Home a turn on ``interface_type`` belongs to."""
    return "web" if interface_type == "voice" else interface_type


def home_owner_id(app_config: AppConfig, user_id: str) -> str:
    """The identity a Home is keyed on for a user id as a login produced it."""
    return UserIdentityResolver(app_config).canonicalize_owner_id(user_id)


def _home_keys(app_config: AppConfig, user_id: str) -> set[str]:
    """Every key the user's Home may be stored under.

    The canonical id, plus each configured alias of it: a Home created before
    an operator mapped a login to this user is stored under that login's own
    id, and must still be found rather than replaced by a second Home.
    """
    resolver = UserIdentityResolver(app_config)
    canonical = resolver.canonicalize_owner_id(user_id)
    return {canonical, *resolver.owner_ids_canonicalizing_to(canonical)}


def viewer_for(app_config: AppConfig, user_id: str) -> Viewer:
    """The viewer a login is, for read markers and unread counts."""
    return Viewer(
        user_id=home_owner_id(app_config, user_id),
        keys=frozenset(_home_keys(app_config, user_id) | {user_id}),
    )


async def find_home(
    db: DatabaseExecutor,
    app_config: AppConfig,
    *,
    user_id: str,
    interface_type: str,
) -> str | None:
    """The user's Home on this surface, if one has been designated."""
    return await db.conversation_threads.find_home_among(
        _home_keys(app_config, user_id), home_interface_type(interface_type)
    )


async def ensure_web_home(
    db: DatabaseExecutor,
    app_config: AppConfig,
    *,
    user_id: str,
    now: datetime,
) -> str:
    """The user's web Home, designating a fresh conversation if they have none."""
    existing = await find_home(db, app_config, user_id=user_id, interface_type="web")
    if existing is not None:
        return existing
    return await db.conversation_threads.designate_home(
        user_id=home_owner_id(app_config, user_id),
        interface_type="web",
        conversation_id=f"web_conv_{uuid.uuid4().hex}",
        now=now,
    )
