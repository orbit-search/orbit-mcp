# Orbit MCP

Public MCP server for Orbit Developer API v3 Search and Enrich. It is a thin authenticated adapter: every Orbit request uses the connecting customer's Developer API key and remains subject to that key's scopes and rate limits.

## Tools

| Tool | Purpose | Orbit API |
|---|---|---|
| `search_people` | Find people from a plain-English query and/or identity signals, optionally discover candidates, and build partial or full profiles | `POST /v3/search`, then `GET /v3/search/{search_id}` |
| `get_profile` | Read an existing profile without scheduling regeneration | `GET /v3/enrich/{profile_id}` |
| `enrich_profile` | Ensure a known profile is partial/full or regenerate a full profile | `POST /v3/enrich/{profile_id}`, then `GET /v3/enrich/requests/{request_id}` when needed |

Search and enrichment tools poll to a terminal state. They honor `Retry-After` and retry `429`/`5xx` responses with exponential backoff and jitter. A caller may provide `request_id`; persist and reuse it only when retrying the exact same logical request.

## Authentication

Use an Orbit Developer API key with:

- `search:read` for Search, polling, and Enrich operations;
- `profile:read` for profile reads and embedded profiles.

Remote HTTP clients must send the key on every MCP request:

```http
Authorization: Bearer sk_orb_REDACTED
```

There is no bypass key. The MCP forwards the key only to the configured Orbit API base URL.

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

```bash
npm run start:http
```

The MCP endpoint is `/mcp`; `/health` is an unauthenticated process-health endpoint.

```json
{
  "mcpServers": {
    "orbit": {
      "type": "streamable-http",
      "url": "https://your-orbit-mcp.example.com/mcp",
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

No v2 endpoint remains in either package in this repository.
