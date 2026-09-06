"""Tests for src/apim_mcp/settings.py (T-03). See docs/SPEC.md §5.3."""

from __future__ import annotations

import json

import pytest

from apim_mcp.settings import (
    Settings,
    SettingsError,
    UnknownServiceAliasError,
    get_settings,
)

VALID_SERVICES = json.dumps(
    [
        {
            "alias": "prod",
            "resourceId": (
                "/subscriptions/00000000-0000-0000-0000-000000000000"
                "/resourceGroups/rg-fixture"
                "/providers/Microsoft.ApiManagement/service/apim-fixture"
            ),
            "logAnalyticsWorkspaceId": (
                "/subscriptions/00000000-0000-0000-0000-000000000000"
                "/resourceGroups/rg-fixture"
                "/providers/Microsoft.OperationalInsights/workspaces/law-fixture"
            ),
        },
        {
            "alias": "nonprod",
            "resourceId": (
                "/subscriptions/00000000-0000-0000-0000-000000000000"
                "/resourceGroups/rg-fixture"
                "/providers/Microsoft.ApiManagement/service/apim-fixture-nonprod"
            ),
        },
    ]
)

REQUIRED_ENV: dict[str, str] = {
    "AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111",
    "AZURE_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
    "MCP_SERVER_AUDIENCE": "api://apim-mcp",
    "MCP_SERVER_APP_ID": "33333333-3333-3333-3333-333333333333",
    "APIM_SERVICES": VALID_SERVICES,
    "APPLICATIONINSIGHTS_CONNECTION_STRING": (
        "InstrumentationKey=00000000-0000-0000-0000-000000000000"
    ),
}


def _set_env(monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str] | None = None) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    for key in (
        "MCP_REQUIRED_ROLE",
        "INDEX_TTL_SECONDS",
        "INDEX_MAX_CONCURRENCY",
        "MAX_RESPONSE_BYTES",
    ):
        monkeypatch.delenv(key, raising=False)
    if overrides:
        for key, value in overrides.items():
            monkeypatch.setenv(key, value)


def test_valid_environment_loads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    settings = get_settings()
    assert settings.azure_tenant_id == REQUIRED_ENV["AZURE_TENANT_ID"]
    assert settings.mcp_required_role == "Apim.Read"
    assert settings.index_ttl_seconds == 900
    assert settings.index_max_concurrency == 8
    assert settings.max_response_bytes == 48000
    assert [s.alias for s in settings.apim_services] == ["prod", "nonprod"]


def test_missing_required_var_raises_with_name_in_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
    with pytest.raises(SettingsError, match="AZURE_TENANT_ID"):
        get_settings()


def test_multiple_missing_required_vars_are_all_named(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
    monkeypatch.delenv("MCP_SERVER_AUDIENCE", raising=False)
    with pytest.raises(SettingsError) as excinfo:
        get_settings()
    assert "AZURE_TENANT_ID" in str(excinfo.value)
    assert "MCP_SERVER_AUDIENCE" in str(excinfo.value)


def test_apim_services_json_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    settings = get_settings()
    prod = settings.apim_services[0]
    assert prod.alias == "prod"
    assert prod.resource_id.endswith("/service/apim-fixture")
    assert prod.log_analytics_workspace_id is not None
    assert prod.log_analytics_workspace_id.endswith("/workspaces/law-fixture")
    # optional field omitted in the second entry
    assert settings.apim_services[1].log_analytics_workspace_id is None


def test_apim_services_malformed_json_gives_readable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, {"APIM_SERVICES": "{not valid json"})
    with pytest.raises(SettingsError, match="APIM_SERVICES is not valid JSON"):
        get_settings()


def test_apim_services_invalid_resource_id_shape_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad_services = json.dumps([{"alias": "prod", "resourceId": "not-an-arm-id"}])
    _set_env(monkeypatch, {"APIM_SERVICES": bad_services})
    with pytest.raises(SettingsError, match="does not look like an ARM resource ID"):
        get_settings()


def test_apim_services_invalid_log_analytics_id_shape_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad_services = json.dumps(
        [
            {
                "alias": "prod",
                "resourceId": (
                    "/subscriptions/00000000-0000-0000-0000-000000000000"
                    "/resourceGroups/rg-fixture"
                    "/providers/Microsoft.ApiManagement/service/apim-fixture"
                ),
                "logAnalyticsWorkspaceId": "not-an-arm-id",
            }
        ]
    )
    _set_env(monkeypatch, {"APIM_SERVICES": bad_services})
    with pytest.raises(SettingsError, match="does not look like an ARM resource ID"):
        get_settings()


def test_alias_lookup_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    settings = get_settings()
    assert settings.service("PROD").alias == "prod"
    assert settings.service("Prod").alias == "prod"
    assert settings.service("  prod  ").alias == "prod"


def test_unknown_alias_raises_typed_error_listing_valid_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    settings = get_settings()
    with pytest.raises(UnknownServiceAliasError) as excinfo:
        settings.service("staging")
    assert excinfo.value.alias == "staging"
    assert excinfo.value.valid_aliases == ["nonprod", "prod"]
    assert "nonprod" in str(excinfo.value)
    assert "prod" in str(excinfo.value)


def test_settings_is_a_pydantic_settings_model() -> None:
    assert issubclass(Settings, object)
    assert "apim_services" in Settings.model_fields
