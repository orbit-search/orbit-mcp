/** General Orbit MCP tools with truthful behavior hints and structured v3 results. */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { OrbitV3Client } from "./orbit-api.js";
import { registerDirectoryTools } from "./directory-tools.js";
import { searchOutputSchema, searchSnapshotOutputSchema, populationSearchOutputSchema, populationQuoteOutputSchema, profileOutputSchema, enrichOutputSchema, creditUsageOutputSchema } from "./output-schemas.js";

function toolResult(value: object, isError = false) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(value, null, 2) }],
    structuredContent: { ...value },
    ...(isError ? { isError: true } : {}),
  };
}

function toolError(error: unknown) {
  // Exception envelopes do not match successful output schemas. Some clients
  // validate any structuredContent even when isError is true, hiding the error.
  return {
    content: [{ type: "text" as const, text: JSON.stringify({ error: error instanceof Error ? error.message : String(error) }) }],
    isError: true,
  };
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

/** Terminal states that are MCP errors, as search_people reports them: some or all of the work failed. */
const PARTIAL_OR_FAILED: ReadonlySet<string> = new Set(["completed_with_errors", "failed"]);

export function createOrbitServer(apiKey: string): McpServer {
  const client = new OrbitV3Client({ apiKey });
  // `name` stays the programmatic id. `title` is what MCP clients show, and
  // `icons` is how they render a logo; without them a client falls back to the
  // raw name and a placeholder. See the Implementation schema in the MCP spec.
  const server = new McpServer({
    name: "orbit-mcp",
    title: "Orbit",
    version: "2.3.0",
    description: "Search people and read source-backed Orbit profiles, enrich context, and manage directories.",
    websiteUrl: "https://developer.orbitsearch.com",
    icons: [
      { src: "https://developer.orbitsearch.com/orbit-icon-512.png", mimeType: "image/png", sizes: ["512x512"] },
    ],
  });

  server.registerTool(
    "get_credit_usage",
    {
      description: "Read net credit usage for the connected Orbit API key and available/reserved credits for its billing account. This read does not purchase credits or start billable work.",
      outputSchema: creditUsageOutputSchema,
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
      inputSchema: {},
    },
    async () => {
      try { return toolResult(await client.getCreditUsage()); }
      catch (error) { return toolError(error); }
    },
  );

  server.registerTool(
    "search_people",
    {
      description: "Find people with Orbit v3 Search using a plain-English query, identity signals, or both. The tool waits for terminal results and includes ready profiles.",
      outputSchema: searchOutputSchema,
      // Search may generate/upgrade profiles; an omitted request_id creates new work.
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
      inputSchema: {
        query: z.string().min(1).max(2_000).optional().describe("Plain-English people search query."),
        signals: signalsSchema.optional().describe("Exact identity signals such as email, LinkedIn URL, username, URL, address, or phone."),
        candidate_discovery: z.boolean().default(false).describe("Allow Orbit to discover people beyond known matches. Address and phone always disable discovery."),
        profile_depth: z.enum(["partial", "full"]).default("partial").describe("Minimum profile depth to build for selected results."),
        limit: z.number().int().min(1).max(20).default(10).describe("Maximum results; v3 supports up to 20."),
        request_id: z.string().min(1).max(200).optional().describe("Stable idempotency key. Persist and reuse it when retrying the same logical search."),
      },
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

  const populationInput = {
    population: z.object({
      kind: z.enum(["company", "school"]).describe("What the population is: the current employees of a company, or the alumni of a school."),
      id: z.string().regex(/^\d{1,20}$/).describe("The numeric id of the company or school."),
      name: z.string().min(1).max(200).describe("The company or school name."),
    }).strict(),
    size: z.number().int().min(0).optional().describe("How many people the population has, when known."),
    profile_depth: z.enum(["partial", "full"]).default("partial").describe("The depth every person in the population is built to. A full population costs more than a partial one."),
  };

  server.registerTool(
    "quote_population_search",
    {
      description: "Price a search for everyone in one population: every current employee of a company, or everyone who attended a school. Returns the whole cost as one number in credits. This read starts no work and reserves no credits. Quote before search_population and confirm the cost with the user.",
      outputSchema: populationQuoteOutputSchema,
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
      inputSchema: populationInput,
    },
    async ({ population, size, profile_depth }) => {
      try { return toolResult(await client.quotePopulation({ population, size, profile_depth })); }
      catch (error) { return toolError(error); }
    },
  );

  server.registerTool(
    "search_population",
    {
      description: "Search everyone in one population: every current employee of a company, or everyone who attended a school. Reserves the quoted credits and returns the search snapshot at once; the search keeps running in Orbit and adds people to its results as it finds them. Poll get_search_status with the returned search_id until status is terminal. Results are append-only.",
      outputSchema: populationSearchOutputSchema,
      // A population search builds profiles; an omitted request_id creates new work.
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
      inputSchema: {
        ...populationInput,
        request_id: z.string().min(1).max(200).optional().describe("Stable idempotency key. Persist and reuse it when retrying the same logical search."),
      },
    },
    async ({ population, size, profile_depth, request_id }) => {
      try {
        const result = await client.startPopulation({ population, size, profile_depth, request_id });
        return toolResult(result, PARTIAL_OR_FAILED.has(result.status));
      } catch (error) {
        return toolError(error);
      }
    },
  );

  server.registerTool(
    "get_search_status",
    {
      description: "Read the latest snapshot of a v3 search by search_id without starting new work. Use it to follow a population search, or any search whose earlier response was still running. Space polls out.",
      outputSchema: searchSnapshotOutputSchema,
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
      inputSchema: {
        search_id: z.string().min(1).max(500).describe("The search_id returned by search_people or search_population."),
      },
    },
    async ({ search_id }) => {
      try {
        const result = await client.getSearch(search_id);
        return toolResult(result, PARTIAL_OR_FAILED.has(result.status));
      } catch (error) {
        return toolError(error);
      }
    },
  );

  server.registerTool(
    "get_profile",
    {
      description: "Read an existing Orbit profile by its canonical profile ID through v3 Enrich. This read does not regenerate the profile.",
      outputSchema: profileOutputSchema,
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
      inputSchema: {
        profile_id: z.string().min(1).max(500).describe("Canonical Orbit profile ID returned by search_people."),
      },
    },
    async ({ profile_id }) => {
      try {
        return toolResult(await client.getProfile(profile_id));
      } catch (error) {
        return toolError(error);
      }
    },
  );

  server.registerTool(
    "enrich_profile",
    {
      description: "Ensure an existing Orbit profile is partial or full, or regenerate a full profile. The tool waits for terminal v3 Enrich status.",
      outputSchema: enrichOutputSchema,
      // Regeneration can replace existing context, and request_id remains optional.
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: true },
      inputSchema: {
        profile_id: z.string().min(1).max(500).describe("Canonical Orbit profile ID. Identity signals belong in search_people instead."),
        operation: z.enum(["partial", "full", "regenerate"]).describe("Desired level-aware enrichment operation."),
        request_id: z.string().min(1).max(200).optional().describe("Stable idempotency key. Persist and reuse it when retrying the same logical enrichment."),
      },
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

  registerDirectoryTools(server);
  return server;
}
