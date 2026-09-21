"""Registry of file-based skills."""

import logging
from typing import TYPE_CHECKING

from family_assistant.skills.types import ParsedSkill

if TYPE_CHECKING:
    from family_assistant.storage.repositories.notes import NoteReadPolicy

logger = logging.getLogger(__name__)


class NoteRegistry:
    """Registry of file-based skills, loaded at startup.

    Holds pre-loaded skills from file directories and provides
    access-controlled lookups. DB-based skills are handled separately
    by the NotesContextProvider (via frontmatter parsing on DB notes).

    This is the second note-resolution boundary, and it consults the same
    :class:`~family_assistant.storage.repositories.notes.NoteReadPolicy` the
    notes repository does. A file skill usually carries no labels at all, so a
    grants-only check admits every skill to every reader; a policy with a
    required-label floor is what keeps a confined profile out of them.
    """

    def __init__(self, skills: list[ParsedSkill]) -> None:
        self._skills: dict[str, ParsedSkill] = {s.name: s for s in skills}
        logger.info(
            "NoteRegistry initialized with %d file-based skill(s)", len(self._skills)
        )

    def get_skill_catalog(self, read_policy: "NoteReadPolicy") -> list[ParsedSkill]:
        """Get all skills the read policy admits."""
        return [s for s in self._skills.values() if self._is_accessible(s, read_policy)]

    def get_skill_by_name(
        self, name: str, read_policy: "NoteReadPolicy"
    ) -> ParsedSkill | None:
        """Get a skill by name, respecting access control.

        Returns ``None`` if the skill doesn't exist or is not accessible.
        """
        skill = self._skills.get(name)
        if skill and self._is_accessible(skill, read_policy):
            return skill
        return None

    @staticmethod
    def _is_accessible(skill: ParsedSkill, read_policy: "NoteReadPolicy") -> bool:
        return read_policy.admits_labels(skill.visibility_labels)
