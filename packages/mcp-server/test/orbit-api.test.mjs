import assert from "node:assert/strict";
import test from "node:test";
import { OrbitApiError, OrbitV3Client } from "../build/orbit-api.js";

function jsonResponse(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function clientWithResponses(responses, calls, sleeps = []) {
  return new OrbitV3Client({
    apiKey: "sk_orb_test",
    baseUrl: "https://api.orbit.test",
    pollTimeoutMs: 10_000,
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      const response = responses.shift();
      if (!response) throw new Error("Unexpected fetch");
      return response;
    },
    sleepImpl: async (milliseconds) => {
      sleeps.push(milliseconds);
    },
  });
}

test("search_people starts v3 work with a stable request ID and polls to terminal", async () => {
  const calls = [];
  const client = clientWithResponses(
    [
      jsonResponse({ search_id: "search-1", request_id: "crm-job-1", status: "running", results: [] }, 202),
      jsonResponse({ search_id: "search-1", request_id: "crm-job-1", status: "completed", results: [{ profile_id: "profile-1", status: "ready", generation_level: 2 }] }),
    ],
    calls,
  );

  const result = await client.searchAndWait({
    request_id: "crm-job-1",
    query: "ML engineers in San Francisco",
    candidate_discovery: true,
    profile_depth: "partial",
    limit: 5,
  });

  assert.equal(result.status, "completed");
  assert.equal(calls[0].url, "https://api.orbit.test/v3/search");
  assert.equal(calls[1].url, "https://api.orbit.test/v3/search/search-1");
  assert.equal(calls[0].init.headers.Authorization, "Bearer sk_orb_test");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    request_id: "crm-job-1",
    query: "ML engineers in San Francisco",
    candidate_discovery: true,
    profile_depth: "partial",
    include_profile: true,
    limit: 5,
  });
});

test("phone and address signals always disable candidate discovery", async () => {
  const calls = [];
  const client = clientWithResponses(
    [jsonResponse({ search_id: "search-2", request_id: "signal-1", status: "completed", results: [] })],
    calls,
  );

  await client.searchAndWait({
    request_id: "signal-1",
    signals: { phone: "+14155550123" },
    candidate_discovery: true,
  });

  assert.equal(JSON.parse(calls[0].init.body).candidate_discovery, false);
});

test("profile reads and enrichment use v3 Enrich", async () => {
  const calls = [];
  const client = clientWithResponses(
    [
      jsonResponse({ profile_id: "profile/1", generation_level: 2, profile: { id: "profile/1" } }),
      jsonResponse({ request_id: "enrich-1", profile_id: "profile/1", status: "completed", operation: "full", include_profile: true }),
    ],
    calls,
  );

  await client.getProfile("profile/1");
  await client.enrichAndWait("profile/1", "full", "enrich-1");

  assert.equal(calls[0].url, "https://api.orbit.test/v3/enrich/profile%2F1");
  assert.equal(calls[1].url, "https://api.orbit.test/v3/enrich/profile%2F1");
  assert.deepEqual(JSON.parse(calls[1].init.body), {
    request_id: "enrich-1",
    operation: "full",
    include_profile: true,
  });
});

test("running enrichment without a status link fails instead of returning early", async () => {
  const calls = [];
  const client = clientWithResponses(
    [jsonResponse({ request_id: "enrich-2", profile_id: "profile-2", status: "running", operation: "full", include_profile: true })],
    calls,
  );

  await assert.rejects(
    () => client.enrichAndWait("profile-2", "full", "enrich-2"),
    /Orbit enrich response missing status link while in state running/,
  );
  assert.equal(calls.length, 1);
});

test("429 retries honor Retry-After while authentication errors do not retry", async () => {
  const retryCalls = [];
  const sleeps = [];
  const retryClient = clientWithResponses(
    [
      jsonResponse({ error: "rate limited" }, 429, { "retry-after": "0" }),
      jsonResponse({ profile_id: "profile-1", generation_level: 1, profile: {} }),
    ],
    retryCalls,
    sleeps,
  );
  await retryClient.getProfile("profile-1");
  assert.equal(retryCalls.length, 2);
  assert.deepEqual(sleeps, [0]);

  const authCalls = [];
  const authClient = clientWithResponses([jsonResponse({ error: "unauthorized" }, 401)], authCalls);
  await assert.rejects(() => authClient.getProfile("profile-1"), (error) => error instanceof OrbitApiError && error.status === 401);
  assert.equal(authCalls.length, 1);
});
