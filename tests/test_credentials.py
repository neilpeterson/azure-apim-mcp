"""Tests for the credential seam (T-05). See docs/SPEC.md §5.1."""

from __future__ import annotations

import inspect
import json

import pytest
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
from pydantic import ValidationError

import apim_mcp.auth.credentials as credentials_module
from apim_mcp.auth.context import CallContext
from apim_mcp.auth.credentials import ARM_SCOPE, LOGS_SCOPE, credential_for

VALID_SERVICES = json.dumps(
    [
        {
            "alias": "prod",
            "resourceId": (
                "/subscriptions/00000000-0000-0000-0000-000000000000"
                "/resourceGroups/rg-fixture"
                "/providers/Microsoft.ApiManagement/service/apim-fixture"
            ),
        }
    ]
)

REQUIRED_ENV: dict[str, str] = {
    "AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111",
    "AZURE_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
    "MCP_SERVER_AUDIENCE": "http://localhost:8000/mcp",
    "MCP_SERVER_APP_ID": "33333333-3333-3333-3333-333333333333",
    "APIM_SERVICES": VALID_SERVICES,
    "APPLICATIONINSIGHTS_CONNECTION_STRING": (
        "InstrumentationKey=00000000-0000-0000-0000-000000000000"
    ),
}


@pytest.fixture(autouse=True)
def _reset_credential_singleton() -> None:
    """The module caches a single ManagedIdentityCredential; keep tests isolated."""
    credentials_module._mi = None


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def _sample_context() -> CallContext:
    return CallContext(
        oid="user-oid", upn="user@example.com", roles=("Apim.Read",), bearer_token="tok"
    )


def test_scopes_are_defined() -> None:
    assert ARM_SCOPE == "https://management.azure.com/.default"
    assert LOGS_SCOPE == "https://api.loganalytics.io/.default"


def test_credential_for_signature_has_no_defaults() -> None:
    signature = inspect.signature(credential_for)
    params = list(signature.parameters.values())
    assert [p.name for p in params] == ["ctx", "scope"]
    for param in params:
        assert param.default is inspect.Parameter.empty
        assert param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )


def test_credential_for_requires_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    ctx = _sample_context()
    with pytest.raises(TypeError):
        credential_for(ctx)  # type: ignore[call-arg]


def test_credential_for_requires_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with pytest.raises(TypeError):
        credential_for(scope=ARM_SCOPE)  # type: ignore[call-arg]


def test_credential_for_returns_managed_identity_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    ctx = _sample_context()
    credential = credential_for(ctx, ARM_SCOPE)
    assert isinstance(credential, ManagedIdentityCredential)
    assert isinstance(credential, AsyncTokenCredential)


def test_credential_for_reuses_cached_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    ctx = _sample_context()
    first = credential_for(ctx, ARM_SCOPE)
    second = credential_for(ctx, LOGS_SCOPE)
    assert first is second


def test_local_dev_credential_opt_in_returns_azure_cli_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`APIM_MCP_LOCAL_DEV_CREDENTIAL=1` is the only way to get anything
    other than `ManagedIdentityCredential` out of the seam - still
    constructed inside `auth/credentials.py`, so this does not violate
    `docs/PRINCIPLES.md` §1."""
    _set_env(monkeypatch)
    monkeypatch.setenv("APIM_MCP_LOCAL_DEV_CREDENTIAL", "1")
    ctx = _sample_context()
    credential = credential_for(ctx, ARM_SCOPE)
    assert isinstance(credential, AzureCliCredential)


def test_local_dev_credential_opt_in_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    monkeypatch.delenv("APIM_MCP_LOCAL_DEV_CREDENTIAL", raising=False)
    ctx = _sample_context()
    credential = credential_for(ctx, ARM_SCOPE)
    assert isinstance(credential, ManagedIdentityCredential)


def test_call_context_is_frozen() -> None:
    ctx = _sample_context()
    with pytest.raises(ValidationError):
        ctx.oid = "someone-else"  # type: ignore[misc]


def test_docstring_explains_obo_migration_rationale() -> None:
    doc = credential_for.__doc__ or ""
    assert "Appendix A" in doc
    assert "OBO" in doc
