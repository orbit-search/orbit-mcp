/** Exercise authenticated credit reads through a real MCP tool call, without live API work. */
import assert from "node:assert/strict";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createOrbitServer } from "../build/server.js";

const usage = {
  status: "success",
  payload: {
    account_type: "user",
    usage_scope: "current_api_key", balance_scope: "billing_account",
    period_start: "2026-09-01T00:00:00.000Z", period_end: "2026-10-01T00:00:00.000Z",
    credits_used: -2, request_count: 4, available_credits: 91, reserved_credits: 9,
  },
};

async function withTool(key, response, status, run) {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify(response), { status });
  };
  const server = createOrbitServer(key);
  const client = new Client({ name: "credit-usage-test", version: "1.0.0" });
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

test("credit usage is caller-key scoped, read only, and strips unexpected account identifiers", async () => {
  for (const key of ["test-user-key", "test-organization-key"]) {
    const expected = { ...usage, payload: { ...usage.payload, account_type: key === "test-user-key" ? "user" : "organization" } };
    await withTool(key, { ...expected, userId: "private", payload: { ...expected.payload, subjectId: "private", apiKey: "private" } }, 200, async (client, calls) => {
      const result = await client.callTool({ name: "get_credit_usage", arguments: {} });
      assert.notEqual(result.isError, true);
      assert.deepEqual(result.structuredContent, expected);
      assert.deepEqual(JSON.parse(result.content[0].text), expected);
      assert.equal(calls.length, 1);
      assert.equal(new URL(calls[0].url).pathname, "/v3/credits/usage");
      assert.equal(new URL(calls[0].url).search, "");
      assert.equal(calls[0].init.headers.Authorization, `Bearer ${key}`);
      assert.equal(calls[0].init.body, undefined);
      assert.equal(calls[0].init.headers["Idempotency-Key"], undefined);
      assert.doesNotMatch(result.content[0].text, /private|test-user-key|test-organization-key/);
    });
  }
});

test("usage authorization and billing failures remain tool errors and are not retried", async () => {
  for (const status of [401, 402, 403, 404]) {
    await withTool("test-key", { error: { code: "unavailable", message: "private" } }, status, async (client, calls) => {
      const result = await client.callTool({ name: "get_credit_usage", arguments: {} });
      assert.equal(result.isError, true);
      assert.equal(result.structuredContent, undefined);
      assert.match(result.content[0].text, new RegExp(`HTTP ${status}`));
      assert.doesNotMatch(result.content[0].text, /private/);
      assert.equal(calls.length, 1);
    });
  }
});
