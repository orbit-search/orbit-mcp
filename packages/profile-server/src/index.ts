#!/usr/bin/env node

import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { createProfileServer } from "./server.js";

async function main(): Promise<void> {
  const server = createProfileServer();
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("Orbit Profile MCP server running on stdio");
}

main().catch((err: unknown) => {
  console.error("Fatal error starting Orbit Profile MCP server:", err);
  process.exit(1);
});
