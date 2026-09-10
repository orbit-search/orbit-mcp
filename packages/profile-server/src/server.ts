import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { ProfileOrbitClient } from "./orbit-api.js";

function toolResult(value: unknown, isError = false) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(value, null, 2) }],
    ...(isError ? { isError: true } : {}),
  };
}

function toolError(error: unknown) {
  return toolResult({ error: error instanceof Error ? error.message : String(error) }, true);
}

export function createProfileServer(apiKey: string): McpServer {
  const client = new ProfileOrbitClient({ apiKey });
  const server = new McpServer({ name: "orbit-profile-mcp", version: "2.0.0" });

  server.tool(
    "get_profile",
    "Resolve a person from a name, email, phone number, or other plain-English identity query using Orbit v3 Search, then perform a billed profile read to return the full profile.",
    {
      query: z.string().min(1).max(2_000).describe("Name, email, phone number, URL, or plain-English identity query."),
      request_id: z.string().min(1).max(200).optional().describe("Stable idempotency key for retrying the same profile resolution."),
    },
    async ({ query, request_id }) => {
      try {
        const profile = await client.resolveProfile(query, "full", request_id);
        return profile ? toolResult(profile) : toolResult({ message: "No matching person found." });
      } catch (error) {
        return toolError(error);
      }
    },
  );

  return server;
}
