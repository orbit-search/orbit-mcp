---
name: orbit-directories
description: Manage Orbit directories through MCP, including membership, imports, scoped research, sharing, refreshes, watchers, and activity.
---

# Orbit directories

Use Orbit's general MCP server at `https://api.orbitsearch.com/mcp`. It provides
people search, rich profiles, enrichment, and organization-scoped directory
management. Directory tools require an OAuth user connection; API keys alone
support the search and enrichment tools, not directory management.

For a remote/headless agent, use `orbit-mcp login --scope 'search.read
directories.read directories.write'` when the issuer discovery advertises
`device_authorization_endpoint`. Share only `verification_uri_complete` and
`user_code` with the user. They sign in on their own device, match the code, and
click **Approve login**; the running CLI connects automatically. Do not open the
remote browser, ask for passwords/tokens, or request a pasted callback URL. Keep
credentials in the private agent-side login file. Use `orbit-mcp tools`/`call` or
configure `orbit-mcp stdio` after login. Denial/expiry ends the attempt. If discovery
lacks the endpoint, report that remote approval is not enabled there. Existing
hosted MCP clients retain their account-linking UI. Request only needed scopes;
read-only directory work can omit `directories.write`.

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
