# @openea/orbit-mcp

MCP server that exposes the Orbit Developer API as tools for any MCP-compatible AI client (Claude Desktop, Cursor, etc.).

## Tools

| Tool | Description | Credits |
|------|-------------|---------|
| `search_people` | Search for people by name, phone, email, or description. Returns matching profiles. | `numUsers` per call |
| `get_profile` | Get a full profile by user ID (from search results). | per Developer API pricing |

## Build

```bash
cd packages/mcp-server
npm install
npm run build
```

## Authentication

The MCP server uses Orbit Developer API keys (`sk_orb_...`). Keys are passed through to the Orbit API, which handles validation, credit metering, and rate limiting.

Clients authenticate with:

```http
Authorization: Bearer sk_orb_YOUR_KEY
```

Get a key via `POST /v2/api-keys` with scopes `["search:read", "profile:read"]`.

## Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "orbit": {
      "command": "node",
      "args": ["/ABSOLUTE/PATH/TO/packages/mcp-server/build/index.js"]
    }
  }
}
```

## Cursor

Add to `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "orbit": {
      "command": "node",
      "args": ["/ABSOLUTE/PATH/TO/packages/mcp-server/build/index.js"]
    }
  }
}
```

## Remote HTTP server

The HTTP entry point exposes the MCP server over Streamable HTTP at `/mcp`.

```bash
npm run start:http
```

This starts on port 3000 by default. Set `PORT` env var to change it.

### Deploy

```bash
gcloud run deploy orbit-mcp \
  --image gcr.io/YOUR_PROJECT/orbit-mcp \
  --port 8080 \
  --allow-unauthenticated \
  --region us-central1
```

### Connect remote clients

```json
{
  "mcpServers": {
    "orbit": {
      "type": "streamable-http",
      "url": "https://your-deployed-url.example.com/mcp",
      "headers": {
        "Authorization": "Bearer sk_orb_YOUR_KEY"
      }
    }
  }
}
```

The `/health` endpoint returns `{"status":"ok"}` for monitoring.

### Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ORBIT_API_URL` | Base URL of the Orbit API | `https://api.orbitsearch.com` |
| `MCP_BYPASS_KEY` | Bypass auth for testing (set to `*` to allow all) | — |
| `PORT` | HTTP server port | `3000` |

## Example tool calls

### search_people

Input:
```json
{ "query": "Elon Musk", "numUsers": 10 }
```

Output:
```json
{
  "searchId": "uuid",
  "results": [
    {
      "displayName": "Elon Musk",
      "username": "elonmusk",
      "userId": "abc-123-def",
      "city": "Austin, TX",
      "age": 53,
      "matchReason": "Exact name match",
      "sourceCount": 42
    }
  ],
  "creditsRemaining": 90
}
```

### get_profile

Input:
```json
{ "profileId": "abc-123-def" }
```

Output:
```json
{
  "id": "abc-123-def",
  "orbitId": "orbit-id",
  "displayName": "Elon Musk",
  "avatarUrl": "https://...",
  "profileUrl": "https://orbitsearch.com/elonmusk",
  "verified": true,
  "location": { "city": "Austin, TX, US" },
  "headline": { "jobTitle": "CEO", "companyName": "Tesla", "schoolName": "UPenn" },
  "sections": {
    "basic": { "school": "University of Pennsylvania", "location": "Austin" },
    "jobs": { "...": "..." },
    "education": { "...": "..." },
    "passions": { "...": "..." },
    "accomplishments": { "...": "..." }
  }
}
```
