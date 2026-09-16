"""The error taxonomy from docs/development/SPEC.md §8.1.

Tool failures are returned *inside* the tool result, never raised as
protocol-level exceptions (`docs/development/PRINCIPLES.md` §8). Every builder here
returns a frozen :class:`ToolError` with a message that names an actionable
next step, so the model can act on a failure instead of just reporting it.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

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


def access_denied(resource: str, required_role: str) -> ToolError:
    # OBO: under on-behalf-of, a 403 here means the *calling user* lacks
    # access to `resource`. This message must be revisited before that
    # migration ships — see the retrofit checklist in SPEC Appendix A.
    if os.environ.get("APIM_MCP_LOCAL_DEV_CREDENTIAL") == "1":
        identity = "Your active `az login` identity"
    else:
        identity = "The Container App's managed identity"
    return ToolError(
        kind="access_denied",
        message=(
            f"{identity} lacks permission to read {resource}. Grant the built-in "
            f"**{required_role}** role at the resource scope."
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
    never returned to the model, per docs/development/PRINCIPLES.md §8."""
    if log_detail is not None:
        logger.warning("upstream Azure error: %s", log_detail)
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
