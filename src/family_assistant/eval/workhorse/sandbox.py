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

The auth environment is read once, when ``family_assistant.web.auth`` is
first imported, so :func:`apply_sandbox_auth_environment` must run before any
application import; :func:`require_sandbox_auth` fails a boot that missed it.
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

import time_machine

from family_assistant.config_models import (
    ModelTierConfig,
    RetryModelConfig,
    UserIdentityConfig,
)
from family_assistant.eval.workhorse.google_sandbox import seed_google_connection
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
    "apply_sandbox_auth_environment",
    "eval_tier_name",
    "frozen_clock",
    "prepare_sandbox_database",
    "require_sandbox_auth",
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

_AUTH_ENVIRONMENT = {
    "OIDC_CLIENT_ID": "workhorse-eval",
    "OIDC_CLIENT_SECRET": "workhorse-eval-secret",
    # Discovery is fetched only for an interactive login, which the sandbox
    # never performs; requests authenticate with an API token instead.
    "OIDC_DISCOVERY_URL": "http://127.0.0.1:9/.well-known/openid-configuration",
    "SESSION_SECRET_KEY": "workhorse-eval-session-secret-0123456789",
}

_PROFILES_UNDER_TEST = ("default_assistant", "complex_tasks")


def apply_sandbox_auth_environment() -> None:
    """Turn on web authentication; call before importing the application."""
    for name, value in _AUTH_ENVIRONMENT.items():
        os.environ.setdefault(name, value)


def require_sandbox_auth() -> None:
    """Fail unless web authentication came up enabled."""
    # Imported here, not at module level: the module reads the auth
    # environment on import, which must come after apply_sandbox_auth_environment.
    from family_assistant.web import auth  # noqa: PLC0415

    if not auth.AUTH_ENABLED:
        raise RuntimeError(
            "The workhorse sandbox needs web authentication, which is read when "
            "family_assistant.web.auth is first imported. Call "
            "apply_sandbox_auth_environment() before importing the application."
        )


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
