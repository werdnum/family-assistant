"""Keychute-brokered HTTP API exposed to Monty scripts."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast
from urllib.parse import SplitResult, urlsplit

import httpx

from family_assistant.security.taint import (
    SinkClass,
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.config_models import KeychuteConfig
    from family_assistant.tools.types import ToolExecutionContext

_API_RESPONSE_LIMIT = 64 * 1024
_CREATE_ATTEMPTS = 3
_WAIT_MAX_NETWORK_ERRORS = 5
_WAIT_POLL_SECONDS = 60
_WAIT_HTTP_SLACK_SECONDS = 15
_RETRY_DELAY_SECONDS = 1
_KEYCHUTE_ERROR_HEADER = "x-keychute-error"
_STRIPPED_REQUEST_HEADERS = frozenset({
    "authorization",
    "connection",
    "content-length",
    "cookie",
    "expect",
    "forwarded",
    "host",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "x-http-method-override",
    "x-method-override",
    "x-original-method",
    "x-original-url",
    "x-real-ip",
    "x-rewrite-url",
})


class KeychuteHttpResponse(TypedDict):
    """Script-safe representation of an upstream response."""

    status_code: int
    headers: dict[str, list[str]]
    body: bytes


class KeychuteScriptError(RuntimeError):
    """Raised when a brokered HTTP call cannot be completed."""


class _TransientKeychuteError(KeychuteScriptError):
    """A transport failure that is safe to retry before proxying."""


def _reviewable_request_body(request_body: bytes | None) -> dict[str, str] | None:
    """Represent the exact outbound body as JSON-safe reviewer input."""
    if request_body is None:
        return None
    try:
        return {"encoding": "utf-8", "content": request_body.decode("utf-8")}
    except UnicodeDecodeError:
        return {
            "encoding": "base64",
            "content": base64.b64encode(request_body).decode("ascii"),
        }


class _AccessRequestStatus(TypedDict):
    request_id: str
    state: str
    grant_id: str | None
    deny_reason: str | None


class _Target(TypedDict):
    parsed: SplitResult
    origin: dict[str, object]
    path: str


def _parse_target(url: str) -> _Target:
    """Validate an HTTPS target and retain its raw path/query spelling."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise KeychuteScriptError(f"Invalid target URL: {exc}") from exc
    if parsed.scheme != "https" or parsed.hostname is None:
        raise KeychuteScriptError("Keychute target URL must use https://")
    if parsed.username is not None or parsed.password is not None:
        raise KeychuteScriptError("Keychute target URL must not contain userinfo")
    if parsed.fragment:
        raise KeychuteScriptError("Keychute target URL must not contain a fragment")
    if ":" in parsed.hostname:
        raise KeychuteScriptError(
            "Keychute target URL must use a DNS name or IPv4 address"
        )

    origin: dict[str, object] = {"host": parsed.hostname.rstrip(".")}
    if port is not None:
        origin["port"] = port
    return _Target(parsed=parsed, origin=origin, path=parsed.path or "/")


def _validate_caller_headers(headers: Mapping[str, str] | None) -> None:
    """Reject headers the broker cannot faithfully forward."""
    if headers is None:
        return
    for name in headers:
        normalized = name.lower()
        if normalized in _STRIPPED_REQUEST_HEADERS or normalized.startswith(
            "x-forwarded-"
        ):
            raise KeychuteScriptError(
                f"Header {name!r} is reserved or stripped by Keychute"
            )


async def _bounded_content(response: httpx.Response, limit: int) -> bytes:
    """Read a response stream under a hard byte limit."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise KeychuteScriptError(
                f"Keychute response exceeded the {limit}-byte limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _api_error(
    response: httpx.Response, body: bytes, operation: str
) -> KeychuteScriptError:
    """Build a secret-free error from Keychute's standard envelope."""
    detail = ""
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = None
    error = decoded.get("error") if isinstance(decoded, dict) else None
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        if isinstance(code, str) and isinstance(message, str):
            detail = f": {message} ({code})"
    return KeychuteScriptError(
        f"Keychute {operation} failed with HTTP {response.status_code}{detail}"
    )


def _parse_status(body: bytes) -> _AccessRequestStatus:
    """Validate the access-request status fields used by the client."""
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KeychuteScriptError(
            "Keychute returned malformed request status JSON"
        ) from exc
    if not isinstance(value, dict):
        raise KeychuteScriptError("Keychute returned malformed request status")
    request_id = value.get("request_id")
    state = value.get("state")
    grant_id = value.get("grant_id")
    deny_reason = value.get("deny_reason")
    if not isinstance(request_id, str) or state not in {
        "pending",
        "approved",
        "denied",
        "expired",
    }:
        raise KeychuteScriptError("Keychute returned malformed request status")
    try:
        uuid.UUID(request_id)
    except ValueError as exc:
        raise KeychuteScriptError("Keychute returned malformed request id") from exc
    if grant_id is not None and not isinstance(grant_id, str):
        raise KeychuteScriptError("Keychute returned malformed grant id")
    if grant_id is not None:
        try:
            uuid.UUID(grant_id)
        except ValueError as exc:
            raise KeychuteScriptError("Keychute returned malformed grant id") from exc
    if deny_reason is not None and not isinstance(deny_reason, str):
        raise KeychuteScriptError("Keychute returned malformed denial reason")
    return _AccessRequestStatus(
        request_id=request_id,
        state=state,
        grant_id=grant_id,
        deny_reason=deny_reason,
    )


class KeychuteScriptHttpClient:
    """Execute Keychute's brokered HTTP flow for one script run."""

    def __init__(
        self,
        config: KeychuteConfig,
        script_source: str,
        execution_context: ToolExecutionContext | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._script_source = script_source
        self._execution_context = execution_context
        self._http_client = http_client

    def _base_url(self) -> str:
        raw = self._config.url or os.getenv("KEYCHUTE_URL")
        if raw is None or not raw.strip():
            raise KeychuteScriptError(
                "Keychute URL is not configured; set keychute_config.url or KEYCHUTE_URL"
            )
        url = raw.strip().rstrip("/")
        try:
            parsed = urlsplit(url)
            _ = parsed.port
        except ValueError as exc:
            raise KeychuteScriptError(f"Invalid Keychute URL: {exc}") from exc
        if parsed.scheme != "https" or parsed.hostname is None:
            raise KeychuteScriptError("Keychute URL must use https://")
        if parsed.username is not None or parsed.password is not None:
            raise KeychuteScriptError("Keychute URL must not contain userinfo")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise KeychuteScriptError("Keychute URL must be an origin without a path")
        return url

    async def _bearer(self) -> str:
        configured = self._config.token
        token = (
            configured.get_secret_value() if configured else os.getenv("KEYCHUTE_TOKEN")
        )
        if token:
            return token
        token_file = self._config.token_file or os.getenv("KEYCHUTE_TOKEN_FILE")
        if not token_file:
            raise KeychuteScriptError(
                "Keychute credentials are not configured; set a token or token_file"
            )
        try:
            raw = await asyncio.to_thread(Path(token_file).read_text, encoding="utf-8")
        except OSError as exc:
            raise KeychuteScriptError(
                "Cannot read the configured Keychute token file"
            ) from exc
        token = raw.strip()
        if not token:
            raise KeychuteScriptError("The configured Keychute token file is empty")
        return token

    def _ssl_context(self) -> ssl.SSLContext:
        ca_bundle = self._config.ca_bundle or os.getenv("KEYCHUTE_CA_BUNDLE")
        try:
            return ssl.create_default_context(cafile=ca_bundle)
        except OSError as exc:
            raise KeychuteScriptError(
                "Cannot load the configured Keychute CA bundle"
            ) from exc

    async def _api_call(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
        timeout: float,
    ) -> tuple[httpx.Response, bytes]:
        headers = {"Authorization": f"Bearer {await self._bearer()}"}
        try:
            async with client.stream(
                method,
                f"{self._base_url()}{path}",
                headers=headers,
                json=json_body,
                timeout=timeout,
            ) as response:
                body = await _bounded_content(response, _API_RESPONSE_LIMIT)
                return response, body
        except httpx.HTTPError as exc:
            raise _TransientKeychuteError(f"Keychute {path} request failed") from exc

    async def _create_access_request(
        self,
        client: httpx.AsyncClient,
        request_body: dict[str, object],
    ) -> _AccessRequestStatus:
        last_error: KeychuteScriptError | None = None
        for attempt in range(_CREATE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
            try:
                response, body = await self._api_call(
                    client,
                    "POST",
                    "/v1/access-requests",
                    json_body=request_body,
                    timeout=30,
                )
            except _TransientKeychuteError as exc:
                last_error = exc
                continue
            if 400 <= response.status_code < 500:
                raise _api_error(response, body, "access request")
            if response.is_success:
                try:
                    return _parse_status(body)
                except KeychuteScriptError as exc:
                    last_error = exc
                    continue
            last_error = _api_error(response, body, "access request")
        raise KeychuteScriptError(
            f"Keychute access request failed after {_CREATE_ATTEMPTS} attempts"
        ) from last_error

    async def _wait_for_resolution(
        self,
        client: httpx.AsyncClient,
        status: _AccessRequestStatus,
        approval_timeout_seconds: int,
    ) -> _AccessRequestStatus:
        deadline = time.monotonic() + approval_timeout_seconds
        network_errors = 0
        while status["state"] == "pending":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KeychuteScriptError("Timed out waiting for Keychute approval")
            poll = min(_WAIT_POLL_SECONDS, max(1, int(remaining)))
            try:
                response, body = await self._api_call(
                    client,
                    "GET",
                    f"/v1/access-requests/{status['request_id']}/wait?timeout_seconds={poll}",
                    timeout=poll + _WAIT_HTTP_SLACK_SECONDS,
                )
            except _TransientKeychuteError as exc:
                network_errors += 1
                if network_errors >= _WAIT_MAX_NETWORK_ERRORS:
                    raise KeychuteScriptError(
                        "Keychute approval wait failed repeatedly"
                    ) from exc
                await asyncio.sleep(2)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                network_errors += 1
                if network_errors >= _WAIT_MAX_NETWORK_ERRORS:
                    raise KeychuteScriptError(
                        "Keychute approval wait failed repeatedly"
                    ) from _api_error(response, body, "approval wait")
                await asyncio.sleep(2)
                continue
            if not response.is_success:
                raise _api_error(response, body, "approval wait")
            try:
                status = _parse_status(body)
            except KeychuteScriptError as exc:
                network_errors += 1
                if network_errors >= _WAIT_MAX_NETWORK_ERRORS:
                    raise KeychuteScriptError(
                        "Keychute approval wait returned malformed responses repeatedly"
                    ) from exc
                await asyncio.sleep(2)
                continue
            network_errors = 0
        return status

    async def _validate_grant(
        self,
        client: httpx.AsyncClient,
        grant_id: str,
        target: _Target,
        method: str,
    ) -> None:
        response, body = await self._api_call(
            client,
            "GET",
            f"/v1/grants/{grant_id}",
            timeout=30,
        )
        if not response.is_success:
            raise _api_error(response, body, "grant lookup")
        try:
            value = json.loads(body)
            constraints = value["constraints"]
            origins = constraints["origins"]
            methods = constraints["methods"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise KeychuteScriptError(
                "Keychute returned malformed grant metadata"
            ) from exc
        if value.get("mechanism") != "brokered" or value.get("revoked") is not False:
            raise KeychuteScriptError("Keychute returned an unusable brokered grant")
        if (
            not isinstance(origins, list)
            or len(origins) != 1
            or origins[0] != target["origin"]
        ):
            raise KeychuteScriptError(
                "Keychute grant origin does not match the target URL"
            )
        if not isinstance(methods, list) or method not in methods:
            raise KeychuteScriptError(
                "Keychute grant does not allow the requested HTTP method"
            )

    async def _proxy(
        self,
        client: httpx.AsyncClient,
        grant_id: str,
        target: _Target,
        method: str,
        headers: Mapping[str, str] | None,
        request_body: bytes | None,
        request_timeout_seconds: float,
    ) -> KeychuteHttpResponse:
        proxy_headers = dict(headers or {})
        proxy_headers["Authorization"] = f"Bearer {await self._bearer()}"
        parsed = target["parsed"]
        proxy_path = f"/v1/grants/{grant_id}/proxy{target['path']}"
        if parsed.query:
            proxy_path += f"?{parsed.query}"
        timeout = None if request_timeout_seconds == 0 else request_timeout_seconds
        try:
            async with client.stream(
                method,
                f"{self._base_url()}{proxy_path}",
                headers=proxy_headers,
                content=request_body,
                timeout=timeout,
            ) as response:
                body = await _bounded_content(response, self._config.max_response_bytes)
        except httpx.HTTPError as exc:
            raise KeychuteScriptError("Keychute brokered proxy request failed") from exc
        if _KEYCHUTE_ERROR_HEADER in response.headers:
            raise _api_error(response, body, "brokered proxy")
        response_headers: dict[str, list[str]] = {}
        for name, value in response.headers.multi_items():
            response_headers.setdefault(name.lower(), []).append(value)
        return KeychuteHttpResponse(
            status_code=response.status_code,
            headers=response_headers,
            body=body,
        )

    async def _request_with_client(
        self,
        client: httpx.AsyncClient,
        *,
        secret_name: str,
        url: str,
        method: str,
        headers: Mapping[str, str] | None,
        request_body: bytes | None,
        reason: str,
        ttl_seconds: int,
        max_uses: int,
        approval_timeout_seconds: int,
        request_timeout_seconds: float,
    ) -> KeychuteHttpResponse:
        target = _parse_target(url)
        normalized_method = method.upper()
        _validate_caller_headers(headers)
        await self._authorize_egress(
            secret_name=secret_name,
            url=url,
            method=normalized_method,
            headers=headers,
            request_body=request_body,
            reason=reason,
            ttl_seconds=ttl_seconds,
            max_uses=max_uses,
            approval_timeout_seconds=approval_timeout_seconds,
            request_timeout_seconds=request_timeout_seconds,
        )
        idempotency_key = str(uuid.uuid4())
        status = await self._create_access_request(
            client,
            {
                "idempotency_key": idempotency_key,
                "secret_name": secret_name,
                "mechanism": "brokered",
                "constraints": {
                    "origins": [target["origin"]],
                    "methods": [normalized_method],
                    "path_prefixes": [target["path"]],
                    "ttl_seconds": ttl_seconds,
                    "max_uses": max_uses or None,
                },
                "context": {
                    "reason": reason,
                    "structured": {"script": self._script_source},
                },
            },
        )
        status = await self._wait_for_resolution(
            client, status, approval_timeout_seconds
        )
        if status["state"] == "denied":
            suffix = f": {status['deny_reason']}" if status["deny_reason"] else ""
            raise KeychuteScriptError(f"Keychute request was denied{suffix}")
        if status["state"] == "expired":
            raise KeychuteScriptError("Keychute request expired before approval")
        grant_id = status["grant_id"]
        if status["state"] != "approved" or grant_id is None:
            raise KeychuteScriptError(
                "Keychute approved the request without a grant id"
            )
        await self._validate_grant(client, grant_id, target, normalized_method)
        response = await self._proxy(
            client,
            grant_id,
            target,
            normalized_method,
            headers,
            request_body,
            request_timeout_seconds,
        )
        if (
            self._execution_context is not None
            and self._execution_context.taint_tracker is not None
        ):
            self._execution_context.taint_tracker.add_source(
                TaintSource(
                    source_type=TaintSourceType.TOOL_OUTPUT,
                    source_id="keychute_http_request",
                    tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                    labels=frozenset(),
                    reason="A Keychute-brokered HTTP response is untrusted external content.",
                )
            )
        return response

    async def _authorize_egress(
        self,
        *,
        secret_name: str,
        url: str,
        method: str,
        headers: Mapping[str, str] | None,
        request_body: bytes | None,
        reason: str,
        ttl_seconds: int,
        max_uses: int,
        approval_timeout_seconds: int,
        request_timeout_seconds: float,
    ) -> None:
        """Apply the profile's runtime-taint policy before contacting Keychute."""
        context = self._execution_context
        if context is None or context.taint_tracker is None:
            return

        provider = context.tools_provider
        if provider is None and context.processing_service is not None:
            provider = context.processing_service.tools_provider
        if provider is None:
            raise KeychuteScriptError(
                "Keychute egress denied because runtime taint policy is unavailable"
            )

        # Lazy import avoids scripting -> tools package initialization cycles.
        from family_assistant.tools.infrastructure import (  # noqa: PLC0415
            TaintTrackingToolsProvider,
            ToolPolicyDeniedError,
            find_provider_by_type,
        )

        authorizer = find_provider_by_type(provider, TaintTrackingToolsProvider)
        if authorizer is None:
            raise KeychuteScriptError(
                "Keychute egress denied because runtime taint policy is unavailable"
            )
        try:
            await authorizer.authorize_taint_sink(
                name="keychute_http_request",
                sink_class=SinkClass.SANDBOX_NETWORK,
                arguments={
                    "secret_name": secret_name,
                    "url": url,
                    "method": method,
                    "headers": dict(headers or {}),
                    "body": await asyncio.to_thread(
                        _reviewable_request_body, request_body
                    ),
                    "reason": reason,
                    "ttl_seconds": ttl_seconds,
                    "max_uses": max_uses,
                    "approval_timeout_seconds": approval_timeout_seconds,
                    "request_timeout_seconds": request_timeout_seconds,
                },
                context=context,
            )
        except ToolPolicyDeniedError as exc:
            raise KeychuteScriptError(
                f"Keychute egress denied by runtime taint policy: {exc.reason}"
            ) from exc

    async def request(
        self,
        secret_name: str,
        url: str,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: str | bytes | None = None,
        reason: str = "",
        ttl_seconds: int = 300,
        max_uses: int = 1,
        approval_timeout_seconds: int = 300,
        request_timeout_seconds: float = 120.0,
    ) -> KeychuteHttpResponse:
        """Make one credential-injected HTTP call through Keychute."""
        request_body = body.encode() if isinstance(body, str) else body
        if self._http_client is not None:
            return await self._request_with_client(
                self._http_client,
                secret_name=secret_name,
                url=url,
                method=method,
                headers=headers,
                request_body=request_body,
                reason=reason,
                ttl_seconds=ttl_seconds,
                max_uses=max_uses,
                approval_timeout_seconds=approval_timeout_seconds,
                request_timeout_seconds=request_timeout_seconds,
            )
        async with httpx.AsyncClient(
            verify=self._ssl_context(), follow_redirects=False
        ) as client:
            return await self._request_with_client(
                client,
                secret_name=secret_name,
                url=url,
                method=method,
                headers=headers,
                request_body=request_body,
                reason=reason,
                ttl_seconds=ttl_seconds,
                max_uses=max_uses,
                approval_timeout_seconds=approval_timeout_seconds,
                request_timeout_seconds=request_timeout_seconds,
            )


def add_keychute_http_api(
    # ast-grep-ignore: no-dict-any - arbitrary values form the script namespace
    globals_dict: dict[str, object] | None,
    *,
    config: KeychuteConfig,
    script_source: str,
    execution_context: ToolExecutionContext | None = None,
) -> dict[str, object] | None:
    """Add the brokered HTTP function when the operator enabled Keychute."""
    if not config.enabled:
        return globals_dict
    result = dict(globals_dict or {})
    result["keychute_http_request"] = KeychuteScriptHttpClient(
        config, script_source, execution_context
    ).request
    return result


def get_keychute_config(
    execution_context: ToolExecutionContext,
) -> KeychuteConfig | None:
    """Return a usable Keychute config from a tool execution context."""
    processing_service = execution_context.processing_service
    candidate = getattr(
        getattr(processing_service, "app_config", None),
        "keychute_config",
        None,
    )
    if not isinstance(getattr(candidate, "enabled", None), bool):
        return None
    return cast("KeychuteConfig", candidate)


def keychute_external_function_names(
    config: KeychuteConfig | None,
) -> list[str] | None:
    """Return the callable namespace exposed by the configured integration."""
    if config is None or not config.enabled:
        return None
    return ["keychute_http_request"]
