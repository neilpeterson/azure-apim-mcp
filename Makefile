.PHONY: check lint types test fmt fixtures run inspect clean

# The gate. No task is complete until this passes.
check: lint types test

lint:
	uv run ruff check .
	uv run ruff format --check .

types:
	uv run mypy --strict src/ tests/

test:
	uv run pytest -q

fmt:
	uv run ruff format .
	uv run ruff check --fix .

# HUMAN ONLY. Records sanitised ARM responses against a real non-prod instance.
# Agents must not run this - it requires live Azure credentials.
fixtures:
	uv run python scripts/record_fixtures.py

run:
	uv run python -m apim_mcp.server

inspect:
	npx @modelcontextprotocol/inspector

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
