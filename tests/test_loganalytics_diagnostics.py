"""Safe operator diagnostics for Log Analytics failures."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest
from azure.core.exceptions import HttpResponseError

from apim_mcp.clients._loganalytics import (
    _empty_partial_is_tolerated,
    _log_http_error,
    _log_partial_error,
    _query_fingerprint,
)


def test_http_error_logs_metadata_and_fixed_query_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resource_id = (
        "/subscriptions/00000000-0000-0000-0000-000000000000"
        "/resourceGroups/rg-fixture/providers/Microsoft.ApiManagement/service/apim-fixture"
    )
    query = (
        f'declare query_parameters(resource_id:string = "{resource_id}");\n'
        "AzureDiagnostics\n"
        "| where _ResourceId =~ resource_id\n"
        '| where Category == "GatewayLogs"'
    )
    error = HttpResponseError("raw Azure parser response must not be logged")
    error.status_code = 400
    error.reason = "Bad Request"
    cast(Any, error).error = SimpleNamespace(code="BadArgument")
    cast(Any, error).response = SimpleNamespace(
        headers={
            "x-ms-request-id": "request-123",
            "x-ms-correlation-request-id": "correlation-456",
        }
    )

    with caplog.at_level(logging.DEBUG, logger="apim_mcp.loganalytics"):
        _log_http_error(error, query, query_id="gateway-log-detail")

    assert "status=400" in caplog.text
    assert "code=BadArgument" in caplog.text
    assert "request-123" in caplog.text
    assert "correlation-456" in caplog.text
    assert "query_sha256=" in caplog.text
    assert "query_id=gateway-log-detail" in caplog.text
    assert "AzureDiagnostics" in caplog.text
    assert resource_id not in caplog.text
    assert "raw Azure parser response" not in caplog.text


def test_query_fingerprint_excludes_bound_parameter_values() -> None:
    first = (
        'declare query_parameters(resource_id:string = "/subscriptions/first");\n'
        "AzureDiagnostics | where _ResourceId =~ resource_id"
    )
    second = (
        'declare query_parameters(resource_id:string = "/subscriptions/second");\n'
        "AzureDiagnostics | where _ResourceId =~ resource_id"
    )

    assert _query_fingerprint(first) == _query_fingerprint(second)


def test_unrecognized_query_shape_never_logs_bound_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    query = 'AzureDiagnostics | where _ResourceId == "/subscriptions/sensitive"'
    error = HttpResponseError("raw Azure parser response must not be logged")
    error.status_code = 400

    with caplog.at_level(logging.DEBUG, logger="apim_mcp.loganalytics"):
        _log_http_error(error, query, query_id="gateway-log-detail")

    assert "/subscriptions/sensitive" not in caplog.text
    assert "query body unavailable" in caplog.text


def test_malformed_declaration_with_separator_never_logs_bound_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    query = (
        'declare query_parameters(resource_id:string = "safe");\n'
        'declare query_parameters(secret:string = "/subscriptions/sensitive");\n'
        "AzureDiagnostics"
    )
    error = HttpResponseError("raw Azure parser response must not be logged")
    error.status_code = 400

    with caplog.at_level(logging.DEBUG, logger="apim_mcp.loganalytics"):
        _log_http_error(error, query, query_id="gateway-log-detail")

    assert "/subscriptions/sensitive" not in caplog.text
    assert "query body unavailable" in caplog.text


def test_partial_error_logs_code_without_message_or_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    query = (
        'declare query_parameters(resource_id:string = "/subscriptions/sensitive");\n'
        "AzureDiagnostics | where _ResourceId =~ resource_id"
    )
    partial_error = SimpleNamespace(
        code="PartialQueryFailure",
        message="secret parser details",
        details=[{"message": "more secret details"}],
    )

    with caplog.at_level(logging.DEBUG, logger="apim_mcp.loganalytics"):
        _log_partial_error(partial_error, query, query_id="gateway-log-detail")

    assert "code=PartialQueryFailure" in caplog.text
    assert "query_sha256=" in caplog.text
    assert "query_id=gateway-log-detail" in caplog.text
    assert "AzureDiagnostics" in caplog.text
    assert "/subscriptions/sensitive" not in caplog.text
    assert "secret parser details" not in caplog.text
    assert "more secret details" not in caplog.text


def test_missing_fuzzy_union_table_can_be_treated_as_empty() -> None:
    partial_error = SimpleNamespace(
        code="PartialError",
        details=[{"code": "FailedToResolveTableExpression"}],
    )

    assert _empty_partial_is_tolerated(
        partial_error,
        frozenset({"FailedToResolveTableExpression", "FuzzyUnionSourceNotFound"}),
    )


def test_unknown_partial_error_is_not_treated_as_empty() -> None:
    partial_error = SimpleNamespace(
        code="PartialError",
        details=[{"code": "SemanticError"}],
    )

    assert not _empty_partial_is_tolerated(
        partial_error,
        frozenset({"FailedToResolveTableExpression", "FuzzyUnionSourceNotFound"}),
    )
