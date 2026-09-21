"""Family Assistant's stable A2A protocol model types.

The A2A SDK moved its primary types from Pydantic models to protobuf messages
in v1. Family Assistant's existing server and persistence boundaries still use
the v0.3 JSON model, so keep that representation stable while the outbound
client uses the SDK's v1 types and compatibility transports at the wire edge.
"""

from a2a.compat.v0_3.types import (
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
    Artifact,
    DataPart,
    FilePart,
    FileWithBytes,
    FileWithUri,
    JSONRPCError,
    JSONRPCErrorResponse,
    JSONRPCRequest,
    JSONRPCResponse,
    Message,
    MessageSendParams,
    Part,
    Role,
    SendMessageSuccessResponse,
    SendStreamingMessageSuccessResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskIdParams,
    TaskQueryParams,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)

__all__ = [
    "AgentCapabilities",
    "AgentCard",
    "AgentProvider",
    "AgentSkill",
    "Artifact",
    "DataPart",
    "FilePart",
    "FileWithBytes",
    "FileWithUri",
    "JSONRPCError",
    "JSONRPCErrorResponse",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "Message",
    "MessageSendParams",
    "Part",
    "Role",
    "SendMessageSuccessResponse",
    "SendStreamingMessageSuccessResponse",
    "Task",
    "TaskArtifactUpdateEvent",
    "TaskIdParams",
    "TaskQueryParams",
    "TaskState",
    "TaskStatus",
    "TaskStatusUpdateEvent",
    "TextPart",
]
