"""Output grading for the Google Maps MCP tools shipped in defaults.yaml."""

from pathlib import Path

import yaml

DEFAULTS = Path(__file__).resolve().parents[3] / "defaults.yaml"


def test_google_maps_grades_structured_fields_but_not_free_text_reviews() -> None:
    config = yaml.safe_load(DEFAULTS.read_text(encoding="utf-8"))
    metadata = config["mcp_config"]["mcpServers"]["google-maps"]["tool_metadata"]

    assert set(metadata) == {
        "maps_geocode",
        "maps_reverse_geocode",
        "maps_search_places",
        "maps_place_details",
        "maps_distance_matrix",
        "maps_elevation",
        "maps_directions",
    }
    assert metadata["maps_place_details"] == ["read_only", "output_untrusted"]
    for name, tags in metadata.items():
        if name != "maps_place_details":
            assert tags == ["read_only", "output_machine_data"]
