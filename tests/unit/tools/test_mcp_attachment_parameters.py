"""Unit tests for attachment plumbing on MCP tools."""

from __future__ import annotations

import base64
import shutil
import tempfile
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest import mock
from zoneinfo import ZoneInfo

import anyio
import pytest
from mcp.types import CallToolResult, TextContent, Tool

from family_assistant.config_models import MCPServerConfig as MCPServerConfigModel
from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.services.attachment_registry import (
    AttachmentMetadata,
    AttachmentRegistry,
)
from family_assistant.tools import MCPServerConfig, MCPToolsProvider, infrastructure
from family_assistant.tools.mcp_attachments import (
    AttachmentParameter,
    MCPAttachmentMode,
    materialised_attachment_arguments,
)
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mcp import ClientSession

    from family_assistant.storage.database import Database

# ast-grep-ignore: no-dict-any - MCP tool arguments are untyped per the MCP protocol
type MCPArguments = dict[str, Any]

SERVER_ID = "meshy"


def _mode(mode: MCPAttachmentMode) -> AttachmentParameter:
    """The low-level helpers take resolved parameters, not raw config."""
    return AttachmentParameter(mode=mode)


IMAGE_BYTES = b"\x89PNG\r\n\x1a\nfake"


def _image_tool(name: str = "meshy_image_to_3d") -> Tool:
    return Tool(
        name=name,
        description="Turn an image into a 3D model",
        inputSchema={
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "format": "uri",
                    "description": "Image to convert",
                },
                "should_texture": {"type": "boolean"},
            },
            "required": ["image_url"],
        },
    )


def _multi_image_tool() -> Tool:
    return Tool(
        name="meshy_multi_image_to_3d",
        description="Turn several images into a 3D model",
        inputSchema={
            "type": "object",
            "properties": {
                "image_urls": {
                    "type": "array",
                    "items": {"type": "string", "format": "uri"},
                }
            },
        },
    )


def _provider(
    # ast-grep-ignore: no-dict-any - Test configuration mirrors untyped MCP config
    attachment_parameters: Mapping[str, Any] | None = None,
    transport: str = "stdio",
) -> MCPToolsProvider:
    config: MCPServerConfig = cast(
        "MCPServerConfig",
        {"transport": transport, "command": "echo"},
    )
    if attachment_parameters is not None:
        cast("dict[str, Any]", config)["attachment_parameters"] = attachment_parameters
    return MCPToolsProvider({SERVER_ID: config})


def _properties(
    # ast-grep-ignore: no-dict-any - Tool schemas are untyped JSON
    definitions: list[Any],
    name: str,
    # ast-grep-ignore: no-dict-any - Tool schemas are untyped JSON
) -> dict[str, Any]:
    definition = next(
        candidate for candidate in definitions if candidate["function"]["name"] == name
    )
    return definition["function"]["parameters"]["properties"]


def _attachment(
    filename: str | None = "photo.png",
    mime_type: str = "image/png",
) -> ScriptAttachment:
    """A ScriptAttachment backed by a registry that serves fixed bytes."""
    attachment_id = str(uuid.uuid4())
    metadata = AttachmentMetadata(
        attachment_id=attachment_id,
        source_type="user",
        source_id="user-1",
        mime_type=mime_type,
        description="a photo",
        size=len(IMAGE_BYTES),
        metadata={"original_filename": filename} if filename else {},
    )

    async def get_attachment_content(
        db_context: object, requested_id: str, *, acting_user_id: str | None
    ) -> bytes:
        assert requested_id == attachment_id
        return IMAGE_BYTES

    registry = cast(
        "AttachmentRegistry",
        SimpleNamespace(get_attachment_content=get_attachment_content),
    )
    return ScriptAttachment(
        metadata=metadata,
        registry=registry,
        db_context_getter=lambda: cast("Database", None),
        user_id="user-1",
    )


def test_configured_parameter_is_advertised_as_an_attachment_uuid() -> None:
    """The model is asked for an attachment ID, not the server's URI."""
    provider = _provider({"meshy_image_to_3d": {"image_url": "data_uri"}})

    definitions = provider._format_mcp_definitions_to_dicts([_image_tool()], SERVER_ID)

    image_url = _properties(definitions, "meshy_image_to_3d")["image_url"]
    assert image_url["type"] == "attachment"
    # A `format: uri` left in place would contradict the UUID we now ask for.
    assert "format" not in image_url
    # The server's description describes the string it used to want, so it goes
    # with the rest of that schema.
    assert "description" not in image_url
    assert _properties(definitions, "meshy_image_to_3d")["should_texture"] == {
        "type": "boolean"
    }


def test_array_parameters_are_marked_on_their_items() -> None:
    provider = _provider({"meshy_multi_image_to_3d": {"image_urls": "data_uri"}})

    definitions = provider._format_mcp_definitions_to_dicts(
        [_multi_image_tool()], SERVER_ID
    )

    image_urls = _properties(definitions, "meshy_multi_image_to_3d")["image_urls"]
    assert image_urls["type"] == "array"
    assert image_urls["items"]["type"] == "attachment"
    assert "format" not in image_urls["items"]


def test_unconfigured_tools_keep_their_schema() -> None:
    provider = _provider({"some_other_tool": {"image_url": "data_uri"}})

    definitions = provider._format_mcp_definitions_to_dicts([_image_tool()], SERVER_ID)

    assert _properties(definitions, "meshy_image_to_3d")["image_url"] == {
        "type": "string",
        "format": "uri",
        "description": "Image to convert",
    }


def test_a_parameter_the_server_does_not_have_is_ignored() -> None:
    """A stale config entry must not invent a parameter the server rejects."""
    provider = _provider({"meshy_image_to_3d": {"picture": "data_uri"}})

    definitions = provider._format_mcp_definitions_to_dicts([_image_tool()], SERVER_ID)

    assert "picture" not in _properties(definitions, "meshy_image_to_3d")


@pytest.mark.asyncio
async def test_advertised_definitions_translate_attachments_to_strings() -> None:
    """What reaches the LLM is a plain string parameter documented as a UUID."""
    provider = _provider({"meshy_image_to_3d": {"image_url": "data_uri"}})
    definitions = provider._format_mcp_definitions_to_dicts([_image_tool()], SERVER_ID)
    provider._register_server_tools(
        SERVER_ID,
        definitions,
        provider._build_mcp_descriptors(
            server_id=SERVER_ID,
            definitions=definitions,
            discovered_tools=[_image_tool()],
        ),
    )
    provider._initialized = True

    advertised = await provider.get_tool_definitions()

    image_url = _properties(list(advertised), "meshy_image_to_3d")["image_url"]
    assert image_url["type"] == "string"
    assert "UUID" in image_url["description"]
    # The provider's own copy keeps the internal type that execution needs.
    assert (
        _properties(provider._definitions, "meshy_image_to_3d")["image_url"]["type"]
        == "attachment"
    )


@pytest.mark.asyncio
async def test_data_uri_mode_inlines_the_attachment_bytes() -> None:
    attachment = _attachment()

    async with materialised_attachment_arguments(
        {"image_url": attachment, "should_texture": True},
        {"image_url": _mode("data_uri")},
    ) as materialised:
        assert materialised["should_texture"] is True
        assert materialised["image_url"] == (
            "data:image/png;base64," + base64.b64encode(IMAGE_BYTES).decode("ascii")
        )


@pytest.mark.asyncio
async def test_data_uri_mode_handles_a_list_of_attachments() -> None:
    attachments = [_attachment(), _attachment()]

    async with materialised_attachment_arguments(
        {"image_urls": attachments}, {"image_urls": _mode("data_uri")}
    ) as materialised:
        assert len(materialised["image_urls"]) == 2
        assert all(
            value.startswith("data:image/png;base64,")
            for value in materialised["image_urls"]
        )


@pytest.mark.asyncio
async def test_file_path_mode_writes_a_readable_file_and_cleans_it_up() -> None:
    attachment = _attachment()

    async with materialised_attachment_arguments(
        {"image_url": attachment}, {"image_url": _mode("file_path")}
    ) as materialised:
        path = anyio.Path(materialised["image_url"])
        assert path.suffix == ".png"
        assert await path.read_bytes() == IMAGE_BYTES

    assert not await path.exists()
    assert not await path.parent.exists()


@pytest.mark.asyncio
async def test_file_path_mode_falls_back_to_the_mime_type_for_a_suffix() -> None:
    attachment = _attachment(filename=None, mime_type="image/jpeg")

    async with materialised_attachment_arguments(
        {"image_url": attachment}, {"image_url": _mode("file_path")}
    ) as materialised:
        assert anyio.Path(materialised["image_url"]).suffix in {".jpg", ".jpeg"}


def test_file_path_mode_is_refused_for_a_remote_server() -> None:
    """A path in our filesystem means nothing to a server we did not spawn."""
    with pytest.raises(ValueError, match="requires a stdio MCP server"):
        MCPServerConfigModel.model_validate({
            "transport": "sse",
            "url": "https://example.invalid/mcp",
            "attachment_parameters": {"meshy_image_to_3d": {"image_url": "file_path"}},
        })


def test_data_uri_mode_is_allowed_for_a_remote_server() -> None:
    config = MCPServerConfigModel.model_validate({
        "transport": "streamable_http",
        "url": "https://example.invalid/mcp",
        "attachment_parameters": {"meshy_image_to_3d": {"image_url": "data_uri"}},
    })

    assert config.attachment_parameters == {
        "meshy_image_to_3d": {"image_url": "data_uri"}
    }


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="attachment_parameters"):
        MCPServerConfigModel.model_validate({
            "command": "echo",
            "attachment_parameters": {"meshy_image_to_3d": {"image_url": "ftp"}},
        })


def _execution_context(
    registry: AttachmentRegistry,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id="conv-1",
        user_name="Test User",
        turn_id=None,
        db_context=cast("Database", None),
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=registry,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
        user_id="user-1",
    )


def _registry_serving(attachment: ScriptAttachment) -> AttachmentRegistry:
    """A registry that resolves and serves exactly the one attachment."""

    async def get_attachment(
        db_context: object, attachment_id: str, *, acting_user_id: str | None
    ) -> AttachmentMetadata | None:
        if attachment_id != attachment.get_id():
            return None
        return attachment._metadata

    async def get_attachment_content(
        db_context: object, attachment_id: str, *, acting_user_id: str | None
    ) -> bytes:
        return IMAGE_BYTES

    return cast(
        "AttachmentRegistry",
        SimpleNamespace(
            get_attachment=get_attachment,
            get_attachment_content=get_attachment_content,
        ),
    )


def _recording_session() -> tuple[ClientSession, list[MCPArguments]]:
    """A session that records the arguments each call_tool receives."""
    calls: list[MCPArguments] = []

    async def call_tool(*, name: str, arguments: MCPArguments) -> CallToolResult:
        calls.append(arguments)
        return CallToolResult(content=[TextContent(type="text", text="ok")])

    return cast("ClientSession", SimpleNamespace(call_tool=call_tool)), calls


async def _connected_provider(
    # ast-grep-ignore: no-dict-any - Test configuration mirrors untyped MCP config
    attachment_parameters: Mapping[str, Any],
    tools: list[Tool],
) -> tuple[MCPToolsProvider, list[MCPArguments]]:
    provider = _provider(attachment_parameters)
    definitions = provider._format_mcp_definitions_to_dicts(tools, SERVER_ID)
    provider._register_server_tools(
        SERVER_ID,
        definitions,
        provider._build_mcp_descriptors(
            server_id=SERVER_ID, definitions=definitions, discovered_tools=tools
        ),
    )
    provider._initialized = True
    session, calls = _recording_session()
    provider._sessions[SERVER_ID] = session
    return provider, calls


@pytest.mark.asyncio
async def test_execution_sends_the_server_a_data_uri_not_a_uuid() -> None:
    """The model names an attachment; the server receives its bytes."""
    attachment = _attachment()
    provider, calls = await _connected_provider(
        {"meshy_image_to_3d": {"image_url": "data_uri"}}, [_image_tool()]
    )

    result = await provider.execute_tool(
        "meshy_image_to_3d",
        {"image_url": attachment.get_id(), "should_texture": True},
        _execution_context(_registry_serving(attachment)),
    )

    assert result == "ok"
    assert calls == [
        {
            "image_url": "data:image/png;base64,"
            + base64.b64encode(IMAGE_BYTES).decode("ascii"),
            "should_texture": True,
        }
    ]


@pytest.mark.asyncio
async def test_execution_leaves_unconfigured_tools_untouched() -> None:
    provider, calls = await _connected_provider(
        {"some_other_tool": {"image_url": "data_uri"}}, [_image_tool()]
    )

    await provider.execute_tool(
        "meshy_image_to_3d",
        {"image_url": "https://example.invalid/cat.png"},
        _execution_context(cast("AttachmentRegistry", SimpleNamespace())),
    )

    assert calls == [{"image_url": "https://example.invalid/cat.png"}]


@pytest.mark.asyncio
async def test_an_unresolvable_attachment_is_reported_not_forwarded() -> None:
    """A UUID the acting user cannot reach must not reach the server at all."""
    attachment = _attachment()
    provider, calls = await _connected_provider(
        {"meshy_image_to_3d": {"image_url": "data_uri"}}, [_image_tool()]
    )

    result = await provider.execute_tool(
        "meshy_image_to_3d",
        {"image_url": str(uuid.uuid4())},
        _execution_context(_registry_serving(attachment)),
    )

    assert "Error" in result
    assert calls == []


@pytest.mark.asyncio
async def test_a_file_path_is_still_readable_while_the_server_is_called() -> None:
    """The temp file outlives the call it was written for, and no longer."""
    attachment = _attachment()
    provider = _provider({"meshy_image_to_3d": {"image_url": "file_path"}})
    tools = [_image_tool()]
    definitions = provider._format_mcp_definitions_to_dicts(tools, SERVER_ID)
    provider._register_server_tools(
        SERVER_ID,
        definitions,
        provider._build_mcp_descriptors(
            server_id=SERVER_ID, definitions=definitions, discovered_tools=tools
        ),
    )
    provider._initialized = True

    seen: list[anyio.Path] = []

    async def call_tool(*, name: str, arguments: MCPArguments) -> CallToolResult:
        path = anyio.Path(arguments["image_url"])
        assert await path.read_bytes() == IMAGE_BYTES
        seen.append(path)
        return CallToolResult(content=[TextContent(type="text", text="ok")])

    provider._sessions[SERVER_ID] = cast(
        "ClientSession", SimpleNamespace(call_tool=call_tool)
    )

    await provider.execute_tool(
        "meshy_image_to_3d",
        {"image_url": attachment.get_id()},
        _execution_context(_registry_serving(attachment)),
    )

    assert seen
    assert not await seen[0].exists()


def _optional_list_tool() -> Tool:
    """What FastMCP emits for an optional `list[str]`: a union, with no outer type."""
    return Tool(
        name="meshy_multi_image_to_3d",
        description="Turn several images into a 3D model",
        inputSchema={
            "type": "object",
            "properties": {
                "image_urls": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                    "default": None,
                }
            },
        },
    )


@pytest.mark.asyncio
async def test_an_optional_array_stays_a_satisfiable_schema() -> None:
    """A union-wrapped array must not end up advertised as a string AND an array."""
    provider = _provider({"meshy_multi_image_to_3d": {"image_urls": "data_uri"}})
    tools = [_optional_list_tool()]
    definitions = provider._format_mcp_definitions_to_dicts(tools, SERVER_ID)
    provider._register_server_tools(
        SERVER_ID,
        definitions,
        provider._build_mcp_descriptors(
            server_id=SERVER_ID, definitions=definitions, discovered_tools=tools
        ),
    )
    provider._initialized = True

    advertised = _properties(
        list(await provider.get_tool_definitions()), "meshy_multi_image_to_3d"
    )["image_urls"]

    assert advertised["type"] == "array"
    assert advertised["items"]["type"] == "string"
    # The union described the string the server used to want; keeping it beside
    # the array we now ask for would leave the model nothing it could satisfy.
    assert "anyOf" not in advertised


def test_the_list_form_of_a_nullable_type_is_read_as_an_array() -> None:
    """`type: ["array", "null"]` is the other legal spelling of an optional list."""
    provider = _provider({"t": {"p": "data_uri"}})
    tool = Tool(
        name="t",
        description="d",
        inputSchema={
            "type": "object",
            "properties": {"p": {"type": ["array", "null"], "items": {}}},
        },
    )

    definitions = provider._format_mcp_definitions_to_dicts([tool], SERVER_ID)

    parameter = _properties(definitions, "t")["p"]
    assert parameter["type"] == "array"
    assert parameter["items"]["type"] == "attachment"


@pytest.mark.asyncio
async def test_a_non_attachment_in_an_array_is_refused() -> None:
    """An unresolved element must not reach the server as the string it is."""
    with pytest.raises(ValueError, match="not a valid attachment UUID"):
        async with materialised_attachment_arguments(
            {"image_urls": [_attachment(), "https://attacker.invalid/x.png"]},
            {"image_urls": _mode("data_uri")},
        ):
            pass


@pytest.mark.asyncio
async def test_a_non_attachment_scalar_is_refused() -> None:
    with pytest.raises(ValueError, match="not a valid attachment UUID"):
        async with materialised_attachment_arguments(
            {"image_url": "https://attacker.invalid/x.png"},
            {"image_url": _mode("data_uri")},
        ):
            pass


@pytest.mark.asyncio
async def test_a_null_optional_attachment_passes_through() -> None:
    """Declining to pass an optional attachment is the server's business."""
    async with materialised_attachment_arguments(
        {"image_url": None}, {"image_url": _mode("data_uri")}
    ) as materialised:
        assert materialised == {"image_url": None}


@pytest.mark.asyncio
async def test_data_uri_mode_touches_no_filesystem() -> None:
    """The mode that needs no files must not create (and then remove) a directory."""
    attachment = _attachment()

    def refuse_mkdtemp(*args: object, **kwargs: object) -> str:
        msg = "data_uri materialisation created a temporary directory"
        raise AssertionError(msg)

    with mock.patch.object(tempfile, "mkdtemp", refuse_mkdtemp):
        async with materialised_attachment_arguments(
            {"image_url": attachment}, {"image_url": _mode("data_uri")}
        ) as materialised:
            assert materialised["image_url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_an_unresolved_array_element_is_reported_not_forwarded() -> None:
    """The whole call fails; nothing partial reaches the server."""
    attachment = _attachment()
    provider, calls = await _connected_provider(
        {"meshy_multi_image_to_3d": {"image_urls": "data_uri"}},
        [_multi_image_tool()],
    )

    result = await provider.execute_tool(
        "meshy_multi_image_to_3d",
        {"image_urls": [attachment.get_id(), "https://attacker.invalid/x.png"]},
        _execution_context(_registry_serving(attachment)),
    )

    assert "Error" in result
    assert calls == []


def test_array_cardinality_constraints_survive_the_overlay() -> None:
    """How many attachments the server wants is unaffected by what each one is."""
    provider = _provider({"t": {"p": "data_uri"}})
    tool = Tool(
        name="t",
        description="d",
        inputSchema={
            "type": "object",
            "properties": {
                "p": {
                    "type": "array",
                    "items": {"type": "string", "format": "uri"},
                    "minItems": 2,
                    "maxItems": 4,
                }
            },
        },
    )

    parameter = _properties(
        provider._format_mcp_definitions_to_dicts([tool], SERVER_ID), "t"
    )["p"]

    assert parameter["minItems"] == 2
    assert parameter["maxItems"] == 4
    assert parameter["items"] == {"type": "attachment"}


def test_cardinality_is_read_from_inside_a_union() -> None:
    """An optional array carries its constraints on the array branch, not outside."""
    provider = _provider({"t": {"p": "data_uri"}})
    tool = Tool(
        name="t",
        description="d",
        inputSchema={
            "type": "object",
            "properties": {
                "p": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                        },
                        {"type": "null"},
                    ]
                }
            },
        },
    )

    parameter = _properties(
        provider._format_mcp_definitions_to_dicts([tool], SERVER_ID), "t"
    )["p"]

    assert parameter["minItems"] == 2


@pytest.mark.asyncio
async def test_a_failed_cleanup_is_reported_and_does_not_mask_the_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A leftover copy of the user's attachment must not look like a clean call."""
    attachment = _attachment()

    def failing_rmtree(path: str) -> None:
        raise OSError(13, "Permission denied")

    with mock.patch.object(shutil, "rmtree", failing_rmtree):
        async with materialised_attachment_arguments(
            {"image_url": attachment}, {"image_url": _mode("file_path")}
        ) as materialised:
            result = materialised["image_url"]

    # The call's own outcome survives; the leak is loud in the error log.
    assert result
    assert "may remain on disk" in caplog.text
    assert any(record.levelname == "ERROR" for record in caplog.records)


@pytest.mark.asyncio
async def test_a_wrapper_with_a_bad_id_is_refused_not_silently_emptied() -> None:
    """Resolution drops entries it cannot use; the call must not go anyway.

    ``process_attachment_arguments`` expands a ScriptToolResult-shaped wrapper
    and discards a nested id that is not a UUID, which would leave an empty
    array and a call that looks successful.
    """
    attachment = _attachment()
    provider, calls = await _connected_provider(
        {"meshy_multi_image_to_3d": {"image_urls": "data_uri"}},
        [_multi_image_tool()],
    )

    result = await provider.execute_tool(
        "meshy_multi_image_to_3d",
        {"image_urls": [{"attachments": [{"id": "not-a-uuid"}]}]},
        _execution_context(_registry_serving(attachment)),
    )

    assert "Error" in result
    assert calls == []


@pytest.mark.asyncio
async def test_attachments_a_script_already_resolved_are_accepted() -> None:
    """A ScriptAttachment object is taken as the attachment it already is."""
    attachment = _attachment()
    provider, calls = await _connected_provider(
        {"meshy_image_to_3d": {"image_url": "data_uri"}}, [_image_tool()]
    )

    result = await provider.execute_tool(
        "meshy_image_to_3d",
        {"image_url": attachment},
        _execution_context(_registry_serving(attachment)),
    )

    assert result == "ok"
    assert calls == [
        {
            "image_url": "data:image/png;base64,"
            + base64.b64encode(IMAGE_BYTES).decode("ascii")
        }
    ]


@pytest.mark.asyncio
async def test_the_dict_attachment_create_returns_is_accepted() -> None:
    """A script reaches an MCP tool with the script API's own dict, not a UUID.

    ``MontyEngine._get_raw_tool_definitions_sync`` collects raw definitions from
    local providers only, so the script layer cannot tell that an MCP parameter
    takes an attachment and leaves ``attachment_create()``'s result untouched.
    """
    attachment = _attachment()
    provider, calls = await _connected_provider(
        {"meshy_image_to_3d": {"image_url": "data_uri"}}, [_image_tool()]
    )

    result = await provider.execute_tool(
        "meshy_image_to_3d",
        {"image_url": {"id": attachment.get_id(), "filename": "photo.png"}},
        _execution_context(_registry_serving(attachment)),
    )

    assert result == "ok"
    assert calls == [
        {
            "image_url": "data:image/png;base64,"
            + base64.b64encode(IMAGE_BYTES).decode("ascii")
        }
    ]


@pytest.mark.asyncio
async def test_a_tool_result_wrapper_names_its_attachments() -> None:
    attachment = _attachment()
    provider, calls = await _connected_provider(
        {"meshy_multi_image_to_3d": {"image_urls": "data_uri"}},
        [_multi_image_tool()],
    )

    result = await provider.execute_tool(
        "meshy_multi_image_to_3d",
        {"image_urls": [{"attachments": [{"id": attachment.get_id()}]}]},
        _execution_context(_registry_serving(attachment)),
    )

    assert result == "ok"
    assert calls[0]["image_urls"] == [
        "data:image/png;base64," + base64.b64encode(IMAGE_BYTES).decode("ascii")
    ]


@pytest.mark.asyncio
async def test_a_wrapper_naming_several_cannot_fill_a_single_parameter() -> None:
    """Taking the first would send an attachment the caller did not choose."""
    attachment = _attachment()
    provider, calls = await _connected_provider(
        {"meshy_image_to_3d": {"image_url": "data_uri"}}, [_image_tool()]
    )

    result = await provider.execute_tool(
        "meshy_image_to_3d",
        {
            "image_url": {
                "attachments": [
                    {"id": attachment.get_id()},
                    {"id": str(uuid.uuid4())},
                ]
            }
        },
        _execution_context(_registry_serving(attachment)),
    )

    assert "names 2" in result
    assert calls == []


@pytest.mark.asyncio
async def test_a_wrapper_naming_no_attachments_is_refused() -> None:
    """An empty wrapper would delete the caller's element from the array."""
    attachment = _attachment()
    provider, calls = await _connected_provider(
        {"meshy_multi_image_to_3d": {"image_urls": "data_uri"}},
        [_multi_image_tool()],
    )

    result = await provider.execute_tool(
        "meshy_multi_image_to_3d",
        {"image_urls": [{"attachments": []}]},
        _execution_context(_registry_serving(attachment)),
    )

    assert "Error" in result
    assert calls == []


MESHY_IMAGE_URL_DESCRIPTION = (
    "PUBLIC image URL (https://...). Use ONLY for remote images. For local "
    "files use file_path instead. NEVER manually base64-encode."
)


def _meshy_shaped_tool() -> Tool:
    """Meshy's real `image_url`: text that contradicts what we now send."""
    return Tool(
        name="meshy_image_to_3d",
        description="Turn an image into a 3D model",
        inputSchema={
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "description": MESHY_IMAGE_URL_DESCRIPTION,
                }
            },
        },
    )


@pytest.mark.asyncio
async def test_the_server_description_does_not_reach_the_model() -> None:
    """Meshy tells the model to send a URL and never base64-encode.

    Prefixed with "UUID of the attachment.", that is two contradictory
    instructions in one sentence, so the server's text goes with the rest of
    the schema it described.
    """
    provider = _provider({"meshy_image_to_3d": {"image_url": "data_uri"}})
    tools = [_meshy_shaped_tool()]
    definitions = provider._format_mcp_definitions_to_dicts(tools, SERVER_ID)
    provider._register_server_tools(
        SERVER_ID,
        definitions,
        provider._build_mcp_descriptors(
            server_id=SERVER_ID, definitions=definitions, discovered_tools=tools
        ),
    )
    provider._initialized = True

    advertised = _properties(
        list(await provider.get_tool_definitions()), "meshy_image_to_3d"
    )["image_url"]

    assert "NEVER manually base64-encode" not in advertised["description"]
    assert "PUBLIC image URL" not in advertised["description"]
    assert "UUID" in advertised["description"]


def test_an_operator_description_replaces_the_server_s() -> None:
    """Dropping the server's text leaves a place for something accurate."""
    provider = _provider({
        "meshy_image_to_3d": {
            "image_url": {
                "mode": "data_uri",
                "description": "The image to build the model from.",
            }
        }
    })

    definitions = provider._format_mcp_definitions_to_dicts(
        [_meshy_shaped_tool()], SERVER_ID
    )

    assert (
        _properties(definitions, "meshy_image_to_3d")["image_url"]["description"]
        == "The image to build the model from."
    )


@pytest.mark.asyncio
async def test_the_mapping_form_carries_its_mode() -> None:
    """A description does not change how the attachment reaches the server."""
    attachment = _attachment()
    provider, calls = await _connected_provider(
        {
            "meshy_image_to_3d": {
                "image_url": {"mode": "data_uri", "description": "The image."}
            }
        },
        [_image_tool()],
    )

    result = await provider.execute_tool(
        "meshy_image_to_3d",
        {"image_url": attachment.get_id()},
        _execution_context(_registry_serving(attachment)),
    )

    assert result == "ok"
    assert calls[0]["image_url"].startswith("data:image/png;base64,")


def test_a_file_path_mapping_is_still_refused_for_a_remote_server() -> None:
    """The transport rule reads the mode wherever the operator wrote it."""
    with pytest.raises(ValueError, match="requires a stdio MCP server"):
        MCPServerConfigModel.model_validate({
            "transport": "sse",
            "url": "https://example.invalid/mcp",
            "attachment_parameters": {
                "t": {"p": {"mode": "file_path", "description": "x"}}
            },
        })


def test_a_resolved_attachment_still_yields_its_id_to_the_taint_gate() -> None:
    """The policy gate runs before this provider, on whatever the caller passed.

    A script resolves its own attachment arguments before dispatch, so the
    argument reaching the collector is the object rather
    than the id. Reading nothing off it would evaluate an externally
    communicating call without the attachment's provenance.
    """
    attachment = _attachment()

    scalar = infrastructure._collect_attachment_argument_ids(
        {"image_url": attachment},
        schema={
            "type": "object",
            "properties": {"image_url": {"type": "attachment"}},
        },
    )
    array = infrastructure._collect_attachment_argument_ids(
        {"image_urls": [attachment]},
        schema={
            "type": "object",
            "properties": {
                "image_urls": {"type": "array", "items": {"type": "attachment"}}
            },
        },
    )

    assert scalar == {attachment.get_id()}
    assert array == {attachment.get_id()}


def test_an_attachment_outside_an_attachment_slot_is_not_collected() -> None:
    """The slot still decides; the object does not make any parameter one."""
    assert (
        infrastructure._collect_attachment_argument_ids(
            {"other": _attachment()},
            schema={"type": "object", "properties": {"other": {"type": "string"}}},
        )
        == set()
    )
