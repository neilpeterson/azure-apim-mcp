# Search index (T-15 / T-16)

## What it is

A tool that lets a model find the right API/operation by describing what it
wants ("get inventory levels") instead of needing to already know exact API
or operation names. It is **lexical (BM25 keyword) search, not semantic
search** — see "Is this semantic search?" below.

Two tools implement this:

- **`apim_search_apis`** — search across all configured APIM instances.
- **`apim_refresh_index`** — force an immediate rebuild instead of waiting
  for the background refresh.

## How the index is built

For each configured APIM service, at startup and on a TTL-based background
refresh (default 15 minutes, `INDEX_TTL_SECONDS`):

1. List the service's current-revision APIs.
2. For each API, list its operations.
3. Best-effort export the API's OpenAPI/Swagger document (the same two-call
   flow as `apim_get_api_spec`) to pull operation descriptions and request/
   response schema property names. If export fails for an API (some APIs
   — e.g. SOAP passthrough — can't export), that API is still indexed from
   its operations list alone, with `specIndexed: false` on its entries.

Each operation becomes one `OperationIndexEntry` — a document combining:

| Field | Weight |
|---|---|
| Operation display name | ×3 |
| API display name | ×2 |
| URL template | ×2 |
| Operation/API description | ×1 |
| Parameter names | ×1 |
| Schema property names | ×1 |
| API tags | ×1 |

Names/paths are tokenized first (camelCase/PascalCase/snake_case/kebab-case
and URL segments split into words — see `src/apim_mcp/index/tokenize.py`),
so `getInventoryLevels` becomes `{get, inventory, levels}` and matches a
plain-English query for "inventory".

## How search works

`apim_search_apis` runs [BM25](https://en.wikipedia.org/wiki/Okapi_BM25)
(via `rank-bm25`) over the tokenized corpus built above. There is no vector
index, no embeddings model, and no external search service — the whole
index is Python objects held in memory by the running server process.

Because it's purely lexical, it only matches on words that actually appear
in the indexed text — it cannot infer that "stock levels" means the same
thing as "inventory". To compensate, the tool asks the calling model to
supply its own synonyms via the `terms` parameter (e.g.
`terms: ["inventory", "stock", "availability", "quantity", "sku",
"warehouse"]` for an inventory-flavored question); `terms` are tokenized
and OR'd into the query the same way the free-text `query` is.

Every hit reports:

- `matchedFields` — which indexed fields actually contained a query token.
- `snippet` — a short excerpt of raw text around the first match, for a
  quick sanity check without a follow-up call.
- `lowConfidence: true` when the top score is at or below a floor — a weak
  or nonsense query still gets hits back (never hidden), but flagged as
  not a confident answer.

`scope` controls whether results are per-operation (`"operations"`,
default-equivalent `"both"`) or deduplicated to the best-scoring operation
per API (`"apis"`).

## Storage and infrastructure

None beyond the running server's memory. There is no database, cache
service, or persisted index file — a server restart rebuilds from scratch
by calling ARM/the spec-export flow again, the same way `apim_get_api`/
`apim_get_api_spec` do for a single API. This keeps the feature consistent
with every other tool's "no state beyond in-flight ARM calls" model.

## Refresh behaviour

- **Eager at startup**: the index build for every configured service kicks
  off (non-blocking) when the server starts; requests don't wait on it —
  they get `index_unavailable` for a not-yet-built service until it
  finishes.
- **Background TTL refresh**: once an index is older than
  `INDEX_TTL_SECONDS`, the next search for that service serves the stale
  index immediately and fires a background rebuild — a request is never
  blocked on a rebuild.
- **Manual refresh**: `apim_refresh_index` forces a rebuild for one service
  (or all, if `service` is omitted) and waits for it to finish before
  returning counts. Rate-limited to once per service per 60 seconds to
  avoid a chatty model hammering ARM.

## One caveat worth flagging

The index is intentionally a single shared structure across every caller —
not one copy per user — because every caller currently has identical read
access via the server's managed identity (see `docs/PRINCIPLES.md` §3's
one documented exception to "every cache key includes `oid`"). If the
server ever moves to on-behalf-of auth, where callers can have different
access, this needs a post-filter step before results are returned, not a
switch to per-caller indexes. Every entry point into the index carries a
`# OBO:` comment noting this.
