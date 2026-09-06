"""The credential seam — every downstream call goes through this module.

See `docs/SPEC.md` §5.1 and `docs/PRINCIPLES.md` §1, §2, §7. This is the
single point that the on-behalf-of retrofit (Appendix A) and the client-token
retrofit (Appendix B) both modify. No other module may construct a
credential directly — see `tests/test_principles.py::test_no_direct_credential_construction`.
"""

from __future__ import annotations

import os

from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential

from apim_mcp.auth.context import CallContext
from apim_mcp.settings import get_settings

ARM_SCOPE = "https://management.azure.com/.default"
LOGS_SCOPE = "https://api.loganalytics.io/.default"

# Opt-in only, never set in Container Apps: `ManagedIdentityCredential`
# cannot acquire a token unless the process is actually running on an
# Azure resource with that identity attached, which no laptop is. Setting
# this to "1" swaps in `az login`'s cached credential instead, so
# `make run` can hit real APIM from a dev machine. See docs/LOCAL_TESTING.md.
_LOCAL_DEV_CREDENTIAL_ENV = "APIM_MCP_LOCAL_DEV_CREDENTIAL"

_mi: AsyncTokenCredential | None = None


def credential_for(ctx: CallContext, scope: str) -> AsyncTokenCredential:
    """Return the credential to use for a downstream call.

    v1: always the server's managed identity (or, only when
    `APIM_MCP_LOCAL_DEV_CREDENTIAL=1`, the local `az login` session); `ctx`
    and `scope` are otherwise unused but MUST be threaded through by every
    caller. Appendix A (OBO) and Appendix B (client token) both need them,
    and retrofitting the parameters later means touching every call site.
    """
    global _mi
    if _mi is None:
        if os.environ.get(_LOCAL_DEV_CREDENTIAL_ENV) == "1":
            _mi = AzureCliCredential()
        else:
            settings = get_settings()
            _mi = ManagedIdentityCredential(client_id=settings.azure_client_id)
    return _mi
