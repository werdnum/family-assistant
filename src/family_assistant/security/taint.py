"""Runtime taint tracking for prompt-injection containment."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from family_assistant.tools.metadata import ToolDescriptor

logger = logging.getLogger(__name__)

TAINT_METADATA_VERSION = "runtime_v1"
A2A_TAINT_METADATA_KEY = "family_assistant_taint_metadata"
LEGACY_MISSING_TAINT_METADATA_LABEL = "legacy_missing_taint_metadata"

_HOME_ASSISTANT_ACTION_TOOL = "call_home_assistant_action"

# Home Assistant domains that act on household devices and state only. This is
# an allowlist on purpose: HA also exposes domains that message people, call
# webhooks, or run code, and only a domain verified to stay inside the
# household belongs here. Deliberately excluded, with reasons:
#   script / automation  — run operator-defined sequences that may themselves
#                          notify or call a REST endpoint
#   media_player         — play_media fetches a caller-supplied URL
#   notify / rest_command / tts / conversation — leave the household by design
_HOUSEHOLD_LOCAL_HA_DOMAINS = frozenset({
    "alarm_control_panel",
    "button",
    "climate",
    "cover",
    "fan",
    "humidifier",
    "input_boolean",
    "input_button",
    "input_datetime",
    "input_number",
    "input_select",
    "input_text",
    "lawn_mower",
    "light",
    "lock",
    "number",
    "scene",
    "select",
    "siren",
    "switch",
    "vacuum",
    "valve",
    "water_heater",
})

# HA domains that run operator-supplied code on the Home Assistant host, which
# is the sandbox_network sink rather than any kind of household-local action.
_CODE_EXECUTING_HA_DOMAINS = frozenset({"python_script", "shell_command"})


class SourceTrustTier(IntEnum):
    """Monotonic source trust tier. Higher values are less trusted."""

    TRUSTED_USER = 0
    KNOWN_CONTACT = 1
    RECOGNIZED_MACHINE = 2
    UNKNOWN_EXTERNAL = 3

    @classmethod
    def from_value(cls, value: object) -> SourceTrustTier:
        """Parse a source trust tier from enum, name, or config value."""
        if isinstance(value, SourceTrustTier):
            return value
        if isinstance(value, int):
            return cls(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            for tier in cls:
                if normalized in {tier.name.lower(), tier.config_value}:
                    return tier
        raise ValueError(f"Unknown source trust tier: {value!r}")

    @property
    def config_value(self) -> str:
        """Return the stable lower-case config representation."""
        return self.name.lower()


class SinkClass(StrEnum):
    """Operation class used by runtime taint policy."""

    USER_LOCAL = "user_local"
    HOME_LOCAL = "home_local"
    ARTIFACT_WRITE = "artifact_write"
    LOW_BANDWIDTH_EXTERNAL = "low_bandwidth_external"
    KNOWN_USER_MESSAGE = "known_user_message"
    ARBITRARY_EXTERNAL_MESSAGE = "arbitrary_external_message"
    ATTACKER_ADDRESSABLE_EGRESS = "attacker_addressable_egress"
    SANDBOX_NETWORK = "sandbox_network"
    SENSITIVE_READ_BROADENING = "sensitive_read_broadening"


class TaintPolicyOutcome(StrEnum):
    """Runtime taint policy outcome."""

    ALLOW = "allow"
    AUDIT = "audit"
    CONFIRM = "confirm"
    REDACT = "redact"
    DENY = "deny"


class TaintPolicyMode(StrEnum):
    """Runtime taint policy rollout mode."""

    OBSERVE = "observe"
    ENFORCE = "enforce"


class TaintSourceType(StrEnum):
    """Kind of source that introduced tainted content."""

    USER_MESSAGE = "user_message"
    EMAIL = "email"
    DOCUMENT = "document"
    NOTE = "note"
    TOOL_OUTPUT = "tool_output"
    BROWSER_SNAPSHOT = "browser_snapshot"
    ATTACHMENT = "attachment"
    AUTOMATION_TRIGGER = "automation_trigger"
    EVENT = "event"
    SANDBOX_OUTPUT = "sandbox_output"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class TaintSource:
    """Explanation for a source trust tier entering the turn."""

    source_type: TaintSourceType
    source_id: str | None
    tier: SourceTrustTier
    labels: frozenset[str]
    reason: str


@dataclass(frozen=True, slots=True)
class SensitiveReadScope:
    """Private corpus scope touched by a sensitive read."""

    kind: Literal["notes", "documents", "message_history", "attachments"]
    qualifier: str
    surfaced_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class SensitiveReadRecord:
    """Sensitive read recorded with turn-local ordering."""

    sequence: int
    scope: SensitiveReadScope
    query_origin: Literal["direct_user", "model_generated", "tool_or_history"]


@dataclass(frozen=True, slots=True)
class TurnTaintState:
    """Immutable taint state for a single processing turn."""

    max_tier: SourceTrustTier
    sources: tuple[TaintSource, ...]
    sensitive_reads: tuple[SensitiveReadRecord, ...]
    fresh_high_taint_seen_at_sequence: int | None
    history_high_taint_present: bool
    sequence: int = 0

    @classmethod
    def empty(cls) -> TurnTaintState:
        """Create a clean turn state."""
        return cls(
            max_tier=SourceTrustTier.TRUSTED_USER,
            sources=(),
            sensitive_reads=(),
            fresh_high_taint_seen_at_sequence=None,
            history_high_taint_present=False,
            sequence=0,
        )

    def add_source(
        self,
        source: TaintSource,
        *,
        from_history: bool = False,
    ) -> TurnTaintState:
        """Return a new state containing one additional source."""
        next_sequence = self.sequence + 1
        max_tier = max(self.max_tier, source.tier)
        fresh_high_sequence = self.fresh_high_taint_seen_at_sequence
        history_high_present = self.history_high_taint_present
        if source.tier >= SourceTrustTier.UNKNOWN_EXTERNAL:
            if from_history:
                history_high_present = True
            elif fresh_high_sequence is None:
                fresh_high_sequence = next_sequence
        return replace(
            self,
            max_tier=max_tier,
            sources=(*self.sources, source),
            fresh_high_taint_seen_at_sequence=fresh_high_sequence,
            history_high_taint_present=history_high_present,
            sequence=next_sequence,
        )

    def add_sensitive_read(
        self,
        scope: SensitiveReadScope,
        query_origin: Literal["direct_user", "model_generated", "tool_or_history"],
    ) -> TurnTaintState:
        """Return a new state with a sensitive read record."""
        next_sequence = self.sequence + 1
        record = SensitiveReadRecord(
            sequence=next_sequence,
            scope=scope,
            query_origin=query_origin,
        )
        return replace(
            self,
            sensitive_reads=(*self.sensitive_reads, record),
            sequence=next_sequence,
        )

    def to_metadata(self, *, max_sources: int = 12) -> TaintMetadata:
        """Serialize a compact metadata representation for persistence."""
        return {
            "version": TAINT_METADATA_VERSION,
            "max_tier": self.max_tier.config_value,
            "history_high_taint_present": self.history_high_taint_present,
            "fresh_high_taint_seen_at_sequence": self.fresh_high_taint_seen_at_sequence,
            "sources": [
                {
                    "source_type": source.source_type.value,
                    "source_id": source.source_id,
                    "tier": source.tier.config_value,
                    "labels": sorted(source.labels),
                    "reason": source.reason,
                }
                for source in self.sources[-max_sources:]
            ],
        }

    @classmethod
    def from_metadata(
        cls,
        metadata: object,
        *,
        from_history: bool = False,
    ) -> TurnTaintState:
        """Deserialize taint metadata, defaulting malformed data to unknown external."""
        if not isinstance(metadata, dict):
            if metadata is None:
                return cls.empty()
            return cls.malformed_history_state()

        try:
            max_tier = SourceTrustTier.from_value(metadata.get("max_tier"))
        except (TypeError, ValueError):
            return cls.malformed_history_state()

        state = cls.empty()
        raw_sources = metadata.get("sources")
        if isinstance(raw_sources, list):
            for raw_source in raw_sources:
                source = _source_from_metadata(raw_source, fallback_tier=max_tier)
                state = state.add_source(source, from_history=from_history)
        elif max_tier > SourceTrustTier.TRUSTED_USER:
            state = state.add_source(
                TaintSource(
                    source_type=TaintSourceType.MANUAL,
                    source_id=None,
                    tier=max_tier,
                    labels=frozenset(),
                    reason="Persisted taint metadata omitted source summaries.",
                ),
                from_history=from_history,
            )
        if max_tier > state.max_tier:
            state = state.add_source(
                TaintSource(
                    source_type=TaintSourceType.MANUAL,
                    source_id=None,
                    tier=max_tier,
                    labels=frozenset(),
                    reason=(
                        "Persisted taint metadata max_tier exceeded retained "
                        "source summaries."
                    ),
                ),
                from_history=from_history,
            )
        if from_history and state.max_tier >= SourceTrustTier.UNKNOWN_EXTERNAL:
            state = replace(state, history_high_taint_present=True)
        return state

    @classmethod
    def malformed_history_state(cls) -> TurnTaintState:
        """Return a conservative state for malformed persisted metadata."""
        return cls.empty().add_source(
            TaintSource(
                source_type=TaintSourceType.MANUAL,
                source_id=None,
                tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                labels=frozenset(),
                reason="Malformed taint metadata treated as unknown external.",
            ),
            from_history=True,
        )


class TaintMetadataSource(TypedDict):
    """Serialized taint source summary."""

    source_type: str
    source_id: str | None
    tier: str
    labels: list[str]
    reason: str


class TaintMetadata(TypedDict, total=False):
    """Compact message/artifact taint metadata."""

    version: str
    max_tier: str
    history_high_taint_present: bool
    fresh_high_taint_seen_at_sequence: int | None
    sources: list[TaintMetadataSource]


class TurnTaintTracker(Protocol):
    """Mutable holder for immutable turn taint state."""

    def snapshot(self) -> TurnTaintState: ...

    def replace(self, state: TurnTaintState) -> None: ...

    def add_source(
        self,
        source: TaintSource,
        *,
        from_history: bool = False,
    ) -> TurnTaintState: ...


class InMemoryTurnTaintTracker:
    """Turn-local tracker with synchronous state replacement."""

    def __init__(self, state: TurnTaintState | None = None) -> None:
        self._state = state or TurnTaintState.empty()

    def snapshot(self) -> TurnTaintState:
        """Return the current immutable state."""
        return self._state

    def replace(self, state: TurnTaintState) -> None:
        """Replace the current state."""
        self._state = state

    def add_source(
        self,
        source: TaintSource,
        *,
        from_history: bool = False,
    ) -> TurnTaintState:
        """Synchronously merge a source into the tracker."""
        self._state = self._state.add_source(source, from_history=from_history)
        return self._state


def merge_taint_state_into_tracker(
    tracker: TurnTaintTracker,
    state: TurnTaintState,
    *,
    from_history: bool = False,
) -> TurnTaintState:
    """Merge a deserialized taint state without losing persisted max_tier."""
    merged = tracker.snapshot()
    for source in state.sources:
        merged = merged.add_source(source, from_history=from_history)
    if state.max_tier > merged.max_tier:
        merged = merged.add_source(
            TaintSource(
                source_type=TaintSourceType.MANUAL,
                source_id=None,
                tier=state.max_tier,
                labels=frozenset(),
                reason=(
                    "Merged taint state max_tier exceeded retained source summaries."
                ),
            ),
            from_history=from_history,
        )
    if state.history_high_taint_present and not merged.history_high_taint_present:
        merged = replace(merged, history_high_taint_present=True)
    tracker.replace(merged)
    return merged


class TaintPolicyConfig(BaseModel):
    """Top-level and profile runtime taint policy configuration."""

    model_config = ConfigDict(extra="forbid")

    mode: TaintPolicyMode = TaintPolicyMode.OBSERVE
    history_taint_epoch: datetime | None = None
    high_taint_tier: SourceTrustTier = SourceTrustTier.UNKNOWN_EXTERNAL
    default_unspecified_tool_output_tier: SourceTrustTier = (
        SourceTrustTier.UNKNOWN_EXTERNAL
    )
    operator_minimum: dict[SourceTrustTier, dict[SinkClass, TaintPolicyOutcome]] = (
        Field(default_factory=dict)
    )
    matrix: dict[SourceTrustTier, dict[SinkClass, TaintPolicyOutcome]] = Field(
        default_factory=dict
    )
    matrix_overrides: dict[SourceTrustTier, dict[SinkClass, TaintPolicyOutcome]] = (
        Field(default_factory=dict)
    )
    artifact_labels: dict[SourceTrustTier, list[str]] = Field(
        default_factory=lambda: {
            SourceTrustTier.UNKNOWN_EXTERNAL: ["source_unknown_external"],
            SourceTrustTier.RECOGNIZED_MACHINE: ["source_recognized_machine"],
            SourceTrustTier.KNOWN_CONTACT: ["source_known_contact"],
        }
    )

    @field_validator("history_taint_epoch", mode="after")
    @classmethod
    def _require_timezone_aware_epoch(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            msg = (
                "taint_policy.history_taint_epoch must be timezone-aware. Quote "
                "the value in YAML so it is parsed as an ISO-8601 string with an "
                'offset, e.g. history_taint_epoch: "2026-08-01T00:00:00+00:00". '
                "Set it to the instant this feature is DEPLOYED (or later), "
                "never earlier: rows written before deploy may contain re-baked "
                "legacy taint poison that read-time amnesty only partially "
                "neutralizes."
            )
            raise ValueError(msg)
        return value.astimezone(UTC)

    @field_validator(
        "high_taint_tier",
        "default_unspecified_tool_output_tier",
        mode="before",
    )
    @classmethod
    def _parse_tier_value(cls, value: object) -> object:
        return SourceTrustTier.from_value(value)

    @field_validator(
        "operator_minimum",
        "matrix",
        "matrix_overrides",
        mode="before",
    )
    @classmethod
    def _parse_tier_keyed_matrix(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        parsed: dict[SourceTrustTier, object] = {}
        for raw_key, raw_value in value.items():
            parsed[SourceTrustTier.from_value(raw_key)] = raw_value
        return parsed

    @field_validator("artifact_labels", mode="before")
    @classmethod
    def _parse_tier_keyed_labels(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        parsed: dict[SourceTrustTier, object] = {}
        for raw_key, raw_value in value.items():
            parsed[SourceTrustTier.from_value(raw_key)] = raw_value
        return parsed


@dataclass(frozen=True, slots=True)
class TaintPolicyEvaluation:
    """Resolved runtime taint policy evaluation for one sink request."""

    sink_class: SinkClass
    requested_outcome: TaintPolicyOutcome
    effective_outcome: TaintPolicyOutcome
    mode: TaintPolicyMode
    reason: str


class TaintPolicyEvaluator:
    """Evaluate turn taint against a sink-class policy matrix."""

    def __init__(self, config: TaintPolicyConfig | None = None) -> None:
        self._config = config or TaintPolicyConfig()
        self._matrix = _default_taint_matrix()
        _merge_matrix(self._matrix, self._config.matrix)
        _merge_matrix(self._matrix, self._config.matrix_overrides)

    @property
    def mode(self) -> TaintPolicyMode:
        """Return the rollout mode."""
        return self._config.mode

    def evaluate(
        self,
        *,
        state: TurnTaintState,
        sink_class: SinkClass,
    ) -> TaintPolicyEvaluation:
        """Evaluate a sink request for the supplied taint state."""
        requested = self._configured_outcome(state.max_tier, sink_class)
        requested = self._apply_operator_minimum(
            tier=state.max_tier,
            sink_class=sink_class,
            outcome=requested,
        )
        effective = (
            TaintPolicyOutcome.AUDIT
            if self._config.mode is TaintPolicyMode.OBSERVE
            and requested not in {TaintPolicyOutcome.ALLOW, TaintPolicyOutcome.AUDIT}
            else requested
        )
        reason = (
            f"Runtime taint max tier {state.max_tier.config_value} maps "
            f"{sink_class.value} to {requested.value}."
        )
        if effective != requested:
            reason += f" Observe mode records this as {effective.value}."
        return TaintPolicyEvaluation(
            sink_class=sink_class,
            requested_outcome=requested,
            effective_outcome=effective,
            mode=self._config.mode,
            reason=reason,
        )

    def evaluate_tool(
        self,
        *,
        descriptor: ToolDescriptor,
        state: TurnTaintState,
        arguments: Mapping[str, object] | None = None,
    ) -> TaintPolicyEvaluation:
        """Resolve a tool sink class from metadata and evaluate it."""
        return self.evaluate(
            state=state,
            sink_class=resolve_tool_sink_class(descriptor, arguments),
        )

    def _configured_outcome(
        self,
        tier: SourceTrustTier,
        sink_class: SinkClass,
    ) -> TaintPolicyOutcome:
        return self._matrix.get(tier, {}).get(sink_class, TaintPolicyOutcome.ALLOW)

    def _apply_operator_minimum(
        self,
        *,
        tier: SourceTrustTier,
        sink_class: SinkClass,
        outcome: TaintPolicyOutcome,
    ) -> TaintPolicyOutcome:
        minimum = self._config.operator_minimum.get(tier, {}).get(sink_class)
        if minimum is None:
            return outcome
        if _outcome_satisfies_minimum(outcome, minimum):
            return outcome
        return minimum


def merge_taint_policy_config(
    *,
    base: TaintPolicyConfig,
    profile: TaintPolicyConfig | None,
) -> TaintPolicyConfig:
    """Merge deployment taint policy with optional profile-level overrides."""
    if profile is None:
        return base

    merged = base.model_copy(deep=True)
    fields_set = profile.model_fields_set
    if "operator_minimum" in fields_set and profile.operator_minimum:
        msg = "Profile taint_policy cannot define operator_minimum"
        raise ValueError(msg)
    if "history_taint_epoch" in fields_set and profile.history_taint_epoch is not None:
        msg = "Profile taint_policy cannot define history_taint_epoch"
        raise ValueError(msg)
    if "mode" in fields_set:
        if (
            base.mode is TaintPolicyMode.ENFORCE
            and profile.mode is TaintPolicyMode.OBSERVE
        ):
            msg = "Profile taint_policy.mode cannot relax enforce to observe"
            raise ValueError(msg)
        merged.mode = profile.mode
    if "high_taint_tier" in fields_set:
        merged.high_taint_tier = profile.high_taint_tier
    if "default_unspecified_tool_output_tier" in fields_set:
        if (
            profile.default_unspecified_tool_output_tier
            < base.default_unspecified_tool_output_tier
        ):
            msg = (
                "Profile taint_policy.default_unspecified_tool_output_tier "
                "cannot be more trusted than the base policy"
            )
            raise ValueError(msg)
        merged.default_unspecified_tool_output_tier = (
            profile.default_unspecified_tool_output_tier
        )
    if "matrix" in fields_set:
        _reject_relaxed_base_policy(
            overrides=profile.matrix,
            base=base,
        )
        _merge_matrix(merged.matrix, profile.matrix)
    if "matrix_overrides" in fields_set:
        _reject_relaxed_base_policy(
            overrides=profile.matrix_overrides,
            base=base,
        )
        _merge_matrix(merged.matrix_overrides, profile.matrix_overrides)
    if "artifact_labels" in fields_set:
        merged.artifact_labels = {
            **merged.artifact_labels,
            **profile.artifact_labels,
        }
    return merged


def _reject_relaxed_base_policy(
    *,
    overrides: dict[SourceTrustTier, dict[SinkClass, TaintPolicyOutcome]],
    base: TaintPolicyConfig,
) -> None:
    base_matrix = _default_taint_matrix()
    _merge_matrix(base_matrix, base.matrix)
    _merge_matrix(base_matrix, base.matrix_overrides)
    for tier, sink_map in overrides.items():
        for sink_class, outcome in sink_map.items():
            minimum = base_matrix.get(tier, {}).get(sink_class)
            operator_minimum = base.operator_minimum.get(tier, {}).get(sink_class)
            if operator_minimum is not None and (
                minimum is None
                or _outcome_strictness(operator_minimum) > _outcome_strictness(minimum)
            ):
                minimum = operator_minimum
            if minimum is None:
                continue
            if not _outcome_satisfies_minimum(outcome, minimum):
                msg = (
                    "Profile taint_policy cannot relax base policy for "
                    f"{tier.config_value}.{sink_class.value}: "
                    f"{outcome.value} < {minimum.value}"
                )
                raise ValueError(msg)


def _home_assistant_action_sink_class(
    arguments: Mapping[str, object] | None,
) -> SinkClass:
    """Classify a Home Assistant action by its domain rather than blanket-tagging.

    ``home_auto`` alone would understate the risk, because HA exposes domains
    that deliver messages, invoke webhooks, or run code on the HA host. The
    household-local set is therefore an allowlist: an unrecognised or absent
    domain keeps the conservative external classification, so a domain added by
    a future HA release is never silently downgraded.
    """
    domain = (arguments or {}).get("domain")
    if not isinstance(domain, str):
        return SinkClass.ARBITRARY_EXTERNAL_MESSAGE
    normalized = domain.strip().lower()
    if normalized in _CODE_EXECUTING_HA_DOMAINS:
        return SinkClass.SANDBOX_NETWORK
    if normalized in _HOUSEHOLD_LOCAL_HA_DOMAINS:
        return SinkClass.HOME_LOCAL
    return SinkClass.ARBITRARY_EXTERNAL_MESSAGE


def resolve_tool_sink_class(
    descriptor: ToolDescriptor,
    arguments: Mapping[str, object] | None = None,
) -> SinkClass:
    """Resolve a conservative sink class from tool metadata tags.

    ``arguments`` lets a tool that spans several sink classes be classified by
    the call rather than by its registration, which the taint design requires
    for Home Assistant actions. Omitting it keeps the tag-only classification.
    """
    tag_values = {str(getattr(tag, "value", tag)) for tag in descriptor.tags}
    if "sensitive_data" in tag_values and "read_only" in tag_values:
        return SinkClass.SENSITIVE_READ_BROADENING
    if "code_execution" in tag_values or "worker" in tag_values:
        return SinkClass.SANDBOX_NETWORK
    if "browser" in tag_values:
        return SinkClass.ATTACKER_ADDRESSABLE_EGRESS
    if "user_facing_media" in tag_values:
        # Attaching media to the current turn's own reply is a local, same-user
        # sink — it cannot address any external recipient. Placed below the
        # high-risk read/code/browser rules so a tool that also carried one of
        # those tags is never downgraded to user_local by this rule.
        return SinkClass.USER_LOCAL
    if descriptor.name == _HOME_ASSISTANT_ACTION_TOOL:
        return _home_assistant_action_sink_class(arguments)
    if "known_user_comm" in tag_values:
        # Outward communication whose recipient the server validates against
        # configured users. Checked before external_comm so the refinement
        # wins, since a tool carries both: the broader tag keeps tool policies
        # that match on it working, while the sink class reflects that the
        # model cannot choose the destination.
        return SinkClass.KNOWN_USER_MESSAGE
    if "external_comm" in tag_values:
        return SinkClass.ARBITRARY_EXTERNAL_MESSAGE
    if "delegation" in tag_values:
        return SinkClass.ARBITRARY_EXTERNAL_MESSAGE
    if "low_bandwidth_external" in tag_values:
        return SinkClass.LOW_BANDWIDTH_EXTERNAL
    if "home_auto" in tag_values:
        return SinkClass.HOME_LOCAL
    if "state_persisting" in tag_values or "automation" in tag_values:
        return SinkClass.ARTIFACT_WRITE
    if (
        "state_changing" in tag_values
        or "scheduling" in tag_values
        or "calendar" in tag_values
    ):
        return SinkClass.ARTIFACT_WRITE
    if "read_only" in tag_values and "open_world" not in tag_values:
        # A closed-world read-only tool cannot deliver free-form external
        # messages, so the arbitrary_external_message fallback below would
        # misstate its risk. Without sensitive_data metadata the read is still
        # conservatively treated as read-broadening rather than allowed outright.
        # Placed last so read-only tools that also carry a higher-risk tag
        # (browser reads, external_comm reads such as ucp_get_cart) keep their
        # stricter class. Open-world read-only tools are deliberately excluded:
        # the model controls the query/URL sent to an external service, so a
        # read-only open-world tool can still exfiltrate. Those fall through to
        # the arbitrary_external_message egress classification below unless an
        # operator narrows them via tool_metadata.
        return SinkClass.SENSITIVE_READ_BROADENING
    logger.warning(
        "Tool '%s' has no sink-class metadata; defaulting to arbitrary external "
        "message for runtime taint policy.",
        descriptor.name,
    )
    return SinkClass.ARBITRARY_EXTERNAL_MESSAGE


def _default_taint_matrix() -> dict[
    SourceTrustTier, dict[SinkClass, TaintPolicyOutcome]
]:
    return {
        SourceTrustTier.TRUSTED_USER: {
            SinkClass.USER_LOCAL: TaintPolicyOutcome.ALLOW,
            SinkClass.HOME_LOCAL: TaintPolicyOutcome.ALLOW,
            SinkClass.ARTIFACT_WRITE: TaintPolicyOutcome.ALLOW,
            SinkClass.LOW_BANDWIDTH_EXTERNAL: TaintPolicyOutcome.ALLOW,
            SinkClass.SENSITIVE_READ_BROADENING: TaintPolicyOutcome.ALLOW,
        },
        SourceTrustTier.KNOWN_CONTACT: {
            SinkClass.USER_LOCAL: TaintPolicyOutcome.ALLOW,
            SinkClass.HOME_LOCAL: TaintPolicyOutcome.ALLOW,
            SinkClass.ARTIFACT_WRITE: TaintPolicyOutcome.AUDIT,
            SinkClass.LOW_BANDWIDTH_EXTERNAL: TaintPolicyOutcome.ALLOW,
            SinkClass.KNOWN_USER_MESSAGE: TaintPolicyOutcome.AUDIT,
            SinkClass.ARBITRARY_EXTERNAL_MESSAGE: TaintPolicyOutcome.CONFIRM,
            SinkClass.ATTACKER_ADDRESSABLE_EGRESS: TaintPolicyOutcome.CONFIRM,
            SinkClass.SANDBOX_NETWORK: TaintPolicyOutcome.CONFIRM,
            SinkClass.SENSITIVE_READ_BROADENING: TaintPolicyOutcome.ALLOW,
        },
        SourceTrustTier.RECOGNIZED_MACHINE: {
            SinkClass.USER_LOCAL: TaintPolicyOutcome.ALLOW,
            SinkClass.HOME_LOCAL: TaintPolicyOutcome.ALLOW,
            SinkClass.ARTIFACT_WRITE: TaintPolicyOutcome.AUDIT,
            SinkClass.LOW_BANDWIDTH_EXTERNAL: TaintPolicyOutcome.ALLOW,
            SinkClass.KNOWN_USER_MESSAGE: TaintPolicyOutcome.AUDIT,
            SinkClass.ARBITRARY_EXTERNAL_MESSAGE: TaintPolicyOutcome.CONFIRM,
            SinkClass.ATTACKER_ADDRESSABLE_EGRESS: TaintPolicyOutcome.CONFIRM,
            SinkClass.SANDBOX_NETWORK: TaintPolicyOutcome.CONFIRM,
            SinkClass.SENSITIVE_READ_BROADENING: TaintPolicyOutcome.ALLOW,
        },
        SourceTrustTier.UNKNOWN_EXTERNAL: {
            SinkClass.USER_LOCAL: TaintPolicyOutcome.ALLOW,
            SinkClass.HOME_LOCAL: TaintPolicyOutcome.ALLOW,
            SinkClass.ARTIFACT_WRITE: TaintPolicyOutcome.AUDIT,
            SinkClass.LOW_BANDWIDTH_EXTERNAL: TaintPolicyOutcome.AUDIT,
            SinkClass.KNOWN_USER_MESSAGE: TaintPolicyOutcome.CONFIRM,
            SinkClass.ARBITRARY_EXTERNAL_MESSAGE: TaintPolicyOutcome.CONFIRM,
            SinkClass.ATTACKER_ADDRESSABLE_EGRESS: TaintPolicyOutcome.CONFIRM,
            SinkClass.SANDBOX_NETWORK: TaintPolicyOutcome.DENY,
            SinkClass.SENSITIVE_READ_BROADENING: TaintPolicyOutcome.CONFIRM,
        },
    }


def _merge_matrix(
    target: dict[SourceTrustTier, dict[SinkClass, TaintPolicyOutcome]],
    overrides: dict[SourceTrustTier, dict[SinkClass, TaintPolicyOutcome]],
) -> None:
    for tier, sink_map in overrides.items():
        target.setdefault(tier, {}).update(sink_map)


def _outcome_strictness(outcome: TaintPolicyOutcome) -> int:
    if outcome is TaintPolicyOutcome.ALLOW:
        return 0
    if outcome is TaintPolicyOutcome.AUDIT:
        return 1
    if outcome is TaintPolicyOutcome.REDACT:
        return 2
    if outcome is TaintPolicyOutcome.CONFIRM:
        return 2
    if outcome is TaintPolicyOutcome.DENY:
        return 3
    raise AssertionError(f"Unhandled taint policy outcome: {outcome}")


def _outcome_satisfies_minimum(
    outcome: TaintPolicyOutcome,
    minimum: TaintPolicyOutcome,
) -> bool:
    if outcome is minimum:
        return True
    if outcome is TaintPolicyOutcome.REDACT or minimum is TaintPolicyOutcome.REDACT:
        return False
    return _outcome_strictness(outcome) >= _outcome_strictness(minimum)


def derive_tool_result_taint_source(
    *,
    descriptor: ToolDescriptor,
    call_id: str | None,
    default_unspecified_tool_output_tier: SourceTrustTier = SourceTrustTier.UNKNOWN_EXTERNAL,
) -> TaintSource | None:
    """Derive result taint from static tool output metadata."""
    tag_values = {str(getattr(tag, "value", tag)) for tag in descriptor.tags}
    if "output_trusted" in tag_values:
        return None
    tier = SourceTrustTier.UNKNOWN_EXTERNAL
    if "output_untrusted" in tag_values:
        reason = f"Tool '{descriptor.name}' is tagged output_untrusted."
    elif "output_unspecified" in tag_values:
        tier = default_unspecified_tool_output_tier
        reason = (
            f"Tool '{descriptor.name}' has unspecified output trust; defaulting "
            f"to {tier.config_value} for runtime taint."
        )
        logger.warning(reason)
    else:
        tier = default_unspecified_tool_output_tier
        reason = (
            f"Tool '{descriptor.name}' has no output trust metadata; defaulting "
            f"to {tier.config_value} for runtime taint."
        )
        logger.warning(reason)

    return TaintSource(
        source_type=TaintSourceType.TOOL_OUTPUT,
        source_id=call_id or descriptor.name,
        tier=tier,
        labels=frozenset(),
        reason=reason,
    )


def amnestied_history_taint_metadata(metadata: object) -> TaintMetadata | None:
    """Recompute a pre-epoch history row's taint from attributed sources only.

    Rows persisted before ``taint_policy.history_taint_epoch`` receive a
    read-time amnesty for legacy taint poison: the synthetic
    ``legacy_missing_taint_metadata`` fallback and the anonymous manual
    escalation artifacts synthesized by :meth:`TurnTaintState.from_metadata`
    for truncated or omitted source summaries are dropped, and the row's taint
    contribution is recomputed from the explicitly attributed sources that
    remain. The persisted ``max_tier`` is deliberately not honored, because
    for pre-epoch rows it may encode nothing but re-baked legacy poison whose
    attribution was truncated away. Rows with no metadata, malformed metadata,
    or no surviving attributed sources contribute no taint at all.
    """
    if not isinstance(metadata, dict):
        return None
    raw_sources = metadata.get("sources")
    if not isinstance(raw_sources, list):
        return None
    state = TurnTaintState.empty()
    for raw_source in raw_sources:
        source = _source_from_metadata(
            raw_source,
            fallback_tier=SourceTrustTier.UNKNOWN_EXTERNAL,
        )
        if _is_amnestied_legacy_artifact(source):
            continue
        state = state.add_source(source, from_history=True)
    if not state.sources:
        return None
    return state.to_metadata()


def _is_amnestied_legacy_artifact(source: TaintSource) -> bool:
    """Return whether a pre-epoch source is legacy poison rather than provenance."""
    if _is_legacy_labeled_echo(source):
        return True
    return (
        source.source_type is TaintSourceType.MANUAL
        and not source.labels
        and source.source_id is None
    )


def _is_legacy_labeled_echo(source: TaintSource) -> bool:
    """Return whether a persisted source is a re-baked legacy fallback echo.

    A source carrying ``legacy_missing_taint_metadata`` inside persisted
    ``runtime_v1`` metadata is, by construction, a second-hand echo of some
    other row's read-time fallback: the label is only ever stamped on the
    synthetic source that :func:`_message_history_taint_metadata` fabricates at
    read time and then re-bakes into that turn's snapshot. It is never a
    first-hand attribution, so it can be dropped regardless of the row's
    timestamp.
    """
    return LEGACY_MISSING_TAINT_METADATA_LABEL in source.labels


def strip_legacy_labeled_echoes(metadata: object) -> TaintMetadata | None:
    """Drop re-baked legacy-fallback echoes from persisted history metadata.

    Applies to rows of any age (including post-epoch rows). Sources labeled
    ``legacy_missing_taint_metadata`` inside persisted ``runtime_v1`` metadata
    are second-hand echoes of another row's read-time fallback (see
    :func:`_is_legacy_labeled_echo`); they poison an active conversation into
    perpetual ``unknown_external`` and re-persist themselves. When at least one
    echo is present, it is dropped and the row's taint contribution is
    recomputed from the surviving sources. A stored ``max_tier`` above the
    retained sources is preserved unless echoes were the only sources, because
    a genuine source at that tier may have been truncated from the compact
    summaries. Anonymous manual escalation artifacts are intentionally NOT
    dropped here: in a post-epoch row they may legitimately stand in for a
    truncated genuine source, so the conservative reading is preserved.

    Returns the original metadata unchanged when no echo is present, so the
    first-hand missing-metadata fallback path (which is synthesized fresh, not
    read from stored ``sources``) is never weakened.
    """
    if not isinstance(metadata, dict):
        return None
    raw_sources = metadata.get("sources")
    if not isinstance(raw_sources, list):
        return cast("TaintMetadata", metadata)
    kept: list[TaintSource] = []
    dropped_echo = False
    for raw_source in raw_sources:
        source = _source_from_metadata(
            raw_source,
            fallback_tier=SourceTrustTier.UNKNOWN_EXTERNAL,
        )
        if _is_legacy_labeled_echo(source):
            dropped_echo = True
            continue
        kept.append(source)
    if not dropped_echo:
        return cast("TaintMetadata", metadata)
    state = TurnTaintState.empty()
    for source in kept:
        state = state.add_source(source, from_history=True)
    if not state.sources:
        return None
    persisted_max_tier = TurnTaintState.from_metadata(metadata).max_tier
    if persisted_max_tier > state.max_tier:
        state = replace(state, max_tier=persisted_max_tier)
    return state.to_metadata()


def merge_history_taint(messages: Sequence[object]) -> TurnTaintState:
    """Build an initial turn state from included message history."""
    state = TurnTaintState.empty()
    for message in messages:
        metadata = getattr(message, "taint_metadata", None)
        if metadata is None:
            continue
        history_state = TurnTaintState.from_metadata(metadata, from_history=True)
        for source in history_state.sources:
            state = state.add_source(source, from_history=True)
        if history_state.max_tier > state.max_tier:
            state = replace(state, max_tier=history_state.max_tier)
        if history_state.history_high_taint_present:
            state = replace(state, history_high_taint_present=True)
    return state


def _source_from_metadata(
    raw_source: object,
    *,
    fallback_tier: SourceTrustTier,
) -> TaintSource:
    if not isinstance(raw_source, dict):
        return TaintSource(
            source_type=TaintSourceType.MANUAL,
            source_id=None,
            tier=fallback_tier,
            labels=frozenset(),
            reason="Malformed source summary in taint metadata.",
        )

    try:
        source_type = TaintSourceType(str(raw_source.get("source_type")))
    except ValueError:
        source_type = TaintSourceType.MANUAL
    try:
        tier = SourceTrustTier.from_value(raw_source.get("tier"))
    except (TypeError, ValueError):
        tier = fallback_tier
    raw_labels = raw_source.get("labels")
    labels = (
        frozenset(str(label) for label in raw_labels)
        if isinstance(raw_labels, list)
        else frozenset()
    )
    source_id = raw_source.get("source_id")
    reason = raw_source.get("reason")
    return TaintSource(
        source_type=source_type,
        source_id=str(source_id) if source_id is not None else None,
        tier=tier,
        labels=labels,
        reason=str(reason) if reason else "Persisted taint source.",
    )


def coerce_taint_metadata(value: object) -> TaintMetadata | None:
    """Return a JSON-compatible taint metadata mapping, if present."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return TurnTaintState.malformed_history_state().to_metadata()
    return cast("TaintMetadata", value)
