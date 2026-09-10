#!/usr/bin/env node
/** Production entrypoint with the pinned public OAuth gateway verifier. */
import { createHttpApp } from "./http-app.js";
const app = createHttpApp();
const PORT = parseInt(process.env.PORT ?? "3000", 10);
const listener = app.listen(PORT, () => {
  const address = listener.address();
  const port = address && typeof address === "object" ? address.port : PORT;
  console.error(`Orbit MCP HTTP server running on http://localhost:${port}/mcp`);
});
