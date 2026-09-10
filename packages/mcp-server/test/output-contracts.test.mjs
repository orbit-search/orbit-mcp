/** Check the advertised schemas against MCP responses without paid API calls. */
import assert from "node:assert/strict";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createOrbitServer } from "../build/server.js";
import { searchOutputSchema, profileOutputSchema, enrichOutputSchema } from "../build/output-schemas.js";

const profile = { id: "person-1", sections: { bio: { bio: "Public context" } }, sources: [{ url: "https://example.org/evidence" }], future_field: { kept: true } };
const billing = { id: "operation-receipt", pricingVersion: "server-owned-version", reservedCredits: 20, consumedCredits: 7, releasedCredits: 13, heldCredits: 0, status: "settled" };
const search = {
  billing,
  search_id: "search-1", request_id: "request-1", status: "completed",
  candidate_discovery: false, profile_depth: "partial", include_profile: true,
  profile_upgrades_completed: true,
  results: [{ profile_id: "person-1", status: "ready", generation_level: 3, sources: ["search"], profile_projection: "summary", profile: { id: "person-1", displayName: "Ada", sections: { socials: { items: [] } } }, confidence: 0.9 }],
  created_at: "2026-09-09T00:00:00Z", updated_at: "2026-09-09T00:00:00Z",
  links: { status: "/v3/search/search-1" }, future_field: "preserved",
};
const enrichment = {
  billing,
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
    // Real clients discover first; this enables client-side output validation.
    await client.listTools();
    return await run(client);
  } finally {
    globalThis.fetch = originalFetch;
    await Promise.all([client.close(), server.close()]);
  }
}

test("all tools advertise output schemas and accurate annotations", async () => {
  await withClient(async client => {
    const discovery = await client.listTools();
    const tools = discovery.tools.filter(tool => tool.name !== "get_credit_usage");
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
    const usage = discovery.tools.find(tool => tool.name === "get_credit_usage");
    assert.deepEqual(usage.annotations, { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false });
    assert.deepEqual(usage.inputSchema.properties, {});
    assert.equal(discovery.tools.length, 4);
  });
});

for (const [name, args, body, schema] of [
  ["search_people", { query: "Test", request_id: "request-1" }, search, searchOutputSchema],
  ["search_people", { query: "No matches" }, { ...search, results: [] }, searchOutputSchema],
  ["get_profile", { profile_id: "person-1" }, { profile_id: "person-1", generation_level: null, profile, billing }, profileOutputSchema],
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
    assert.equal(result.structuredContent, undefined);
    const error = JSON.parse(result.content[0].text).error;
    assert.match(error, /HTTP 403/);
    assert.match(error, /profile:read/);
    assert.match(error, /developer.orbitsearch.com\/dashboard\/keys/);
  }, { error: "denied" }, 403);
});

test("HTTP 402 remains an actionable MCP tool error after schema discovery", async () => {
  await withClient(async client => {
    const result = await client.callTool({ name: "get_profile", arguments: { profile_id: "person-1" } });
    assert.equal(result.isError, true);
    assert.equal(result.structuredContent, undefined);
    const error = JSON.parse(result.content[0].text).error;
    assert.match(error, /HTTP 402/);
    assert.match(error, /dashboard\/billing/);
    assert.doesNotMatch(error, /sensitive detail/);
  }, { error: { code: "insufficient_credits", message: "sensitive detail" } }, 402);
});

test("missing operation billing remains unknown rather than a fabricated zero receipt", async () => {
  const body = { profile_id: "person-1", generation_level: 3, profile };
  await withClient(async client => {
    const result = await client.callTool({ name: "get_profile", arguments: { profile_id: "person-1" } });
    assert.notEqual(result.isError, true);
    assert.deepEqual(result.structuredContent, body);
    assert.equal(Object.hasOwn(result.structuredContent, "billing"), false);
  }, body);
});

test("invalid keys surface reconnect instructions after tool discovery", async () => {
  await withClient(async client => {
    const result = await client.callTool({ name: "get_profile", arguments: { profile_id: "person-1" } });
    assert.equal(result.isError, true);
    assert.equal(result.structuredContent, undefined);
    assert.match(JSON.parse(result.content[0].text).error, /rejected this API key \(HTTP 403\)/);
    assert.match(JSON.parse(result.content[0].text).error, /reconnect/);
  }, { status: "failure", error: { code: "invalid_api_key", message: "sensitive detail" } }, 403);
});

test("malformed successful output is rejected by SDK output validation", async () => {
  await withClient(async client => {
    const result = await client.callTool({ name: "get_profile", arguments: { profile_id: "person-1" } });
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, /Output validation error/);
  }, { profile_id: "person-1", generation_level: 2 });
});
