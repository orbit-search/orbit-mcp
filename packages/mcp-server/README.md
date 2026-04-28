# @openea/orbit-mcp

MCP server that exposes the Orbit people-search API as tools for any MCP-compatible AI client (Claude Desktop, Cursor, etc.).

## Tools

| Tool | Description |
|------|-------------|
| `search_people` | Search for people by name, phone, email, or description. Returns matching profiles with basic info. |
| `get_profile` | Get a full profile by user ID or username. Returns bio, jobs, education, family, social links, and more. |

## Build

```bash
cd packages/mcp-server
npm install
npm run build
```

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

## Authentication & Billing

The HTTP server requires an `x-api-key` header on all `/mcp` requests. Keys are validated against your billing API, which also tracks credit usage.

### Credit costs

| Tool | Cost |
|------|------|
| `search_people` | 1 credit per person returned |
| `get_profile` | 2 credits per call |

### Billing API contract

The MCP server calls your billing service at `BILLING_API_URL` (defaults to `https://api.orbitsearch.com/v1/mcp/billing`). Your service must implement:

**Check credits** — `GET {BILLING_API_URL}/credits`

Request header: `x-api-key: <client key>`

Response:
```json
{ "valid": true, "credits": 100 }
```

**Deduct credits** — `POST {BILLING_API_URL}/deduct`

Request header: `x-api-key: <client key>`

Request body:
```json
{ "credits": 5, "tool": "search_people" }
```

Response:
```json
{ "success": true, "remaining": 95 }
```

### Error responses

| Status | Meaning |
|--------|---------|
| `401` | Missing or invalid API key |
| `403` | Insufficient credits |
| `502` | Billing service unavailable |

### Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BILLING_API_URL` | Base URL of your billing service | `https://api.orbitsearch.com/v1/mcp/billing` |

The stdio transport (`npm start`) bypasses authentication entirely — no API key or billing required.

## Remote HTTP server

The HTTP entry point exposes the MCP server over Streamable HTTP at `/mcp`.

```bash
npm run start:http
```

This starts on port 3000 by default. Set `PORT` env var to change it.

### Deploy to Railway / Render / Fly.io

1. Set the build command to `npm install && npm run build`
2. Set the start command to `node build/http.js`
3. Expose port `3000` (or set `PORT` env var to match the platform)

### Connect remote clients

Point your MCP client to the deployed URL. Include `x-api-key` if auth is enabled:

```json
{
  "mcpServers": {
    "orbit": {
      "type": "streamable-http",
      "url": "https://your-deployed-url.example.com/mcp",
      "headers": {
        "x-api-key": "YOUR_API_KEY"
      }
    }
  }
}
```

The `/health` endpoint returns `{"status":"ok"}` for monitoring.

## Example tool calls

### search_people

Input:
```json
{ "query": "Elon Musk" }
```

Output:
```json
{
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
  ]
}
```

### get_profile

Input:
```json
{ "userId": "abc-123-def" }
```

Or by username:
```json
{ "username": "elonmusk" }
```

Output:
```json
{
  "displayName": "Elon Musk",
  "avatarUrl": "https://...",
  "username": "elonmusk",
  "location": { "city": "Austin", "region": "TX", "country": "US" },
  "bio": "CEO of Tesla and SpaceX...",
  "birthday": "June 28, 1971",
  "school": "University of Pennsylvania",
  "jobs": [{ "title": "CEO", "company": "Tesla" }],
  "education": [{ "school": "University of Pennsylvania", "degree": "BS Physics" }],
  "interests": ["AI", "space exploration", "electric vehicles"],
  "family": [{ "name": "...", "relationship": "..." }],
  "accomplishments": [{ "description": "..." }],
  "controversies": [{ "description": "..." }],
  "socialLinks": [{ "network": "twitter", "url": "https://x.com/elonmusk", "username": "elonmusk" }],
  "worldview": "...",
  "sources": [{ "source": "LinkedIn", "url": "https://..." }]
}
```
