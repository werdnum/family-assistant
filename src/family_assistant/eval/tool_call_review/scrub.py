"""Structural privacy chokepoint for history-derived eval material.

Two independent tools live here, both operating on the harness's committed and
private corpora:

* :class:`TaskTemplate` and :meth:`TaskTemplate.validate_committable` — the
  privacy chokepoint for the history-template quarry. A template is a structured
  record of *enumerated fields only* (a closed-vocabulary intent, registry tool
  names, argument shapes rather than values, a sink class, a taint tier, and a
  fixed content-kind tag). No field admits verbatim household text, so the
  boundary is structural: :meth:`validate_committable` fails closed on any
  free-text or unrecognized value, and a template that survives it carries no
  private text because there was no field for private text to travel in. The
  maintainer skim is a second layer on top of the validator, not the boundary.

* :func:`pseudonymize_case` — an on-demand, deterministic pseudonymizer for the
  identifiers (emails, phone numbers, URLs, long numeric ids) in a captured raw
  case. Evaluation always runs on the raw corpus; the pseudonymized copy is
  generated per case only when a capture needs to be quoted or shared. It is
  deterministic so the same identifier maps to the same pseudonym every time,
  keeping a quoted excerpt internally consistent.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from family_assistant.eval.tool_call_review.schema import (
    ConversationPayload,
    EvalCase,
    ToolResolutionError,
    resolve_tool_descriptor,
)
from family_assistant.security.taint import SinkClass, SourceTrustTier

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.tools.metadata import ToolDescriptor

__all__ = [
    "CONTENT_KINDS",
    "INTENT_CATEGORIES",
    "JSON_TYPE_NAMES",
    "Pseudonymizer",
    "TaskTemplate",
    "TemplatePrivacyError",
    "pseudonymize_case",
    "pseudonymize_text",
    "task_template_from_case",
]

# --- Closed vocabularies -----------------------------------------------------

# Intent categories are deliberately coarse: the point is a distribution of task
# *shapes*, not a taxonomy of household activity. Add entries when a genuine new
# shape appears rather than encoding specifics.
INTENT_CATEGORIES: frozenset[str] = frozenset({
    "send_message",
    "schedule_event",
    "create_reminder",
    "summarize_content",
    "record_note",
    "read_note",
    "delegate_task",
    "answer_question",
    "smart_home_action",
    "browse_web",
    "manage_automation",
    "manage_calendar",
})

# Content-kind tags describe the *kind* of data a turn handled, never its
# content. Each is a fixed slug drawn from this list.
CONTENT_KINDS: frozenset[str] = frozenset({
    "school-newsletter-summary",
    "known-contact-message",
    "calendar-invite",
    "email-thread",
    "web-page-excerpt",
    "shopping-list",
    "household-note",
    "reminder-text",
    "none",
})

JSON_TYPE_NAMES: frozenset[str] = frozenset({
    "string",
    "number",
    "integer",
    "boolean",
    "object",
    "array",
    "null",
})

_SINK_CLASS_VALUES: frozenset[str] = frozenset(item.value for item in SinkClass)
_TAINT_TIER_VALUES: frozenset[str] = frozenset(
    item.config_value for item in SourceTrustTier
)
_BOUNDARY_VALUES: frozenset[str] = frozenset({"conversation", "browser", "derivation"})

# A field may hold an enumerated value or an explicit placeholder token such as
# ``<unknown>`` — never free text. Placeholders let an abstraction pass leave a
# field it could not classify empty of content without smuggling text into it.
_PLACEHOLDER_RE = re.compile(r"^<[a-z0-9_]+>$")
# Argument keys are schema-declared identifiers, not household text.
_ARG_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Template ids are author-supplied slugs; constrained so an id cannot become a
# free-text field of its own.
_TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")


class TemplatePrivacyError(Exception):
    """A task template carries a value that is not enumerated or a placeholder.

    Raised by :meth:`TaskTemplate.validate_committable`. The export aborts rather
    than committing a template whose fields might contain verbatim household
    text — the chokepoint fails closed.
    """


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER_RE.match(value))


class TaskTemplate(BaseModel):
    """An enumerated, committable abstraction of a historical task shape.

    Every field is validated by :meth:`validate_committable` against a closed
    vocabulary or the placeholder-token rule. The model itself stores plain
    strings so a partially malformed template can be *constructed* and then
    *rejected* by the validator, which is the fail-closed boundary — not the
    type system.
    """

    model_config = ConfigDict(extra="forbid")

    template_id: str
    boundary: str = "conversation"
    intent_category: str
    tool_names: list[str] = Field(default_factory=list)
    argument_shapes: dict[str, str] = Field(default_factory=dict)
    sink_class: str
    taint_tier: str
    content_kind: str = "none"

    def validate_committable(
        self,
        *,
        descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
    ) -> None:
        """Fail closed unless every field is enumerated or a placeholder token.

        Collects every violating field before raising so a maintainer sees all
        problems at once. Tool names resolve against the live registry (or the
        supplied ``descriptor_registry``): a name that does not resolve is a
        violation, because an unresolved name is exactly where a free-text value
        would hide.
        """
        errors: list[str] = []

        if not _TEMPLATE_ID_RE.match(self.template_id):
            errors.append(
                f"template_id {self.template_id!r} is not a slug "
                "([a-z0-9][a-z0-9-]*, <=128 chars)"
            )
        if self.boundary not in _BOUNDARY_VALUES:
            errors.append(f"boundary {self.boundary!r} is not a known boundary")
        if not (
            self.intent_category in INTENT_CATEGORIES
            or _is_placeholder(self.intent_category)
        ):
            errors.append(
                f"intent_category {self.intent_category!r} is not in the closed "
                "vocabulary or a placeholder"
            )
        if not (
            self.sink_class in _SINK_CLASS_VALUES or _is_placeholder(self.sink_class)
        ):
            errors.append(f"sink_class {self.sink_class!r} is not a known sink class")
        if not (
            self.taint_tier in _TAINT_TIER_VALUES or _is_placeholder(self.taint_tier)
        ):
            errors.append(f"taint_tier {self.taint_tier!r} is not a known taint tier")
        if not (
            self.content_kind in CONTENT_KINDS or _is_placeholder(self.content_kind)
        ):
            errors.append(
                f"content_kind {self.content_kind!r} is not a fixed content-kind tag"
            )

        for name in self.tool_names:
            try:
                resolve_tool_descriptor(name, registry=descriptor_registry)
            except ToolResolutionError:
                errors.append(f"tool_name {name!r} does not resolve in the registry")

        for key, shape in self.argument_shapes.items():
            if not _ARG_KEY_RE.match(key):
                errors.append(f"argument key {key!r} is not a schema identifier")
            if not (shape in JSON_TYPE_NAMES or _is_placeholder(shape)):
                errors.append(
                    f"argument {key!r} shape {shape!r} is not a JSON type name"
                )

        if errors:
            raise TemplatePrivacyError(
                f"Template {self.template_id!r} is not committable: "
                + "; ".join(errors)
            )


def _json_type_name(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return "null"


def task_template_from_case(
    case: EvalCase,
    *,
    template_id: str | None = None,
    intent_category: str = "<unknown>",
    content_kind: str = "none",
) -> TaskTemplate:
    """Abstract a conversation case into an enumerated task template.

    Argument *shapes* (key and JSON type) are derived from the case's arguments;
    values never cross into the template. Intent and content-kind are not
    recoverable from a case alone, so they default to a placeholder and ``none``
    respectively — a classification pass overrides them with closed-vocabulary
    values before the template is committed. The result is not automatically
    committable; the caller must run :meth:`TaskTemplate.validate_committable`.
    """
    payload = case.payload
    if not isinstance(payload, ConversationPayload):
        raise ValueError(
            "task_template_from_case only abstracts conversation cases; "
            f"case {case.id!r} has a {type(payload).__name__} payload."
        )
    return TaskTemplate(
        template_id=template_id or f"tmpl-{_stable_hex(case.id, length=12)}",
        boundary=case.boundary,
        intent_category=intent_category,
        tool_names=[payload.tool_name],
        argument_shapes={
            key: _json_type_name(value) for key, value in payload.arguments.items()
        },
        sink_class=payload.sink_class,
        taint_tier=_case_taint_tier(payload),
        content_kind=content_kind,
    )


def _case_taint_tier(payload: ConversationPayload) -> str:
    tier = payload.taint_state.get("max_tier")
    if isinstance(tier, str) and tier in _TAINT_TIER_VALUES:
        return tier
    return "<unknown>"


# --- Deterministic pseudonymizer --------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"\bhttps?://[^\s\"'<>)\]]+", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<![\w.])\+?\d[\d()\-.\s]{6,}\d(?![\w.])")
# A bare numeric identifier (chat id, account number) of four or more digits.
_NUMERIC_ID_RE = re.compile(r"(?<![\w.])\d{4,}(?![\w.])")


def _stable_hex(*parts: str, length: int = 8) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


class Pseudonymizer:
    """Deterministically replace identifiers in captured text with pseudonyms.

    Determinism is keyed by ``seed`` and the identifier's literal value, so the
    same value always maps to the same pseudonym within and across runs. This is
    for quoting only — evaluation runs on the raw corpus, whose byte fidelity the
    destination-echo signal depends on.
    """

    def __init__(
        self,
        *,
        seed: str = "review-eval",
        literals: Mapping[str, str] | None = None,
    ) -> None:
        self._seed = seed
        # Explicit literals (names, street addresses) a maintainer supplies for a
        # given case, matched verbatim before the pattern passes run.
        self._literals = dict(literals or {})

    def scrub_text(self, text: str) -> str:
        """Return ``text`` with detected identifiers replaced by pseudonyms."""
        for literal, replacement in sorted(
            self._literals.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if literal:
                text = text.replace(literal, replacement)
        text = _EMAIL_RE.sub(self._email, text)
        text = _URL_RE.sub(self._url, text)
        text = _PHONE_RE.sub(self._phone, text)
        text = _NUMERIC_ID_RE.sub(self._numeric_id, text)
        return text

    def pseudonymize_case(self, case: EvalCase) -> EvalCase:
        """Return a copy of ``case`` with identifiers pseudonymized throughout.

        The whole serialized case is walked, so identifiers in messages,
        arguments, guidance, and policy contexts are all covered. The case id is
        left intact so a quoted copy still points back at the raw case.
        """
        raw = case.model_dump(mode="json")
        scrubbed = self._walk(raw, skip_keys={"id"})
        return EvalCase.model_validate(scrubbed)

    def _walk(
        self, value: object, *, skip_keys: frozenset[str] | set[str] = frozenset()
    ) -> object:
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, dict):
            return {
                key: (sub_value if key in skip_keys else self._walk(sub_value))
                for key, sub_value in value.items()
            }
        if isinstance(value, list):
            return [self._walk(item) for item in value]
        return value

    def _email(self, match: re.Match[str]) -> str:
        token = _stable_hex(self._seed, "email", match.group(0), length=8)
        return f"person-{token}@example.invalid"

    def _url(self, match: re.Match[str]) -> str:
        token = _stable_hex(self._seed, "url", match.group(0), length=8)
        return f"https://host-{token}.invalid/redacted"

    def _phone(self, match: re.Match[str]) -> str:
        token = _stable_hex(self._seed, "phone", match.group(0), length=7)
        digits = str(int(token, 16))[:7].rjust(7, "0")
        return f"+1000{digits}"

    def _numeric_id(self, match: re.Match[str]) -> str:
        token = _stable_hex(self._seed, "id", match.group(0), length=8)
        return str(int(token, 16))[:10].rjust(10, "0")


def pseudonymize_text(text: str, *, seed: str = "review-eval") -> str:
    """Deterministically pseudonymize identifiers in a single string."""
    return Pseudonymizer(seed=seed).scrub_text(text)


def pseudonymize_case(
    case: EvalCase,
    *,
    seed: str = "review-eval",
    literals: Mapping[str, str] | None = None,
) -> EvalCase:
    """Deterministically pseudonymize the identifiers throughout a raw case."""
    return Pseudonymizer(seed=seed, literals=literals).pseudonymize_case(case)
