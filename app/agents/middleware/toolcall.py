"""Reading a tool call out of a middleware request.

LangChain's `ToolCall` is a `TypedDict`, so at runtime it is a plain dict and its
fields are only reachable by subscript. `getattr(request.tool_call, "name", default)`
therefore *always* returns the default — silently, with no error and no failing type
check, because `getattr` with a fallback cannot fail.

That failure mode is quiet enough to be worth a named helper rather than inline
access: an audit trail full of `unknown_tool`, or a safety scan handed an empty
argument dict, looks exactly like a system with nothing to report.

Both shapes are accepted so a future LangChain that promotes `ToolCall` to an object
does not silently reintroduce the same bug in the other direction.
"""

from typing import Any


def _field(tool_call: Any, key: str, default: Any) -> Any:
    if isinstance(tool_call, dict):
        value = tool_call.get(key, default)
    else:
        value = getattr(tool_call, key, default)
    return default if value is None else value


def tool_name(request: Any) -> str:
    """The name of the tool being invoked, or `"unknown_tool"` if it cannot be read."""
    return _field(getattr(request, "tool_call", None), "name", "unknown_tool")


def tool_args(request: Any) -> dict[str, Any]:
    """The tool's arguments. Always a dict, never None."""
    args = _field(getattr(request, "tool_call", None), "args", {})
    return args if isinstance(args, dict) else {}


def tool_call_id(request: Any) -> str:
    """The tool call's correlation id, used to answer it with a ToolMessage."""
    return _field(getattr(request, "tool_call", None), "id", "")
