#!/usr/bin/env node

import { randomUUID } from "node:crypto";
import express from "express";
import cors from "cors";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { createOrbitServer } from "./server.js";

const app = express();
app.use(cors());
app.use(express.json());

const BYPASS_KEY = process.env.MCP_BYPASS_KEY;

const transports = new Map<string, StreamableHTTPServerTransport>();
const sessionApiKeys = new Map<string, string>();

function extractApiKey(req: express.Request): string | null {
  const auth = req.headers.authorization;
  if (auth?.startsWith("Bearer ")) {
    return auth.slice(7);
  }
  return null;
}

function requireApiKey(
  req: express.Request,
  res: express.Response,
  next: express.NextFunction,
): void {
  if (BYPASS_KEY === "*") {
    next();
    return;
  }

  const mcpSessionId = req.headers["mcp-session-id"] as string | undefined;
  if (mcpSessionId && sessionApiKeys.has(mcpSessionId)) {
    next();
    return;
  }

  const key = extractApiKey(req);

  if (BYPASS_KEY && key === BYPASS_KEY) {
    next();
    return;
  }

  if (!key) {
    res.status(401).json({
      jsonrpc: "2.0",
      error: { code: -32600, message: "Unauthorized: missing Authorization: Bearer <api_key> header" },
      id: null,
    });
    return;
  }

  next();
}

app.use("/mcp", requireApiKey);

app.post("/mcp", async (req, res) => {
  const sessionId = req.headers["mcp-session-id"] as string | undefined;

  if (sessionId && transports.has(sessionId)) {
    const transport = transports.get(sessionId)!;
    await transport.handleRequest(req, res, req.body);
    return;
  }

  if (!sessionId && isInitializeRequest(req.body)) {
    const apiKey = extractApiKey(req) ?? BYPASS_KEY ?? "";
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => randomUUID(),
      onsessioninitialized: (sid) => {
        transports.set(sid, transport);
        sessionApiKeys.set(sid, apiKey);
      },
    });
    transport.onclose = () => {
      if (transport.sessionId) {
        transports.delete(transport.sessionId);
        sessionApiKeys.delete(transport.sessionId);
      }
    };
    const server = createOrbitServer(apiKey);
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
    return;
  }

  res.status(400).json({
    jsonrpc: "2.0",
    error: { code: -32600, message: "Bad Request: missing session ID or not an initialize request" },
    id: null,
  });
});

app.get("/mcp", async (req, res) => {
  const sessionId = req.headers["mcp-session-id"] as string | undefined;
  if (!sessionId || !transports.has(sessionId)) {
    res.status(400).send("Invalid or missing session ID");
    return;
  }
  await transports.get(sessionId)!.handleRequest(req, res);
});

app.delete("/mcp", async (req, res) => {
  const sessionId = req.headers["mcp-session-id"] as string | undefined;
  if (!sessionId || !transports.has(sessionId)) {
    res.status(400).send("Invalid or missing session ID");
    return;
  }
  await transports.get(sessionId)!.handleRequest(req, res);
});

app.get("/health", (_req, res) => {
  res.json({ status: "ok" });
});

const PORT = parseInt(process.env.PORT ?? "3000", 10);
app.listen(PORT, () => {
  console.error(`Orbit MCP HTTP server running on http://localhost:${PORT}/mcp`);
});
