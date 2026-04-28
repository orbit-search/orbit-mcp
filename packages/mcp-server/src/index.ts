#!/usr/bin/env node

import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { createOrbitServer } from "./server.js";

async function main(): Promise<void> {
  const server = createOrbitServer();
  const transport = new StdioServerTransport();
  await server.connect(transport);
  // stdout is the MCP JSON-RPC transport — all logs MUST go to stderr
  console.error("Orbit MCP server running on stdio");
}

main().catch((err: unknown) => {
  console.error("Fatal error starting Orbit MCP server:", err);
  process.exit(1);
});
