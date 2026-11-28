import { ContractMcpServer as McpServer } from "./mcp-compat.js";
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
  const server = new McpServer({ name: "orbit-profile-mcp", version: "2.0.0" }, { maxToolInputElements: 10_000 });

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

  server.registerTool(
    "get_profile",
    { description: "Resolve one person from a single query (a name, email address, phone number, LinkedIn or other profile URL, or a plain-English identity description) and return their full (level 3) Orbit profile with source evidence, or a message when no matching person is found. Waits for the Orbit v3 Search to finish. A phone number matches only people Orbit already has. Uses credits for the search and profile; search_billing is Orbit's receipt.", inputSchema: {
      query: z.string().min(1).max(2_000).describe("Name, email, phone number, URL, or plain-English identity query."),
      request_id: z.string().min(1).max(200).optional().describe("Stable idempotency key for retrying the same profile resolution."),
    } },
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
