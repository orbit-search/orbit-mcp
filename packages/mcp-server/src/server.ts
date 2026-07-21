import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { OrbitV3Client } from "./orbit-api.js";

function toolResult(value: unknown, isError = false) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(value, null, 2) }],
    ...(isError ? { isError: true } : {}),
  };
}

function toolError(error: unknown) {
  return toolResult({ error: error instanceof Error ? error.message : String(error) }, true);
}

const signalsSchema = z
  .object({
    email: z.string().email().optional(),
    linkedin_url: z.string().url().optional(),
    usernames: z.array(z.string().min(1)).max(20).optional(),
    urls: z.array(z.string().url()).max(20).optional(),
    address: z.string().min(1).optional(),
    phone: z.string().min(1).optional(),
  })
  .strict();

export function createOrbitServer(apiKey: string): McpServer {
  const client = new OrbitV3Client({ apiKey });
  const server = new McpServer({ name: "orbit-mcp", version: "2.0.0" });

  server.tool(
    "search_people",
    "Find people with Orbit v3 Search using a plain-English query, identity signals, or both. The tool waits for terminal results and includes ready profiles.",
    {
      query: z.string().min(1).max(2_000).optional().describe("Plain-English people search query."),
      signals: signalsSchema.optional().describe("Exact identity signals such as email, LinkedIn URL, username, URL, address, or phone."),
      candidate_discovery: z.boolean().default(false).describe("Allow Orbit to discover people beyond known matches. Address and phone always disable discovery."),
      profile_depth: z.enum(["partial", "full"]).default("partial").describe("Minimum profile depth to build for selected results."),
      limit: z.number().int().min(1).max(20).default(10).describe("Maximum results; v3 supports up to 20."),
      request_id: z.string().min(1).max(200).optional().describe("Stable idempotency key. Persist and reuse it when retrying the same logical search."),
    },
    async ({ query, signals, candidate_discovery, profile_depth, limit, request_id }) => {
      try {
        const result = await client.searchAndWait({ query, signals, candidate_discovery, profile_depth, limit, request_id });
        return toolResult(result, result.status !== "completed");
      } catch (error) {
        return toolError(error);
      }
    },
  );

  server.tool(
    "get_profile",
    "Read an existing Orbit profile by its canonical profile ID through v3 Enrich. This read does not regenerate the profile.",
    {
      profile_id: z.string().min(1).max(500).describe("Canonical Orbit profile ID returned by search_people."),
    },
    async ({ profile_id }) => {
      try {
        return toolResult(await client.getProfile(profile_id));
      } catch (error) {
        return toolError(error);
      }
    },
  );

  server.tool(
    "enrich_profile",
    "Ensure an existing Orbit profile is partial or full, or regenerate a full profile. The tool waits for terminal v3 Enrich status.",
    {
      profile_id: z.string().min(1).max(500).describe("Canonical Orbit profile ID. Identity signals belong in search_people instead."),
      operation: z.enum(["partial", "full", "regenerate"]).describe("Desired level-aware enrichment operation."),
      request_id: z.string().min(1).max(200).optional().describe("Stable idempotency key. Persist and reuse it when retrying the same logical enrichment."),
    },
    async ({ profile_id, operation, request_id }) => {
      try {
        const result = await client.enrichAndWait(profile_id, operation, request_id);
        return toolResult(result, result.status === "failed");
      } catch (error) {
        return toolError(error);
      }
    },
  );

  return server;
}
