"""SREGym MCP integration.

SREGym (https://github.com/SREGym/SREGym) is an SRE agent benchmark that runs
real microservice applications on Kubernetes, injects faults, and grades an
agent's diagnosis and mitigation with oracles.

It exposes its agent interface as MCP tool-servers over SSE, mounted per
concern on one HTTP server:

  /kubectl     exec_kubectl_cmd_safely, rollback_command,
               get_previous_rollbackable_cmd
  /jaeger      get_services, get_operations, get_traces, get_dependency_graph
  /loki        get_logs, get_labels, get_label_values
  /prometheus  get_metrics, get_alerts
  /submit      submit

This module centralizes SREGym MCP configuration, validation, and tool-calling
so the verifier and the investigation tools share one transport and parsing
path. Only the ``sse`` transport is supported, because that is what SREGym's
server (``mcp_server/sregym_mcp_server.py``) mounts.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Coroutine, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import httpx
from mcp import ClientSession, types  # type: ignore[import-not-found]
from mcp.client.sse import sse_client  # type: ignore[import-not-found]
from pydantic import Field, field_validator
from typing_extensions import TypedDict

from config.strict_config import StrictConfigModel
from integrations._validation_helpers import report_classify_failure, report_validation_failure

logger = logging.getLogger(__name__)

DEFAULT_SREGYM_BASE_URL = "http://127.0.0.1:9954"

#: Endpoint path -> mounted on the SREGym MCP server.
SREGYM_ENDPOINTS: tuple[str, ...] = (
    "kubectl",
    "jaeger",
    "loki",
    "prometheus",
    "submit",
)

#: Endpoints enabled by default. Start narrow (investigate + submit) and widen
#: once the loop is proven; see ``endpoints`` on the config.
DEFAULT_SREGYM_ENDPOINTS: tuple[str, ...] = ("kubectl", "submit")


class SREGymToolDescriptor(TypedDict):
    """A tool exposed by a SREGym MCP endpoint."""

    name: str
    description: str
    input_schema: object | None


class SREGymContentItem(TypedDict, total=False):
    """Normalized content item returned by an MCP tool call."""

    type: str
    text: str
    uri: str
    mime_type: str


class SREGymToolCallResult(TypedDict, total=False):
    """Normalized response from a SREGym MCP tool call."""

    is_error: bool
    text: str
    content: list[SREGymContentItem]
    structured_content: object | None
    tool: str
    arguments: dict[str, object]
    endpoint: str


class SREGymConfig(StrictConfigModel):
    """Normalized SREGym MCP connection settings."""

    url: str = DEFAULT_SREGYM_BASE_URL
    endpoints: tuple[str, ...] = DEFAULT_SREGYM_ENDPOINTS
    auth_token: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=60.0, gt=0)
    integration_id: str = ""

    @field_validator("url", mode="before")
    @classmethod
    def _normalize_url(cls, value: object) -> str:
        normalized = str(value or "").strip()
        return normalized.rstrip("/") if normalized else ""

    @field_validator("endpoints", mode="before")
    @classmethod
    def _normalize_endpoints(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return DEFAULT_SREGYM_ENDPOINTS
        if isinstance(value, str):
            raw = [part.strip() for part in value.replace(",", " ").split()]
        elif isinstance(value, (list, tuple, set)):
            raw = [str(part).strip() for part in value]
        else:
            return DEFAULT_SREGYM_ENDPOINTS
        kept = tuple(part for part in raw if part in SREGYM_ENDPOINTS)
        return kept or DEFAULT_SREGYM_ENDPOINTS

    @field_validator("auth_token", mode="before")
    @classmethod
    def _normalize_auth_token(cls, value: object) -> str:
        token = str(value or "").strip()
        if token.lower().startswith("bearer "):
            token = token.split(None, 1)[1].strip()
        return token

    @field_validator("headers", mode="before")
    @classmethod
    def _normalize_headers(cls, value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {str(k): str(v).strip() for k, v in value.items() if str(v).strip()}

    @property
    def is_configured(self) -> bool:
        return bool(self.url)

    @property
    def request_headers(self) -> dict[str, str]:
        headers = {k: v for k, v in self.headers.items() if v}
        if self.auth_token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def endpoint_url(self, endpoint: str) -> str:
        """Return the SSE URL for one mounted endpoint."""
        return f"{self.url}/{endpoint}/sse"


@dataclass(frozen=True)
class SREGymValidationResult:
    """Result of validating a SREGym MCP connection."""

    ok: bool
    detail: str
    tool_names: tuple[str, ...] = ()


def build_sregym_config(raw: Mapping[str, object] | None) -> SREGymConfig:
    """Build a normalized SREGym config object from env/store data."""
    payload = dict(raw or {})
    allowed = set(SREGymConfig.model_fields)
    sanitized = {key: value for key, value in payload.items() if key in allowed}
    return SREGymConfig.model_validate(sanitized)


def sregym_config_from_env() -> SREGymConfig | None:
    """Load a SREGym config from environment variables."""
    url = os.getenv("SREGYM_URL", "").strip() or DEFAULT_SREGYM_BASE_URL
    endpoints = os.getenv("SREGYM_ENDPOINTS", "").strip()
    auth_token = os.getenv("SREGYM_AUTH_TOKEN", "").strip()
    return build_sregym_config(
        {
            "url": url,
            "endpoints": endpoints or list(DEFAULT_SREGYM_ENDPOINTS),
            "auth_token": auth_token,
        }
    )


def sregym_runtime_unavailable_reason(config: SREGymConfig) -> str | None:
    """Return a setup error when the config cannot be used."""
    if not config.is_configured:
        return (
            "SREGym is not configured: provide the MCP server base URL "
            "(set SREGYM_URL, e.g. http://127.0.0.1:9954)."
        )
    return None


def resolve_endpoint_for_tool(config: SREGymConfig, tool_name: str) -> str | None:
    """Best-effort map of a tool name to the endpoint that serves it.

    Kept as a static map so a tool call does not have to re-list every endpoint
    just to find its owner. Unknown names return ``None`` and the caller falls
    back to searching the configured endpoints.
    """
    static_map = {
        "exec_kubectl_cmd_safely": "kubectl",
        "rollback_command": "kubectl",
        "get_previous_rollbackable_cmd": "kubectl",
        "get_services": "jaeger",
        "get_operations": "jaeger",
        "get_traces": "jaeger",
        "get_dependency_graph": "jaeger",
        "get_logs": "loki",
        "get_labels": "loki",
        "get_label_values": "loki",
        "get_metrics": "prometheus",
        "get_alerts": "prometheus",
        "submit": "submit",
    }
    endpoint = static_map.get(tool_name)
    if endpoint and endpoint in config.endpoints:
        return endpoint
    return None


@asynccontextmanager
async def _open_sregym_session(config: SREGymConfig, endpoint: str) -> AsyncIterator[ClientSession]:
    """Open an MCP client session against one SREGym SSE endpoint."""
    stack = AsyncExitStack()
    try:
        url = config.endpoint_url(endpoint)
        read_timeout = max(60.0, config.timeout_seconds)
        read_stream, write_stream = await stack.enter_async_context(
            sse_client(
                url,
                headers=config.request_headers,
                timeout=config.timeout_seconds,
                sse_read_timeout=read_timeout,
            )
        )
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        yield session
    finally:
        await stack.aclose()


def _run_async(coro: Coroutine[object, object, object]) -> object:
    try:
        return asyncio.run(coro)
    except BaseException:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        raise


def _root_cause_message(exc: BaseException) -> str:
    """Best-effort unwrap for ExceptionGroup/TaskGroup chains."""
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return _root_cause_message(exc.exceptions[0])
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException):
        return _root_cause_message(cause)
    context = getattr(exc, "__context__", None)
    if isinstance(context, BaseException):
        return _root_cause_message(context)
    if isinstance(exc, TimeoutError):
        return "SREGym MCP tool call timed out"
    return str(exc).strip() or exc.__class__.__name__


def describe_sregym_error(err: BaseException, config: SREGymConfig) -> str:
    """Render a human-readable error with a setup hint when useful."""
    detail = _root_cause_message(err)
    hints: list[str] = []

    if isinstance(err, (httpx.ConnectError, httpx.ConnectTimeout)):
        hints.append(
            f"Could not reach {config.url}. Confirm the SREGym MCP server is running "
            "(python -m mcp_server.sregym_mcp_server in the SREGym repo)."
        )
    if "timed out" in detail.lower():
        hints.append(
            f"The tool did not return within {config.timeout_seconds:.1f}s. "
            "Raise SREGymConfig.timeout_seconds for slow kubectl calls."
        )

    if hints:
        return f"{detail} Hint: {' '.join(hints)}"
    return detail


def _tool_result_to_dict(result: types.CallToolResult) -> SREGymToolCallResult:
    text_parts: list[str] = []
    content_items: list[SREGymContentItem] = []

    for item in result.content:
        if isinstance(item, types.TextContent):
            text_parts.append(item.text)
            content_items.append({"type": "text", "text": item.text})
        elif isinstance(item, types.EmbeddedResource):
            resource = item.resource
            if isinstance(resource, types.TextResourceContents):
                content_items.append(
                    {
                        "type": "resource_text",
                        "uri": str(resource.uri),
                        "text": resource.text,
                    }
                )
                text_parts.append(resource.text)
            elif isinstance(resource, types.BlobResourceContents):
                content_items.append(
                    {
                        "type": "resource_blob",
                        "uri": str(resource.uri),
                        "mime_type": resource.mimeType or "",
                    }
                )
        else:
            content_items.append({"type": getattr(item, "type", "unknown")})

    structured = getattr(result, "structuredContent", None)
    text_output = "\n".join(part.strip() for part in text_parts if part.strip()).strip()
    return {
        "is_error": bool(result.isError),
        "text": text_output,
        "content": content_items,
        "structured_content": structured,
    }


async def _list_tools_async(config: SREGymConfig, endpoint: str) -> list[types.Tool]:
    async with _open_sregym_session(config, endpoint) as session:
        result = await session.list_tools()
        return list(result.tools)


def list_sregym_tools(config: SREGymConfig) -> list[SREGymToolDescriptor]:
    """List tools across every configured SREGym endpoint."""
    descriptors: list[SREGymToolDescriptor] = []
    for endpoint in config.endpoints:
        tools = cast(
            list[types.Tool],
            _run_async(
                asyncio.wait_for(
                    _list_tools_async(config, endpoint), timeout=config.timeout_seconds
                )
            ),
        )
        for tool in tools:
            descriptors.append(
                {
                    "name": tool.name,
                    "description": f"[{endpoint}] {tool.description or ''}".strip(),
                    "input_schema": getattr(tool, "inputSchema", None),
                }
            )
    return descriptors


async def _call_tool_async(
    config: SREGymConfig,
    endpoint: str,
    tool_name: str,
    arguments: dict[str, object] | None = None,
) -> SREGymToolCallResult:
    async with _open_sregym_session(config, endpoint) as session:
        result = await session.call_tool(tool_name, arguments or {})
        payload = _tool_result_to_dict(result)
        payload["tool"] = tool_name
        payload["arguments"] = arguments or {}
        payload["endpoint"] = endpoint
        return payload


def call_sregym_tool(
    config: SREGymConfig,
    tool_name: str,
    arguments: dict[str, object] | None = None,
    endpoint: str | None = None,
) -> SREGymToolCallResult:
    """Call a SREGym MCP tool and normalize the result.

    When ``endpoint`` is not given, the tool name is mapped to its owning
    endpoint; unmapped names fall back to trying each configured endpoint.
    """
    target = endpoint or resolve_endpoint_for_tool(config, tool_name)
    candidates = (target,) if target else config.endpoints

    last_error: BaseException | None = None
    for candidate in candidates:
        try:
            return cast(
                SREGymToolCallResult,
                _run_async(
                    asyncio.wait_for(
                        _call_tool_async(config, candidate, tool_name, arguments),
                        timeout=config.timeout_seconds,
                    )
                ),
            )
        except Exception as err:  # try the next endpoint before giving up
            last_error = err
            continue

    if last_error is not None:
        raise last_error
    raise ValueError(f"No SREGym endpoint configured for tool '{tool_name}'.")


def validate_sregym_config(config: SREGymConfig) -> SREGymValidationResult:
    """Validate SREGym connectivity by listing tools on every endpoint."""
    runtime_error = sregym_runtime_unavailable_reason(config)
    if runtime_error is not None:
        return SREGymValidationResult(
            ok=False,
            detail=f"SREGym validation failed: {runtime_error}",
        )

    try:
        tools = list_sregym_tools(config)
        tool_names = tuple(sorted(t["name"] for t in tools))
        if not tool_names:
            return SREGymValidationResult(
                ok=False,
                detail=(
                    f"SREGym connected at {config.url} but exposed no tools. "
                    "Check that the MCP server mounted the expected endpoints."
                ),
            )
        return SREGymValidationResult(
            ok=True,
            detail=(
                f"SREGym connected at {config.url} "
                f"({', '.join(config.endpoints)}); discovered {len(tool_names)} tool(s)."
            ),
            tool_names=tool_names,
        )
    except Exception as err:
        report_validation_failure(
            err,
            logger=logger,
            integration="sregym",
            method="validate_sregym_config",
        )
        return SREGymValidationResult(
            ok=False,
            detail=f"SREGym validation failed: {describe_sregym_error(err, config)}",
        )


def classify(credentials: dict[str, Any], record_id: str) -> tuple[SREGymConfig | None, str | None]:
    try:
        cfg = build_sregym_config(
            {
                "url": credentials.get("url", ""),
                "endpoints": credentials.get("endpoints", list(DEFAULT_SREGYM_ENDPOINTS)),
                "auth_token": credentials.get("auth_token", ""),
                "integration_id": record_id,
            }
        )
    except Exception as exc:
        report_classify_failure(exc, logger=logger, integration="sregym", record_id=record_id)
        return None, None
    if cfg.is_configured:
        return cfg, "sregym"
    return None, None
