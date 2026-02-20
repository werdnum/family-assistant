"""Pydantic models for the A2A (Agent-to-Agent) protocol.

Based on the A2A specification (https://a2a-protocol.org/).
Covers Agent Card, JSON-RPC messages, Tasks, Parts, Artifacts, and SSE events.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

# ===== Parts =====
# Note: A2A protocol fields use dict[str, Any] for metadata and data payloads
# because the spec defines these as open-ended JSON objects for extensibility.
# This is the same pattern used for OpenAI/Google provider compatibility elsewhere.


class TextPart(BaseModel):
    """Text content within a message or artifact."""

    type: Literal["text"] = "text"
    text: str
    # ast-grep-ignore: no-dict-any - A2A protocol spec allows arbitrary metadata
    metadata: dict[str, Any] | None = None


class FilePart(BaseModel):
    """File content, either inline (base64) or by URI reference."""

    type: Literal["file"] = "file"
    file: FileContent
    # ast-grep-ignore: no-dict-any - A2A protocol spec allows arbitrary metadata
    metadata: dict[str, Any] | None = None


class FileContent(BaseModel):
    """File content details for a FilePart."""

    name: str | None = None
    mimeType: str | None = None
    bytes: str | None = None  # base64-encoded
    uri: str | None = None


class DataPart(BaseModel):
    """Structured JSON data within a message or artifact."""

    type: Literal["data"] = "data"
    # ast-grep-ignore: no-dict-any - A2A protocol: arbitrary structured JSON data
    data: dict[str, Any]
    # ast-grep-ignore: no-dict-any - A2A protocol spec allows arbitrary metadata
    metadata: dict[str, Any] | None = None


Part = TextPart | FilePart | DataPart


# ===== Messages =====


class Message(BaseModel):
    """A single message in an A2A conversation."""

    role: Literal["user", "agent"]
    parts: list[Part]
    messageId: str | None = None
    taskId: str | None = None
    contextId: str | None = None
    # ast-grep-ignore: no-dict-any - A2A protocol spec allows arbitrary metadata
    metadata: dict[str, Any] | None = None


# ===== Task State =====


class TaskState(StrEnum):
    """Task lifecycle states."""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class TaskStatus(BaseModel):
    """Current status of a task."""

    state: TaskState
    message: Message | None = None
    timestamp: str | None = None


# ===== Artifacts =====


class Artifact(BaseModel):
    """An output produced by an agent during task execution."""

    name: str | None = None
    description: str | None = None
    parts: list[Part]
    index: int | None = None
    append: bool | None = None
    lastChunk: bool | None = None
    # ast-grep-ignore: no-dict-any - A2A protocol spec allows arbitrary metadata
    metadata: dict[str, Any] | None = None


# ===== Task =====


class Task(BaseModel):
    """A2A Task representing a unit of work."""

    id: str
    contextId: str | None = None
    status: TaskStatus
    artifacts: list[Artifact] | None = None
    history: list[Message] | None = None
    # ast-grep-ignore: no-dict-any - A2A protocol spec allows arbitrary metadata
    metadata: dict[str, Any] | None = None


# ===== JSON-RPC 2.0 =====


class JSONRPCRequest(BaseModel):
    """JSON-RPC 2.0 request."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int
    method: str
    # ast-grep-ignore: no-dict-any - JSON-RPC params are method-specific
    params: dict[str, Any] | None = None


class JSONRPCError(BaseModel):
    """JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Any | None = None


class JSONRPCResponse(BaseModel):
    """JSON-RPC 2.0 response."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None
    result: Any | None = None
    error: JSONRPCError | None = None


# ===== JSON-RPC Method Params =====


class SendMessageParams(BaseModel):
    """Parameters for message/send and message/stream."""

    message: Message
    configuration: TaskConfiguration | None = None


class TaskConfiguration(BaseModel):
    """Configuration for task execution."""

    acceptedOutputModes: list[str] | None = None
    blocking: bool | None = None
    pushNotification: PushNotificationConfig | None = None


class PushNotificationConfig(BaseModel):
    """Push notification configuration."""

    url: str
    token: str | None = None
    # ast-grep-ignore: no-dict-any - A2A protocol: flexible auth config
    authentication: dict[str, Any] | None = None


class TaskIdParams(BaseModel):
    """Parameters for task operations requiring a task ID."""

    id: str


class TaskQueryParams(BaseModel):
    """Parameters for tasks/list."""

    contextId: str | None = None


# ===== SSE Events =====


class TaskStatusUpdateEvent(BaseModel):
    """SSE event for task status changes."""

    type: Literal["status"] = "status"
    taskId: str
    contextId: str | None = None
    status: TaskStatus
    final: bool = False


class TaskArtifactUpdateEvent(BaseModel):
    """SSE event for artifact delivery."""

    type: Literal["artifact"] = "artifact"
    taskId: str
    contextId: str | None = None
    artifact: Artifact


# ===== Agent Card =====


class AgentSkill(BaseModel):
    """A skill/capability advertised in an Agent Card."""

    id: str
    name: str
    description: str | None = None
    tags: list[str] | None = None
    examples: list[str] | None = None


class AgentCapabilities(BaseModel):
    """Capabilities declared in an Agent Card."""

    streaming: bool = False
    pushNotifications: bool = False
    stateTransitionHistory: bool = False


class AgentSecurityScheme(BaseModel):
    """Security scheme in an Agent Card (aligned with OpenAPI)."""

    type: str  # "http", "apiKey", "oauth2", "openIdConnect"
    scheme: str | None = None  # e.g. "bearer"
    bearerFormat: str | None = None
    description: str | None = None


class AgentAuthentication(BaseModel):
    """Authentication configuration for an Agent Card."""

    schemes: list[str] = Field(default_factory=list)
    credentials: str | None = None


class AgentProvider(BaseModel):
    """Provider information for an Agent Card."""

    organization: str
    url: str | None = None


class AgentCard(BaseModel):
    """A2A Agent Card for capability discovery."""

    name: str
    description: str | None = None
    url: str
    version: str = "0.3.0"
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    skills: list[AgentSkill] = Field(default_factory=list)
    defaultInputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    defaultOutputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    provider: AgentProvider | None = None
    authentication: AgentAuthentication | None = None
    securitySchemes: dict[str, AgentSecurityScheme] | None = None
    security: list[dict[str, list[str]]] | None = None
