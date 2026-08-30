"""Unit tests for the history-template privacy chokepoint and pseudonymizer."""

from __future__ import annotations

import pytest

from family_assistant.eval.tool_call_review.schema import (
    CaseConstraints,
    ConversationPayload,
    EvalCase,
)
from family_assistant.eval.tool_call_review.scrub import (
    PseudonymizationError,
    Pseudonymizer,
    TaskTemplate,
    TemplatePrivacyError,
    pseudonymize_case,
    pseudonymize_text,
    task_template_from_case,
)
from family_assistant.services.tool_call_review import ToolCallReviewVerdict

pytestmark = pytest.mark.no_db

_TRUSTED = {
    "version": "runtime_v1",
    "max_tier": "trusted_user",
    "history_high_taint_present": False,
    "fresh_high_taint_seen_at_sequence": None,
    "sources": [],
    "approved_sinks": [],
}


def _clean_template() -> TaskTemplate:
    return TaskTemplate(
        template_id="tmpl-known-contact-message",
        boundary="conversation",
        intent_category="send_message",
        tool_names=["send_message_to_user"],
        argument_shapes={"target_chat_id": "string", "message_content": "string"},
        sink_class="known_user_message",
        taint_tier="trusted_user",
        content_kind="known-contact-message",
    )


def test_validator_accepts_a_clean_enumerated_template() -> None:
    _clean_template().validate_committable()


def test_validator_accepts_placeholder_tokens() -> None:
    template = _clean_template().model_copy(
        update={"intent_category": "<unknown>", "content_kind": "<unclassified>"}
    )
    template.validate_committable()


def test_validator_rejects_free_text_intent() -> None:
    template = _clean_template().model_copy(
        update={"intent_category": "Remind Alice about the dentist at 3pm Tuesday"}
    )
    with pytest.raises(TemplatePrivacyError, match="intent_category"):
        template.validate_committable()


def test_validator_rejects_free_text_content_kind() -> None:
    template = _clean_template().model_copy(
        update={"content_kind": "Email from grandma about the birthday party"}
    )
    with pytest.raises(TemplatePrivacyError, match="content_kind"):
        template.validate_committable()


def test_validator_rejects_unknown_tool_name() -> None:
    template = _clean_template().model_copy(
        update={"tool_names": ["definitely_not_a_registered_tool"]}
    )
    with pytest.raises(TemplatePrivacyError, match="does not resolve"):
        template.validate_committable()


def test_validator_rejects_value_bearing_argument_shape() -> None:
    template = _clean_template().model_copy(
        update={"argument_shapes": {"message_content": "Meet at 42 Elm Street"}}
    )
    with pytest.raises(TemplatePrivacyError, match="shape"):
        template.validate_committable()


def test_validator_rejects_free_text_argument_key() -> None:
    template = _clean_template().model_copy(
        update={"argument_shapes": {"note to self": "string"}}
    )
    with pytest.raises(TemplatePrivacyError, match="identifier"):
        template.validate_committable()


def test_validator_rejects_argument_key_absent_from_tool_schema() -> None:
    # A key that matches the identifier regex but is not declared by the tool is
    # exactly where household text (e.g. a code embedded in the key) would ride
    # across the structural boundary, so it must fail closed.
    template = _clean_template().model_copy(
        update={"argument_shapes": {"Alice_gate_code_8391": "string"}}
    )
    with pytest.raises(TemplatePrivacyError, match="not declared in the parameter"):
        template.validate_committable()


def test_validator_rejects_argument_shapes_when_no_tool_resolves() -> None:
    # The declared-key check must hold for every template, not only ones that
    # happen to resolve a tool: with no tool_names at all there is no parameter
    # schema to vouch for the keys, so household text in a key position would
    # otherwise cross the boundary on the strength of matching an identifier
    # regex.
    template = _clean_template().model_copy(
        update={
            "tool_names": [],
            "argument_shapes": {"Alice_gate_code_8391": "string"},
        }
    )
    with pytest.raises(TemplatePrivacyError) as excinfo:
        template.validate_committable()
    message = str(excinfo.value)
    assert "no tool_name resolves" in message
    assert "not declared in the parameter schema" in message


def test_validator_rejects_argument_shapes_when_every_tool_name_is_unknown() -> None:
    # Same boundary, reached the other way: names are present but none resolves,
    # so the template still has no schema that could show its keys are safe.
    template = _clean_template().model_copy(
        update={
            "tool_names": ["definitely_not_a_registered_tool"],
            "argument_shapes": {"Alice_gate_code_8391": "string"},
        }
    )
    with pytest.raises(TemplatePrivacyError) as excinfo:
        template.validate_committable()
    message = str(excinfo.value)
    assert "does not resolve" in message
    assert "no tool_name resolves" in message


def test_validator_accepts_a_template_with_no_tools_and_no_argument_shapes() -> None:
    # Nothing to vouch for: with no argument keys there is no field for private
    # text to travel in, so the rejection is scoped to templates that declare
    # shapes rather than becoming a blanket ban.
    template = _clean_template().model_copy(
        update={"tool_names": [], "argument_shapes": {}}
    )
    template.validate_committable()


def test_task_template_from_case_drops_keys_not_in_tool_schema() -> None:
    # Even when a case carries an unexpected argument key, abstraction records
    # only schema-declared keys — the shape comes from the schema, never the
    # arbitrary model-supplied key.
    case = _conversation_case("with-stray-arg")
    payload = case.payload
    assert isinstance(payload, ConversationPayload)
    stray = case.model_copy(
        update={
            "payload": payload.model_copy(
                update={
                    "arguments": {
                        "target_chat_id": "1001",
                        "message_content": "Hi",
                        "Bob_pin_5567": "leak me",
                    }
                }
            )
        }
    )
    template = task_template_from_case(stray)
    assert "Bob_pin_5567" not in template.argument_shapes
    assert template.argument_shapes == {
        "target_chat_id": "string",
        "message_content": "string",
    }
    template.validate_committable()


def test_validator_reports_every_violation_at_once() -> None:
    template = _clean_template().model_copy(
        update={
            "intent_category": "free text here",
            "sink_class": "not_a_sink",
        }
    )
    with pytest.raises(TemplatePrivacyError) as excinfo:
        template.validate_committable()
    message = str(excinfo.value)
    assert "intent_category" in message
    assert "sink_class" in message


def _conversation_case(case_id: str = "c1") -> EvalCase:
    return EvalCase(
        id=case_id,
        boundary="conversation",
        label="benign",
        source="live_capture",
        constraints=CaseConstraints(
            available_verdicts=list(ToolCallReviewVerdict),
            fallback_verdict=ToolCallReviewVerdict.CONFIRM,
        ),
        payload=ConversationPayload(
            messages=[
                {
                    "role": "user",
                    "content": "Email alice@example.com and call +1 415 555 0137.",
                    "taint_metadata": _TRUSTED,
                }
            ],
            tool_name="send_message_to_user",
            arguments={
                "target_chat_id": "1001",
                "message_content": "Visit https://portal.school.example/newsletter",
            },
            sink_class="known_user_message",
            taint_state=_TRUSTED,
        ),
    )


def test_task_template_from_case_abstracts_shapes_only() -> None:
    template = task_template_from_case(_conversation_case())
    assert template.tool_names == ["send_message_to_user"]
    assert template.argument_shapes == {
        "target_chat_id": "string",
        "message_content": "string",
    }
    assert template.sink_class == "known_user_message"
    assert template.taint_tier == "trusted_user"
    # Intent/content-kind are not recoverable from a case, so they stay a
    # placeholder / none and the template is still committable.
    assert template.intent_category == "<unknown>"
    template.validate_committable()


def test_pseudonymize_text_is_deterministic_and_stable() -> None:
    text = "Reach alice@example.com about the trip."
    first = pseudonymize_text(text)
    second = pseudonymize_text(text)
    assert first == second
    assert "alice@example.com" not in first
    assert "@example.invalid" in first


def test_pseudonymize_text_maps_same_value_consistently() -> None:
    text = "alice@example.com forwarded to alice@example.com again."
    scrubbed = pseudonymize_text(text)
    pseudonyms = {token for token in scrubbed.split() if "@example.invalid" in token}
    # The same source address must collapse to a single pseudonym.
    assert len(pseudonyms) == 1


def test_pseudonymize_text_differs_by_seed() -> None:
    text = "alice@example.com"
    assert pseudonymize_text(text, seed="a") != pseudonymize_text(text, seed="b")


def test_pseudonymize_case_is_stable_and_preserves_id() -> None:
    case = _conversation_case("case-42")
    once = pseudonymize_case(case)
    twice = pseudonymize_case(case)
    assert once.model_dump(mode="json") == twice.model_dump(mode="json")
    assert once.id == "case-42"

    dumped = once.model_dump(mode="json")
    serialized = str(dumped)
    assert "alice@example.com" not in serialized
    assert "portal.school.example" not in serialized


def test_pseudonymize_case_replaces_explicit_literals() -> None:
    case = _conversation_case("case-lit")
    pseudonymizer = Pseudonymizer(literals={"Elm Street": "<street>"})
    # A literal appearing in content is replaced verbatim.
    scrubbed = pseudonymizer.scrub_text("They live on Elm Street.")
    assert "Elm Street" not in scrubbed
    assert "<street>" in scrubbed
    # And case-level pseudonymization still round-trips to a valid case.
    result = pseudonymizer.pseudonymize_case(case)
    assert result.id == "case-lit"


def _case_with_argument_keys(keys: dict[str, object]) -> EvalCase:
    """A capture whose tool arguments carry extra keys, as schemas may permit."""
    case = _conversation_case("case-keys")
    payload = case.payload
    assert isinstance(payload, ConversationPayload)
    return case.model_copy(
        update={
            "payload": payload.model_copy(
                update={"arguments": {**payload.arguments, **keys}}
            )
        }
    )


def test_pseudonymize_case_scrubs_mapping_keys() -> None:
    # An additional-property key is as private as a value: an address or an
    # account id sitting in the key position would otherwise survive verbatim
    # into a document described as pseudonymized.
    case = _case_with_argument_keys({
        "alice@example.com": "owes for the excursion",
        "Alice Smith": "next door",
    })
    result = Pseudonymizer(literals={"Alice Smith": "<person>"}).pseudonymize_case(case)

    payload = result.payload
    assert isinstance(payload, ConversationPayload)
    assert "alice@example.com" not in payload.arguments
    assert "Alice Smith" not in payload.arguments
    assert any(key.endswith("@example.invalid") for key in payload.arguments)
    assert "<person>" in payload.arguments
    assert "alice@example.com" not in str(result.model_dump(mode="json"))


def test_pseudonymized_case_round_trips_as_a_valid_case() -> None:
    case = _case_with_argument_keys({"alice@example.com": "owes for the excursion"})
    result = pseudonymize_case(case)
    dumped = result.model_dump(mode="json")
    assert EvalCase.model_validate(dumped) == result


def test_key_pseudonyms_are_deterministic_and_match_value_pseudonyms() -> None:
    case = _case_with_argument_keys({"alice@example.com": "mail alice@example.com"})
    once = pseudonymize_case(case)
    twice = pseudonymize_case(case)
    assert once.model_dump(mode="json") == twice.model_dump(mode="json")

    payload = once.payload
    assert isinstance(payload, ConversationPayload)
    key = next(key for key in payload.arguments if key.endswith("@example.invalid"))
    # One identifier, one pseudonym, whichever position it appeared in.
    assert key in str(payload.arguments[key])


def test_colliding_key_pseudonyms_fail_closed() -> None:
    # Emitting a case with one of two entries silently dropped would be a
    # document that lies about what the capture contained.
    case = _case_with_argument_keys({"Bob": "cousin", "Rob": "neighbour"})
    pseudonymizer = Pseudonymizer(literals={"Bob": "<person>", "Rob": "<person>"})
    with pytest.raises(PseudonymizationError, match="collide"):
        pseudonymizer.pseudonymize_case(case)


def test_non_string_mapping_key_fails_closed() -> None:
    # A key that is not text cannot go through the substitution path, and may
    # itself be household data (a chat id), so the walk refuses it rather than
    # passing it through into a "pseudonymized" document.
    with pytest.raises(PseudonymizationError, match="not a string"):
        Pseudonymizer()._walk({1234: "chat"})
