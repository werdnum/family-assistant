"""Storage table for asynchronous profile delegation runs."""

from typing import Literal, NotRequired, TypedDict

from sqlalchemy import JSON, Column, DateTime, Index, Integer, String, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import functions as func

from family_assistant.storage.base import metadata

DelegationRunStatus = Literal[
    "queued",
    "running",
    "awaiting_remote",
    "completed",
    "failed",
]

TERMINAL_DELEGATION_STATUSES: frozenset[DelegationRunStatus] = frozenset({
    "completed",
    "failed",
})

DelegationLocalFailureKind = Literal[
    "timeout",
    "transport",
    "remote_status",
    "empty_completion",
    "internal_error",
    "not_pollable",
    "stranded",
]
"""Why *Family Assistant* failed a run, which is not why the provider did.

``status`` records the decision this application took; this records what drove
it. ``timeout`` and ``transport`` are local calls made without the provider
agreeing, ``remote_status`` is a terminal status the provider reported,
``empty_completion`` is a provider "completed" that carried nothing,
``internal_error`` is a fault on this side, ``not_pollable`` is a target that
stopped being pollable under a live run, and ``stranded`` is the cleanup
reaper failing a run that never reached a provider at all.
"""

RECONCILABLE_FAILURE_KINDS: frozenset[DelegationLocalFailureKind] = frozenset({
    "timeout",
    "transport",
    "remote_status",
    "empty_completion",
    "internal_error",
})
"""Failure kinds where the remote run may still be running, or may have finished.

Everything here is a decision taken while the provider was, as far as we know,
still holding the run -- including ``remote_status``, because a reported
``cancelled`` or ``failed`` has been observed to change back. The two excluded
kinds are the ones with nothing to re-read: ``not_pollable`` leaves no service
able to observe the run, and ``stranded`` never reached a provider.
"""

DelegationNotifyStage = Literal[
    "initial",
    "failed_forward",
    "canned_pending",
    "gave_up",
]
"""How far a terminal run has got through trying to reach the requester.

``initial`` is the run's own result. ``failed_forward`` means that could not be
delivered and the delegating profile was asked what to do instead.
``canned_pending`` means that answer could not be delivered either and only the
short standard notice is left. ``gave_up`` means nothing reached them.

The stage is what bounds the work: it is committed when entered, before the
send it describes, so a retry resumes at the send that has not yet succeeded
rather than repeating one already known to fail.
"""

AuthenticatedSiteTaskStatus = Literal[
    "running",
    "completed",
    "login_required",
    "blocked_by_scope",
    "review_blocked",
    "handoff_pending",
    "approval_pending",
    "needs_human",
    "site_changed",
    "failed",
]
"""The actionable outcomes ``run_authenticated_site_task`` reports.

``handoff_pending`` and ``approval_pending`` are the two *parked* outcomes: the
run is terminal but its browser session stays alive, confined and fail-closed,
until the resume handle is consumed or the lifetime backstop reclaims it.
"""

PARKED_AUTHENTICATED_STATUSES: frozenset[AuthenticatedSiteTaskStatus] = frozenset({
    "handoff_pending",
    "approval_pending",
})


class AuthenticatedSiteEnvelope(TypedDict):
    """The authenticated-site state carried on a delegation run row.

    One column holds both halves because they have one lifetime: the session
    binding is what a resume rebinds, and the typed result is what a completed
    run hands back. Nothing secret goes in here -- no jar contents, no
    credential, no handback token.
    """

    site_id: str
    status: AuthenticatedSiteTaskStatus
    # The browser-server session this run owns. Retained on a parked run so a
    # resume rebinds the same session instead of creating a second one, and
    # cleared when the session is closed.
    session_id: NotRequired[str | None]
    summary: NotRequired[str]
    detail: NotRequired[str]
    handoff_url: NotRequired[str | None]
    final_url: NotRequired[str | None]
    warnings: NotRequired[list[str]]
    # Which acting user and caller profile the run was authorized for, so a
    # resume can be re-authorized rather than trusted.
    acting_user: NotRequired[str | None]
    caller_profile_id: NotRequired[str | None]


delegation_runs_table = Table(
    "delegation_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("delegation_id", String(100), nullable=False, unique=True, index=True),
    Column("task_id", String(100), nullable=False, unique=True),
    Column("status", String(50), nullable=False),
    Column("source_profile_id", String(100), nullable=False),
    Column("target_service_id", String(100), nullable=False),
    Column("interface_type", String(50), nullable=False),
    Column("conversation_id", String(255), nullable=False),
    Column("user_id", String(255), nullable=True),
    Column("user_name", String(255), nullable=True),
    Column("source_turn_id", String(100), nullable=True),
    Column("source_subconversation_id", String(36), nullable=True),
    Column("subconversation_id", String(36), nullable=False),
    Column("request_text", Text, nullable=False),
    Column(
        "content_parts_json", JSON().with_variant(JSONB, "postgresql"), nullable=False
    ),
    Column("taint_state_json", JSON().with_variant(JSONB, "postgresql"), nullable=True),
    # The model-selection envelope resolved when the run was created. Persisted
    # rather than re-resolved, so a restart or a configuration deployment cannot
    # silently change the models of a run somebody already authorized. Null for
    # runs created before tier selection existed, and for a target that admits
    # none.
    Column(
        "model_selection_json", JSON().with_variant(JSONB, "postgresql"), nullable=True
    ),
    Column("handed_off_at", DateTime(timezone=True), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=True, onupdate=func.now()),
    Column("result_text", Text, nullable=True),
    Column(
        "result_attachment_ids_json",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    ),
    Column("result_message_internal_id", Integer, nullable=True),
    Column("error", Text, nullable=True),
    Column("notified_at", DateTime(timezone=True), nullable=True),
    Column("notify_stage", String(20), nullable=False, server_default="initial"),
    Column("notify_attempts", Integer, nullable=False, server_default="0"),
    # Why delivery last failed, kept apart from ``error`` so a completed
    # run's result is not overwritten by a transport problem.
    Column("notify_error", Text, nullable=True),
    # When delivery first failed, so a transient failure that never recovers
    # can be reclassified as permanent instead of retrying for as long as the
    # outage lasts.
    Column("notify_first_failed_at", DateTime(timezone=True), nullable=True),
    Column("notify_last_failed_at", DateTime(timezone=True), nullable=True),
    # Remote (A2A) task identifiers for the submit-then-poll async path. Null
    # for local delegations, which have no remote task to poll.
    Column("remote_task_id", String(255), nullable=True),
    Column("remote_context_id", String(255), nullable=True),
    Column("poll_attempts", Integer, nullable=False, server_default="0"),
    # Why this application failed the run, kept apart from ``error`` (which is
    # prose for the user) and from ``remote_status`` (which is the provider's
    # own account). Null for a run that did not fail.
    Column("local_failure_kind", String(32), nullable=True),
    # Cancellation, split into the two facts it was previously collapsed from:
    # when we asked, and when a later read actually showed the provider had
    # done it. A run may have the first without ever getting the second, and
    # must not claim to have been cancelled on the strength of the request.
    Column("cancel_requested_at", DateTime(timezone=True), nullable=True),
    Column("cancel_confirmed_at", DateTime(timezone=True), nullable=True),
    # The last remote read: the provider's verbatim status, when we took the
    # reading, and the bounded record (see ``RemoteObservation.to_metadata``).
    Column("remote_status", String(64), nullable=True),
    Column("remote_observed_at", DateTime(timezone=True), nullable=True),
    Column(
        "remote_observation_json",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    ),
    # Reconciliation bookkeeping. ``reconciled_at`` is the stable-terminal
    # marker: set when the run settled or reconciliation gave up on it, and
    # the reason a settled run is never re-read.
    Column("reconcile_attempts", Integer, nullable=False, server_default="0"),
    Column("reconciled_at", DateTime(timezone=True), nullable=True),
    # When a locally failed run was recovered from a late provider success.
    # Also the exactly-once guard: recovery is conditioned on it being NULL.
    Column("late_recovered_at", DateTime(timezone=True), nullable=True),
    # The typed AuthenticatedSiteTaskResult for a run started by
    # run_authenticated_site_task, plus the session binding it parks. Persisted
    # here because the background completion machinery carries only text and
    # attachments and its notification is advisory: the caller retrieves the
    # typed result by presenting the run's opaque handle, never by parsing
    # notification prose. Null for every other delegation.
    Column(
        "authenticated_site_json",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Index(
        "ix_delegation_runs_conversation_created",
        "conversation_id",
        "created_at",
    ),
    Index(
        "ix_delegation_runs_status_created",
        "status",
        "created_at",
    ),
    # Drives the reconciliation sweep, which asks for failed runs that have
    # not settled yet, newest first.
    Index(
        "ix_delegation_runs_reconciled_completed",
        "reconciled_at",
        "completed_at",
    ),
    # At most one non-terminal (queued/running/awaiting_remote) run may target a
    # given subconversation. A fresh delegation always mints a unique
    # subconversation_id, so this never constrains the normal path; it atomically
    # serializes resumes, which reuse a prior run's subconversation_id, so two
    # concurrent resumes cannot both create active runs that interleave in one
    # delegated history. Terminal statuses are excluded so a completed run can be
    # resumed. Keep the predicate in sync with TERMINAL_DELEGATION_STATUSES.
    Index(
        "uq_delegation_runs_active_subconversation",
        "subconversation_id",
        unique=True,
        sqlite_where=text("status NOT IN ('completed', 'failed')"),
        postgresql_where=text("status NOT IN ('completed', 'failed')"),
    ),
)
