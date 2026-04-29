import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { searchPeople, getProfile } from "./orbit-api.js";

export function createOrbitServer(apiKey?: string): McpServer {
  const server = new McpServer({
    name: "orbit-mcp",
    version: "1.0.0",
  });

  server.tool(
    "search_people",
    "Search for people by name, phone number, email, or description. Returns a list of matching profiles with basic info. Costs numUsers credits.",
    {
      query: z
        .string()
        .describe(
          "The search query — a person's name, phone number, email address, or a description like 'CEO of Acme Corp'.",
        ),
      numUsers: z
        .number()
        .int()
        .min(1)
        .max(100)
        .default(10)
        .describe("Number of results to return (1-100). Each result costs 1 credit."),
    },
    async ({ query, numUsers }) => {
      if (!apiKey) {
        return {
          content: [{ type: "text" as const, text: "No API key provided. Connect with an Authorization header." }],
          isError: true,
        };
      }

      try {
        const { results, searchId, creditsRemaining } = await searchPeople(query, numUsers, apiKey);

        if (results.length === 0) {
          return {
            content: [{ type: "text" as const, text: "No matching people found for the given query." }],
          };
        }

        const payload: Record<string, unknown> = { searchId, results };
        if (creditsRemaining !== null) {
          payload.creditsRemaining = creditsRemaining;
        }

        return {
          content: [{ type: "text" as const, text: JSON.stringify(payload, null, 2) }],
        };
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : "Unknown error occurred";
        return {
          content: [{ type: "text" as const, text: `Error searching for people: ${message}` }],
          isError: true,
        };
      }
    },
  );

  server.tool(
    "get_profile",
    "Get a person's full profile by their user ID (from search results). Returns detailed info including bio, jobs, education, interests, and more.",
    {
      profileId: z
        .string()
        .describe("The user ID returned from search_people results."),
    },
    async ({ profileId }) => {
      if (!apiKey) {
        return {
          content: [{ type: "text" as const, text: "No API key provided. Connect with an Authorization header." }],
          isError: true,
        };
      }

      try {
        const profile = await getProfile(profileId, apiKey);

        return {
          content: [{ type: "text" as const, text: JSON.stringify(profile, null, 2) }],
        };
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : "Unknown error occurred";
        return {
          content: [{ type: "text" as const, text: `Error fetching profile: ${message}` }],
          isError: true,
        };
      }
    },
  );

  return server;
}
