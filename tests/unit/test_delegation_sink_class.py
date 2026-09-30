"""Which sink a delegation to each kind of profile reaches."""

from __future__ import annotations

from family_assistant.assistant import delegation_sink_class
from family_assistant.config_models import (
    ProcessingConfig,
    RemoteA2AConfig,
    ServiceProfile,
)
from family_assistant.security.taint import SinkClass


def test_a_profile_on_the_local_loop_is_not_an_egress() -> None:
    """Taint follows the turn into the delegate, so the handoff is local."""
    profile = ServiceProfile(
        id="complex_tasks", processing_config=ProcessingConfig(model_tier="deep")
    )

    assert delegation_sink_class(profile) is SinkClass.USER_LOCAL


def test_a_declared_sink_wins() -> None:
    profile = ServiceProfile(
        id="coder",
        processing_config=ProcessingConfig(
            provider="google",
            llm_model="antigravity-preview-09-2026",
            taint_sink_class=SinkClass.SANDBOX_NETWORK,
        ),
    )

    assert delegation_sink_class(profile) is SinkClass.SANDBOX_NETWORK


def test_agents_outside_the_local_loop_keep_the_tag_classification() -> None:
    """Taint cannot follow a remote or server-side agent, so nothing is declared."""
    remote = ServiceProfile(
        id="k8s_agent",
        remote_a2a=RemoteA2AConfig(agent_url="https://agent.example.com/a2a"),
    )
    deep_research = ServiceProfile(
        id="research",
        processing_config=ProcessingConfig(
            provider="google", llm_model="deep-research-preview-04-2026"
        ),
    )

    assert delegation_sink_class(remote) is None
    assert delegation_sink_class(deep_research) is None
