# Local testing

Brief, practical reference for running this repo's **test suite and dev
loop** on a machine — `make check`, fixture recording, dependency
management. For running the actual MCP server (locally or deployed) and
connecting a client like VS Code, see `docs/DEPLOYMENT.md`.

Every `make` target runs `uv run --no-sync ...` — it never touches the
network or re-resolves dependencies, only uses whatever is already installed
in `.venv`. If you add or upgrade a dependency in `pyproject.toml`, run
`uv sync` yourself once before the next `make check`/`make run`; otherwise
you'll get a `ModuleNotFoundError` instead of a package being fetched
automatically.

## `.env` file (for running the server, not the test suite)

The test suite never reads `.env` or any real environment variable —
`tests/conftest.py` blocks real socket connections for every test, and ARM
calls are replayed from `tests/fixtures/*.json` via `FixtureTransport`
(`tests/_fixture_transport.py`). `Settings`/`get_settings()` only sees real
process env vars (via `monkeypatch` in tests), so a stray `.env` at the repo
root cannot affect `make check`/`make test`.

If you do need to run the server (`make run`) or record fixtures
(`make fixtures`), the idiomatic way to supply env vars without committing
them: copy `.env.example` to `.env` and fill in real values.

```bash
cp .env.example .env      # PowerShell: Copy-Item .env.example .env
```

`make run` loads it automatically (`src/apim_mcp/server.py:main` calls
`dotenv.load_dotenv()` before reading settings) — no `export`/`$env:` needed,
and it works identically on bash and PowerShell since the loading happens in
Python, not the shell. `.env` is already listed in `.gitignore`, so it can
never be committed by accident. A real exported/`$env:` variable still wins
over anything in `.env` (`load_dotenv()` defaults to `override=False`).

See `docs/DEPLOYMENT.md` for the full environment variable reference and
what each one does.

## Running tests

```bash
make check   # the gate: ruff check + ruff format --check + mypy --strict + pytest
make test    # pytest only
make fmt     # auto-fix formatting/lint
```

No test touches the network or a real Azure credential.

To run a single file or test:

```bash
uv run pytest tests/test_tools_discovery.py -q
uv run pytest tests/test_tools_discovery.py::test_cert_expiry_computed -q
```

## Recording/refreshing fixtures — human only

`make fixtures` calls real Azure APIs and must be run by a human against a
non-prod APIM instance, never by an agent:

**macOS/Linux (bash)**
```bash
az login
export APIM_RECORD_RESOURCE_ID="<your non-prod APIM resource ID>"
make fixtures
```

**Windows (PowerShell)**
```powershell
az login
$env:APIM_RECORD_RESOURCE_ID = "<your non-prod APIM resource ID>"
make fixtures
# or, if `make` isn't installed:
uv run python scripts/record_fixtures.py
```

Output is written to `tests/fixtures/*.json`. See
`tests/fixtures/README.md` for the sanitisation rules the recorder applies.
