"""Test JSON functions in scripting engines."""

import pytest


class TestJSONFunctions:
    """Test JSON encode/decode functions in both engines."""

    @pytest.mark.asyncio
    async def test_json_encode_decode(self, engine_class: type) -> None:
        """Test that json_encode and json_decode work correctly."""
        engine = engine_class()

        script = """
data = {"name": "Alice", "age": 30, "items": [1, 2, 3]}
encoded = json_encode(data)
encoded
"""
        result = await engine.evaluate_async(script)
        assert result == '{"name": "Alice", "age": 30, "items": [1, 2, 3]}'

        script2 = """
json_str = '{"name": "Bob", "count": 42, "tags": ["python", "starlark"]}'
decoded = json_decode(json_str)
decoded["name"] + " has " + str(decoded["count"]) + " items"
"""
        result2 = await engine.evaluate_async(script2)
        assert result2 == "Bob has 42 items"

    @pytest.mark.asyncio
    async def test_json_roundtrip(self, engine_class: type) -> None:
        """Test encoding and then decoding preserves data."""
        engine = engine_class()

        script = """
original = {
    "string": "hello",
    "number": 123,
    "float": 45.67,
    "bool": True,
    "null": None,
    "list": [1, 2, 3],
    "nested": {"a": 1, "b": 2}
}
encoded = json_encode(original)
decoded = json_decode(encoded)

tests = [
    decoded["string"] == "hello",
    decoded["number"] == 123,
    decoded["float"] == 45.67,
    decoded["bool"] == True,
    decoded["null"] == None,
    decoded["list"] == [1, 2, 3],
    decoded["nested"]["a"] == 1,
    decoded["nested"]["b"] == 2
]
all(tests)
"""
        result = await engine.evaluate_async(script)
        assert result is True

    @pytest.mark.asyncio
    async def test_json_decode_error_handling(self, engine_class: type) -> None:
        """Test that json_decode handles invalid JSON gracefully."""
        engine = engine_class()

        script = """
json_decode("not valid json")
"""
        with pytest.raises(Exception) as exc_info:
            await engine.evaluate_async(script)
        error_msg = str(exc_info.value).lower()
        assert "json" in error_msg or "expecting value" in error_msg

    @pytest.mark.asyncio
    async def test_json_with_tools_mock(self, engine_class: type) -> None:
        """Test using JSON functions with mock tool results."""
        engine = engine_class()

        script = """
def process_search_results():
    mock_result = '[{"id": 1, "title": "Note 1"}, {"id": 2, "title": "Note 2"}]'
    notes = json_decode(mock_result)
    titles = []
    for note in notes:
        titles.append(note["title"])
    return titles

process_search_results()
"""
        result = await engine.evaluate_async(script)
        assert result == ["Note 1", "Note 2"]
