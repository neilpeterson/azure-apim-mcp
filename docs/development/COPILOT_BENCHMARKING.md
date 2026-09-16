# Copilot efficiency benchmarking

The observer records completed interactions from a normal Copilot CLI session.
It does not launch Copilot, replace `COPILOT_HOME`, or control which MCP
servers and native tools the agent chooses.

Result files are created with owner-only permissions (`0600`) and symbolic-link
destinations are rejected. By default, rows contain metrics plus prompt and
response character counts, but not conversation text. Full prompts and
responses can contain sensitive information and must be enabled explicitly.

## Common scenarios

Observe the latest active session with safe defaults:

```bash
make bench
```

Observe a specific session and label its results:

```bash
make bench BENCH_OBSERVE_SESSION=7b23eb51 BENCH_OBSERVE_LABEL=apim-local
```

Write multiple sequential sessions into one comparison file:

```bash
make bench BENCH_OBSERVE_SESSION=7b23eb51 BENCH_OBSERVE_LABEL=apim-local BENCH_OBSERVE_RESULTS=comparison.bench-results.jsonl
```

Record full prompts and responses when content-level review is required:

```bash
make bench BENCH_OBSERVE_SESSION=7b23eb51 BENCH_OBSERVE_ARGS=--include-content
```

Import interactions that finished before the observer started:

```bash
make bench BENCH_OBSERVE_SESSION=7b23eb51 BENCH_OBSERVE_ARGS='--include-existing --include-content'
```

Check that known response markers are present:

```bash
make bench BENCH_OBSERVE_SESSION=7b23eb51 BENCH_OBSERVE_ARGS='--expect echo-api --expect hello-web'
```

Use a nondefault Copilot home:

```bash
make bench BENCH_OBSERVE_SESSION=7b23eb51 BENCH_OBSERVE_ARGS='--copilot-home /path/to/copilot-home'
```

The session ID is a directory name under `~/.copilot/session-state/`. A unique
prefix is sufficient. Different terminal or VS Code windows do not matter when
they use Copilot CLI under the same user and `COPILOT_HOME`. If multiple
sessions are active, `latest` selects the one with the most recently updated
event stream; specify a session ID when that choice would be ambiguous.

## Recorded data

The observer incrementally follows the selected session's `events.jsonl`.
Each completed interaction records timing, tool names/count/duration,
model-call count, premium-request and nano-AIU deltas, model, prompt/response
character counts, and the prompt/cache snapshot exposed at the usage
checkpoint. With `--include-content`, the row also contains the full prompt and
response.

`--expect` performs case-insensitive marker checks. It records
`expectations_met` and `missing_expectations`; it is not a semantic accuracy
evaluation.

Copilot does not expose complete per-interaction token totals in the live event
stream. The `*_tokens_snapshot` values describe the latest model context at the
checkpoint and must not be summed across interactions. When the observed
Copilot session exits normally, the observer records a `session_summary`
containing complete input, output, cached, cache-write, and reasoning token
totals, plus AI credits, nano-AIU, premium requests, API duration, session
duration, and per-model metrics. The observer then exits automatically.

For a direct comparison, use a fresh Copilot session for each configuration,
run the same tasks, and exit each session after the final answer. Observe
simultaneous sessions into separate result files; use one shared file only for
sequential observers.
