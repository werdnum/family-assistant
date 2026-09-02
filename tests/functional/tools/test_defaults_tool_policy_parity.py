from __future__ import annotations

import inspect
from pathlib import Path
from typing import cast

import yaml

from family_assistant import context_providers
from family_assistant.assistant import (
    _build_profile_policy_engine,  # noqa: PLC2701 - smallest helper that applies global_tools_policy injection to a profile
)
from family_assistant.config_loader import (
    load_prompts_yaml,
    resolve_all_service_profiles,
)
from family_assistant.config_models import (
    CONTEXT_PROVIDER_NAMES,
    DefaultProfileSettings,
    ServiceProfile,
)
from family_assistant.context_providers import (
    ContextProvider,
    TaintedContextProvider,
)
from family_assistant.security.taint import (
    SinkClass,
    SourceTrustTier,
    derive_tool_result_taint_source,
    resolve_tool_sink_class,
)
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS, PolicyEngine, ToolDescriptor
from family_assistant.tools.metadata import ToolTag
from family_assistant.tools.policy import ToolPolicyConfig, ToolPolicyDecision

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULTS_PATH = REPO_ROOT / "defaults.yaml"
PROMPTS_PATH = REPO_ROOT / "prompts.yaml"
MCP_SERVER_IDS = (
    "time",
    "brave",
    "homeassistant",
    "google-maps",
    "scrape",
    "browser",
)

CONFINED_NON_SENSITIVE_READ_ALLOWLIST = {
    # Static documentation shipped with the application, not acting-user data.
    "get_user_documentation_content": "packaged application documentation",
}


def _load_defaults_yaml() -> dict[str, object]:
    loaded = yaml.safe_load(DEFAULTS_PATH.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _load_resolved_profiles() -> tuple[DefaultProfileSettings, list[ServiceProfile]]:
    config_data = _load_defaults_yaml()
    config_data.setdefault("default_service_profile_id", "default_assistant")
    default_profile_settings = config_data["default_profile_settings"]
    assert isinstance(default_profile_settings, dict)
    processing_config = default_profile_settings["processing_config"]
    assert isinstance(processing_config, dict)
    default_prompts, service_profile_prompts = load_prompts_yaml(str(PROMPTS_PATH))
    if default_prompts:
        processing_config["prompts"] = default_prompts
    config_data["service_profiles"] = resolve_all_service_profiles(
        config_data, service_profile_prompts
    )
    default_settings = DefaultProfileSettings.model_validate(default_profile_settings)
    profiles = [
        ServiceProfile.model_validate(profile)
        for profile in config_data["service_profiles"]
    ]
    return default_settings, profiles


def _make_mcp_descriptor(server_id: str) -> ToolDescriptor:
    tool_name = f"{server_id.replace('-', '_')}_tool"
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
        tags=frozenset(),
        origin="mcp",
        mcp_server_id=server_id,
    )


def test_defaults_yaml_uses_policy_only_for_tool_access() -> None:
    defaults_data = _load_defaults_yaml()
    default_profile_settings = defaults_data["default_profile_settings"]
    assert isinstance(default_profile_settings, dict)
    assert "tools_policy" in default_profile_settings
    default_tools_config = default_profile_settings.get("tools_config", {})
    assert isinstance(default_tools_config, dict)
    assert "enable_local_tools" not in default_tools_config
    assert "enable_mcp_server_ids" not in default_tools_config
    assert "confirm_tools" not in default_tools_config

    service_profiles = defaults_data["service_profiles"]
    assert isinstance(service_profiles, list)

    for profile in service_profiles:
        assert isinstance(profile, dict)
        tools_config = profile.get("tools_config", {})
        assert isinstance(tools_config, dict)
        assert "enable_local_tools" not in tools_config
        assert "enable_mcp_server_ids" not in tools_config
        assert "confirm_tools" not in tools_config


def test_shipped_profiles_define_effective_tool_policy() -> None:
    default_settings, profiles = _load_resolved_profiles()
    descriptors = [*LOCAL_TOOL_DESCRIPTORS]
    descriptors.extend(_make_mcp_descriptor(server_id) for server_id in MCP_SERVER_IDS)

    profile_map: dict[str, DefaultProfileSettings | ServiceProfile] = {
        "default_profile_settings": default_settings,
        **{profile.id: profile for profile in profiles},
    }

    for profile_id, profile in profile_map.items():
        assert profile.tools_policy is not None, profile_id
        engine = PolicyEngine.from_policy_config(profile.tools_policy)

        for descriptor in descriptors:
            advertised_without_confirmation = engine.evaluate_for_advertisement(
                descriptor,
                can_confirm=False,
            )
            executable_without_confirmation = engine.evaluate_for_execution(
                descriptor,
                can_confirm=False,
            )

            assert isinstance(
                advertised_without_confirmation.decision,
                ToolPolicyDecision,
            )
            assert isinstance(
                executable_without_confirmation.decision,
                ToolPolicyDecision,
            )


def test_confined_profile_private_read_inventory_is_sensitive_tagged() -> None:
    """Audit every reachable read in every shipped confined profile."""
    _, profiles = _load_resolved_profiles()
    global_policy = _load_global_tools_policy()
    seen_allowlist_entries: set[str] = set()

    for profile in profiles:
        if profile.processing_config.include_aggregated_context:
            continue
        engine = _build_profile_policy_engine(
            profile.id,
            profile.tools_policy,
            None,
            global_policy,
            profile.excluded_global_tools,
        )
        for descriptor in LOCAL_TOOL_DESCRIPTORS:
            if ToolTag.READ_ONLY not in descriptor.tags:
                continue
            decision = engine.evaluate_for_advertisement(
                descriptor,
                can_confirm=True,
            ).decision
            if decision is ToolPolicyDecision.DENY:
                continue
            if ToolTag.SENSITIVE_DATA in descriptor.tags:
                continue
            assert descriptor.name in CONFINED_NON_SENSITIVE_READ_ALLOWLIST, (
                f"{profile.id}: reachable read-only tool {descriptor.name!r} must "
                "be tagged sensitive_data or explicitly justified in "
                "CONFINED_NON_SENSITIVE_READ_ALLOWLIST"
            )
            seen_allowlist_entries.add(descriptor.name)

    assert seen_allowlist_entries == set(CONFINED_NON_SENSITIVE_READ_ALLOWLIST)


def test_ucp_tools_are_default_on_demand_and_not_confirm_gated() -> None:
    default_settings, profiles = _load_resolved_profiles()
    shopping_tool_names = {
        "ucp_add_to_cart",
        "ucp_get_cart",
        "ucp_transfer_checkout_to_human",
    }
    descriptors = {
        descriptor.name: descriptor
        for descriptor in LOCAL_TOOL_DESCRIPTORS
        if descriptor.name in shopping_tool_names
    }
    assert set(descriptors) == shopping_tool_names
    cart_descriptor = descriptors["ucp_get_cart"]
    assert ToolTag.SENSITIVE_DATA in cart_descriptor.tags
    assert (
        resolve_tool_sink_class(cart_descriptor) is SinkClass.ARBITRARY_EXTERNAL_MESSAGE
    )

    default_engine = PolicyEngine.from_policy_config(default_settings.tools_policy)
    assert shopping_tool_names <= set(
        default_settings.tools_config.on_demand_local_tools
    )
    profile_by_id = {profile.id: profile for profile in profiles}
    assert "shopping_profile" not in profile_by_id
    browser_profile = profile_by_id["browser_profile"]
    browser_engine = PolicyEngine.from_policy_config(browser_profile.tools_policy)
    complex_tasks_profile = profile_by_id["complex_tasks"]
    complex_tasks_engine = PolicyEngine.from_policy_config(
        complex_tasks_profile.tools_policy
    )
    assert shopping_tool_names <= set(
        complex_tasks_profile.tools_config.on_demand_local_tools
    )

    for descriptor in descriptors.values():
        assert (
            default_engine.evaluate_for_advertisement(
                descriptor,
                can_confirm=True,
            ).decision
            == ToolPolicyDecision.ALLOW
        )
        assert (
            browser_engine.evaluate_for_advertisement(
                descriptor,
                can_confirm=False,
            ).decision
            == ToolPolicyDecision.ALLOW
        )
        assert (
            default_engine.evaluate_for_advertisement(
                descriptor,
                can_confirm=False,
            ).decision
            == ToolPolicyDecision.ALLOW
        )
        assert (
            complex_tasks_engine.evaluate_for_advertisement(
                descriptor,
                can_confirm=False,
            ).decision
            == ToolPolicyDecision.ALLOW
        )


def _load_global_tools_policy() -> ToolPolicyConfig:
    defaults_data = _load_defaults_yaml()
    global_policy = defaults_data["global_tools_policy"]
    assert isinstance(global_policy, dict)
    return ToolPolicyConfig.model_validate(global_policy)


def _local_descriptor(name: str) -> ToolDescriptor:
    for descriptor in LOCAL_TOOL_DESCRIPTORS:
        if descriptor.name == name:
            return descriptor
    raise AssertionError(f"{name} descriptor not found")


def test_large_result_tools_available_in_every_profile() -> None:
    """read_text_attachment and jq_query must be reachable from every profile.

    Large tool results are auto-converted to attachments and the assistant is
    told to read them back with these tools, so they have to survive even a
    deny-by-default profile that never mentions them. The shipped
    global_tools_policy injects them into every profile's engine.
    """
    default_settings, profiles = _load_resolved_profiles()
    global_policy = _load_global_tools_policy()
    read_text = _local_descriptor("read_text_attachment")
    jq = _local_descriptor("jq_query")

    profile_map: dict[str, DefaultProfileSettings | ServiceProfile] = {
        "default_profile_settings": default_settings,
        **{profile.id: profile for profile in profiles},
    }

    for profile_id, profile in profile_map.items():
        engine = _build_profile_policy_engine(
            profile_id,
            profile.tools_policy,
            None,
            global_policy,
        )
        for descriptor in (read_text, jq):
            assert (
                engine.evaluate_for_execution(
                    descriptor,
                    can_confirm=False,
                ).decision
                is ToolPolicyDecision.ALLOW
            ), f"{descriptor.name} not allowed in {profile_id}"


def _delegate_descriptor() -> ToolDescriptor:
    for descriptor in LOCAL_TOOL_DESCRIPTORS:
        if descriptor.name == "delegate_to_service":
            return descriptor
    raise AssertionError("delegate_to_service descriptor not found")


def test_delegation_to_engineer_is_review_gated_not_blocked() -> None:
    default_settings, profiles = _load_resolved_profiles()
    profile_by_id = {profile.id: profile for profile in profiles}
    delegate = _delegate_descriptor()

    # Every profile that can delegate at all must route the engineer through a
    # review gate (never an unconditional allow or confirm). browser_profile,
    # telephone, and complex_tasks each replace tools_policy wholesale, so each
    # needs its own gate; default_assistant inherits default_profile_settings.
    source_engines = {
        "default_profile_settings": PolicyEngine.from_policy_config(
            default_settings.tools_policy
        ),
        "complex_tasks": PolicyEngine.from_policy_config(
            profile_by_id["complex_tasks"].tools_policy
        ),
        "telephone": PolicyEngine.from_policy_config(
            profile_by_id["telephone"].tools_policy
        ),
        "browser_profile": PolicyEngine.from_policy_config(
            profile_by_id["browser_profile"].tools_policy
        ),
    }

    for source_id, engine in source_engines.items():
        to_engineer = engine.evaluate_for_execution(
            delegate,
            arguments={"target_service_id": "engineer"},
            can_confirm=True,
        )
        assert to_engineer.decision is ToolPolicyDecision.REVIEW, source_id

    # Profiles that block specific internal targets outright keep blocking them.
    for source_id in ("default_profile_settings", "complex_tasks", "telephone"):
        to_reminder = source_engines[source_id].evaluate_for_execution(
            delegate,
            arguments={"target_service_id": "reminder"},
            can_confirm=True,
        )
        assert to_reminder.decision is ToolPolicyDecision.DENY, source_id


def test_engineer_delegation_out_is_review_gated() -> None:
    _, profiles = _load_resolved_profiles()
    engineer = {profile.id: profile for profile in profiles}["engineer"]
    engine = PolicyEngine.from_policy_config(engineer.tools_policy)
    delegate = _delegate_descriptor()

    outbound = engine.evaluate_for_execution(
        delegate,
        arguments={"target_service_id": "default_assistant"},
        can_confirm=True,
    )
    assert outbound.decision is ToolPolicyDecision.REVIEW

    # The engineer must not reach internal/external-caller profiles that the
    # ordinary delegating policies deny outright, even with confirmation.
    for blocked_target in ("reminder", "event_handler", "telephone_external"):
        outbound_blocked = engine.evaluate_for_execution(
            delegate,
            arguments={"target_service_id": blocked_target},
            can_confirm=True,
        )
        assert outbound_blocked.decision is ToolPolicyDecision.DENY, blocked_target


def test_engineer_side_effects_and_reads_policy() -> None:
    """The engineer uses judged review for workers/delegation, confirm for issues, and allow for MCP reconnect/cancel."""
    _, profiles = _load_resolved_profiles()
    engineer = {profile.id: profile for profile in profiles}["engineer"]
    engine = PolicyEngine.from_policy_config(engineer.tools_policy)

    # spawn_worker is review-gated
    spawn = _local_descriptor("spawn_worker")
    assert (
        engine.evaluate_for_execution(spawn, can_confirm=True).decision
        is ToolPolicyDecision.REVIEW
    )

    # create_github_issue requires confirmation
    issue_desc = _local_descriptor("create_github_issue")
    attended = engine.evaluate_for_execution(issue_desc, can_confirm=True)
    assert attended.decision is ToolPolicyDecision.CONFIRM
    unattended = engine.evaluate_for_execution(issue_desc, can_confirm=False)
    assert unattended.decision is ToolPolicyDecision.DENY

    # reconnect_mcp_server and cancel_worker_task are allowed outright
    for allowed_name in ("reconnect_mcp_server", "cancel_worker_task"):
        descriptor = _local_descriptor(allowed_name)
        assert (
            engine.evaluate_for_execution(descriptor, can_confirm=False).decision
            is ToolPolicyDecision.ALLOW
        ), allowed_name

    # Diagnostic reads and introspection are allowed outright
    for allowed_name in (
        "read_task_result",
        "list_worker_tasks",
        "resolve_tool_policy",
        "read_source_file",
        "query_database",
        "read_error_logs",
    ):
        descriptor = _local_descriptor(allowed_name)
        decision = engine.evaluate_for_execution(descriptor, can_confirm=False).decision
        assert decision is ToolPolicyDecision.ALLOW, allowed_name


def test_engineer_has_review_guidance() -> None:
    """The engineer profile processing config declares review guidance for the reviewer."""
    engineer = _engineer_profile()
    guidance = engineer.processing_config.review_guidance
    assert guidance
    assert "This profile investigates the application" in guidance
    assert "never household content" in guidance


# Every tool the engineer can reach whose result is still rendered to the
# tool-call reviewer as trusted conversation, with the reason it may be. A
# result qualifies only if its content is deployment-authored (source,
# configuration, packaged documentation, runtime facts, statistics) or is
# restored with per-item provenance on read. Anything else must carry
# OUTPUT_UNTRUSTED so the turn's taint snapshot stubs it on the way to the
# judge. See docs/design/judge-gated-engineer-side-effects.md.
ENGINEER_TRUSTED_OUTPUT_ALLOWLIST = {
    "read_source_file": "application source, authored by the deployment",
    "search_source_code": "matches from application source",
    "get_user_documentation_content": "documentation packaged with the app",
    "get_resolved_config": "the deployment's own configuration, redacted",
    "get_profile_config": "one profile's configuration, redacted",
    "get_system_info": "interpreter, platform and dialect facts",
    # The MCP-registry diagnostics report on servers the operator configured
    # and has therefore already approved: the names and descriptions those
    # servers publish sit on the trusted side of the same boundary as the rest
    # of the deployment's configuration, and their tool definitions are already
    # injected into every profile that reaches them, on every turn. If that
    # approval is ever withdrawn, the fix is admission control on MCP tool
    # definitions -- one chokepoint at the prompt -- not taint on the
    # diagnostics that echo them back.
    "get_mcp_server_status": "connection state for operator-configured servers",
    "reconnect_mcp_server": "the same operator-configured server status payload",
    "get_profile_tool_inventory": "size accounting over configured tool sources",
    "resolve_tool_policy": "a policy decision and the rule that produced it",
    "get_automation_stats": "execution counts, timestamps and statuses only",
    "get_note": "merges the note's stored provenance onto the turn on read",
    "list_notes": "merges each listed note's stored provenance on read",
    "create_github_issue": (
        "issue number and URL, plus the title the call itself supplied"
    ),
    "cancel_worker_task": "task id and lifecycle status, never task content",
    "report_technical_problem": "a fixed acknowledgement of the recorded report",
    # The one exception, held open deliberately: get_message_history is granted
    # far beyond the engineer (the default baseline, email_intake, telephone,
    # complex_tasks), so a blanket retag would taint the default assistant's
    # turn whenever it reads its own history. Its rows already carry stored
    # taint metadata, so the correct treatment is the per-row OUTPUT_DYNAMIC
    # mode designed in docs/design/risk-adjudicated-taint-enforcement.md (M4),
    # which does not exist yet. Recorded as an accepted residual in
    # docs/design/judge-gated-engineer-side-effects.md rather than closed by a
    # tag that would break other profiles.
    "get_message_history": "pending per-row provenance (OUTPUT_DYNAMIC)",
}


def _engineer_profile() -> ServiceProfile:
    _, profiles = _load_resolved_profiles()
    return {profile.id: profile for profile in profiles}["engineer"]


def _engineer_reachable_descriptors() -> list[ToolDescriptor]:
    engineer = _engineer_profile()
    engine = _build_profile_policy_engine(
        engineer.id,
        engineer.tools_policy,
        None,
        _load_global_tools_policy(),
        engineer.excluded_global_tools,
    )
    return [
        descriptor
        for descriptor in LOCAL_TOOL_DESCRIPTORS
        if engine.evaluate_for_advertisement(descriptor, can_confirm=True).decision
        is not ToolPolicyDecision.DENY
    ]


def test_engineer_trusted_reads_are_deployment_authored() -> None:
    """No engineer tool launders external content into the trusted tier.

    ``derive_tool_result_taint_source`` records no source at all for an
    ``output_trusted`` tool, so its result stays at the turn's snapshot tier
    and renders to the tool-call reviewer inside ``<trusted_conversation>``.
    For a profile whose reads return whatever anyone ever emailed, messaged or
    browsed through the assistant, that hands the judge the attacker's text as
    the household's. Enumerating the retags would decay as tools are added, so
    the invariant is enforced over the profile's *effective* inventory: a tool
    that becomes reachable from the engineer has to be classified here before
    it can ship.
    """
    trusted = {
        descriptor.name
        for descriptor in _engineer_reachable_descriptors()
        if ToolTag.OUTPUT_TRUSTED in descriptor.tags
    }

    unclassified = sorted(trusted - set(ENGINEER_TRUSTED_OUTPUT_ALLOWLIST))
    assert not unclassified, (
        "engineer-reachable tools tagged output_trusted must either return "
        "deployment-authored content (or restore per-item provenance on read) "
        "and be justified in ENGINEER_TRUSTED_OUTPUT_ALLOWLIST, or be retagged "
        f"OUTPUT_UNTRUSTED: {unclassified}"
    )

    stale = sorted(set(ENGINEER_TRUSTED_OUTPUT_ALLOWLIST) - trusted)
    assert not stale, (
        "ENGINEER_TRUSTED_OUTPUT_ALLOWLIST entries no longer reachable from the "
        f"engineer as output_trusted; drop them: {stale}"
    )


def test_engineer_reads_that_return_external_content_are_untrusted() -> None:
    """Pin the reads the design names, so a retag cannot be quietly undone.

    Asserted through ``derive_tool_result_taint_source`` rather than on the tag
    alone, because the tag matters only insofar as it makes the read record an
    ``unknown_external`` source on the turn -- which is what stubs every later
    row on the way to the reviewer.
    """
    descriptors = {
        descriptor.name: descriptor for descriptor in _engineer_reachable_descriptors()
    }
    for name in (
        "query_database",
        "read_error_logs",
        "get_llm_request_history",
        "get_delegation_status",
        "list_delegations",
        "list_worker_tasks",
        "list_pending_callbacks",
        "list_automations",
        "get_automation",
    ):
        descriptor = descriptors[name]
        assert ToolTag.OUTPUT_UNTRUSTED in descriptor.tags, name
        source = derive_tool_result_taint_source(descriptor=descriptor, call_id=None)
        assert source is not None, name
        assert source.tier is SourceTrustTier.UNKNOWN_EXTERNAL, name


def _context_provider_classes() -> dict[str, type[ContextProvider]]:
    """Map every shipped provider's ``name`` to its class.

    Discovered from the module rather than listed, so a provider added later is
    covered by the engineer's invariant instead of silently escaping it.
    """
    discovered: dict[str, type[ContextProvider]] = {}
    for _, member in inspect.getmembers(context_providers, inspect.isclass):
        if member.__module__ != context_providers.__name__:
            continue
        # ContextProvider is a plain Protocol, so issubclass is unavailable;
        # match its shape instead -- a literal `name` property plus the
        # fragment method. Protocol declarations themselves are skipped.
        if getattr(member, "_is_protocol", False):
            continue
        if not callable(getattr(member, "get_context_fragments", None)):
            continue
        name_attr = inspect.getattr_static(member, "name", None)
        if not isinstance(name_attr, property) or name_attr.fget is None:
            continue
        discovered[cast("str", name_attr.fget(member))] = cast(
            "type[ContextProvider]", member
        )
    assert set(discovered) == CONTEXT_PROVIDER_NAMES
    return discovered


def test_engineer_admits_only_tainted_context_providers() -> None:
    """Ambient context reaching the engineer must declare its provenance.

    The engineer has ``include_aggregated_context: true``, so every provider it
    does not exclude contributes text to the same prompt the reviewer later
    judges. A provider that does not implement ``TaintedContextProvider``
    contributes externally sourced text with no provenance at all -- an emailed
    invitation's description, an entity state an integration wrote -- which
    then renders as the household's own words. The assertion is on the
    invariant rather than on the exclusion list, so a new provider fails here
    instead of joining the engineer's prompt silently.
    """
    engineer = _engineer_profile()
    assert engineer.processing_config.include_aggregated_context
    excluded = set(engineer.processing_config.excluded_context_providers)
    provider_classes = _context_provider_classes()

    undeclared = sorted(
        name
        for name, provider_cls in provider_classes.items()
        if name not in excluded and not issubclass(provider_cls, TaintedContextProvider)
    )
    assert not undeclared, (
        "context providers admitted by the engineer must implement "
        "TaintedContextProvider (or be listed in excluded_context_providers): "
        f"{undeclared}"
    )
