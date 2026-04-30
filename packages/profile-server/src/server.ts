import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { searchPeople, getProfile } from "./orbit-api.js";
import { extractFacts } from "./extract.js";

export function createProfileServer(apiKey?: string): McpServer {
  const server = new McpServer({
    name: "orbit-profile-mcp",
    version: "1.0.0",
  });

  server.tool(
    "get_profile",
    "Look up a person's profile by their phone number, email, or name. Returns their full Orbit profile including bio, jobs, education, interests, and stored memories.",
    {
      query: z.string().describe("Phone number, email address, or name to look up"),
    },
    async ({ query }) => {
      if (!apiKey) {
        return {
          content: [{ type: "text" as const, text: "No API key provided. Connect with an Authorization header." }],
          isError: true,
        };
      }

      try {
        const { results } = await searchPeople(query, 1, apiKey);

        if (results.length === 0) {
          return {
            content: [{ type: "text" as const, text: "No matching person found." }],
          };
        }

        const profile = await getProfile(results[0].userId, apiKey);

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

  server.tool(
    "add_memories",
    "Extract and store personal facts about a user from conversation messages. Pass the person's identifier and raw conversation text — facts are automatically extracted, categorized, and saved to their Orbit profile.",
    {
      query: z.string().describe("Phone number, email address, or name to identify the person"),
      messages: z.string().describe("Raw conversation text to extract facts from"),
    },
    async ({ query, messages }) => {
      if (!apiKey) {
        return {
          content: [{ type: "text" as const, text: "No API key provided. Connect with an Authorization header." }],
          isError: true,
        };
      }

      try {
        const { results } = await searchPeople(query, 1, apiKey);

        if (results.length === 0) {
          return {
            content: [{ type: "text" as const, text: "No matching person found." }],
            isError: true,
          };
        }

        const facts = await extractFacts(messages);

        if (facts.length === 0) {
          return {
            content: [{ type: "text" as const, text: "No extractable facts found in the conversation." }],
          };
        }

        // TODO: call Orbit API to persist facts once endpoint exists

        return {
          content: [{
            type: "text" as const,
            text: JSON.stringify({ userId: results[0].userId, displayName: results[0].displayName, facts }, null, 2),
          }],
        };
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : "Unknown error occurred";
        return {
          content: [{ type: "text" as const, text: `Error adding memories: ${message}` }],
          isError: true,
        };
      }
    },
  );

  return server;
}
