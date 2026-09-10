/** Response contracts track backend families while retaining partial failures and evidence. */
import assert from "node:assert/strict";
import test from "node:test";
import { z } from "zod";
import { directoryTools } from "../build/directory-tools.js";
import { directoryOutputSchema, directoryPayloadSchemas } from "../build/directory-output-schemas.js";

test("every directory tool has a response-family schema", () => {
  assert.deepEqual(Object.keys(directoryPayloadSchemas).sort(), directoryTools.map(t => t.name).sort());
  for (const tool of directoryTools) {
    const schema = directoryPayloadSchemas[tool.name];
    const families = schema instanceof z.ZodUnion ? schema.options : [schema];
    assert.ok(families.some(family => family instanceof z.ZodObject && Object.keys(family.shape).length > 0), tool.name);
  }
  assert.throws(() => directoryOutputSchema("unknown_tool"), /Missing directory response schema/);
});

test("directory response families preserve known and extended data but reject mistyped fields", () => {
  for (const [name, payload, bad] of [
    ["list_directories", { directories: [{ id: "directory", name: "Contacts", metadata: { future_setting: true } }] }, { directories: "wrong" }],
    ["get_directory", { directory: { id: "directory", visibility: "private", description: null } }, { directory: { is_archived: "wrong" } }],
    ["list_directory_people", { people: [{ person_id: "row", profile_id: null, future_profile: { facts: ["retained"] } }], pagination: { total: 3, has_more: true } }, { pagination: { total: "wrong" } }],
    ["count_directory_people", { count: 3 }, { count: "wrong" }],
    ["list_pending_directory_people", { in_progress: 3, people: [] }, { in_progress: true }],
    ["get_directory_source_status", { source_status: "failed", source: { status: "failed", error: "Import failed", validation_errors: [{ row: 2, reasons: ["Invalid identity"] }] }, backfills: [{ failed: 1, completed: 2 }] }, { backfills: {} }],
    ["get_directory_readiness", { readiness: { canSearch: false, rowCounts: { acceptedRows: 2 }, latestError: null } }, { readiness: { canSearch: "wrong" } }],
    ["list_directory_grants", { grants: [{ id: "grant", permission: "search", principal_type: "user" }] }, { grants: {} }],
    ["refresh_directory_profiles", { queued: true, refresh: { status: "queued", runs: { total: 4 }, error: null } }, { refresh: { runs: { total: "wrong" } } }],
    ["list_directory_refreshes", { refreshes: [{ status: "failed", error: "Partial failure", runs: { failed: 1, completed: 2 } }] }, { refreshes: "wrong" }],
    ["update_directory_watcher", { enabled: false, phases: null, interval_seconds: 3600 }, { interval_seconds: "wrong" }],
    ["delete_directory_watcher", { deleted: true }, { deleted: "wrong" }],
    ["list_directory_activity", { events: [{ id: "event", date: null, changes: [{ summary: "New role", source_link: "https://example.com" }] }], total: 1 }, { events: {} }],
    ["check_directory_slug", { slug: null, available: false }, { available: "wrong" }],
    ["search_directory", { users: [{ id: "profile", facts: ["preserved"] }], searchSummary: "Matches" }, { users: "wrong" }],
    ["search_directory", [], { users: "wrong" }]
  ]) {
    const schema = z.object(directoryOutputSchema(name));
    const value = { data: { status: "success", payload, future_envelope_field: "preserved" } };
    assert.deepEqual(schema.parse(value), value, name);
    assert.equal(schema.safeParse({ data: { status: "success", payload: bad } }).success, false, name);
    assert.equal(schema.safeParse({ data: { status: "failed", error: { code: "failure", message: "Diagnostic" } } }).success, true, name);
    assert.equal(schema.safeParse({ data: {} }).success, true, name);
  }
});
