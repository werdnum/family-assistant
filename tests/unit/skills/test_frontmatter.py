"""Tests for YAML frontmatter parsing."""

from family_assistant.skills.frontmatter import parse_frontmatter


class TestParseFrontmatter:
    def test_no_frontmatter(self) -> None:
        content = "Just regular markdown content."
        fm, body = parse_frontmatter(content)
        assert fm is None
        assert body == content

    def test_empty_content(self) -> None:
        fm, body = parse_frontmatter("")
        assert fm is None
        assert not body

    def test_basic_frontmatter(self) -> None:
        content = "---\nname: Test Skill\ndescription: A test.\n---\nBody here."
        fm, body = parse_frontmatter(content)
        assert fm == {"name": "Test Skill", "description": "A test."}
        assert body == "Body here."

    def test_frontmatter_with_leading_newlines(self) -> None:
        content = "\n\n---\nname: Test\n---\nBody."
        fm, body = parse_frontmatter(content)
        assert fm == {"name": "Test"}
        assert body == "Body."

    def test_multiline_body(self) -> None:
        content = "---\nname: Skill\n---\n# Title\n\nParagraph one.\n\nParagraph two."
        fm, body = parse_frontmatter(content)
        assert fm == {"name": "Skill"}
        assert body == "# Title\n\nParagraph one.\n\nParagraph two."

    def test_complex_frontmatter(self) -> None:
        content = (
            "---\n"
            "name: Home Automation\n"
            "description: Control smart home devices.\n"
            "visibility_labels:\n"
            "  - skill_internal\n"
            "  - sensitive\n"
            "---\n"
            "# Instructions"
        )
        fm, body = parse_frontmatter(content)
        assert fm is not None
        assert fm["name"] == "Home Automation"
        assert fm["description"] == "Control smart home devices."
        assert fm["visibility_labels"] == ["skill_internal", "sensitive"]
        assert body == "# Instructions"

    def test_no_closing_delimiter(self) -> None:
        content = "---\nname: Broken\nNo closing delimiter."
        fm, body = parse_frontmatter(content)
        assert fm is None
        assert body == content

    def test_invalid_yaml(self) -> None:
        content = "---\n: invalid: yaml: [unclosed\n---\nBody."
        fm, body = parse_frontmatter(content)
        assert fm is None
        assert body == content

    def test_yaml_returns_non_dict(self) -> None:
        content = "---\n- just\n- a\n- list\n---\nBody."
        fm, body = parse_frontmatter(content)
        assert fm is None
        assert body == content

    def test_empty_frontmatter(self) -> None:
        content = "---\n---\nBody after empty frontmatter."
        fm, body = parse_frontmatter(content)
        # yaml.safe_load of empty string returns None, which is not a dict
        assert fm is None
        assert body == content

    def test_delimiter_in_body(self) -> None:
        content = "---\nname: Test\n---\nBody with --- in it."
        fm, body = parse_frontmatter(content)
        assert fm == {"name": "Test"}
        assert body == "Body with --- in it."

    def test_empty_body(self) -> None:
        content = "---\nname: Test\n---\n"
        fm, body = parse_frontmatter(content)
        assert fm == {"name": "Test"}
        assert not body

    def test_dashes_not_at_start(self) -> None:
        content = "Some text first\n---\nname: Test\n---\nBody."
        fm, body = parse_frontmatter(content)
        assert fm is None
        assert body == content

    def test_partial_delimiter(self) -> None:
        content = "--\nname: Test\n---\nBody."
        fm, body = parse_frontmatter(content)
        assert fm is None
        assert body == content
