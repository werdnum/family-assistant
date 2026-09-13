"""Invariants for the shipped read-only GitHub MCP servers.

These servers give the ``engineer`` profile the version history the running
application cannot see for itself: the production image excludes ``.git``, so
without them a regression can only be guessed at from logs. What keeps that
access safe is not our tool policy -- GitHub enforces read-only server-side
when the URL says so -- which is exactly why the URL is worth a test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from family_assistant.assistant import (
    _build_profile_policy_engine,  # noqa: PLC2701 - the helper that applies global_tools_policy injection to a profile
)
from family_assistant.config_loader import (
    load_prompts_yaml,
    resolve_all_service_profiles,
)
from family_assistant.config_models import (
    MCPConfig,
    MCPServerConfig,
    ServiceProfile,
    mcp_servers_for_runtime,
)
from family_assistant.security.taint import (
    SinkClass,
    derive_tool_result_taint_source,
    is_externally_authored,
    resolve_tool_sink_class,
)
from family_assistant.tools import ToolDescriptor
from family_assistant.tools.mcp import MCP_STREAMABLE_HTTP_TRANSPORTS
from family_assistant.tools.metadata import (
    normalize_mcp_tool_metadata,
    resolve_mcp_tool_tags,
)
from family_assistant.tools.policy import ToolPolicyConfig, ToolPolicyDecision

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULTS_PATH = REPO_ROOT / "defaults.yaml"
PROMPTS_PATH = REPO_ROOT / "prompts.yaml"

GITHUB_MCP_SERVER_IDS = (
    "github-repos",
    "github-issues",
    "github-pull-requests",
)

# The profile these servers exist for. Every other shipped profile must be
# unable to reach them; see test_no_other_profile_can_reach_github.
GITHUB_READER_PROFILE_ID = "engineer"


def _load_defaults() -> dict[str, object]:
    loaded = yaml.safe_load(DEFAULTS_PATH.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _github_server_entries() -> dict[str, MCPServerConfig]:
    mcp_config = _load_defaults()["mcp_config"]
    assert isinstance(mcp_config, dict)
    servers = mcp_config["mcpServers"]
    assert isinstance(servers, dict)
    missing = set(GITHUB_MCP_SERVER_IDS) - servers.keys()
    assert not missing, f"defaults.yaml is missing GitHub MCP servers: {missing}"
    return {
        server_id: MCPServerConfig.model_validate(servers[server_id])
        for server_id in GITHUB_MCP_SERVER_IDS
    }


def _extra_field(config: MCPServerConfig, key: str) -> object:
    """Read a field the model leaves to ``extra="allow"`` (``url``, ``transport``)."""
    return (config.__pydantic_extra__ or {}).get(key)


@pytest.mark.parametrize("server_id", GITHUB_MCP_SERVER_IDS)
def test_github_mcp_servers_are_readonly_endpoints(server_id: str) -> None:
    """The ``/readonly`` suffix is the whole read-only guarantee.

    Our tool policy grants these servers wholesale by id, so it cannot tell a
    read from a write. Dropping the suffix would silently hand the engineer
    profile the ability to open, close and comment on anything in the
    repository, which is precisely the posture the profile is built to refuse.
    """
    url = _extra_field(_github_server_entries()[server_id], "url")
    assert isinstance(url, str)
    assert url.startswith("https://api.githubcopilot.com/mcp/"), url
    assert url.endswith("/readonly"), (
        f"{server_id} must point at a read-only GitHub endpoint; got {url}"
    )


@pytest.mark.parametrize("server_id", GITHUB_MCP_SERVER_IDS)
def test_github_mcp_servers_take_a_token_by_reference(server_id: str) -> None:
    """A credential belongs in the environment, never in the shipped defaults."""
    entry = _github_server_entries()[server_id]
    assert _extra_field(entry, "transport") in MCP_STREAMABLE_HTTP_TRANSPORTS
    assert entry.token is not None
    token = entry.token.get_secret_value()
    assert token.startswith("$"), (
        f"{server_id} must reference an environment variable, not inline a token"
    )


def test_github_mcp_token_is_masked_in_diagnostic_dumps() -> None:
    """The engineer can dump the live config, so the token must not be in it."""
    config = _github_server_entries()["github-repos"]
    assert "$GITHUB_MCP_TOKEN" not in str(config.model_dump(mode="json")["token"])
    # ...while the value still reaches the client that has to authenticate.
    runtime = mcp_servers_for_runtime(MCPConfig(mcpServers={"github-repos": config}))
    assert runtime["github-repos"]["token"] == "$GITHUB_MCP_TOKEN"


def _github_descriptor(
    server_id: str, tool_name: str = "list_commits"
) -> ToolDescriptor:
    """Build a descriptor as the runtime would, via the configured wildcard."""
    configured = normalize_mcp_tool_metadata(
        _github_server_entries()[server_id].tool_metadata
    )
    return ToolDescriptor(
        name=tool_name,
        definition={
            "type": "function",
            "function": {
                "name": tool_name,
                "description": f"Tool from {server_id}",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        tags=resolve_mcp_tool_tags(tool_name, configured, frozenset()),
        origin="mcp",
        mcp_server_id=server_id,
    )


@pytest.mark.parametrize("server_id", GITHUB_MCP_SERVER_IDS)
def test_github_tool_results_are_externally_authored(server_id: str) -> None:
    """Repository text is written by anyone who can reach the repository.

    Commit messages, issue bodies, PR descriptions and review comments come
    from bots and drive-by contributors as readily as from the household, so a
    result must never render to the tool-call reviewer as the user's own words.
    """
    taint = derive_tool_result_taint_source(
        descriptor=_github_descriptor(server_id),
        call_id="call-1",
    )
    assert taint is not None, "GitHub results must carry taint"
    assert is_externally_authored(taint.tier)


@pytest.mark.parametrize("server_id", GITHUB_MCP_SERVER_IDS)
def test_github_tools_are_low_bandwidth_rather_than_arbitrary_egress(
    server_id: str,
) -> None:
    """A fixed endpoint with a query argument is the low-bandwidth class.

    Without the wildcard metadata these land on the ``arbitrary_external_message``
    fallback, which adjudicates every call once a turn has read anything
    untrusted -- and an engineer's diagnostic reads routinely do. That would
    put a judge in front of reading one's own commit log.
    """
    assert (
        resolve_tool_sink_class(_github_descriptor(server_id))
        is SinkClass.LOW_BANDWIDTH_EXTERNAL
    )


@pytest.mark.parametrize("server_id", GITHUB_MCP_SERVER_IDS)
def test_wildcard_metadata_covers_tools_github_has_not_shipped_yet(
    server_id: str,
) -> None:
    """A tool name nobody has seen inherits the same classification.

    The point of the wildcard over a name list: GitHub's tool surface moves,
    and an enumeration would quietly drop anything it missed to the
    annotation-derived default.
    """
    descriptor = _github_descriptor(server_id, tool_name="some_future_github_tool")
    assert resolve_tool_sink_class(descriptor) is SinkClass.LOW_BANDWIDTH_EXTERNAL
    taint = derive_tool_result_taint_source(descriptor=descriptor, call_id="call-1")
    assert taint is not None
    assert is_externally_authored(taint.tier)


def _resolved_profiles() -> list[ServiceProfile]:
    config_data = _load_defaults()
    default_profile_settings = config_data["default_profile_settings"]
    assert isinstance(default_profile_settings, dict)
    processing_config = default_profile_settings["processing_config"]
    assert isinstance(processing_config, dict)
    default_prompts, service_profile_prompts = load_prompts_yaml(str(PROMPTS_PATH))
    if default_prompts:
        processing_config["prompts"] = default_prompts
    config_data.setdefault("default_service_profile_id", "default_assistant")
    resolved = resolve_all_service_profiles(config_data, service_profile_prompts)  # type: ignore[arg-type]
    return [ServiceProfile.model_validate(profile) for profile in resolved]


def _load_global_tools_policy() -> ToolPolicyConfig:
    global_policy = _load_defaults()["global_tools_policy"]
    assert isinstance(global_policy, dict)
    return ToolPolicyConfig.model_validate(global_policy)


def _advertises_github(
    profile: ServiceProfile, global_policy: ToolPolicyConfig
) -> bool:
    engine = _build_profile_policy_engine(
        profile.id,
        profile.tools_policy,
        None,
        global_policy,
        profile.excluded_global_tools,
    )
    return any(
        engine.evaluate_for_advertisement(
            _github_descriptor(server_id),
            can_confirm=True,
        ).decision
        is not ToolPolicyDecision.DENY
        for server_id in GITHUB_MCP_SERVER_IDS
    )


def test_engineer_can_read_github() -> None:
    global_policy = _load_global_tools_policy()
    profiles = {profile.id: profile for profile in _resolved_profiles()}
    engineer = profiles[GITHUB_READER_PROFILE_ID]
    assert _advertises_github(engineer, global_policy)


def test_no_other_profile_can_reach_github() -> None:
    """Repository access is the engineer's alone.

    Every shipped profile is deny-by-default, so this holds without a deny
    rule anywhere -- which is the point: a future profile has to name these
    server ids on purpose to reach them, rather than inheriting them.
    """
    global_policy = _load_global_tools_policy()
    reachable = {
        profile.id
        for profile in _resolved_profiles()
        if profile.id != GITHUB_READER_PROFILE_ID
        and _advertises_github(profile, global_policy)
    }
    assert not reachable, f"profiles unexpectedly reaching GitHub: {sorted(reachable)}"


def test_engineer_loads_github_servers_on_demand() -> None:
    """Three toolsets are too much surface to advertise on every turn.

    The engineer reaches for history on a minority of investigations, so the
    servers are activated when wanted rather than carried in every prompt.
    """
    profiles = {profile.id: profile for profile in _resolved_profiles()}
    on_demand = set(
        profiles[GITHUB_READER_PROFILE_ID].tools_config.get_on_demand_mcp_server_ids()
    )
    assert set(GITHUB_MCP_SERVER_IDS) <= on_demand


def test_all_github_servers_share_one_classification() -> None:
    """The three entries repeat one wildcard; a drifting copy is a bug.

    defaults.yaml spells the metadata out per server rather than sharing a YAML
    anchor, because the formatter leaves trailing whitespace on alias lines.
    This is the check that keeps the repetition honest.
    """
    classifications = {
        server_id: entry.tool_metadata
        for server_id, entry in _github_server_entries().items()
    }
    distinct = {tuple(sorted(metadata["*"])) for metadata in classifications.values()}
    assert len(distinct) == 1, f"GitHub servers disagree on metadata: {classifications}"
