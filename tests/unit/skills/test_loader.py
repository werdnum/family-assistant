"""Tests for skill file loading."""

from pathlib import Path

from family_assistant.skills.loader import load_skills_from_directory


class TestLoadSkillsFromDirectory:
    def test_load_valid_skill(self, tmp_path: Path) -> None:
        skill_file = tmp_path / "meeting-notes.md"
        skill_file.write_text(
            "---\n"
            "name: Meeting Notes\n"
            "description: Format meeting notes.\n"
            "---\n"
            "# Instructions\n\nUse this structure."
        )
        skills = load_skills_from_directory(tmp_path)
        assert len(skills) == 1
        assert skills[0].name == "Meeting Notes"
        assert skills[0].description == "Format meeting notes."
        assert skills[0].content == "# Instructions\n\nUse this structure."
        assert skills[0].source_path == skill_file
        assert skills[0].visibility_labels == frozenset()

    def test_load_skill_with_visibility_labels(self, tmp_path: Path) -> None:
        skill_file = tmp_path / "internal.md"
        skill_file.write_text(
            "---\n"
            "name: Internal Skill\n"
            "description: For internal use.\n"
            "visibility_labels:\n"
            "  - skill_internal\n"
            "  - sensitive\n"
            "---\n"
            "Content here."
        )
        skills = load_skills_from_directory(tmp_path)
        assert len(skills) == 1
        assert skills[0].visibility_labels == frozenset({"skill_internal", "sensitive"})

    def test_skip_file_without_frontmatter(self, tmp_path: Path) -> None:
        (tmp_path / "readme.md").write_text("Just a README with no frontmatter.")
        skills = load_skills_from_directory(tmp_path)
        assert len(skills) == 0

    def test_skip_file_missing_name(self, tmp_path: Path) -> None:
        (tmp_path / "incomplete.md").write_text(
            "---\ndescription: Missing name field.\n---\nBody."
        )
        skills = load_skills_from_directory(tmp_path)
        assert len(skills) == 0

    def test_skip_file_missing_description(self, tmp_path: Path) -> None:
        (tmp_path / "incomplete.md").write_text(
            "---\nname: Missing Description\n---\nBody."
        )
        skills = load_skills_from_directory(tmp_path)
        assert len(skills) == 0

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        skills = load_skills_from_directory(tmp_path / "nonexistent")
        assert len(skills) == 0

    def test_empty_directory(self, tmp_path: Path) -> None:
        skills = load_skills_from_directory(tmp_path)
        assert len(skills) == 0

    def test_multiple_skills_sorted(self, tmp_path: Path) -> None:
        (tmp_path / "b-skill.md").write_text(
            "---\nname: Beta\ndescription: Second.\n---\nBody B."
        )
        (tmp_path / "a-skill.md").write_text(
            "---\nname: Alpha\ndescription: First.\n---\nBody A."
        )
        skills = load_skills_from_directory(tmp_path)
        assert len(skills) == 2
        assert skills[0].name == "Alpha"
        assert skills[1].name == "Beta"

    def test_non_md_files_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "skill.txt").write_text(
            "---\nname: Text File\ndescription: Should be ignored.\n---\nBody."
        )
        (tmp_path / "skill.yaml").write_text("name: YAML File\ndescription: Nope.")
        skills = load_skills_from_directory(tmp_path)
        assert len(skills) == 0

    def test_mixed_valid_and_invalid(self, tmp_path: Path) -> None:
        (tmp_path / "valid.md").write_text(
            "---\nname: Valid\ndescription: Works.\n---\nBody."
        )
        (tmp_path / "invalid.md").write_text("No frontmatter here.")
        (tmp_path / "incomplete.md").write_text("---\nname: No Description\n---\nBody.")
        skills = load_skills_from_directory(tmp_path)
        assert len(skills) == 1
        assert skills[0].name == "Valid"
