"""The model-selection envelope for one run.

A profile says how the agent operates; a model tier says how inference runs.
This module carries the second choice through a run: what was requested, by
whom, what it resolved to, and -- once Auto routing lands -- how the routing
went. It is resolved once per turn, before any model-dependent preparation, and
frozen for the rest of the run.

Admission is a gate of its own rather than a tool-policy rule. The policy
builder injects a synthetic self-delegation ALLOW above a profile's own
``tools_policy``, and that rule assumes self-delegation cannot escalate -- which
a spend-selecting argument breaks. ``ToolMatcher`` also matches only arguments
that are *present*, so a rule could never authorize the tier a request resolved
to when it named none. :func:`resolve_model_selection` is therefore the one
place a tier becomes permitted, and it authorizes the resolved tier rather than
the supplied one.

The two eligibility lists say who may ask for what. A user's explicit selection
is authorized by ``allowed_model_tiers``: choosing to spend more on one's own
request is the authorization. A model-composed request -- a
``delegate_to_service`` ``model_tier`` argument, and Auto routing later -- is
authorized by ``auto_model_tiers``, which is a subset. A profile that names an
inline model instead of a tier admits no selection at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Self

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.config_models import ModelTierConfig, ServiceProfile

__all__ = [
    "ModelSelectionRequest",
    "ModelTierEligibility",
    "ModelTierNotPermitted",
    "ModelTierOption",
    "ResolvedModelSelection",
    "RoutingOutcome",
    "SelectionSource",
    "resolve_model_selection",
]

type SelectionSource = Literal["user", "model", "default"]
"""Who chose the tier. ``"user"`` is an authenticated person's own selection,
``"model"`` a selection composed by a model (a delegation argument), and
``"default"`` no selection at all -- the profile's configured tier. Auto routing
adds ``"auto"`` here."""

type RoutingOutcome = Literal["not_requested", "decided", "timeout", "invalid"]
"""How Auto routing went, recorded apart from the resolved tier so a classifier
outage cannot masquerade as a run of confident decisions. Only
``"not_requested"`` is produced until routing lands."""


class ModelTierNotPermitted(ValueError):
    """A run asked for a tier the target profile does not admit from that source.

    Carries the parts a caller needs to render its own refusal -- the web layer
    turns it into a 400, the delegation tool into an error result -- so neither
    has to re-derive what was eligible.
    """

    def __init__(
        self,
        message: str,
        *,
        profile_id: str,
        requested_tier: str | None,
        source: SelectionSource,
        eligible_tiers: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.profile_id = profile_id
        self.requested_tier = requested_tier
        self.source = source
        self.eligible_tiers = eligible_tiers


@dataclass(frozen=True)
class ModelTierOption:
    """One tier a profile can be run on, as a surface should present it.

    ``label`` is the user-facing name (``Max`` for ``frontier``): config
    vocabulary and product vocabulary are deliberately allowed to differ, and a
    surface that showed the config name would leak the operator's spelling into
    the product.
    """

    id: str
    label: str
    description: str | None = None


@dataclass(frozen=True)
class ModelTierEligibility:
    """What a profile may be run on, and by whom.

    The default -- no tiers at all -- is a profile pinned to an inline model,
    which is what every profile whose runtime is coupled to one provider or one
    API surface stays at.
    """

    default_tier: str | None = None
    selectable: tuple[ModelTierOption, ...] = ()
    """Tiers a user may explicitly select, in configured tier order."""
    auto: frozenset[str] = frozenset()
    """Tiers a model may select without a confirmation. A subset of
    ``selectable``, enforced at startup."""

    @property
    def selectable_ids(self) -> tuple[str, ...]:
        return tuple(option.id for option in self.selectable)

    @classmethod
    def from_profile(
        cls,
        profile_conf: ServiceProfile,
        model_tiers: Mapping[str, ModelTierConfig],
    ) -> Self:
        """Project a validated profile's tier configuration.

        Assumes ``validate_profile_model_tier`` has already run: it is what
        rejects an unknown tier or an eligibility list reaching past what the
        profile may be explicitly run on, and doing it again here would state
        the same rules in a second place.

        Ordering follows the ``model_tiers`` map rather than the profile's own
        list, so every surface presents the tiers in one configured order --
        the capability ordering an operator wrote the map in.
        """
        default_tier = profile_conf.processing_config.model_tier
        if default_tier is None:
            return cls()
        allowed = profile_conf.allowed_model_tiers
        selectable_ids = [default_tier] if allowed is None else list(allowed)
        selectable = tuple(
            ModelTierOption(
                id=tier_name,
                label=tier.label or tier_name,
                description=tier.description,
            )
            for tier_name, tier in model_tiers.items()
            if tier_name in selectable_ids
        )
        auto = profile_conf.auto_model_tiers
        return cls(
            default_tier=default_tier,
            selectable=selectable,
            auto=frozenset(auto if auto is not None else [default_tier]),
        )


@dataclass(frozen=True)
class ModelSelectionRequest:
    """A tier asked for, and who asked. ``tier=None`` asks for the default."""

    tier: str | None
    source: SelectionSource


@dataclass(frozen=True)
class ResolvedModelSelection:
    """The tier a run is frozen to, with the provenance of the choice.

    ``tier is None`` means the profile's inline model: a pinned profile has no
    tier to name, and saying so explicitly keeps "no selection" distinguishable
    from "the default tier".
    """

    tier: str | None
    requested: str | None
    source: SelectionSource
    routing_outcome: RoutingOutcome = "not_requested"

    @classmethod
    def unselected(cls, default_tier: str | None) -> Self:
        """The profile's own tier, chosen by nobody."""
        return cls(
            tier=default_tier,
            requested=None,
            source="default",
            routing_outcome="not_requested",
        )

    def to_json(self) -> dict[str, str | None]:
        """Serialize for persistence on a queued run.

        A queued run persists the selection so a restart or a configuration
        deployment cannot silently change the models of a run already created.
        """
        return {
            "tier": self.tier,
            "requested": self.requested,
            "source": self.source,
            "routing_outcome": self.routing_outcome,
        }

    @classmethod
    # ast-grep-ignore: no-dict-any - JSON column payload, validated below
    def from_json(cls, data: Mapping[str, Any]) -> Self:
        """Rehydrate a persisted selection, refusing one that is not one.

        Nothing downstream can act sensibly on a half-understood envelope: a
        selection that failed to load is not "no selection", it is a run whose
        models are unknown, so this raises rather than falling back.
        """
        source = data.get("source")
        if source not in {"user", "model", "default"}:
            msg = f"Persisted model selection has unknown source {source!r}."
            raise ValueError(msg)
        outcome = data.get("routing_outcome", "not_requested")
        if outcome not in {"not_requested", "decided", "timeout", "invalid"}:
            msg = f"Persisted model selection has unknown routing outcome {outcome!r}."
            raise ValueError(msg)
        tier = data.get("tier")
        requested = data.get("requested")
        if not (tier is None or isinstance(tier, str)) or not (
            requested is None or isinstance(requested, str)
        ):
            msg = "Persisted model selection has a non-string tier."
            raise ValueError(msg)
        return cls(
            tier=tier,
            requested=requested,
            source=source,
            routing_outcome=outcome,
        )


def resolve_model_selection(
    eligibility: ModelTierEligibility,
    request: ModelSelectionRequest | None,
    *,
    profile_id: str,
) -> ResolvedModelSelection:
    """Admit a requested tier, or refuse it. The single tier-admission gate.

    Asking for the tier the profile already defaults to is always fine and
    keeps its stated source: "I chose Standard" and "I chose nothing" differ in
    what they authorize later even where they run the same models.
    """
    if request is None or request.tier is None:
        return ResolvedModelSelection.unselected(eligibility.default_tier)

    if eligibility.default_tier is None:
        msg = (
            f"Profile '{profile_id}' does not support model tier selection: it "
            "runs on the model configured on the profile itself. Omit the tier."
        )
        raise ModelTierNotPermitted(
            msg,
            profile_id=profile_id,
            requested_tier=request.tier,
            source=request.source,
            eligible_tiers=(),
        )

    eligible = _eligible_tiers(eligibility, request.source)
    if request.tier not in eligible:
        raise ModelTierNotPermitted(
            _refusal_message(profile_id, request, eligible),
            profile_id=profile_id,
            requested_tier=request.tier,
            source=request.source,
            eligible_tiers=eligible,
        )

    return ResolvedModelSelection(
        tier=request.tier,
        requested=request.tier,
        source=request.source,
        routing_outcome="not_requested",
    )


def _eligible_tiers(
    eligibility: ModelTierEligibility,
    source: SelectionSource,
) -> tuple[str, ...]:
    """The tiers a request from *source* may name."""
    if source == "user":
        return eligibility.selectable_ids
    if source == "model":
        return tuple(
            option.id
            for option in eligibility.selectable
            if option.id in eligibility.auto
        )
    # A "default"-sourced request naming a tier is only coherent when it names
    # the default; anything else is a selection claiming to be no selection.
    return () if eligibility.default_tier is None else (eligibility.default_tier,)


def _refusal_message(
    profile_id: str,
    request: ModelSelectionRequest,
    eligible: tuple[str, ...],
) -> str:
    eligible_text = ", ".join(eligible) or "(none)"
    if request.source == "model":
        return (
            f"Profile '{profile_id}' does not accept model tier "
            f"'{request.tier}' from another profile. Tiers it may be delegated "
            f"to at: {eligible_text}. Omit model_tier to use its default."
        )
    return (
        f"Profile '{profile_id}' cannot be run on model tier "
        f"'{request.tier}'. Tiers it accepts: {eligible_text}."
    )


PINNED_ELIGIBILITY: ModelTierEligibility = ModelTierEligibility()
"""Shared eligibility for a profile that names an inline model.

A module-level constant so ``ProcessingServiceConfig`` and its remote
counterpart can default to it without each spelling the pinned case for
themselves.
"""
