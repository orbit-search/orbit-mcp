# Orbit MCP

Public MCP server for Orbit Developer API v3 Search and Enrich. The hosted endpoint supports OAuth sign-in or an Orbit Developer API key. This package forwards authenticated requests to the v3 API, which enforces scopes and rate limits.

## Tools

| Tool | Purpose | Orbit API |
|---|---|---|
| `search_people` | Find people from a plain-English query and/or identity signals, optionally discover candidates, and build partial or full profiles | `POST /v3/search`, then `GET /v3/search/{search_id}` |
| `get_profile` | Read an existing profile without scheduling regeneration | `GET /v3/enrich/{profile_id}` |
| `enrich_profile` | Ensure a known profile is partial/full or regenerate a full profile | `POST /v3/enrich/{profile_id}`, then `GET /v3/enrich/requests/{request_id}` when needed |

Search and enrichment tools poll to a terminal state. They honor `Retry-After` and retry `429`/`5xx` responses with exponential backoff and jitter. A caller may provide `request_id`; persist and reuse it only when retrying the exact same logical request.

## Billing

Orbit uses usage-based credits. Customers connect their own Orbit API key, and Orbit applies charges under their account plan. The Developer API owns pricing and the billing ledger; neither MCP package calculates or debits credits locally. Consult the central [pricing catalog](https://api.orbitsearch.com/v2/developer/pricing) for operation rates and purchase packages, and manage your balance in the [billing dashboard](https://developer.orbitsearch.com/dashboard/billing).

HTTP `402` is returned as a tool error with billing guidance, without automatic retries. Automatic retries of Search and Enrich submissions reuse the same serialized body and `request_id`; status polling continues the existing operation rather than starting new work. When retrying a logical operation manually, retain its `request_id` too.

The profile-resolution package returns an embedded Search profile when available. If Search omits it, the package performs an explicit profile read, subject to the API's profile-read billing policy.

All three tools declare output schemas and return `structuredContent` alongside
the same serialized JSON in `content` for compatibility. Schemas describe the v3
response envelopes; profile bodies and new API fields remain open-ended so no
person context or source evidence is dropped. Tool errors retain `isError: true`;
exception payloads are `{ error: string }` in text content only, outside the
successful output schema. This keeps schema-validating clients from hiding the
actionable error behind a structured-output validation failure.

Annotations mark only `get_profile` as read-only and idempotent. Search may build
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
authorization header. Start its sign-in flow, review Orbit's consent screen, and
continue with Google. The client handles Orbit access tokens and refresh.

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

Both packages use v3 endpoints for search and enrichment. The shared pricing catalog is served separately at `/v2/developer/pricing`.
