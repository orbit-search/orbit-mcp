/** Security upgrades must retain the published MCP contract and reject invalid inputs before backend work. */
import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import express from "express";
import { randomUUID } from "node:crypto";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { createOrbitServer } from "../build/server.js";

const baseline = JSON.parse(fs.readFileSync(new URL("./fixtures/security-contract.json", import.meta.url), "utf8"));

test("security upgrade preserves tools/list, calls, errors and unknown keys", async () => {
  const client = new Client({ name: "security-contract", version: "1" });
  const app = express();
  app.use(express.json());
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: () => randomUUID(), enableJsonResponse: true });
  app.all("/mcp", (req, res) => transport.handleRequest(req, res, req.body));
  const listener = app.listen(0, "127.0.0.1");
  await new Promise(resolve => listener.once("listening", resolve));
  const localOrigin = `http://127.0.0.1:${listener.address().port}`;
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    if (String(url).startsWith(localOrigin + "/")) return originalFetch(url, options);
    calls.push({ url: String(url), body: options?.body });
    return new Response(JSON.stringify({ profile_id: "p1", generation_level: 3, profile: { name: "Ada" }, status: "completed", results: [{ profile_id: "p1", status: "ready", generation_level: 3, profile: { name: "Ada" } }], payload: {
      account_type: "user", usage_scope: "current_api_key", balance_scope: "billing_account",
      period_start: "2026-01-01", period_end: "2026-02-01", credits_used: 0, request_count: 0,
      available_credits: 100, reserved_credits: 0,
    } }), { status: 200, headers: { "content-type": "application/json" } });
  };
  const server = createOrbitServer("sk_orb_test");
  try {
    await server.connect(transport);
    await client.connect(new StreamableHTTPClientTransport(new URL(localOrigin + "/mcp")));
    assert.deepEqual(JSON.parse(JSON.stringify(await client.listTools())), baseline.tools);
    for (const item of baseline.results) {
      const count = calls.length;
      assert.deepEqual(JSON.parse(JSON.stringify(await client.callTool({ name: item.name, arguments: item.args }))), item.result);
      // JSON normalization omits undefined optional fetch properties, as the captured receipt does.
      assert.deepEqual(JSON.parse(JSON.stringify(calls.slice(count))), item.backendCalls);
    }
  } finally {
    globalThis.fetch = originalFetch;
    await Promise.all([client.close(), server.close()]);
    listener.closeAllConnections();
    await new Promise(resolve => listener.close(resolve));
  }
});
