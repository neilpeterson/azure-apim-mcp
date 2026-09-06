"""Tests for the error taxonomy (T-04). See docs/SPEC.md §8.1."""

from __future__ import annotations

import inspect

import pytest

from apim_mcp.common import errors
from apim_mcp.common.errors import (
    ToolError,
    access_denied,
    error_envelope,
    index_unavailable,
    invalid_input,
    not_found,
    throttled,
    timeout,
    upstream_error,
)


def test_access_denied_names_actionable_next_step() -> None:
    error = access_denied("service 'prod'")
    assert error.kind == "access_denied"
    assert "configuration issue" in error.message
    assert "Contact the server operator" in error.message


def test_not_found_names_actionable_next_step() -> None:
    error = not_found("API", "orders-api", "prod")
    assert error.kind == "not_found"
    assert "orders-api" in error.message
    assert "apim_list_apis" in error.message


def test_throttled_names_actionable_next_step() -> None:
    error = throttled(7)
    assert error.kind == "throttled"
    assert "Retry in 7s" in error.message


def test_timeout_names_actionable_next_step() -> None:
    error = timeout(narrow_param="timespan")
    assert error.kind == "timeout"
    assert "Narrow `timespan`" in error.message
    assert "limit" in error.message


def test_invalid_input_names_actionable_next_step() -> None:
    error = invalid_input("timespan", "PT1H")
    assert error.kind == "invalid_input"
    assert "timespan" in error.message
    assert "PT1H" in error.message


def test_upstream_error_names_actionable_next_step() -> None:
    error = upstream_error(log_detail="500 from ARM: boom")
    assert error.kind == "upstream_error"
    assert "Try again" in error.message
    # internals must never leak into the model-facing message
    assert "boom" not in error.message


def test_index_unavailable_names_actionable_next_step() -> None:
    error = index_unavailable()
    assert error.kind == "index_unavailable"
    assert "Retry in ~30s" in error.message


def test_all_seven_kinds_covered() -> None:
    builders = {
        "access_denied": access_denied("resource"),
        "not_found": not_found("API", "id", "svc"),
        "throttled": throttled(1),
        "timeout": timeout(),
        "invalid_input": invalid_input("param", "example"),
        "upstream_error": upstream_error(),
        "index_unavailable": index_unavailable(),
    }
    assert set(builders) == {
        "access_denied",
        "not_found",
        "throttled",
        "timeout",
        "invalid_input",
        "upstream_error",
        "index_unavailable",
    }
    for kind, error in builders.items():
        assert isinstance(error, ToolError)
        assert error.kind == kind
        assert error.message  # every kind has a non-empty, actionable message


def test_error_envelope_wraps_error() -> None:
    error = index_unavailable()
    envelope = error_envelope(error)
    assert envelope == {"error": {"kind": "index_unavailable", "message": error.message}}


def test_tool_error_is_frozen() -> None:
    from pydantic import ValidationError

    error = index_unavailable()
    with pytest.raises(ValidationError):
        error.kind = "timeout"  # type: ignore[misc]


def test_access_denied_has_obo_comment() -> None:
    """docs/PRINCIPLES.md §8: the access_denied message changes meaning
    under on-behalf-of, and that must be flagged with a `# OBO:` comment."""
    source = inspect.getsource(errors)
    assert "# OBO:" in source
    # the comment must sit inside/next to the access_denied builder itself
    access_denied_source = inspect.getsource(access_denied)
    assert "# OBO:" in access_denied_source
