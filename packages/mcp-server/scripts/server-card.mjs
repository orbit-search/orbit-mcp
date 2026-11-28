/** Print discovery metadata from the built MCP contract; never calls Orbit APIs. */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createOrbitServer } from "../build/server.js";

const server = createOrbitServer("metadata-generation-only-not-a-valid-key");
const client = new Client({ name: "orbit-server-card", version: "1.0.0" });
const [ct, st] = InMemoryTransport.createLinkedPair();
try {
  await Promise.all([server.connect(st), client.connect(ct)]);
  const { tools } = await client.listTools();
  const card = {
    // Title and description come from the server's own initialize response, so the card matches what clients show.
    serverInfo: { ...client.getServerVersion() },
    authentication: { required: true, schemes: ["bearer"] },
    tools, resources: [], prompts: [],
  };
  console.log(JSON.stringify(card, null, 2));
} finally {
  await Promise.all([client.close(), server.close()]);
}
