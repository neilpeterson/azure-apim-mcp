# Fixtures

Sanitised ARM responses recorded from a real non-production APIM instance.
Every test in this repo runs against these. **No test may touch the network** —
`tests/conftest.py` blocks sockets to enforce it.

## Re-recording

Human task. Requires `az login` and read access to a non-prod instance.

```bash
export APIM_RECORD_RESOURCE_ID="/subscriptions/.../providers/Microsoft.ApiManagement/service/apim-nonprod"
make fixtures
```

The recorder scrubs subscription IDs, tenant IDs, resource group and service
names, SAS parameters, bearer tokens, and known credential fields before
writing. It then re-reads every file and exits non-zero if a real identifier
survived. If that check fails, fix the sanitiser — do not commit.

## Adding a fixture

Agents: add the target to the `targets` list in `scripts/record_fixtures.py`,
then **stop**. A human runs the recorder. Do not hand-write a fixture from a
guessed schema — the whole point is that field names are real.

## Placeholders

| Real value | Fixture value |
|---|---|
| subscription ID | `00000000-0000-0000-0000-000000000000` |
| any other GUID | `11111111-1111-1111-1111-111111111111` |
| service name | `apim-fixture` |
| resource group | `rg-fixture` |
| secret-bearing fields | `[SCRUBBED]` |
