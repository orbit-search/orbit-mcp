/** General MCP directory operations backed by Orbit organization permissions. */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { requestDirectory, DirectoryApiError } from "./directory-api.js";
import { directoryContext } from "./directory-auth.js";

const uuid = z.string().uuid();
const text = z.string().trim().min(1).max(200);
const org = { organizationId: uuid };
const dir = { ...org, directoryId: uuid };
const person = { ...dir, personId: text };
const source = { ...dir, sourceId: uuid };
const page = { limit: z.number().int().min(1).max(100).optional(), offset: z.number().int().min(0).optional() };
const phases = z.array(z.enum(["existing_sources", "existing_socials", "new_sources"])).min(1).max(3);
const interval = z.number().int().min(3600).max(2592000);
const people = z.array(text).min(1).max(500);
const fields = {
  name: z.string().trim().min(1).max(200).optional(),
  description: z.string().max(5000).nullable().optional(),
  slug: z.string().min(1).max(200).nullable().optional(),
  visibility: z.enum(["private", "public"]).optional(),
  metadata: z.record(z.string(), z.unknown()).nullable().optional().describe("Existing directory metadata with explicitly requested changes only; preserve unrelated keys. Null explicitly clears metadata.")
};
const base = "/v2/organizations/:organizationId/directories";
const directory = `${base}/:directoryId`;
type Definition = {
  name: string; description: string; method: "GET" | "POST" | "PATCH" | "DELETE";
  path: string; input: Record<string, z.ZodType>; read: boolean; external?: boolean;
};
const read = (name: string, description: string, path: string, input: Definition["input"]): Definition =>
  ({ name, description, method: "GET", path, input, read: true });
const write = (name: string, description: string, method: Definition["method"], path: string, input: Definition["input"], external = false): Definition =>
  ({ name, description, method, path, input, read: false, external });

// Fixed, audited routes only. No tool accepts a URL, arbitrary method, or user credential.
export const directoryTools: Definition[] = [
  read("list_directory_organizations", "find the user's organizations before choosing a directory. Do not guess organization IDs.", "/v2/organizations", {}),
  read("list_directories", "list directories in a selected organization.", base, { ...org, limit: page.limit, after_directory_id: uuid.optional(), include_archived: z.boolean().optional() }),
  read("get_directory", "read a directory and its readiness before changing it.", directory, dir),
  write("search_directory", "search within one directory using natural language. Never fall back to global search if access is denied or the directory is not ready.", "POST", "/v2/social/profiles/searches/smart", { ...dir, query: z.string().trim().min(1).max(2000), numUsers: z.number().int().min(1).max(100).default(20) }, true),
  read("check_directory_slug", "check if a public directory slug is available.", `${base}/slug-availability`, { ...org, slug: text }),
  write("create_directory", "create a directory. Confirm the organization, name, and visibility; public makes it discoverable outside the organization.", "POST", base, { ...org, ...fields, name: z.string().trim().min(1).max(200), visibility: z.enum(["private", "public"]).default("private") }),
  write("update_directory", "change a directory's details. Read it first and confirm changes, especially public visibility.", "PATCH", directory, { ...dir, ...fields }),
  write("archive_directory", "archive a directory after confirming the exact target. Orbit's delete route also archives; this does not permanently erase data.", "POST", `${directory}/archive`, dir),
  read("list_directory_people", "list directory members, optionally filtering by name or source. This is not semantic search.", `${directory}/people`, { ...dir, ...page, name: z.string().max(120).optional(), source_id: uuid.optional(), include_unmatched: z.boolean().optional() }),
  read("get_directory_person", "read a directory member. Use its returned profile ID with get_profile for deep person research.", `${directory}/people/:personId`, person),
  read("count_directory_people", "count resolved directory members.", `${directory}/people/count`, dir),
  read("list_pending_directory_people", "inspect unresolved or processing directory members.", `${directory}/people/pending`, { ...dir, limit: page.limit }),
  { ...read("check_directory_membership", "check supplied identities for existing directory membership before import.", `${directory}/people/membership`, { ...dir, emails: z.array(text).min(1).max(100).optional(), linkedin_urls: z.array(text).min(1).max(100).optional(), profile_ids: z.array(text).min(1).max(100).optional() }), method: "POST" },
  write("add_directory_person", "add a known Orbit profile to a directory. Confirm the directory and profile.", "POST", `${directory}/people`, { ...dir, profile_id: text }, true),
  write("remove_directory_person", "remove one directory member after confirming its directory-person ID, not its Orbit profile ID.", "DELETE", `${directory}/people/:personId`, person),
  write("remove_directory_people", "remove selected directory members after confirming the exact selection. Save the returned removal source for restoration.", "POST", `${directory}/people/bulk-remove`, { ...dir, person_ids: people }),
  write("restore_directory_people", "restore selected removed members or a recorded removal source, within the backend's restoration window. Confirm the exact selection or source.", "POST", `${directory}/people/restore`, { ...dir, person_ids: people.optional(), removal_source_id: uuid.optional() }),
  write("leave_directory", "remove the authenticated user's own membership after confirmation.", "DELETE", `${directory}/me`, dir),
  read("get_directory_readiness", "check whether a directory is ready to search.", `${directory}/readiness`, dir),
  // These GETs can recover pending CSV processing, so they are not read-only.
  write("list_directory_sources", "list import sources and recover pending CSV processing where needed.", "GET", `${directory}/sources`, { ...dir, limit: page.limit, after_source_id: uuid.optional() }, true),
  write("get_directory_source", "read a source and recover pending CSV processing where needed.", "GET", `${directory}/sources/:sourceId`, source, true),
  write("get_directory_source_status", "check import progress and recover pending CSV processing where needed. Queued or importing is not completion.", "GET", `${directory}/sources/:sourceId/status`, source, true),
  write("update_directory_source_status", "make an explicitly requested administrative source-status correction. Never mark an import completed merely to hide a failure; verify the actual processing state first.", "PATCH", `${directory}/sources/:sourceId/status`, { ...source, status: z.enum(["pending", "importing", "completed", "failed", "canceled"]), error: z.string().max(2000).nullable().optional(), import_metadata: z.record(z.string(), z.unknown()).optional() }),
  write("upload_directory_csv", "import user-supplied CSV text into a directory. Confirm the target and field mapping. Never invent rows. Limited to 512 KiB; use the dashboard for larger files. Preserve idempotency_key on retries.", "POST", `${directory}/sources/csv-upload`, { ...dir, csv_text: z.string().min(1).max(524288), filename: z.string().regex(/^[A-Za-z0-9_.-]+\.csv$/).max(120).default("import.csv"), field_mapping: z.record(z.string(), z.union([text, z.array(text).min(1).max(20)])).optional(), idempotency_key: uuid, label: text.optional(), max_rows: z.number().int().min(1).max(10000).optional() }, true),
  write("infer_directory_csv_mapping", "suggest CSV column mapping from user-supplied headers. Review the mapping before upload; this may call an external model.", "POST", `${directory}/sources/csv-mapping`, { ...dir, headers: z.array(text).min(1).max(200) }, true),
  write("populate_directory_from_entities", "populate a directory from known companies or schools. Confirm IDs, AND/OR criteria and research costs. Never guess entity IDs. Poll source status after acceptance.", "POST", `${directory}/sources/entity-enrichment`, { ...dir, operator: z.enum(["and", "or"]), filters: z.array(z.object({ type: z.enum(["company", "school"]), id: text, name: text.optional() }).strict()).min(1).max(20), async: z.literal(true).default(true) }, true),
  write("remove_directory_source_people", "remove members contributed by a source after confirming the source and impact.", "DELETE", `${directory}/sources/:sourceId/people`, source),
  write("resync_directory_people", "rematerialize selected directory members from existing data, after confirming the selection. This is separate from a research refresh.", "POST", `${directory}/people/refresh`, { ...dir, person_ids: people }),
  write("refresh_directory_profiles", "start a credit-consuming one-time refresh. Confirm selected members or everyone and cost implications; preserve request_id on retries.", "POST", `${directory}/refreshes`, { ...dir, request_id: uuid, selection: z.union([z.object({ all: z.literal(true) }).strict(), z.object({ person_ids: people }).strict()]) }, true),
  read("list_directory_refreshes", "check one-time refresh progress. Report acceptance separately from completion.", `${directory}/refreshes`, { ...dir, limit: page.limit }),
  read("list_directory_grants", "inspect directory access grants before sharing or revoking access.", `${directory}/grants`, dir),
  write("grant_directory_access", "share directory access. Confirm the exact recipient, directory, and permissions; never infer a principal ID.", "POST", `${directory}/grants`, { ...dir, principal_type: z.enum(["organization", "team", "user", "api_key"]), principal_id: uuid, permissions: z.array(z.enum(["search", "upload", "manage"])).min(1).max(3) }),
  write("revoke_directory_access", "revoke a specific access grant after confirming the recipient and permissions affected.", "DELETE", `${directory}/grants/:grantId`, { ...dir, grantId: uuid }),
  read("list_directory_watchers", "list watchers for directory members.", `${directory}/watchers`, dir),
  write("create_directory_watchers", "schedule updates for selected members. Confirm profile IDs, interval, phases, and ongoing organization credit usage.", "POST", `${directory}/watchers`, { ...dir, orbit_ids: z.array(uuid).min(1).max(100), interval_seconds: interval, phases: phases.optional() }, true),
  write("update_directory_watcher", "change a member's watcher. Confirm schedule, phases, enabled state, and credit implications.", "PATCH", `${directory}/watchers/:watcherId`, { ...dir, watcherId: uuid, enabled: z.boolean().optional(), interval_seconds: interval.optional(), phases: phases.nullable().optional() }, true),
  write("delete_directory_watcher", "stop a directory member's watcher after confirming the target.", "DELETE", `${directory}/watchers/:watcherId`, { ...dir, watcherId: uuid }),
  read("list_directory_activity", "read sourced directory-member updates sorted by event date or discovery date.", `${directory}/activity`, { ...dir, ...page, sort: z.enum(["event", "discovered"]).optional() })
];

export function directoryRequest(def: Definition, args: Record<string, unknown>) {
  const used = new Set<string>();
  const path = def.path.replace(/:([A-Za-z]+)/g, (_, key: string) => {
    used.add(key);
    return encodeURIComponent(String(args[key]));
  });
  const data = Object.fromEntries(Object.entries(args).filter(([key, value]) => !used.has(key) && value !== undefined));
  if (def.name === "search_directory") {
    // The directory scope is mandatory and cannot be overridden by tool input.
    delete data.organizationId;
    delete data.directoryId;
    data.searchScope = { type: "directory", directoryId: args.directoryId };
  }
  if (data.selection) {
    Object.assign(data, data.selection);
    delete data.selection;
  }
  if (def.name === "upload_directory_csv") {
    if (Buffer.byteLength(String(data.csv_text), "utf8") > 524288) throw new Error("csv_too_large");
    const form = new FormData();
    form.set("file", new Blob([String(data.csv_text)], { type: "text/csv" }), String(data.filename));
    for (const key of ["field_mapping", "idempotency_key", "label", "max_rows"]) {
      if (data[key] !== undefined) form.set(key, key === "field_mapping" ? JSON.stringify(data[key]) : String(data[key]));
    }
    return { path, options: { method: def.method, form } };
  }
  if (def.method === "GET") {
    const query = new URLSearchParams(Object.entries(data).map(([key, value]) => [key, String(value)]));
    return { path: `${path}${query.size ? `?${query}` : ""}`, options: { method: def.method } };
  }
  return { path, options: { method: def.method, ...(Object.keys(data).length ? { body: data } : {}) } };
}

export function registerDirectoryTools(server: McpServer) {
  for (const def of directoryTools) {
    const permission = def.read ? "directories.read" : "directories.write";
    server.registerTool(def.name, {
      title: def.name.replaceAll("_", " "),
      description: `Use this when the user wants to ${def.description} Requires an Orbit OAuth connection; Orbit enforces organization and directory permissions.`,
      inputSchema: def.input,
      outputSchema: { data: z.record(z.string(), z.unknown()) },
      annotations: { readOnlyHint: def.read, destructiveHint: !def.read, openWorldHint: def.external ?? false },
      _meta: { securitySchemes: [{ type: "oauth2", scopes: ["search.read", permission] }] }
    }, async (args: Record<string, unknown>): Promise<CallToolResult> => {
      // Read request-local authorization on EVERY call, never from MCP initialization.
      const auth = directoryContext.getStore();
      if (!auth || auth.expiresAt <= Date.now() || !auth.scopes.includes(permission)) {
        return { isError: true, content: [{ type: "text", text: `Reconnect Orbit at https://api.orbitsearch.com/mcp with ${permission}. Directory management requires a signed-in user, not an API key alone.` }],
          _meta: { "mcp/www_authenticate": [`Bearer resource_metadata="https://api.orbitsearch.com/.well-known/oauth-protected-resource/mcp", error="insufficient_scope", scope="search.read ${permission}"`] } };
      }
      try {
        if (def.name === "update_directory" && !Object.keys(fields).some(key => args[key] !== undefined)) throw new Error("empty_update");
        if (def.name === "update_directory_watcher" && !["enabled", "interval_seconds", "phases"].some(key => args[key] !== undefined)) throw new Error("empty_update");
        if (def.name === "check_directory_membership" && !["emails", "linkedin_urls", "profile_ids"].some(key => args[key] !== undefined)) throw new Error("missing_identity");
        if (def.name === "restore_directory_people" && !args.person_ids && !args.removal_source_id) throw new Error("missing_restoration_target");
        const { path, options } = directoryRequest(def, args);
        const result = await requestDirectory(path, options, auth);
        if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("invalid_response");
        const envelope = result as Record<string, unknown>;
        if (envelope.status === "failed" || envelope.status === "error") throw new Error("upstream_failure");
        return { structuredContent: { data: envelope }, content: [{ type: "text", text: JSON.stringify(envelope) }] };
      } catch (error) {
        const status = error instanceof DirectoryApiError ? error.status : undefined;
        return { isError: true, content: [{ type: "text", text: status === 401 || status === 403
          ? "Orbit denied this directory operation. Check your organization permissions; reconnect if the login expired."
          : `Orbit directory operation failed${status ? ` (HTTP ${status})` : ""}. Check current state before retrying a change; it may already have been accepted.` }] };
      }
    });
  }
}
