"""SREGym MCP-backed tools.

Exposes a running SREGym benchmark environment — cluster inspection and
mutation via kubectl, traces, logs, metrics, and answer submission — to the
investigation surface. The tool surface is intentionally generic (a discovery
tool plus a named-call tool) so it keeps working as SREGym adds or renames
individual MCP-side tools.
"""

from __future__ import annotations

from core.tool_framework.telemetry import report_run_error
from core.tool_framework.tool_decorator import tool
from core.tool_framework.utils.mcp_params import first_list, first_string
from core.tool_framework.utils.mcp_tool_listing import build_mcp_tool_listing
from integrations.sregym import (
    SREGymConfig,
    SREGymToolCallResult,
    build_sregym_config,
    describe_sregym_error,
    sregym_config_from_env,
    sregym_runtime_unavailable_reason,
)
from integrations.sregym import call_sregym_tool as invoke_sregym_tool
from integrations.sregym import list_sregym_tools as list_sregym_server_tools

SREGymParams = dict[str, object]
SREGymResponse = dict[str, object]

_COMPONENT = "integrations.sregym.tools.sregym_tool"


def _unavailable_response(
    error: str,
    *,
    tool_name: str | None = None,
    arguments: SREGymParams | None = None,
) -> SREGymResponse:
    payload: SREGymResponse = {
        "source": "sregym",
        "available": False,
        "error": error,
    }
    if tool_name:
        payload["tool"] = tool_name
    if arguments is not None:
        payload["arguments"] = arguments
    return payload


def _resolve_config(
    sregym_url: str | None,
    sregym_endpoints: list[str] | None,
    sregym_token: str | None,
) -> SREGymConfig | None:
    env_config = sregym_config_from_env()
    if any((sregym_url, sregym_endpoints, sregym_token)):
        raw_config: SREGymParams = {
            "url": sregym_url or (env_config.url if env_config else ""),
            "endpoints": sregym_endpoints
            or (list(env_config.endpoints) if env_config else []),
            "auth_token": sregym_token or (env_config.auth_token if env_config else ""),
            "headers": env_config.headers if env_config else {},
        }
        return build_sregym_config(raw_config)
    return env_config


def _sregym_available(sources: dict[str, dict]) -> bool:
    return bool(sources.get("sregym", {}).get("connection_verified"))


def _sregym_extract_params(sources: dict[str, dict]) -> SREGymParams:
    source = sources.get("sregym", {})
    if not source:
        return {}
    return {
        "sregym_url": first_string(source, "sregym_url", "url"),
        "sregym_endpoints": first_list(source, "sregym_endpoints", "endpoints"),
        "sregym_token": first_string(source, "sregym_token", "auth_token"),
    }


def _normalize_tool_result(result: SREGymToolCallResult) -> SREGymResponse:
    if result.get("is_error"):
        return _unavailable_response(
            str(result.get("text") or "SREGym tool call failed."),
            tool_name=str(result.get("tool", "")).strip() or None,
            arguments=result.get("arguments", {}),
        )
    return {
        "source": "sregym",
        "available": True,
        "tool": result.get("tool"),
        "endpoint": result.get("endpoint"),
        "arguments": result.get("arguments", {}),
        "text": result.get("text", ""),
        "structured_content": result.get("structured_content"),
        "content": result.get("content", []),
    }


@tool(
    name="list_sregym_tools",
    source="sregym",
    description=(
        "List the tools exposed by the connected SREGym benchmark environment "
        "(kubectl execution and rollback, traces, logs, metrics, and answer "
        "submission). Pass name_filter (e.g. 'kubectl logs') to narrow the list, "
        "and include_schema=true on a narrowed list to fetch the input schema of "
        "the specific tool you intend to call."
    ),
    use_cases=[
        "Discovering which SREGym tools are available before calling one",
        "Finding the right tool for a task by passing a name_filter (e.g. 'kubectl')",
        "Fetching the input schema of a specific tool with include_schema before calling it",
    ],
    surfaces=("investigation", "chat"),
    input_schema={
        "type": "object",
        "properties": {
            "name_filter": {
                "type": "string",
                "description": (
                    "Optional space- or comma-separated terms; tools whose name or "
                    "description contains any term are returned (e.g. 'kubectl logs')."
                ),
            },
            "include_schema": {
                "type": "boolean",
                "description": (
                    "Include each tool's full input_schema. Only honored when the "
                    "(filtered) result set is small; narrow with name_filter first."
                ),
            },
            "sregym_url": {"type": "string"},
            "sregym_endpoints": {"type": "array", "items": {"type": "string"}},
            "sregym_token": {"type": "string"},
        },
        "required": [],
    },
    injected_params=("sregym_url", "sregym_endpoints", "sregym_token"),
    is_available=_sregym_available,
    extract_params=_sregym_extract_params,
)
def list_sregym_tools_tool(
    name_filter: str | None = None,
    include_schema: bool = False,
    sregym_url: str | None = None,
    sregym_endpoints: list[str] | None = None,
    sregym_token: str | None = None,
    **_kwargs: object,
) -> SREGymResponse:
    """List tools available from the connected SREGym MCP endpoints."""
    config = _resolve_config(sregym_url, sregym_endpoints, sregym_token)
    if config is None:
        payload = _unavailable_response("SREGym integration is not configured.")
        payload["tools"] = []
        return payload

    runtime_error = sregym_runtime_unavailable_reason(config)
    if runtime_error is not None:
        payload = _unavailable_response(runtime_error)
        payload["tools"] = []
        return payload

    try:
        tools = list_sregym_server_tools(config)
    except Exception as err:
        report_run_error(
            err,
            tool_name="list_sregym_tools",
            source="sregym",
            component=_COMPONENT,
            method="list_sregym_server_tools",
            extras={"endpoints": ",".join(config.endpoints)},
        )
        payload = _unavailable_response(describe_sregym_error(err, config))
        payload["tools"] = []
        return payload

    listing = build_mcp_tool_listing(
        [dict(descriptor) for descriptor in tools],
        name_filter=(name_filter or "").strip() or None,
        include_schema=bool(include_schema),
        filter_example="kubectl logs metrics",
    )
    return {
        "source": "sregym",
        "available": True,
        "endpoint": config.url,
        **listing,
    }


@tool(
    name="call_sregym_tool",
    source="sregym",
    description=(
        "Call a named tool on the connected SREGym environment — for example "
        "exec_kubectl_cmd_safely to inspect or change the cluster, get_logs / "
        "get_metrics / get_traces to gather evidence, rollback_command to undo the "
        "last kubectl change, or submit to hand in the final answer."
    ),
    use_cases=[
        "Running a kubectl command against the benchmark cluster to gather evidence",
        "Applying a fix to the cluster once the root cause is identified",
        "Submitting the final diagnosis to the benchmark for grading",
    ],
    requires=["tool_name"],
    surfaces=("investigation", "chat"),
    input_schema={
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "arguments": {"type": "object"},
            "sregym_url": {"type": "string"},
            "sregym_endpoints": {"type": "array", "items": {"type": "string"}},
            "sregym_token": {"type": "string"},
        },
        "required": ["tool_name"],
    },
    injected_params=("sregym_url", "sregym_endpoints", "sregym_token"),
    is_available=_sregym_available,
    extract_params=_sregym_extract_params,
)
def call_sregym_tool_tool(
    tool_name: str | None = None,
    arguments: SREGymParams | None = None,
    sregym_url: str | None = None,
    sregym_endpoints: list[str] | None = None,
    sregym_token: str | None = None,
    **_kwargs: object,
) -> SREGymResponse:
    """Call a specific SREGym MCP tool by name."""
    normalized_tool_name = (tool_name or "").strip()
    if not normalized_tool_name:
        return _unavailable_response(
            "tool_name is required to call a SREGym tool.",
            arguments=arguments or {},
        )

    config = _resolve_config(sregym_url, sregym_endpoints, sregym_token)
    if config is None:
        return _unavailable_response(
            "SREGym integration is not configured.",
            tool_name=normalized_tool_name,
            arguments=arguments or {},
        )

    runtime_error = sregym_runtime_unavailable_reason(config)
    if runtime_error is not None:
        return _unavailable_response(
            runtime_error,
            tool_name=normalized_tool_name,
            arguments=arguments or {},
        )

    try:
        result = invoke_sregym_tool(config, normalized_tool_name, arguments or {})
    except Exception as err:
        report_run_error(
            err,
            tool_name="call_sregym_tool",
            source="sregym",
            component=_COMPONENT,
            method="invoke_sregym_tool",
            extras={"mcp_tool": normalized_tool_name},
        )
        return _unavailable_response(
            describe_sregym_error(err, config),
            tool_name=normalized_tool_name,
            arguments=arguments or {},
        )

    return _normalize_tool_result(result)
