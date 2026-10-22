import assert from "node:assert/strict";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createOrbitServer } from "../build/server.js";

test("search_people marks completed_with_errors as an MCP error", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        search_id: "search-1",
        request_id: "request-1",
        status: "completed_with_errors",
        results: [
          {
            profile_id: "profile-1",
            status: "failed",
            generation_level: null,
            failure: { code: "enrichment_failed", message: "Profile enrichment failed", retryable: true },
          },
        ],
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );

  const server = createOrbitServer("sk_orb_test");
  const client = new Client({ name: "orbit-server-test", version: "1.0.0" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();

  try {
    await Promise.all([server.connect(serverTransport), client.connect(clientTransport)]);
    const result = await client.callTool({ name: "search_people", arguments: { query: "Ada Lovelace" } });
    assert.equal(result.isError, true);
  } finally {
    globalThis.fetch = originalFetch;
    await Promise.all([client.close(), server.close()]);
  }
});

// Exercise SDK validation before a large collection reaches any backend tool handler.
test("bounds tool argument collections before downstream work", async () => {
  const server = createOrbitServer("sk_orb_test");
  const client = new Client({ name: "bounded-input-test", version: "1.0.0" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const originalFetch = globalThis.fetch;
  let backendCalls = 0;
  globalThis.fetch = async () => { backendCalls++; throw new Error("must not call backend"); };
  try {
    await Promise.all([server.connect(serverTransport), client.connect(clientTransport)]);
    const result = await client.callTool({ name: "search_people", arguments: { query: "Ada Lovelace", oversized: Array(10_001).fill(true) } });
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, /maximum of 10000 elements/);
    assert.equal(backendCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
    await Promise.all([client.close(), server.close()]);
  }
});
