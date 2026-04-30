const BASE_URL = process.env.ORBIT_API_URL ?? "https://api.orbitsearch.com";
const APP_ID = process.env.ORBIT_APP_ID ?? "0eae6b0f-c7aa-43c3-af09-7bd5a0a7df7d";
const APP_VERSION = "1.0.0";

interface SearchResult {
  displayName: string;
  username: string | null;
  userId: string;
  city: string | null;
  age: number | null;
  matchReason: string;
  sourceCount: number;
}

export async function searchPeople(
  query: string,
  numUsers: number,
  apiKey: string,
): Promise<{ results: SearchResult[] }> {
  const response = await fetch(`${BASE_URL}/v2/social/profiles/searches/smart`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
      "App-Id": APP_ID,
      "App-Version": APP_VERSION,
    },
    body: JSON.stringify({ query, numUsers }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Orbit API error ${response.status}: ${body}`);
  }

  const data = (await response.json()) as {
    payload: { users: Array<Record<string, unknown>> };
  };

  const users = data.payload?.users ?? [];

  return {
    results: users.map((u) => ({
      displayName: (u.displayName as string) ?? "",
      username: (u.username as string) ?? null,
      userId: u.userId as string,
      city: (u.city as string) ?? null,
      age: (u.age as number) ?? null,
      matchReason: typeof u.matchReason === "string" ? u.matchReason : "",
      sourceCount: (u.sourceCount as number) ?? 0,
    })),
  };
}

export async function getProfile(
  profileId: string,
  apiKey: string,
): Promise<Record<string, unknown>> {
  const response = await fetch(
    `${BASE_URL}/v2/developer/profiles/${encodeURIComponent(profileId)}`,
    {
      method: "GET",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "App-Id": APP_ID,
        "App-Version": APP_VERSION,
      },
    },
  );

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Orbit API error ${response.status}: ${body}`);
  }

  const data = (await response.json()) as { payload: Record<string, unknown> };
  return data.payload;
}


