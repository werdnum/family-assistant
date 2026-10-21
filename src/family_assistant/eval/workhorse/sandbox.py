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
- **External services**: the deployment's MCP servers are kept only where the
  sandbox runs a local instance (Tuit, code execution, ha-mcp), pointed at
  it; the Home Assistant plugin points at the sandbox's own ``hass``; Google
  goes through the eval's transport (see
  :mod:`family_assistant.eval.workhorse.google_sandbox`). Telegram, MQTT,
  push, outbound mail, Keychute, browser handoff, UCP and the remaining
  plugins are off.
- **Sealed delegations**: profiles that act outside the sandbox -- remote
  agents, the managed coder, computer use, the browser profiles and deep
  research -- are removed, and every remaining profile carries an operator
  rule denying browser, worker, GitHub, authenticated-site and shopping-cart
  tools. The rule sits in the operator layer, which outranks every profile
  grant, so a profile cannot re-open what the sandbox seals.
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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import time_machine
from pydantic import SecretStr

from family_assistant.config_models import (
    ApnsConfig,
    MCPServerConfig,
    ModelTierConfig,
    MQTTConfig,
    PWAConfig,
    RetryModelConfig,
    UserIdentityConfig,
)
from family_assistant.eval.workhorse.google_sandbox import seed_google_connection
from family_assistant.plugins.config import PluginsConfig
from family_assistant.plugins.home_assistant.config import HomeAssistantConfig
from family_assistant.storage import api_tokens as api_tokens_storage
from family_assistant.storage.base import metadata
from family_assistant.storage.database import Database
from family_assistant.tools.metadata import ToolTag
from family_assistant.tools.policy import (
    MAX_POLICY_RULE_PRIORITY,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping
    from datetime import datetime
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.config_models import AppConfig, GoogleIntegrationConfig

__all__ = [
    "CREDENTIAL_TABLES",
    "SEAL_RULE",
    "LocalServices",
    "eval_tier_name",
    "frozen_clock",
    "prepare_sandbox_database",
    "sandbox_config",
    "sealed_profile_ids",
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

# Tools that act on the world outside the sandbox. Denied in the operator
# layer of every profile, which outranks every grant a profile makes.
SEAL_RULE = PolicyRule(
    match=ToolMatcher(
        tags_any=[ToolTag.BROWSER, ToolTag.WORKER],
        names=[
            "run_authenticated_site_task",
            "create_github_issue",
            "ucp_add_to_cart",
            "ucp_get_cart",
            "ucp_transfer_checkout_to_human",
        ],
    ),
    decision=ToolPolicyDecision.DENY,
    priority=MAX_POLICY_RULE_PRIORITY,
    description="The workhorse eval sandbox does not reach the world outside it.",
)


def sealed_profile_ids(config: AppConfig) -> list[str]:
    """Profiles whose work happens outside the sandbox, so are removed from it."""
    sealed = []
    for profile in config.service_profiles:
        processing = profile.processing_config
        model = processing.llm_model or ""
        if (
            profile.remote_a2a is not None
            or processing.antigravity_config is not None
            or processing.enable_computer_use
            or "browser" in profile.id
            or "deep-research" in model
        ):
            sealed.append(profile.id)
    return sealed


@dataclass(frozen=True)
class LocalServices:
    """What the sandbox runs locally in place of the deployment's services."""

    # MCP server name -> fields overriding the deployment's entry (url, token).
    mcp_servers: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    # The Home Assistant plugin's instance, as (api_url, token).
    home_assistant: tuple[str, str] | None = None


def _sealed_policy(existing: ToolPolicyConfig | None) -> ToolPolicyConfig:
    rules = [*(existing.rules if existing else []), SEAL_RULE]
    return ToolPolicyConfig(
        rules=rules, default_decision=existing.default_decision if existing else None
    )


def _local_mcp_servers(
    config: AppConfig, services: LocalServices
) -> dict[str, MCPServerConfig]:
    deployed = config.mcp_config.mcpServers
    return {
        name: (
            deployed[name].model_copy(update=dict(fields))
            if name in deployed
            else MCPServerConfig.model_validate(dict(fields))
        )
        for name, fields in services.mcp_servers.items()
    }


def _local_plugins(config: AppConfig, services: LocalServices) -> PluginsConfig:
    if services.home_assistant is None:
        return PluginsConfig()
    api_url, token = services.home_assistant
    deployed = config.plugins.home_assistant.get("default")
    instance = (
        deployed.model_copy(update={"api_url": api_url, "token": SecretStr(token)})
        if deployed is not None
        else HomeAssistantConfig(api_url=api_url, token=SecretStr(token))
    )
    return PluginsConfig(home_assistant={"default": instance})


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
    services: LocalServices,
    profiles: Iterable[str] = _PROFILES_UNDER_TEST,
) -> AppConfig:
    """The deployment's configuration, adjusted into a sandbox."""
    tiers = dict(config.model_tiers)
    for name, model in configurations.items():
        tiers[eval_tier_name(name)] = ModelTierConfig(
            chain=[model], label=eval_tier_name(name)
        )
    under_test = set(profiles)
    sealed = set(sealed_profile_ids(config))
    service_profiles = []
    for profile in config.service_profiles:
        if profile.id in sealed:
            continue
        # Contribution off everywhere means the memory review sweep is never
        # scheduled; reading memory stays as configured.
        update: dict[str, object] = {
            "processing_config": profile.processing_config.model_copy(
                update={"memory_contribute": False}
            ),
            "operator_tools_policy": _sealed_policy(profile.operator_tools_policy),
        }
        if profile.id in under_test:
            update["allowed_model_tiers"] = [
                *(profile.allowed_model_tiers or []),
                *(eval_tier_name(name) for name in configurations),
            ]
        service_profiles.append(profile.model_copy(update=update))
    users = (
        config.users
        if any(user.id == user_id for user in config.users)
        else [
            UserIdentityConfig.model_validate({
                "id": user_id,
                "oidc": {"emails": [user_id]},
            })
        ]
    )
    return config.model_copy(
        update={
            "database_url": database_url,
            "server_url": f"http://127.0.0.1:{port}",
            "server_port": port,
            "telegram_enabled": False,
            "document_storage_path": str(work_dir / "documents"),
            "attachment_storage_path": str(work_dir / "mailbox"),
            "chat_attachment_storage_path": str(work_dir / "chat-attachments"),
            "mcp_config": config.mcp_config.model_copy(
                update={"mcpServers": _local_mcp_servers(config, services)}
            ),
            "plugins": _local_plugins(config, services),
            "mqtt_config": MQTTConfig(),
            "keychute_config": config.keychute_config.model_copy(
                update={"enabled": False}
            ),
            "browser_handoff_config": config.browser_handoff_config.model_copy(
                update={"enabled": False}
            ),
            "ucp_config": config.ucp_config.model_copy(update={"enabled": False}),
            "pwa_config": PWAConfig(),
            "apns": ApnsConfig(),
            "email_intake": config.email_intake.model_copy(
                update={
                    "outbound_mailgun_api_key": None,
                    "outbound_mailgun_domain": None,
                }
            ),
            "model_tiers": tiers,
            "service_profiles": service_profiles,
            "model_routing": config.model_routing.model_copy(update={"mode": "off"}),
            "conversation_summaries": config.conversation_summaries.model_copy(
                update={"enabled": False}
            ),
            "users": users,
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
