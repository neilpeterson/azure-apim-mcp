"""CallContext: the identity of the caller making a tool invocation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CallContext(BaseModel):
    """Per-request caller identity, threaded through every downstream call.

    Immutable (frozen) because it is constructed once per request by the
    JWT middleware (T-08) from the validated bearer token and must not be
    mutated afterwards — every credential and cache-key decision downstream
    depends on it staying exactly what the token said.

    v1 does not use `oid` for authorization (the shared managed identity
    grants every caller the same access), but `docs/PRINCIPLES.md` §3
    requires every cache key to include it anyway, and the on-behalf-of
    migration (`docs/SPEC.md` Appendix A) needs the full context — `oid`,
    `upn`, `roles`, and `bearer_token` — to perform its token exchange.
    """

    model_config = ConfigDict(frozen=True)

    oid: str
    upn: str
    roles: tuple[str, ...] = ()
    bearer_token: str
