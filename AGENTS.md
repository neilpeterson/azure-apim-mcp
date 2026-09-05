# AGENTS.md

Read-only MCP server that answers questions about Azure API Management. Python 3.12, FastMCP, streamable HTTP, deployed to Azure Container Apps.

**This file is loaded on every request. It is deliberately short.** Full detail lives in `docs/`.

| Need | Read |
|---|---|
| Non-negotiable rules, with rationale | `docs/PRINCIPLES.md` — **read before writing any code** |
| What to build, in order | `TASKS.md` |
| Full technical spec | `docs/SPEC.md` |
| Tenant quirks, environment findings | `docs/RUNBOOK.md` |

## The loop

Every task ends the same way. No task is complete until this passes:

```bash
make check      # ruff + mypy --strict + pytest
```

If `make check` fails, you are not done. Fix it and re-run. Do not report a task complete with a failing gate, and do not disable a check to make it pass.

## Working agreement

- **One task at a time**, from `TASKS.md`, in order, unless the task is marked `[PARALLEL-SAFE]`.
- **Read the referenced spec section** (`docs/SPEC.md` §N) before starting. The task list is a summary, not the requirement.
- **Tests before implementation.** Every task lists named tests in its acceptance criteria. Write them failing, then make them pass.
- **Never call live Azure.** Tests run against recorded fixtures in `tests/fixtures/`. If you need a fixture that does not exist, add a recorder entry in `scripts/record_fixtures.py` and stop — a human runs it.
- **Update `TASKS.md`** — tick the checkboxes as you complete them. That file is the shared state between sessions.

## The ten principles

Summary only. Numbering matches `docs/PRINCIPLES.md` exactly — read that file for rationale, violation examples, and which test enforces each one.

1. **Credential seam.** All downstream calls go through `credential_for(ctx, scope)`. Never construct a credential elsewhere.
2. **Explicit scope.** Always pass `ARM_SCOPE` or `LOGS_SCOPE`. No defaults, even though v1 ignores it.
3. **Cache keys include `oid`.** Always. The API index is the one documented exception.
4. **Read-only.** No POST, PUT, PATCH, or DELETE against a management endpoint.
5. **Never retrieve secrets.** No `listSecrets`, `listValue`, `listKeys`, `users/token`. Not even to redact afterwards.
6. **No user input in KQL.** Bound parameters only. Never interpolation.
7. **Per-request SDK clients.** Never module-level or startup singletons.
8. **Errors are results, not exceptions.** Never raise out of a tool handler.
9. **Label untrusted content.** API descriptions and policy text reach the model as data, not instructions.
10. **Audit every tool call.** Caller `oid`, tool, arguments, outcome. Never response bodies.

## Commands

```bash
make check      # the gate: lint + types + test
make fmt        # ruff format
make test       # pytest only
make run        # local server on :8000
make inspect    # MCP Inspector against local server
```

## Style

- `ruff` and `mypy --strict` are configured in `pyproject.toml`. Do not relax either.
- Full type annotations on every function, including tests.
- `async` for all I/O. No sync Azure SDK clients.
- Pydantic v2 models for every tool input and output.
- No `# type: ignore` without an inline explanation of why.

## When you are stuck

Do not guess at Azure API shapes. If a response schema is unclear, say so and stop rather than inventing field names. `docs/SPEC.md` §14 lists the open questions that only a human can resolve — if you hit one, note it in `TASKS.md` and move to the next task.
