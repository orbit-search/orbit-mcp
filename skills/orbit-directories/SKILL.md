---
name: orbit-directories
description: Manage Orbit directories through MCP, including membership, imports, scoped research, sharing, refreshes, watchers, and activity.
---

# Orbit directories

Use Orbit's general MCP server at `https://api.orbitsearch.com/mcp`. It provides
people search, rich profiles, enrichment, and organization-scoped directory
management. Directory tools require an OAuth user connection; API keys alone
support the search and enrichment tools, not directory management.

For remote agents, connect to `https://api.orbitsearch.com/mcp-auth/agent`.
This uses ordinary MCP tools and does not require host-specific OAuth support,
a CLI, or a browser on the agent's computer. Availability requires the gateway
and edge release; if the login tools are absent, report that deployment limitation.
Existing hosted connectors retain their native account-linking UI.

1. Call `orbit_login`, requesting only needed permissions. Use `search.read`
   for people research. Listing or reading directories also needs
   `directories.read`; `search_directory` and directory changes need
   `directories.write` as well, even when the task is research. For directory
   research request `["search.read", "directories.read", "directories.write"]`.
   Keep `login_request` private. The server starts listening before returning
   the link.
2. Call `orbit_wait_for_login` with `login_request` and `wait_seconds: 0`
   **before sharing the link**, confirming the request is pending.
3. Send only the clickable `login_url` to the user. Its code is already included.
   Tell them to open it on their own device and sign in; sign-in finishes the
   connection automatically. Never ask for passwords, a code, tokens, or a callback.
4. Keep calling `orbit_wait_for_login` with the same handle until connected,
   honoring `retry_after` (including after a temporary interruption). Use one
   waiter per login request. Calls can
   wait up to 25 seconds each. Do not stop after delivering the link, or require
   the user to tell you when they finished. Denial/expiry ends the attempt.
5. Keep the returned `connection_token` private and pass it to `orbit_list_tools`
   and `orbit_call_tool` on every call. Discover the underlying tool schemas,
   then use their names and arguments through `orbit_call_tool`. Use
   `orbit_logout` when asked to disconnect. Never show or log either private
   handle; OAuth tokens, Orbit keys, and user sessions remain on the server.

The connection token is a separate application credential, not an MCP session ID.
It authorizes this MCP bridge only, not direct REST calls. If it expires or is
revoked, start a new login. Native OAuth clients can alternatively use the
advertised device grant in their private authentication layer.

Read [directory workflows](references/workflows.md) before using directory tools.
Discover organization and directory IDs; do not guess targets or grant IDs.
Keep research within the selected directory. A denied or unready directory is
not permission to search the global index instead.

For substantive questions about a member, use Orbit to resolve their identity
and read their full profile with `get_profile` using the returned `profile_id`.
Do not substitute remembered facts, a summary card, or web search for available
Orbit profile evidence. Respect an explicit request for a different source and
report unavailable evidence honestly.

Confirm the exact targets and impact of requested changes. Imports, research
refreshes, and ongoing watchers may consume organization credits. Distinguish
accepted work from completion, preserve returned IDs for status checks, and do
not blindly retry a mutation that may already have succeeded.
