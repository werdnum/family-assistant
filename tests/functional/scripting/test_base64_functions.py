"""Test base64 functions in scripting engines."""

import base64

import pytest

from family_assistant.scripting.errors import ScriptExecutionError


class TestBase64Functions:
    """Test base64 encode/decode functions."""

    @pytest.mark.asyncio
    async def test_base64_encode_string(self, engine_class: type) -> None:
        """Test encoding a string to base64."""
        engine = engine_class()

        script = """
base64_encode("Hello, World!")
"""
        result = await engine.evaluate_async(script)
        assert result == base64.b64encode(b"Hello, World!").decode("ascii")

    @pytest.mark.asyncio
    async def test_base64_decode_string(self, engine_class: type) -> None:
        """Test decoding a base64 string."""
        engine = engine_class()

        encoded = base64.b64encode(b"Hello, World!").decode("ascii")
        script = f"""
base64_decode("{encoded}")
"""
        result = await engine.evaluate_async(script)
        assert result == "Hello, World!"

    @pytest.mark.asyncio
    async def test_base64_roundtrip(self, engine_class: type) -> None:
        """Test encoding and then decoding preserves data."""
        engine = engine_class()

        script = """
original = "The quick brown fox jumps over the lazy dog"
encoded = base64_encode(original)
decoded = base64_decode(encoded)
decoded == original
"""
        result = await engine.evaluate_async(script)
        assert result is True

    @pytest.mark.asyncio
    async def test_base64_encode_empty_string(self, engine_class: type) -> None:
        """Test encoding an empty string."""
        engine = engine_class()

        script = """
base64_encode("")
"""
        result = await engine.evaluate_async(script)
        assert result is not None
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_base64_decode_empty_string(self, engine_class: type) -> None:
        """Test decoding an empty base64 string."""
        engine = engine_class()

        script = """
base64_decode("")
"""
        result = await engine.evaluate_async(script)
        assert result is not None
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_base64_encode_unicode(self, engine_class: type) -> None:
        """Test encoding unicode text."""
        engine = engine_class()

        script = """
encoded = base64_encode("héllo wörld")
base64_decode(encoded) == "héllo wörld"
"""
        result = await engine.evaluate_async(script)
        assert result is True

    @pytest.mark.asyncio
    async def test_base64_encode_bytes(self, engine_class: type) -> None:
        """Test encoding bytes to base64."""
        engine = engine_class()

        script = """
base64_encode(b"binary data")
"""
        result = await engine.evaluate_async(script)
        assert result == base64.b64encode(b"binary data").decode("ascii")

    @pytest.mark.asyncio
    async def test_base64_decode_invalid_padding(self, engine_class: type) -> None:
        """Test that decoding base64 with invalid padding raises an error."""
        engine = engine_class()

        script = """
base64_decode("not-valid-base64!!!")
"""
        with pytest.raises(ScriptExecutionError):
            await engine.evaluate_async(script)

    @pytest.mark.asyncio
    async def test_base64_decode_illegal_characters(self, engine_class: type) -> None:
        """Test that decoding base64 with illegal characters raises an error.

        With validate=True, base64.b64decode rejects non-alphabet characters
        like '$' even when padding is otherwise valid.
        """
        engine = engine_class()

        script = """
base64_decode("YWJj$")
"""
        with pytest.raises(ScriptExecutionError):
            await engine.evaluate_async(script)

    @pytest.mark.asyncio
    async def test_base64_decode_bytes(self, engine_class: type) -> None:
        """Test decoding base64 to raw bytes for non-UTF-8 data."""
        engine = engine_class()

        # Encode some non-UTF-8 bytes (0xff 0xfe are invalid UTF-8)
        raw = bytes([0xFF, 0xFE, 0x00, 0x01])
        encoded = base64.b64encode(raw).decode("ascii")
        script = f"""
base64_decode_bytes("{encoded}")
"""
        result = await engine.evaluate_async(script)
        assert result == raw

    @pytest.mark.asyncio
    async def test_base64_decode_bytes_roundtrip(self, engine_class: type) -> None:
        """Test encode then decode_bytes preserves arbitrary binary data."""
        engine = engine_class()

        script = """
data = b"\\xff\\xfe\\x00\\x01"
encoded = base64_encode(data)
base64_decode_bytes(encoded) == data
"""
        result = await engine.evaluate_async(script)
        assert result is True

    @pytest.mark.asyncio
    async def test_base64_with_json(self, engine_class: type) -> None:
        """Test combining base64 with JSON for a realistic use case."""
        engine = engine_class()

        script = """
data = {"key": "secret_value"}
json_str = json_encode(data)
encoded = base64_encode(json_str)
decoded_json = base64_decode(encoded)
result = json_decode(decoded_json)
result["key"]
"""
        result = await engine.evaluate_async(script)
        assert result == "secret_value"
