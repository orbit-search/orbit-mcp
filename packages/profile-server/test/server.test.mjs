import assert from "node:assert/strict";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createProfileServer } from "../build/server.js";

test("profile server exposes profile resolution and read-only credit usage", async () => {
  const server = createProfileServer("sk_orb_test");
  const client = new Client({ name: "profile-server-test", version: "1.0.0" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();

  await Promise.all([server.connect(serverTransport), client.connect(clientTransport)]);

  try {
    const { tools } = await client.listTools();
    assert.deepEqual(tools.map(({ name }) => name), ["get_credit_usage", "get_profile"]);
    assert.deepEqual(tools[0].annotations, { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false });
    assert.deepEqual(tools[0].inputSchema.properties, {});
  } finally {
    await Promise.all([client.close(), server.close()]);
  }
});
