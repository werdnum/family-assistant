"""The core memory note's derived topic index.

Slice 2 of docs/design/conversation-memory.md: the index section is a
projection of the topic notes that exist, and nothing a writer says inside the
markers survives a regeneration.
"""

from datetime import UTC, datetime

from family_assistant.memory.index import (
    INDEX_END_MARKER,
    INDEX_START_MARKER,
    TopicIndexEntry,
    regenerate_topic_index,
    strip_topic_index,
)


def _topic(title: str, day: int) -> TopicIndexEntry:
    return TopicIndexEntry(title=title, last_changed=datetime(2026, 9, day, tzinfo=UTC))


def test_index_is_appended_to_a_core_note_with_no_markers() -> None:
    result = regenerate_topic_index(
        "- the household eats at 6pm", [_topic("Sam", 15)], max_chars=1500
    )

    assert result.startswith("- the household eats at 6pm")
    assert INDEX_START_MARKER in result
    assert INDEX_END_MARKER in result
    assert "- Sam (changed 2026-09-15)" in result


def test_an_empty_core_note_gets_only_the_section() -> None:
    result = regenerate_topic_index("", [_topic("Sam", 15)], max_chars=1500)

    assert result.startswith(INDEX_START_MARKER)
    assert result.endswith(INDEX_END_MARKER)


def test_no_topics_means_no_section() -> None:
    assert regenerate_topic_index("- a standing fact", [], max_chars=1500) == (
        "- a standing fact"
    )


def test_a_hand_written_index_section_is_replaced() -> None:
    hand_edited = (
        "- a standing fact\n\n"
        f"{INDEX_START_MARKER}\n\n- Invented Topic (changed 1999-01-01)\n\n"
        f"{INDEX_END_MARKER}"
    )

    result = regenerate_topic_index(hand_edited, [_topic("Sam", 15)], max_chars=1500)

    assert "Invented Topic" not in result
    assert "- Sam (changed 2026-09-15)" in result
    assert "- a standing fact" in result


def test_the_author_s_text_after_the_section_survives() -> None:
    content = f"before\n\n{INDEX_START_MARKER}\nstale\n{INDEX_END_MARKER}\n\nafter"

    assert strip_topic_index(content) == "before\n\nafter"


def test_topics_are_listed_most_recently_changed_first() -> None:
    result = regenerate_topic_index(
        "",
        [_topic("Older", 10), _topic("Newest", 16), _topic("Middle", 14)],
        max_chars=1500,
    )

    order = [line for line in result.split("\n") if line.startswith("- ")]
    assert order == [
        "- Newest (changed 2026-09-16)",
        "- Middle (changed 2026-09-14)",
        "- Older (changed 2026-09-10)",
    ]


def test_the_index_is_truncated_to_its_share_of_the_core_note() -> None:
    topics = [_topic(f"Topic {n:02d}", 10) for n in range(40)]

    result = regenerate_topic_index("", topics, max_chars=200)

    assert len(result) <= 200
    listed = [line for line in result.split("\n") if line.startswith("- ")]
    assert 0 < len(listed) < len(topics)


def test_a_cap_too_small_for_one_entry_emits_no_section() -> None:
    result = regenerate_topic_index(
        "- a standing fact", [_topic("Sam", 15)], max_chars=10
    )

    assert result == "- a standing fact"


def test_an_unterminated_marker_does_not_swallow_the_next_regeneration() -> None:
    truncated = f"- a standing fact\n\n{INDEX_START_MARKER}\n- half written"

    result = regenerate_topic_index(truncated, [_topic("Sam", 15)], max_chars=1500)

    assert "half written" not in result
    assert "- a standing fact" in result
    assert result.count(INDEX_START_MARKER) == 1
