#!/usr/bin/env node

import { randomUUID } from "node:crypto";
import express from "express";
import cors from "cors";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { createOrbitServer } from "./server.js";
import { getCredits } from "./billing.js";

const app = express();
app.use(cors());
app.use(express.json());

const transports = new Map<string, StreamableHTTPServerTransport>();
const sessionApiKeys = new Map<string, string>();

async function requireApiKey(
  req: express.Request,
  res: express.Response,
  next: express.NextFunction,
): Promise<void> {
  const mcpSessionId = req.headers["mcp-session-id"] as string | undefined;
  if (mcpSessionId && sessionApiKeys.has(mcpSessionId)) {
    next();
    return;
  }

  const key = req.headers["x-api-key"];
  const bypassKey = process.env.MCP_BYPASS_KEY;
  if (bypassKey === "*" || (typeof key === "string" && bypassKey && key === bypassKey)) {
    next();
    return;
  }

  if (typeof key !== "string" || key.length === 0) {
    res.status(401).json({
      jsonrpc: "2.0",
      error: { code: -32600, message: "Unauthorized: missing x-api-key header" },
      id: null,
    });
    return;
  }

  try {
    const { valid, credits } = await getCredits(key);

    if (!valid) {
      res.status(401).json({
        jsonrpc: "2.0",
        error: { code: -32600, message: "Unauthorized: invalid API key" },
        id: null,
      });
      return;
    }

    if (credits <= 0) {
      res.status(403).json({
        jsonrpc: "2.0",
        error: { code: -32600, message: "Forbidden: insufficient credits" },
        id: null,
      });
      return;
    }

    next();
  } catch {
    res.status(502).json({
      jsonrpc: "2.0",
      error: { code: -32600, message: "Billing service unavailable" },
      id: null,
    });
  }
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
    const apiKey = req.headers["x-api-key"] as string;
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
