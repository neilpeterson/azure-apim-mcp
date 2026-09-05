# apim-mcp

Read-only MCP server that answers questions about Azure API Management: API inventory
and configuration, OpenAPI specs, semantic search across the API surface, service health,
metrics, and gateway logs. Consumed from GitHub Copilot in VS Code and from Foundry agents.

## Repository map

| File | Reader | Role |
|---|---|---|
| `AGENTS.md` | agent, every request | Short. Routing table plus the ten principles as one-liners. |
| `docs/PRINCIPLES.md` | agent, when coding | The non-negotiables, with rationale and enforcement. |
| `docs/SPEC.md` | agent, per task | The full technical requirement. |
| `TASKS.md` | agent, every task | Ordered backlog and shared state. The only file with churn. |
| `docs/RUNBOOK.md` | human / ops | Tenant quirks, environment findings, operational facts. |
| `Makefile` | both | `make check` is the gate. |

`.github/agents/*.agent.md` define three subagents: `azure-auth`, `kql-safety`, and
`spec-auditor`.

## Quick start

```bash
uv sync
make check        # will fail until T-01 creates src/ - that is expected
```

Then complete H-01 in `TASKS.md`, and start on T-01.

---

# Driving this repo autonomously

No framework. Five files plus a Makefile do the work a spec-driven-development
toolkit would have done. The mechanism that makes unattended work possible is not
the task list — it is `make check` plus recorded fixtures, which let the agent
determine for itself whether it is finished, without you and without Azure.

---

|---|---|
| `AGENTS.md` | agent instructions | Loaded every request. Short on purpose. |
| `docs/PRINCIPLES.md` | `constitution.md` | Non-negotiables with rationale and enforcement. |
| `docs/SPEC.md` | `spec.md` + `plan.md` | The requirement. |
| `TASKS.md` | `tasks.md` | Ordered backlog, acceptance criteria, shared state. |
| `Makefile` | — | The gate. This is the part that actually buys autonomy. |

The mechanism that makes unattended work possible is not the task list. It is `make check` plus recorded fixtures: the agent can determine for itself whether it is finished, without you and without Azure.

---

## Setup, once

```bash
gh extension install github/gh-copilot   # if not already
uv sync
make check                               # must be green before you delegate anything
```

Then complete **H-01** and **H-02** from `TASKS.md` yourself. Both are blocking, both take minutes, and neither can be delegated.

---

## The session pattern

### Interactive, for load-bearing tasks (T-03 through T-09)

These define the seams that everything else depends on. Do them with Shift+Tab plan mode and review the plan before accepting.

```bash
copilot --deny-tool='shell(az)' --deny-tool='shell(rm)'
```

Then:

```
Read AGENTS.md, docs/PRINCIPLES.md, and TASKS.md.
Work T-05 only. Read docs/SPEC.md §5.1 first.
Plan before you write.
```

Review the plan against `docs/PRINCIPLES.md` §1, §2 and §7 before accepting. This is the task an agent is most likely to get subtly wrong — a module-level singleton credential passes every test and quietly forecloses the OBO migration.

### Autopilot, for everything from T-10 onward

```bash
copilot --allow-all --max-autopilot-continues 15 \
        --deny-tool='shell(az)' \
        --deny-tool='shell(rm)' \
        --deny-tool='shell(git push)'
```

Shift+Tab into plan mode, prompt, review the plan, then choose **Accept plan and build on autopilot**.

```
Read AGENTS.md, docs/PRINCIPLES.md, and TASKS.md.
Work T-13. Read docs/SPEC.md §6 Group B first.
Write the tests from the Done-when list, then implement until make check passes.
Tick the boxes in TASKS.md when done. Stop after T-13.
```

`--max-autopilot-continues` is the runaway guard — without it a confused agent will loop on a failing test indefinitely and burn credits.

### Non-interactive, for batching overnight

```bash
copilot -p "Read AGENTS.md and TASKS.md. Work T-11 to completion. Stop when make check passes." \
        --yolo --deny-tool='shell(az)' --max-autopilot-continues 20
```

Note `--yolo` rather than `--allow-all-tools` here: the latter grants tool execution but *not* path or URL access, so an agent running `uv run pytest` hits "Permission denied and could not request permission from user" with no way to prompt you. `--yolo` is the equivalent of `--allow-all-tools --allow-all-paths --allow-all-urls`.

---

## Why the deny list matters here

`--deny-tool` takes precedence over `--allow-all-tools` and `--allow-all`, so "allow broadly, deny the foot-guns" is the workable pattern.

**`shell(az)` is the important one for this project.** An agent trying to understand an ARM response shape will reasonably decide the fastest path is `az apim api list` against a real instance. On a good day that is a read against non-prod. On a bad day it is a read against prod with your credentials, or an `az role assignment create` to "fix" a permissions error. Keep it denied until T-07 fixtures exist, and keep it denied afterwards — the fixtures are the supported path.

Lift the restriction only for T-20 (infra), and only in a session scoped to that task.

---

## Custom agents (subagents)

`.github/agents/*.agent.md` define specialists. When Copilot judges one is a good fit, the work is carried out by a **subagent** — a temporary agent with its own context window, so specialist detail does not clutter the main agent's context. The main agent stays focused on coordination.

This repo ships three:

- **`azure-auth`** — owns the credential seam, JWT validation, and RBAC. Invoked for T-05, T-08, and anything touching `auth/`.
- **`kql-safety`** — owns query construction. Invoked for T-18 and T-19.
- **`spec-auditor`** — reviews a completed task against `docs/PRINCIPLES.md` and the task's Done-when list. Run it *after* each task, before you review.

The audit step is worth the extra call. Ask for it explicitly:

```
Task T-13 is complete. Use the spec-auditor agent to review it against
docs/PRINCIPLES.md and the T-13 Done-when list before I look at it.
```

Subagents are about **context isolation, not throughput**. They will not make the work faster; they will make it more accurate on long tasks.

---

## Parallelism, honestly

Real parallelism comes from the GitHub cloud agent, not from running several CLI sessions against one working tree.

For the four tasks marked `[PARALLEL-SAFE]` in `TASKS.md`, file an issue per task and assign it to Copilot. The cloud agent does better with clear, well-scoped tasks that include complete acceptance criteria and directions about which files to change — which is exactly what each task block already contains. Paste the block into the issue body verbatim.

For everything else, work sequentially. Most tasks touch the credential seam or the error taxonomy, and parallel agents will independently invent competing abstractions for both. Reviewing and reconciling four such PRs costs more than the sequential work saved.

If you do want concurrent local sessions for the parallel-safe four, use `git worktree` so each has its own checkout:

```bash
git worktree add ../apim-mcp-redaction -b task/t-11
git worktree add ../apim-mcp-tokenize  -b task/t-12
```

---

## Review cadence

Review at **task boundaries**, not step boundaries. That is the whole point.

Three places to actually stop and look:

1. **After T-09** — the seams are set. Everything downstream inherits them. Read `auth/credentials.py` and `clients/arm.py` line by line.
2. **After T-10** — the first end-to-end milestone. Deploy it, point VS Code at it, call a tool from Copilot. Do not proceed on faith.
3. **After T-16** — search is the feature with the loosest acceptance criteria. Run real questions against it before trusting the tests.

Between those, skim the diff and trust the gate.

---

## When the loop breaks

**Agent loops on a failing test.** Usually a fixture is missing or wrong. Check `tests/fixtures/` before assuming the implementation is at fault.

**Agent disables a check to go green.** Treat as a bug in the task description, not just in the code. Revert, and add the missing constraint to the Done-when list so it does not recur.

**Agent invents Azure field names.** `AGENTS.md` tells it to stop rather than guess, but it will sometimes guess anyway. This is what fixtures prevent — if a test passes against a fixture, the field names are real.

**Repository hooks not firing in `-p` mode.** Known issue: hooks in `.github/hooks/` are silently ignored in non-interactive mode with no warning, while user-level hooks in `~/.copilot/` load correctly. If you add hooks for audit or gating, put them at user level and verify they fire.
