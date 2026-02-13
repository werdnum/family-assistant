"""Registry of file-based skills."""

import logging

from family_assistant.skills.types import ParsedSkill

logger = logging.getLogger(__name__)


class NoteRegistry:
    """Registry of file-based skills, loaded at startup.

    Holds pre-loaded skills from file directories and provides
    access-controlled lookups. DB-based skills are handled separately
    by the NotesContextProvider (via frontmatter parsing on DB notes).
    """

    def __init__(self, skills: list[ParsedSkill]) -> None:
        self._skills: dict[str, ParsedSkill] = {s.name: s for s in skills}
        logger.info(
            "NoteRegistry initialized with %d file-based skill(s)", len(self._skills)
        )

    def get_skill_catalog(
        self, visibility_grants: set[str] | None
    ) -> list[ParsedSkill]:
        """Get all skills accessible to a profile.

        When ``visibility_grants`` is ``None``, no filtering is applied.
        Otherwise, only skills whose labels are a subset of the grants
        are returned.
        """
        return [
            s
            for s in self._skills.values()
            if self._is_accessible(s, visibility_grants)
        ]

    def get_skill_by_name(
        self, name: str, visibility_grants: set[str] | None
    ) -> ParsedSkill | None:
        """Get a skill by name, respecting access control.

        Returns ``None`` if the skill doesn't exist or is not accessible.
        """
        skill = self._skills.get(name)
        if skill and self._is_accessible(skill, visibility_grants):
            return skill
        return None

    @staticmethod
    def _is_accessible(skill: ParsedSkill, visibility_grants: set[str] | None) -> bool:
        if visibility_grants is None:
            return True
        return skill.visibility_labels <= visibility_grants
