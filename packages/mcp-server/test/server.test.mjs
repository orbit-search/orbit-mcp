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
