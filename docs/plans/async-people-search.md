# Async people search implementation

Status: implementation and deterministic regression validation complete; PR review and release pending.

The public MCP held `search_people` open while polling discovery and enrichment.
The native ChatGPT adapter returned the first API response, but API intake could
still wait on parsing and provider work. Client deadlines could expire while
server work continued. This change makes durable acceptance the response boundary.

- API: `Prefer: respond-async` on `POST /v3/search` persists the existing run,
  request hash and credit admission before parsing/search/provider work. The
  normal reconciler and after-response advance recover pending intake. Replays
  return the same search; client disconnects do not cancel durable work. Ordinary
  REST callers keep their existing response path.
- Ownership: async advances keep a renewable, token-owned Redis lease with a
  local deadline and cooperative execution fences. An expired owner cannot
  release a replacement's lock or continue new IO after losing ownership.
- Tools: search returns initial snapshots; status polling returns existing partial
  hits and phases. Public enrichment also returns promptly with a polling tool.
  `find_people` is an explicit quick-resolution step; deeper enrichment uses
  selected profile IDs. No existing depth or candidate defaults change.
- Batches: reuse `/v3/search/bulk` durable admission, scheduler, authorization,
  pagination and idempotency. Completed people publish through the existing
  incremental settlement before siblings finish. Row-local upstream outages
  retain their state and retry independently. Storage/lease/billing failures
  remain job recovery concerns. No new tariff, provider, schema or queue.
- Progress: matching/discovering/enriching comes from stored workflow state.
  Counts are observed results. Percent is unknown until discovery is complete;
  ETA stays null without measured data. No fabricated linear progress.
- Recovery: schema and skill text instruct callers to persist request IDs, poll
  known IDs, reuse identical requests after timeout, preserve payer context,
  keep partial successes and stop only at terminal states.

Validation must include running results accepted by advertised output schemas,
no internal polling for search/enrich tools, full-depth/default preservation,
timeout retries with identical request IDs, quick-resolution routing, paginated
mixed-person results, per-person payment before publication, interrupted payment
recovery and two workers across lease renewal/loss. Use deterministic providers
and disposable local databases. Do not spend real customer/provider credits to
test timeout handling.

Release order: API first, then public MCP and ChatGPT app. Adapters rely on the
API honoring `Prefer: respond-async`; staged deployment must verify that header
before claiming low-latency acceptance. Follow each repository's review/merge
and release-check gates. Hosted catalog refresh and packaged skill delivery are
separate from building/deploying the server. Existing app/plugin identity stays.

The Deep Search FSM and generation modes are unchanged. This is an API intake,
transport, ownership and result-publication change; the new phase labels project
existing states rather than adding workflow transitions.

Validation evidence (2026-11-01): public MCP 74 tests and native ChatGPT 369 tests
pass. API focused tests cover intake/replay, lease renewal/loss and per-person
publication/retry/settlement failure. The full agent check covers typecheck,
contract, lint, format and the complete unit suite. The disposable CockroachDB
bulk suite is blocked before scenarios run: Sequelize model index setup times
out in its 60-second beforeAll hook (also reproduced with the unchanged model
alone). Do not report those database-backed scenarios as passed. No real
provider/customer calls were made. Temporary local database/Redis processes
were stopped after diagnosis.
