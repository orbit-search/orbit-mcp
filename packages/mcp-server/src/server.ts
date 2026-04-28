import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import {
  searchPeople,
  getProfileById,
  getProfileByUsername,
} from "./orbit-api.js";
import { getCredits, deductCredits } from "./billing.js";

async function assertCredits(
  apiKey: string | null,
  required: number,
): Promise<void> {
  if (!apiKey) return;
  const { credits } = await getCredits(apiKey);
  if (credits < required) {
    throw new Error(`Insufficient credits: ${credits} available, ${required} required`);
  }
}

export function createOrbitServer(apiKey?: string): McpServer {
  const server = new McpServer({
    name: "orbit-mcp",
    version: "1.0.0",
  });

  server.tool(
    "search_people",
    "Search for people by name, phone number, email, or description. Returns a list of matching profiles with basic info.",
    {
      query: z
        .string()
        .describe(
          "The search query — a person's name, phone number, email address, or a description like 'CEO of Acme Corp'.",
        ),
    },
    async ({ query }) => {
      try {
        if (apiKey) {
          await assertCredits(apiKey, 1);
        }

        const { results, timedOut } = await searchPeople(query);

        if (results.length === 0) {
          return {
            content: [
              {
                type: "text" as const,
                text: "No matching people found for the given query.",
              },
            ],
          };
        }

        if (apiKey) {
          await deductCredits(apiKey, results.length, "search_people");
        }

        const payload: Record<string, unknown> = { results };
        if (timedOut) {
          payload.note =
            "Search timed out after 60 seconds. These are partial results.";
        }

        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify(payload, null, 2),
            },
          ],
        };
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : "Unknown error occurred";
        return {
          content: [
            {
              type: "text" as const,
              text: `Error searching for people: ${message}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  server.tool(
    "get_profile",
    "Get a person's full profile by their user ID (from search results) or username. Returns detailed info including bio, jobs, education, interests, family, social links, and more.",
    {
      userId: z
        .string()
        .optional()
        .describe("The user ID returned from search_people results."),
      username: z
        .string()
        .optional()
        .describe("The person's username (alternative to userId)."),
    },
    async ({ userId, username }) => {
      if (!userId && !username) {
        return {
          content: [
            {
              type: "text" as const,
              text: "Either userId or username must be provided.",
            },
          ],
          isError: true,
        };
      }

      try {
        if (apiKey) {
          await assertCredits(apiKey, 2);
        }

        const profile = userId
          ? await getProfileById(userId)
          : await getProfileByUsername(username!);

        if (apiKey) {
          await deductCredits(apiKey, 2, "get_profile");
        }

        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify(profile, null, 2),
            },
          ],
        };
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : "Unknown error occurred";
        return {
          content: [
            {
              type: "text" as const,
              text: `Error fetching profile: ${message}`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  return server;
}
