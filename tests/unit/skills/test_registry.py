"""Tests for NoteRegistry."""

from pathlib import Path

from family_assistant.skills.registry import NoteRegistry
from family_assistant.skills.types import ParsedSkill
from family_assistant.storage.repositories.notes import NoteReadPolicy


def _skill(
    name: str,
    description: str = "A skill.",
    labels: frozenset[str] | None = None,
) -> ParsedSkill:
    return ParsedSkill(
        name=name,
        description=description,
        content=f"Instructions for {name}.",
        source_path=Path(f"/fake/{name}.md"),
        visibility_labels=labels or frozenset(),
    )


class TestNoteRegistry:
    def test_empty_registry(self) -> None:
        registry = NoteRegistry([])
        assert registry.get_skill_catalog(NoteReadPolicy.UNRESTRICTED) == []
        assert (
            registry.get_skill_by_name("anything", NoteReadPolicy.UNRESTRICTED) is None
        )

    def test_get_catalog_no_filtering(self) -> None:
        skills = [_skill("Alpha"), _skill("Beta")]
        registry = NoteRegistry(skills)
        catalog = registry.get_skill_catalog(NoteReadPolicy.UNRESTRICTED)
        assert len(catalog) == 2
        names = {s.name for s in catalog}
        assert names == {"Alpha", "Beta"}

    def test_get_catalog_with_grants(self) -> None:
        skills = [
            _skill("Public"),
            _skill("Internal", labels=frozenset({"internal"})),
            _skill("Secret", labels=frozenset({"sensitive", "internal"})),
        ]
        registry = NoteRegistry(skills)

        # No grants: only public
        catalog = registry.get_skill_catalog(NoteReadPolicy(grants=frozenset()))
        assert [s.name for s in catalog] == ["Public"]

        # internal grant: public + internal
        catalog = registry.get_skill_catalog(
            NoteReadPolicy(grants=frozenset({"internal"}))
        )
        names = {s.name for s in catalog}
        assert names == {"Public", "Internal"}

        # both grants: all
        catalog = registry.get_skill_catalog(
            NoteReadPolicy(grants=frozenset({"internal", "sensitive"}))
        )
        assert len(catalog) == 3

    def test_get_skill_by_name(self) -> None:
        skills = [_skill("Meeting Notes"), _skill("Research")]
        registry = NoteRegistry(skills)

        result = registry.get_skill_by_name(
            "Meeting Notes", NoteReadPolicy.UNRESTRICTED
        )
        assert result is not None
        assert result.name == "Meeting Notes"

    def test_get_skill_by_name_not_found(self) -> None:
        registry = NoteRegistry([_skill("Alpha")])
        assert (
            registry.get_skill_by_name("Nonexistent", NoteReadPolicy.UNRESTRICTED)
            is None
        )

    def test_get_skill_by_name_access_denied(self) -> None:
        skills = [_skill("Secret", labels=frozenset({"sensitive"}))]
        registry = NoteRegistry(skills)

        # Without the grant, should return None
        assert (
            registry.get_skill_by_name("Secret", NoteReadPolicy(grants=frozenset()))
            is None
        )

        # With the grant, should return the skill
        result = registry.get_skill_by_name(
            "Secret", NoteReadPolicy(grants=frozenset({"sensitive"}))
        )
        assert result is not None
        assert result.name == "Secret"

    def test_duplicate_names_last_wins(self) -> None:
        skills = [
            _skill("Dup", description="First"),
            _skill("Dup", description="Second"),
        ]
        registry = NoteRegistry(skills)
        result = registry.get_skill_by_name("Dup", NoteReadPolicy.UNRESTRICTED)
        assert result is not None
        assert result.description == "Second"
