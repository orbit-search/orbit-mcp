import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { ProfileOrbitClient, ProfileResolutionError } from "./orbit-api.js";
import { creditUsageOutputSchema } from "./billing.js";

function toolResult(value: unknown, isError = false) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(value, null, 2) }],
    ...(isError ? { isError: true } : {}),
  };
}

function toolError(error: unknown) {
  return toolResult({
    error: error instanceof Error ? error.message : String(error),
    ...(error instanceof ProfileResolutionError && error.search_billing ? { search_billing: error.search_billing } : {}),
  }, true);
}

export function createProfileServer(apiKey: string): McpServer {
  const client = new ProfileOrbitClient({ apiKey });
  const server = new McpServer({ name: "orbit-profile-mcp", version: "2.0.0" });

  server.registerTool("get_credit_usage", {
    description: "Read net credit usage for the connected Orbit API key and available/reserved credits for its billing account. No purchase or billable work is started.",
    inputSchema: {}, outputSchema: creditUsageOutputSchema,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  }, async () => {
    try {
      const result = await client.getCreditUsage();
      return { ...toolResult(result), structuredContent: result };
    } catch (error) { return toolError(error); }
  });

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
        return { ...toolResult(profile), structuredContent: { ...profile } };
      } catch (error) {
        return toolError(error);
      }
    },
  );

  return server;
}
