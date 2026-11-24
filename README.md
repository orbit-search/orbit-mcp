# Orbit (Cursor plugin)

Search people, read source-backed profiles, enrich context, and manage directories through Orbit MCP.

## Install

1. Install from the Cursor Marketplace, or load this folder under `~/.cursor/plugins/local/orbit`.
2. Set **Orbit API key** (`ORBIT_API_KEY`) from https://developer.orbitsearch.com/dashboard/keys with `search:read` and `profile:read`.
3. Clients that support remote OAuth can connect to `https://api.orbitsearch.com/mcp` without an API key for account sign-in.

## MCP

- URL: `https://api.orbitsearch.com/mcp`
- Transport: Streamable HTTP
- Auth: `Authorization: Bearer <sk_orb_...>` or OAuth

## Skills

- `orbit-people-search` — search, profiles, enrichment
- `orbit-directories` — organization directory workflows (OAuth)

## Docs

- https://docs.orbitsearch.com/mcp-server
- https://developer.orbitsearch.com/
