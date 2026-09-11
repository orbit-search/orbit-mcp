/** Keep both constituent operation receipts visible through the profile MCP tool. */
import assert from "node:assert/strict";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createProfileServer } from "../build/server.js";

const searchBilling = { id: "search-operation", pricingVersion: "server-owned-version", reservedCredits: 20, consumedCredits: 7, releasedCredits: 13, heldCredits: 0, status: "settled" };
const readBilling = { id: "read-operation", pricingVersion: "server-owned-version", reservedCredits: 2, consumedCredits: 2, releasedCredits: 0, heldCredits: 0, status: "settled" };
const usage = { status: "success", payload: { account_type: "user", usage_scope: "current_api_key", balance_scope: "billing_account", period_start: "2026-09-01T00:00:00Z", period_end: "2026-10-01T00:00:00Z", credits_used: -2, request_count: 4, available_credits: 91, reserved_credits: 9 } };

async function withTool(responses, run, key = "test-key") {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    const next = responses.shift();
    assert(next, "unexpected HTTP request");
    return new Response(JSON.stringify(next.body), { status: next.status ?? 200 });
  };
  const server = createProfileServer(key);
  const client = new Client({ name: "profile-billing-test", version: "1.0.0" });
  const [ct, st] = InMemoryTransport.createLinkedPair();
  try {
    await Promise.all([server.connect(st), client.connect(ct)]);
    await client.listTools();
    await run(client, calls);
  } finally {
    globalThis.fetch = originalFetch;
    await Promise.all([client.close(), server.close()]);
  }
}

test("profile tool preserves separate Search and read receipts without fetching usage", async () => {
  const read = { profile_id: "profile-1", generation_level: 3, profile: { name: "Ada" }, billing: readBilling };
  await withTool([
    { body: { search_id: "search-1", status: "completed", billing: searchBilling, results: [{ profile_id: "profile-1", status: "ready", generation_level: 3 }] } },
    { body: read },
  ], async (client, calls) => {
    const result = await client.callTool({ name: "get_profile", arguments: { query: "Ada Lovelace", request_id: "stable-search" } });
    assert.notEqual(result.isError, true);
    assert.deepEqual(result.structuredContent, { ...read, search_billing: searchBilling });
    assert.deepEqual(JSON.parse(result.content[0].text), result.structuredContent);
    assert.equal(calls.length, 2);
    assert(calls.every(call => !call.url.includes("/credits/usage")));
    assert.match(calls[1].init.headers["Idempotency-Key"], /^[0-9a-f-]{36}$/);
  });
});

test("an embedded Search profile returns with its Search receipt and no profile read", async () => {
  const profile = { displayName: "Ada", sections: { bio: { bio: "Public context" } }, emails: ["ada@example.test"] };
  await withTool([
    { body: { search_id: "search-1", status: "completed", billing: searchBilling, results: [{ profile_id: "profile-1", status: "ready", generation_level: 3, profile }] } },
  ], async (client, calls) => {
    const result = await client.callTool({ name: "get_profile", arguments: { query: "Ada Lovelace" } });
    assert.notEqual(result.isError, true);
    assert.deepEqual(result.structuredContent, { profile_id: "profile-1", generation_level: 3, profile, search_billing: searchBilling });
    assert.deepEqual(JSON.parse(result.content[0].text), result.structuredContent);
    assert.equal(calls.length, 1);
  });
});

test("no-match profile resolution preserves search billing and does not read a profile", async () => {
  await withTool([{ body: { search_id: "search-1", status: "completed", billing: searchBilling, results: [] } }], async (client, calls) => {
    const result = await client.callTool({ name: "get_profile", arguments: { query: "No matches" } });
    assert.notEqual(result.isError, true);
    assert.deepEqual(result.structuredContent, { message: "No matching person found.", search_billing: searchBilling });
    assert.equal(calls.length, 1);
  });
});

test("missing Search and read receipts remain absent rather than zero", async () => {
  const read = { profile_id: "profile-1", generation_level: 3, profile: { name: "Ada" } };
  await withTool([
    { body: { search_id: "search-1", status: "completed", results: [{ profile_id: "profile-1", status: "ready", generation_level: 3 }] } },
    { body: read },
  ], async (client, calls) => {
    const result = await client.callTool({ name: "get_profile", arguments: { query: "Ada" } });
    assert.deepEqual(result.structuredContent, read);
    assert.equal(Object.hasOwn(result.structuredContent, "billing"), false);
    assert.equal(Object.hasOwn(result.structuredContent, "search_billing"), false);
    assert.equal(calls.length, 2);
  });
});

test("read 402 preserves prior search billing as an error, never an empty result", async () => {
  await withTool([
    { body: { search_id: "search-1", status: "completed", billing: searchBilling, results: [{ profile_id: "profile-1", status: "ready", generation_level: 3 }] } },
    { status: 402, body: { error: { code: "insufficient_credits", message: "private" } } },
  ], async (client, calls) => {
    const result = await client.callTool({ name: "get_profile", arguments: { query: "Ada" } });
    assert.equal(result.isError, true);
    const body = JSON.parse(result.content[0].text);
    assert.deepEqual(body.search_billing, searchBilling);
    assert.match(body.error, /HTTP 402/);
    assert.doesNotMatch(body.error, /private/);
    assert.equal(calls.length, 2);
  });
});

test("credit usage uses only the connected key and strips account identifiers", async () => {
  for (const key of ["user-test-key", "organization-test-key"]) {
    const expected = { ...usage, payload: { ...usage.payload, account_type: key === "user-test-key" ? "user" : "organization" } };
    await withTool([{ body: { ...expected, payload: { ...expected.payload, userId: "private", apiKeyId: "private" } } }], async (client, calls) => {
      const result = await client.callTool({ name: "get_credit_usage", arguments: {} });
      assert.deepEqual(result.structuredContent, expected);
      assert.deepEqual(JSON.parse(result.content[0].text), expected);
      assert.equal(calls.length, 1);
      assert.equal(new URL(calls[0].url).pathname, "/v3/credits/usage");
      assert.equal(new URL(calls[0].url).search, "");
      assert.equal(calls[0].init.body, undefined);
      assert.equal(calls[0].init.headers.Authorization, `Bearer ${key}`);
    }, key);
  }
});

test("usage 402/404 are unretried MCP errors", async () => {
  for (const status of [402, 404]) {
    await withTool([{ status, body: { error: { message: "private" } } }], async (client, calls) => {
      const result = await client.callTool({ name: "get_credit_usage", arguments: {} });
      assert.equal(result.isError, true);
      assert.match(result.content[0].text, new RegExp(`HTTP ${status}`));
      assert.equal(calls.length, 1);
    });
  }
});
