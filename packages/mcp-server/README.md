# Orbit MCP

General MCP server for Orbit people search, profiles, enrichment, and directory management. The hosted endpoint is `https://api.orbitsearch.com/mcp`. Search and enrichment support OAuth sign-in or an Orbit Developer API key; directory management requires a signed-in Orbit user and the appropriate organization permissions.

## Tools

| Tool | Purpose | Orbit API |
|---|---|---|
| `search_people` | Find people from a plain-English query and/or identity signals, optionally discover candidates, and build partial or full profiles | `POST /v3/search`, then `GET /v3/search/{search_id}` |
| `get_profile` | Read an existing profile without scheduling regeneration | `GET /v3/enrich/{profile_id}` |
| `enrich_profile` | Ensure a known profile is partial/full or regenerate a full profile | `POST /v3/enrich/{profile_id}`, then `GET /v3/enrich/requests/{request_id}` when needed |

Search and enrichment tools poll to a terminal state. They honor `Retry-After` and retry `429`/`5xx` responses with exponential backoff and jitter. A caller may provide `request_id`; persist and reuse it only when retrying the exact same logical request.

The three search/profile tools declare output schemas and return `structuredContent` alongside
the same serialized JSON in `content` for compatibility. Schemas describe the v3
response envelopes; profile bodies and new API fields remain open-ended so no
person context or source evidence is dropped. Tool errors retain `isError: true`;
exception payloads are `{ error: string }` in text content only, outside the
successful output schema. This keeps schema-validating clients from hiding the
actionable error behind a structured-output validation failure.

Of these three tools, only `get_profile` is read-only and idempotent. Search may build
profiles; enrichment may regenerate and replace existing context. Neither write
tool promises unconditional idempotency because `request_id` is optional.

After building, `node scripts/server-card.mjs` prints a credential-free discovery
card from the actual registered tools. Mirror it into the infrastructure Worker's
`server-card.mjs` only with a coordinated release: deploy the MCP server first,
compare live discovery, then deploy the card and republish Smithery. Do not
advertise the new contract before its runtime is deployed.

## Authentication

For API-key connections, use an Orbit Developer API key with:

- `search:read` for Search, polling, and Enrich operations;
- `profile:read` for profile reads and embedded profiles.

HTTP clients using API-key authentication must send the key on every MCP request:

```http
Authorization: Bearer sk_orb_REDACTED
```

There is no bypass key. The MCP forwards the key only to the configured Orbit API base URL.

### Smithery setup

Create a key at [Orbit API keys](https://developer.orbitsearch.com/dashboard/keys)
with `search:read` and `profile:read`. In Smithery's **Orbit API key** field, paste
only the `sk_orb_...` key. Existing `Bearer sk_orb_...` values still work.
The same `orbitAuthorization` config field and `x-orbit-authorization` header are
retained so saved connections do not need to be recreated. Smithery forwards this
secret in the Authorization header, never in the URL. The MCP normalizes the key
and sends standard Bearer authentication to the v3 API.

Missing credentials return setup instructions. If a tool returns HTTP 401, check
whether the key is invalid, expired, or revoked, then reconnect with a valid key.
Invalid-key HTTP 403 responses also explain how to reconnect; other HTTP 403
responses point to scopes and account access. Discovery alone does not validate
the key: run `get_profile` with a known profile ID to verify upstream access.
Changing to a different key requires a new MCP session.

## Local stdio

```bash
npm install
npm run build
ORBIT_DEVELOPER_API_KEY=sk_orb_REDACTED npm start
```

Claude Desktop or another stdio MCP client:

```json
{
  "mcpServers": {
    "orbit": {
      "command": "node",
      "args": ["/absolute/path/to/orbit-mcp/packages/mcp-server/build/index.js"],
      "env": {
        "ORBIT_DEVELOPER_API_KEY": "sk_orb_REDACTED"
      }
    }
  }
}
```

## Remote Streamable HTTP

Use the public endpoint `https://api.orbitsearch.com/mcp`. See the
[connection guide](https://docs.orbitsearch.com/mcp-server) for setup and troubleshooting.
For OAuth, add the URL to an OAuth-capable MCP client without a custom
authorization header. Start its sign-in flow and continue with Google or Orbit
email/password. The client handles Orbit access tokens and refresh.

API-key connections remain supported using the configuration below. Smithery's
API-key setup is unchanged. OAuth is provided by the hosted endpoint's gateway;
self-hosted instances of this package use API keys.

To run your own instance:

```bash
npm run start:http
```

The MCP endpoint is `/mcp`; `/health` is an unauthenticated process-health endpoint.

```json
{
  "mcpServers": {
    "orbit": {
      "type": "streamable-http",
      "url": "https://api.orbitsearch.com/mcp",
      "headers": {
        "Authorization": "Bearer sk_orb_REDACTED"
      }
    }
  }
}
```

## Tool examples

Known people search:

```json
{
  "query": "machine learning engineers in San Francisco",
  "candidate_discovery": false,
  "profile_depth": "partial",
  "limit": 10,
  "request_id": "crm-search-job-123"
}
```

Identity resolution:

```json
{
  "signals": {
    "email": "person@example.com",
    "linkedin_url": "https://www.linkedin.com/in/example"
  },
  "candidate_discovery": true,
  "profile_depth": "full",
  "limit": 1
}
```

Phone and address signals always disable candidate discovery, even if the caller requests it.

Profile enrichment:

```json
{
  "profile_id": "profile_123",
  "operation": "full",
  "request_id": "crm-enrich-job-456"
}
```

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `ORBIT_DEVELOPER_API_KEY` | Required for stdio transport | none |
| `ORBIT_API_URL` | Orbit API base URL | `https://api.orbitsearch.com` |
| `ORBIT_POLL_TIMEOUT_MS` | Maximum time spent polling one operation | `300000` |
| `PORT` | HTTP server port | `3000` |

## v2 migration notes

- v2 synchronous smart search was replaced by asynchronous v3 Search polling.
- `numUsers` became `limit` and is capped at 20.
- Match reasons and credit headers are not part of the v3 Search contract.
- `get_profile` now returns the v3 Enrich read envelope.
- `enrich_profile` is the explicit path for upgrading or regenerating known profile IDs.
- Identity signals belong in `search_people`, not `enrich_profile`.

Search and enrichment use v3. Directory tools use the existing user-authenticated
directory management routes; developer API keys do not authorize those routes.

## Directory management

The catalog also includes 38 directory tools: organization discovery, directory
lifecycle, member management, scoped search, CSV imports and source tracking,
entity population, access grants, one-time refreshes, directory watchers, and
activity. Their schemas and exact routes live in `src/directory-tools.ts`.
Response-family schemas in `src/directory-output-schemas.ts` describe directories,
members, pagination, counts, sources, grants, refreshes and watchers while retaining
unknown profile evidence and partial responses. Failed operations preserve their
structured diagnostics with `isError`; empty successful mutations return `{ data: {} }`.
Use the [general MCP directory skill](../../skills/orbit-directories/SKILL.md)
for workflows and safeguards. These capabilities are not ChatGPT-specific.

Request `search.read directories.read directories.write` through hosted OAuth.
Read and write scopes are independent. Existing connections must reconnect for
new scopes; API-key-only and local stdio connections cannot manage directories.
Orbit still enforces organization roles and directory grants on each API call.
The directory login expires after its existing upstream lifetime; OAuth refresh
does not prolong it. Reconnect if directory tools report an expired login.

CSV text is limited to 512 KiB of UTF-8, with a stable UUID idempotency key.
Use the dashboard for larger files and logo/banner uploads. Archive is a soft
delete. Research refreshes and watchers may consume credits; accepted or queued
work is not completion. Writes are not automatically retried after uncertainty.

### Gateway trust boundary

The separate public OAuth gateway validates the session for every request, then
forwards a short-lived signed delegation to this service. This service pins the
gateway's public JWKS, issuer, audience and EdDSA algorithm and checks the API-key
hash, exact body hash, HTTP method and MCP session ID. Authorization is
request-local, never retained from initialization. No client can supply an
unsigned user token or scopes to gain directory access. Search remains key-only
when no valid directory delegation is present.

The delegation header contains a private user credential: never log or expose it.
Self-hosted instances do not accept arbitrary gateway keys or URLs. Production
OAuth configuration is owned by the shared gateway, not MCP tool inputs.

### Validation and release

Run `npm test`. Tests exercise all 38 tools over real local HTTP with signed
synthetic delegations, request-local scope changes, expiration, forged grants,
API-key compatibility, CSV limits, scoped search, and backend denial. Orbit API
responses are local fakes, not production behavior proof.

Deploy this server and the companion shared OAuth gateway change before
publishing directory docs. Verify the 41-tool catalog and a disposable directory
through the canonical public endpoint. Update the infrastructure-owned static
server card only after the deployed contract is verified.
