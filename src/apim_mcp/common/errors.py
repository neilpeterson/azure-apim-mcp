"""The error taxonomy from docs/SPEC.md §8.1.

Tool failures are returned *inside* the tool result, never raised as
protocol-level exceptions (`docs/PRINCIPLES.md` §8). Every builder here
returns a frozen :class:`ToolError` with a message that names an actionable
next step, so the model can act on a failure instead of just reporting it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

ErrorKind = Literal[
    "access_denied",
    "not_found",
    "throttled",
    "timeout",
    "invalid_input",
    "upstream_error",
    "index_unavailable",
]


class ToolError(BaseModel):
    """A structured tool failure. Embed via :func:`error_envelope`."""

    model_config = ConfigDict(frozen=True)

    kind: ErrorKind
    message: str


def error_envelope(error: ToolError) -> dict[str, object]:
    """Wrap a :class:`ToolError` for inclusion in a tool result payload."""
    return {"error": error.model_dump()}


def access_denied(resource: str) -> ToolError:
    # OBO: under on-behalf-of, a 403 here means the *calling user* lacks
    # access to `resource`, not that the server's identity is misconfigured.
    # This message must be rewritten before that migration ships — see the
    # retrofit checklist in docs/SPEC.md Appendix A.
    return ToolError(
        kind="access_denied",
        message=(
            f"The server's identity lacks permission to read {resource}. "
            "This is a configuration issue, not a user permission issue. "
            "Contact the server operator to grant the required role."
        ),
    )


def not_found(resource_type: str, resource_id: str, service: str) -> ToolError:
    return ToolError(
        kind="not_found",
        message=(
            f"No {resource_type} named '{resource_id}' on service '{service}'. "
            "Use `apim_list_apis` to see available IDs."
        ),
    )


def throttled(retry_after_seconds: int) -> ToolError:
    return ToolError(
        kind="throttled",
        message=(
            f"Azure Resource Manager is throttling requests. Retry in {retry_after_seconds}s."
        ),
    )


def timeout(narrow_param: str = "timespan") -> ToolError:
    return ToolError(
        kind="timeout",
        message=(f"Query exceeded the time budget. Narrow `{narrow_param}` or lower `limit`."),
    )


def invalid_input(parameter: str, example: str) -> ToolError:
    return ToolError(
        kind="invalid_input",
        message=f"Invalid value for `{parameter}`. Example of a valid value: {example}.",
    )


def upstream_error(*, log_detail: str | None = None) -> ToolError:
    """Generic 5xx result. `log_detail` is for server-side logging only —
    never returned to the model, per docs/PRINCIPLES.md §8."""
    return ToolError(
        kind="upstream_error",
        message=(
            "An upstream Azure service returned an unexpected error. Try again; "
            "if it persists, this is likely a transient Azure issue."
        ),
    )


def index_unavailable() -> ToolError:
    return ToolError(
        kind="index_unavailable",
        message="The API index is still building. Retry in ~30s.",
    )
