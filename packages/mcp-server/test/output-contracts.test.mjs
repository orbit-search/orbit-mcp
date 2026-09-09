/** Check the advertised schemas against MCP responses without paid API calls. */
import assert from "node:assert/strict";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createOrbitServer } from "../build/server.js";
import { searchOutputSchema, profileOutputSchema, enrichOutputSchema } from "../build/output-schemas.js";

const profile = { id: "person-1", sections: { bio: { bio: "Public context" } }, sources: [{ url: "https://example.org/evidence" }], future_field: { kept: true } };
const search = {
  search_id: "search-1", request_id: "request-1", status: "completed",
  candidate_discovery: false, profile_depth: "partial", include_profile: true,
  profile_upgrades_completed: true,
  results: [{ profile_id: "person-1", status: "ready", generation_level: 2, sources: ["search"], profile, confidence: 0.9 }],
  created_at: "2026-09-09T00:00:00Z", updated_at: "2026-09-09T00:00:00Z",
  links: { status: "/v3/search/search-1" }, future_field: "preserved",
};
const enrichment = {
  profile_id: "person-1", request_id: "enrich-1", status: "completed", operation: "full",
  include_profile: true, generation_level: 3, profile, links: { profile: "/v3/enrich/person-1" },
};

async function withClient(run, responseBody, responseStatus = 200) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify(responseBody), { status: responseStatus });
  const server = createOrbitServer("contract-test-only");
  const client = new Client({ name: "output-contract-test", version: "1.0.0" });
  const [ct, st] = InMemoryTransport.createLinkedPair();
  try {
    await Promise.all([server.connect(st), client.connect(ct)]);
    return await run(client);
  } finally {
    globalThis.fetch = originalFetch;
    await Promise.all([client.close(), server.close()]);
  }
}

test("all three tools advertise substantive output schemas and accurate annotations", async () => {
  await withClient(async client => {
    const { tools } = await client.listTools();
    assert.equal(client.getServerVersion().version, "2.1.0");
    assert.deepEqual(tools.map(t => t.name), ["search_people", "get_profile", "enrich_profile"]);
    for (const tool of tools) {
      assert.equal(tool.outputSchema.type, "object");
      assert(tool.outputSchema.required.length >= 3);
      assert.equal(tool.outputSchema.additionalProperties, true);
    }
    assert.deepEqual(tools[0].annotations, { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true });
    assert.deepEqual(tools[1].annotations, { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true });
    assert.deepEqual(tools[2].annotations, { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: true });
  });
});

for (const [name, args, body, schema] of [
  ["search_people", { query: "Test", request_id: "request-1" }, search, searchOutputSchema],
  ["search_people", { query: "No matches" }, { ...search, results: [] }, searchOutputSchema],
  ["get_profile", { profile_id: "person-1" }, { profile_id: "person-1", generation_level: null, profile }, profileOutputSchema],
  ["enrich_profile", { profile_id: "person-1", operation: "full" }, enrichment, enrichOutputSchema],
]) {
  test(`${name} returns schema-valid structured content and unchanged text JSON (${body.results?.length ?? "profile"})`, async () => {
    await withClient(async client => {
      const result = await client.callTool({ name, arguments: args });
      assert.notEqual(result.isError, true);
      assert.deepEqual(result.structuredContent, body);
      assert.deepEqual(JSON.parse(result.content[0].text), body);
      assert.deepEqual(schema.parse(result.structuredContent), body);
    }, body);
  });
}

for (const status of ["completed_with_errors", "failed"]) {
  test(`search ${status} preserves structured failure details and isError`, async () => {
    const body = { ...search, status, results: [{ profile_id: "person-1", status: "failed", generation_level: null, failure: { code: "failed", message: "Failed", retryable: true, reason: "upstream" } }] };
    await withClient(async client => {
      const result = await client.callTool({ name: "search_people", arguments: { query: "Test" } });
      assert.equal(result.isError, true);
      assert.deepEqual(searchOutputSchema.parse(result.structuredContent), body);
      assert.deepEqual(JSON.parse(result.content[0].text), body);
    }, body);
  });
}

test("failed enrichment preserves the terminal failure envelope", async () => {
  const body = { ...enrichment, status: "failed", failure: { code: "failed", message: "Failed", retryable: true } };
  await withClient(async client => {
    const result = await client.callTool({ name: "enrich_profile", arguments: { profile_id: "person-1", operation: "full" } });
    assert.equal(result.isError, true);
    assert.deepEqual(enrichOutputSchema.parse(result.structuredContent), body);
  }, body);
});

test("HTTP auth errors stay tool errors, not successful profile envelopes", async () => {
  await withClient(async client => {
    const result = await client.callTool({ name: "get_profile", arguments: { profile_id: "person-1" } });
    assert.equal(result.isError, true);
    assert.deepEqual(result.structuredContent, { error: "Orbit API request failed with HTTP 403" });
    assert.deepEqual(JSON.parse(result.content[0].text), result.structuredContent);
  }, { error: "denied" }, 403);
});

test("malformed successful output is rejected by SDK output validation", async () => {
  await withClient(async client => {
    const result = await client.callTool({ name: "get_profile", arguments: { profile_id: "person-1" } });
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, /Output validation error/);
  }, { profile_id: "person-1", generation_level: 2 });
});
