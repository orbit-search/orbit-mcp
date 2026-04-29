import type { DeveloperSearchResponse, DeveloperProfileResponse } from "./types.js";

const BASE_URL = process.env.ORBIT_API_URL ?? "https://api.orbitsearch.com";

export interface SearchResult {
  displayName: string;
  username: string | null;
  userId: string;
  city: string | null;
  age: number | null;
  matchReason: string;
  sourceCount: number;
}

export interface SearchResponse {
  searchId: string;
  results: SearchResult[];
  creditsRemaining: number | null;
}

export async function searchPeople(
  query: string,
  numUsers: number,
  apiKey: string,
): Promise<SearchResponse> {
  const response = await fetch(`${BASE_URL}/v2/social/profiles/searches/smart`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ query, numUsers }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Orbit API error ${response.status}: ${body}`);
  }

  const creditsHeader = response.headers.get("X-Developer-API-Credits-Remaining");
  const creditsRemaining = creditsHeader !== null ? parseInt(creditsHeader, 10) : null;

  const data = (await response.json()) as DeveloperSearchResponse;
  const users = data.payload?.users ?? [];

  return {
    searchId: data.searchId,
    creditsRemaining,
    results: users.map((u) => ({
      displayName: u.displayName ?? "",
      username: u.username ?? null,
      userId: u.userId,
      city: u.city ?? null,
      age: u.age ?? null,
      matchReason: typeof u.matchReason === "string" ? u.matchReason : "",
      sourceCount: u.sourceCount ?? 0,
    })),
  };
}

export async function getProfile(
  profileId: string,
  apiKey: string,
): Promise<DeveloperProfileResponse["payload"]> {
  const response = await fetch(
    `${BASE_URL}/v2/developer/profiles/${encodeURIComponent(profileId)}`,
    {
      method: "GET",
      headers: { Authorization: `Bearer ${apiKey}` },
    },
  );

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Orbit API error ${response.status}: ${body}`);
  }

  const data = (await response.json()) as DeveloperProfileResponse;
  return data.payload;
}
