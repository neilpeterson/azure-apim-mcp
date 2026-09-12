"""Server configuration, loaded from environment variables.

See ``docs/SPEC.md`` §5.3 for the authoritative variable table. ``Settings``
is a Pydantic model; :func:`get_settings` wraps construction so that a
missing or malformed variable fails fast with a message naming the exact
environment variable at fault, per ``AGENTS.md``'s "fail fast" requirement.
"""

from __future__ import annotations

import json
import re
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings import SettingsError as _PydanticSettingsSourceError

# ARM resource IDs look like:
#   /subscriptions/{guid}/resourceGroups/{rg}/providers/{namespace}/{type}/{name}
_ARM_RESOURCE_ID_RE = re.compile(
    r"^/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[^/]+/providers/[^/]+/.+$",
    re.IGNORECASE,
)
_DNS_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


class SettingsError(RuntimeError):
    """Raised when configuration is missing or malformed."""


class UnknownServiceAliasError(SettingsError):
    """Raised when a tool is asked to operate on an unconfigured APIM alias."""

    def __init__(self, alias: str, valid_aliases: list[str]) -> None:
        self.alias = alias
        self.valid_aliases = valid_aliases
        known = ", ".join(valid_aliases) if valid_aliases else "(none configured)"
        super().__init__(f"Unknown APIM service alias {alias!r}. Valid aliases: {known}")


def _validate_arm_resource_id(value: str, *, field_label: str) -> str:
    if not _ARM_RESOURCE_ID_RE.match(value):
        raise ValueError(f"{field_label} {value!r} does not look like an ARM resource ID")
    return value


def _valid_url_hostname(hostname: str) -> bool:
    try:
        ip_address(hostname)
    except ValueError:
        return all(_DNS_LABEL_RE.fullmatch(label) for label in hostname.split("."))
    return True


class ApimServiceConfig(BaseModel):
    """One entry of the ``APIM_SERVICES`` allowlist."""

    model_config = {"populate_by_name": True}

    alias: str
    resource_id: str = Field(validation_alias="resourceId")
    log_analytics_workspace_id: str | None = Field(
        default=None, validation_alias="logAnalyticsWorkspaceId"
    )

    @field_validator("resource_id")
    @classmethod
    def _check_resource_id(cls, value: str) -> str:
        return _validate_arm_resource_id(value, field_label="resourceId")

    @field_validator("log_analytics_workspace_id")
    @classmethod
    def _check_law_id(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _validate_arm_resource_id(value, field_label="logAnalyticsWorkspaceId")


class Settings(BaseSettings):
    """Every environment variable this server reads. See §5.3."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    azure_tenant_id: str
    azure_client_id: str
    mcp_server_audience: str
    mcp_server_app_id: str
    mcp_required_role: str = "Apim.Read"
    apim_services: list[ApimServiceConfig]
    index_ttl_seconds: int = 900
    index_max_concurrency: int = 8
    max_response_bytes: int = 48000
    applicationinsights_connection_string: str

    @field_validator("mcp_server_audience")
    @classmethod
    def _check_single_audience(cls, value: str) -> str:
        # §4.4: "accept exactly one configured value — do not accept a list."
        # A JSON array or comma-separated string is a config mistake, not a
        # legitimate multi-audience setup; fail fast rather than silently
        # picking one.
        stripped = value.strip()
        if stripped.startswith("[") or "," in stripped:
            raise ValueError(
                "MCP_SERVER_AUDIENCE must be exactly one value, not a list — see docs/SPEC.md §4.4"
            )
        if any(character.isspace() for character in stripped) or "\\" in stripped:
            raise ValueError("MCP_SERVER_AUDIENCE must not contain whitespace or backslashes")
        try:
            parsed = urlsplit(stripped)
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"MCP_SERVER_AUDIENCE is not a valid URL: {exc}") from exc
        hostname = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or hostname is None
            or not _valid_url_hostname(hostname)
            or (parsed.netloc.endswith(":") and port is None)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.endswith("/mcp")
            or stripped.endswith("/")
        ):
            raise ValueError(
                "MCP_SERVER_AUDIENCE must be an absolute http(s) MCP endpoint URL ending in "
                "'/mcp', with no credentials, query, fragment, or trailing slash — see "
                "docs/SPEC.md §4.3"
            )
        return stripped

    @field_validator("apim_services", mode="before")
    @classmethod
    def _parse_apim_services(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"APIM_SERVICES is not valid JSON: {exc}") from exc
        return value

    def service(self, alias: str) -> ApimServiceConfig:
        """Case-insensitive lookup of a configured APIM service by alias."""
        normalized = alias.strip().lower()
        for svc in self.apim_services:
            if svc.alias.lower() == normalized:
                return svc
        raise UnknownServiceAliasError(alias, sorted(s.alias for s in self.apim_services))


def get_settings() -> Settings:
    """Construct :class:`Settings` from the environment, failing loudly.

    Wraps :exc:`pydantic.ValidationError` in :exc:`SettingsError` so that a
    missing variable names itself (e.g. ``AZURE_TENANT_ID``) rather than
    surfacing pydantic's lowercase field name, and so that malformed JSON or
    an invalid resource ID reads as an actionable message.
    """
    try:
        return Settings()  # values come from the environment
    except ValidationError as exc:
        missing = [str(err["loc"][0]).upper() for err in exc.errors() if err["type"] == "missing"]
        if missing:
            raise SettingsError(
                f"Missing required environment variable(s): {', '.join(missing)}"
            ) from exc
        raise SettingsError(str(exc)) from exc
    except _PydanticSettingsSourceError as exc:
        # pydantic-settings pre-decodes complex (list/dict) env vars as JSON
        # before our own field validator ever runs, so malformed JSON in
        # APIM_SERVICES surfaces here rather than as a ValidationError.
        cause = exc.__cause__
        if cause is not None:
            raise SettingsError(f"APIM_SERVICES is not valid JSON: {cause}") from exc
        raise SettingsError(str(exc)) from exc
