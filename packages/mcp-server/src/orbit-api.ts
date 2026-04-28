import { consumeSSEStream, type SSEParseResult } from "./sse-parser.js";
import type {
  NormalizedProfile,
  OrbitProfileResponse,
  RawSocialProfile,
} from "./types.js";

const BASE_URL = "https://api.orbitsearch.com";

const COMMON_HEADERS: Record<string, string> = {
  "app-id": "0eae6b0f-c7aa-43c3-af09-7bd5a0a7df7d",
  "app-version": "1.0.0",
};

const AUTH_HEADERS: Record<string, string> = {
  ...COMMON_HEADERS,
  Authorization:
    "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCIsImN0eSI6InZuZC5zZW5kaXQuand0LmFjY2VzcyJ9" +
    ".eyJhcHBfaWQiOiI3MzMzMDQwNC1lNzdiLTRkOTAtYTQwZC04N2U4YmYyNDc1NDUiLCJpYXQiOjE2NTYwMDI1NzMs" +
    "ImV4cCI6MTY1NjYwNzM3MywiaXNzIjoic2VuZGl0LmFwaS1lbnY6cHJvZCIsInN1YiI6InNlbmRpdC5pZGVudGl0eS" +
    "10eXBlLmlkOmVjMWJlNTE0LWFmYzYtNDIxNy1hYWExLTIxOTlhNDU5OGVkMCIsImp0aSI6IjMxNDgxYzYzLTYzZWQt" +
    "NGQ5Ni1hYjViLWE3ODFlMmY5ZDQ1OCJ9.wvUf9R29K6myNwFnzs6LYHM7g8wFaJdMJO9wsjgC0d4",
};

const SSE_TIMEOUT_MS = 60_000;

export async function searchPeople(query: string): Promise<SSEParseResult> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), SSE_TIMEOUT_MS);

  try {
    const response = await fetch(
      `${BASE_URL}/v2/social/profiles/searches/smart/sse`,
      {
        method: "POST",
        headers: {
          ...AUTH_HEADERS,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          query,
          numUsers: "20",
          isManualInput: true,
          searchModel: "agentic",
        }),
        signal: controller.signal,
      },
    );

    if (!response.ok) {
      throw new Error(
        `Orbit search API error ${response.status}: ${response.statusText}`,
      );
    }

    return await consumeSSEStream(response, controller.signal);
  } finally {
    clearTimeout(timeout);
  }
}

export async function getProfileById(userId: string): Promise<NormalizedProfile> {
  const url =
    `${BASE_URL}/v2/social/profiles/users/${encodeURIComponent(userId)}/light` +
    `?sortImagesAsOrbit=true&showFirstOrbit=true`;

  const response = await fetch(url, {
    method: "GET",
    headers: COMMON_HEADERS,
  });

  if (!response.ok) {
    throw new Error(
      `Orbit profile API error ${response.status}: ${response.statusText}`,
    );
  }

  const data = (await response.json()) as OrbitProfileResponse;
  return normalizeProfile(data);
}

export async function getProfileByUsername(
  username: string,
): Promise<NormalizedProfile> {
  const url =
    `${BASE_URL}/v2/social/profiles/usernames/${encodeURIComponent(username)}` +
    `?sortImagesAsOrbit=true&showFirstOrbit=true`;

  const response = await fetch(url, {
    method: "GET",
    headers: COMMON_HEADERS,
  });

  if (!response.ok) {
    throw new Error(
      `Orbit profile API error ${response.status}: ${response.statusText}`,
    );
  }

  const data = (await response.json()) as OrbitProfileResponse;
  return normalizeProfile(data);
}

function normalizeProfile(data: OrbitProfileResponse): NormalizedProfile {
  const profile: RawSocialProfile = data?.payload?.socialProfile ?? {};
  const ai = profile.aiRating ?? {};
  const loc = profile.location ?? {};

  return {
    displayName: profile.displayName ?? "",
    avatarUrl: profile.avatarUrl ?? null,
    username: profile.username ?? null,
    location: {
      city: loc.city ?? null,
      region: loc.region ?? null,
      country: loc.country ?? null,
    },
    bio: ai.bio ?? null,
    birthday: ai.birthday ?? null,
    school: ai.school ?? null,
    jobs: ai.jobs ?? [],
    education: ai.education ?? [],
    interests: [...(ai.interests ?? []), ...(ai.passions ?? [])],
    family: ai.family ?? [],
    accomplishments: ai.accomplishments ?? [],
    controversies: ai.controversies ?? [],
    socialLinks: (profile.socialMediaHandles ?? []).map((h) => ({
      network: h.network ?? null,
      url: h.url ?? null,
      username: h.username ?? null,
    })),
    worldview: ai.worldview ?? null,
    sources: (profile.orbitSources ?? []).map((s) => ({
      source: s.source ?? null,
      url: s.url ?? null,
    })),
  };
}
