.PHONY: check lint types test fmt fixtures run token inspect clean

# --no-sync: never touch the network on a plain `make check`/`make run`. The
# venv is expected to already satisfy pyproject.toml; run `uv sync` by hand
# after adding/upgrading a dependency (see docs/LOCAL_TESTING.md).
UV := uv run --no-sync

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

# Acquire a token and write it to .vscode/mcp.json for local VS Code dev.
# Workaround for macOS platform broker issues - see docs/DEPLOYMENT.md.
token:
	$(UV) python scripts/get_token.py

inspect:
	npx @modelcontextprotocol/inspector

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
