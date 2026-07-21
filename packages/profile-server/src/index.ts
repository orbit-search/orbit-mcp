#!/usr/bin/env node

import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { createProfileServer } from "./server.js";

async function main(): Promise<void> {
  const apiKey = process.env.ORBIT_DEVELOPER_API_KEY?.trim();
  if (!apiKey) throw new Error("ORBIT_DEVELOPER_API_KEY is required");
  const server = createProfileServer(apiKey);
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("Orbit Profile MCP server running on stdio");
}

main().catch((err: unknown) => {
  console.error("Fatal error starting Orbit Profile MCP server:", err);
  process.exit(1);
});
