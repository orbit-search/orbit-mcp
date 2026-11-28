# Orbit Search MCP server

Orbit Search (orbitsearch.com) is a people data API and search engine from Orbit Intelligence. It finds people from a name, email address, phone number, username, photo or plain-English description and returns source-backed identity profiles for AI agents and apps. Use it through the REST API, the MCP server (Claude Code, Codex, Cursor and others) or the ChatGPT app. Pay as you go from $10 for 1,000 credits, with free credits at sign-up.

This repository holds Orbit's MCP server. The hosted endpoint is `https://api.orbitsearch.com/mcp`
(Streamable HTTP), with OAuth sign-in or an Orbit API key. The MCP tools take names, email addresses,
phone numbers, usernames, profile URLs, street addresses and plain-English descriptions; photo (face)
search is available through the REST API.

## Connect

Claude Code:

```bash
claude mcp add --transport http orbit https://api.orbitsearch.com/mcp
```

Other MCP clients: add `https://api.orbitsearch.com/mcp` as a remote (Streamable HTTP) server and sign in,
or send an API key from [developer.orbitsearch.com/dashboard/keys](https://developer.orbitsearch.com/dashboard/keys)
(scopes `search:read` and `profile:read`) as `Authorization: Bearer sk_orb_...`. Setup and troubleshooting:
[docs.orbitsearch.com/mcp-server](https://docs.orbitsearch.com/mcp-server).

## Tools

| Tool | What it does |
|---|---|
| `search_people` | Find people from a name, email, phone, username, URL, address or description; poll `get_search_status`. |
| `find_people` | Identify who an email, phone, address or profile URL belongs to, at quick depth. |
| `get_search_status` | Read a search's progress and results without starting work. |
| `get_profile` | Read a source-backed profile by ID, as JSON or Markdown files. |
| `enrich_profile`, `get_enrichment_status` | Build a profile deeper (quick, partial, full) or regenerate it. |
| `search_people_batch`, `get_people_batch_status` | Look up to 100 people in one job. |
| `quote_population_search`, `search_population` | Price, then search, every current employee of a company or everyone who attended a school. |
| `find_network_path` | Show how two people are connected through Orbit's Network Graph. |
| `get_credit_usage` | Read credits used and the balance. |
| 38 directory tools | Manage Orbit directories (requires OAuth sign-in, not an API key alone). |

Tools that build or read profiles use credits; see [developer.orbitsearch.com/pricing](https://developer.orbitsearch.com/pricing).

## In this repository

- [`packages/mcp-server`](packages/mcp-server/README.md): the server behind `api.orbitsearch.com/mcp`, with local stdio and self-hosting instructions.
- [`packages/profile-server`](packages/profile-server): a single-tool profile-resolution MCP server.
- [`skills/`](skills): agent skills for person research and directory management.
- [`server.json`](server.json): the [MCP Registry](https://registry.modelcontextprotocol.io) entry, `io.github.orbit-search/orbit`.
