"""Boot the shipped application as a workhorse eval sandbox.

The sandbox is the deployment's own configuration pointed at a copy of the
household's database, with only what a test machine cannot or must not reach
replaced:

- **Database**: a snapshot copy. The snapshot leaves out the credential
  tables, so they are created empty and hold only the eval's own API token
  and Google connection.
- **Identity**: real web authentication, with the case's household user
  calling through an API token. Connected-account integrations refuse the
  shared development identity, and the state a case reads is the real
  user's.
- **External services**: MCP servers and Telegram off; Google through the
  eval's transport (see :mod:`family_assistant.eval.workhorse.google_sandbox`).
- **Background work**: summarisation, memory contribution and Auto routing
  off, so nothing spends tokens or changes state that the case did not
  cause. Memory is still read, as in production.
- **Models**: one eval-only tier per configuration under test, a single
  chain entry with no fallback, allowed on the profiles a case runs on.
- **Time**: the process clock is moved to the moment the case was asked.

Authentication is switched on by
:mod:`family_assistant.eval.workhorse.sandbox_auth`, which must run before
any application import.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import time_machine
from pydantic import TypeAdapter

from family_assistant.config_models import (
    ModelTierConfig,
    RetryModelConfig,
    UserIdentityConfig,
)
from family_assistant.eval.workhorse.google_sandbox import seed_google_connection
from family_assistant.security.taint import SourceTrustTier
from family_assistant.storage import api_tokens as api_tokens_storage
from family_assistant.storage.base import metadata
from family_assistant.storage.database import Database

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping
    from datetime import datetime
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.config_models import AppConfig, GoogleIntegrationConfig

__all__ = [
    "CREDENTIAL_TABLES",
    "DEPLOYMENT_OVERLAY_KEYS",
    "deployment_overlay",
    "eval_tier_name",
    "frozen_clock",
    "prepare_sandbox_database",
    "sandbox_config",
]

# Tables the snapshot dump deliberately leaves out: every one holds a
# credential or a device registration.
CREDENTIAL_TABLES = (
    "api_tokens",
    "ios_push_tokens",
    "oauth_clients",
    "pending_oauth_flows",
    "push_subscriptions",
    "user_oauth_connections",
)

_PROFILES_UNDER_TEST = ("default_assistant", "complex_tasks")

# Deployment settings that change how the assistant behaves without reaching
# anything outside the sandbox. The rest of a recorded deployment config --
# plugins, MQTT, push, outbound mail, MCP servers, remote agents -- points at
# the real house and the real world, and is never taken. Model tiers and the
# global tool policy stay as shipped too: both are coupled to the shipped
# profiles, which a dump taken at another revision may not match.
DEPLOYMENT_OVERLAY_KEYS = (
    "taint_policy",
    "memory_config",
    "notes_config",
    "tool_call_review",
)
_TIER_FIELDS = ("high_taint_tier", "default_unspecified_tool_output_tier")
_TIER_KEYED_FIELDS = (
    "operator_minimum",
    "matrix",
    "matrix_overrides",
    "artifact_labels",
)


def _tier_name(value: object) -> object:
    """A tier the config dump wrote as its number, back as its name.

    Config dumps serialise the tier enum by value, but configuration accepts
    names only, since numbers shift when a tier is inserted.
    """
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return SourceTrustTier(int(value)).config_value
    return value


def _taint_policy_by_name(policy: Mapping[str, object]) -> dict[str, object]:
    named = dict(policy)
    for field_name in _TIER_FIELDS:
        if field_name in named:
            named[field_name] = _tier_name(named[field_name])
    for field_name in _TIER_KEYED_FIELDS:
        value = named.get(field_name)
        if isinstance(value, dict):
            named[field_name] = {str(_tier_name(k)): v for k, v in value.items()}
    return named


def deployment_overlay(config: AppConfig, recorded: Mapping[str, object]) -> AppConfig:
    """``config`` with the deployment's behaviour settings from a recorded dump.

    ``recorded`` is the ``config`` object of a ``get_resolved_config`` result.
    Only :data:`DEPLOYMENT_OVERLAY_KEYS` are taken.
    """
    update: dict[str, object] = {}
    for key in DEPLOYMENT_OVERLAY_KEYS:
        value = recorded.get(key)
        if value is None:
            continue
        if key == "taint_policy" and isinstance(value, dict):
            value = _taint_policy_by_name(value)
        # Validated field by field: the rest of the config is left as it is,
        # secrets included, rather than round-tripped through a dump.
        annotation = type(config).model_fields[key].annotation
        update[key] = TypeAdapter(annotation).validate_python(value)
    return config.model_copy(update=update)


def eval_tier_name(configuration: str) -> str:
    return f"eval_{configuration}"


def sandbox_config(
    config: AppConfig,
    *,
    database_url: str,
    port: int,
    work_dir: Path,
    user_id: str,
    google_integration: GoogleIntegrationConfig,
    configurations: Mapping[str, RetryModelConfig],
    profiles: Iterable[str] = _PROFILES_UNDER_TEST,
) -> AppConfig:
    """The deployment's configuration, adjusted into a sandbox."""
    tiers = dict(config.model_tiers)
    for name, model in configurations.items():
        tiers[eval_tier_name(name)] = ModelTierConfig(
            chain=[model], label=eval_tier_name(name)
        )
    under_test = set(profiles)
    service_profiles = []
    for profile in config.service_profiles:
        # Contribution off everywhere means the memory review sweep is never
        # scheduled; reading memory stays as configured.
        update: dict[str, object] = {
            "processing_config": profile.processing_config.model_copy(
                update={"memory_contribute": False}
            )
        }
        if profile.id in under_test:
            update["allowed_model_tiers"] = [
                *(profile.allowed_model_tiers or []),
                *(eval_tier_name(name) for name in configurations),
            ]
        service_profiles.append(profile.model_copy(update=update))
    return config.model_copy(
        update={
            "database_url": database_url,
            "server_url": f"http://127.0.0.1:{port}",
            "server_port": port,
            "telegram_enabled": False,
            "document_storage_path": str(work_dir / "documents"),
            "attachment_storage_path": str(work_dir / "mailbox"),
            "chat_attachment_storage_path": str(work_dir / "chat-attachments"),
            "mcp_config": config.mcp_config.model_copy(update={"mcpServers": {}}),
            "model_tiers": tiers,
            "service_profiles": service_profiles,
            "model_routing": config.model_routing.model_copy(update={"mode": "off"}),
            "conversation_summaries": config.conversation_summaries.model_copy(
                update={"enabled": False}
            ),
            "users": [
                UserIdentityConfig.model_validate({
                    "id": user_id,
                    "oidc": {"emails": [user_id]},
                })
            ],
            "google_integration": google_integration,
        }
    )


async def prepare_sandbox_database(
    engine: AsyncEngine,
    *,
    user_id: str,
    google_integration: GoogleIntegrationConfig,
) -> str:
    """Create the credential tables empty, connect Google, and mint an API token.

    Returns the token the runner calls with, as ``user_id``.
    """
    # Schema DDL before any application code runs, as storage.init_db does.
    # ast-grep-ignore: no-raw-transaction-management
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: metadata.create_all(
                sync,
                tables=[metadata.tables[name] for name in CREDENTIAL_TABLES],
                checkfirst=True,
            )
        )
    db = Database(engine)
    await seed_google_connection(
        db, google_integration, user_id=user_id, account_email=user_id
    )
    token, _, _ = await api_tokens_storage.create_and_store_api_token(
        db_context=db,
        user_identifier=user_id,
        name="workhorse-eval",
        expires_at=None,
    )
    return token


@contextlib.contextmanager
def frozen_clock(moment: datetime) -> Iterator[None]:
    """Move the process clock to ``moment`` and let it tick from there.

    time-machine moves wall-clock reads (``datetime.now``, ``time.time``);
    asyncio's monotonic clock is untouched, so timers and timeouts behave
    normally. Database-side ``now()`` defaults are not moved.
    """
    with time_machine.travel(moment, tick=True):
        yield
