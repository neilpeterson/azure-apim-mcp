.PHONY: check lint types test fmt fixtures run token bench inspect clean

# --no-sync: never touch the network on a plain `make check`/`make run`. The
# venv is expected to already satisfy pyproject.toml; run `uv sync` by hand
# after adding/upgrading a dependency (see docs/development/LOCAL_TESTING.md).
UV := uv run --no-sync
BENCH_BASE ?= scripts/bench
BENCH_OBSERVE_LABEL ?= interactive
BENCH_OBSERVE_RESULTS ?= interactive.bench-results.jsonl
BENCH_OBSERVE_SESSION ?= latest
BENCH_OBSERVE_ARGS ?=

# The gate. No task is complete until this passes.
check: lint types test

lint:
	$(UV) ruff check .
	$(UV) ruff format --check .

types:
	$(UV) mypy --strict src/ tests/

test:
	$(UV) pytest -q

fmt:
	$(UV) ruff format .
	$(UV) ruff check --fix .

# HUMAN ONLY. Records sanitised ARM responses against a real non-prod instance.
# Agents must not run this - it requires live Azure credentials.
fixtures:
	$(UV) python scripts/record_fixtures.py

run:
	$(UV) python -m apim_mcp.server

# Optional manual token acquisition for protocol or client diagnostics.
# Normal local and hosted VS Code connections use automatic OAuth discovery.
token:
	$(UV) python scripts/get_token.py

bench:
	$(UV) python "$(BENCH_BASE)/copilot_bench.py" \
		observe \
		--base-dir "$(BENCH_BASE)" \
		--results "$(BENCH_OBSERVE_RESULTS)" \
		--session "$(BENCH_OBSERVE_SESSION)" \
		--label "$(BENCH_OBSERVE_LABEL)" \
		--follow $(BENCH_OBSERVE_ARGS)

inspect:
	npx @modelcontextprotocol/inspector

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
