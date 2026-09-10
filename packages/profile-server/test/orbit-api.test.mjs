import assert from "node:assert/strict";
import test from "node:test";
import { ProfileOrbitClient } from "../build/orbit-api.js";

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

test("profile billing failures stop immediately with safe HTTP 402 guidance", async () => {
  for (const operation of ["search", "read"]) {
    const calls = [];
    const sleeps = [];
    const body = { error: { code: "insufficient_credits", message: "sensitive-upstream-value" } };
    const client = new ProfileOrbitClient({
      apiKey: "sk_orb_test",
      fetchImpl: async (url, init) => {
        calls.push({ url: String(url), init });
        return jsonResponse(body, 402);
      },
      sleepImpl: async (ms) => sleeps.push(ms),
    });
    await assert.rejects(
      () => operation === "search" ? client.resolveProfile("Ada Lovelace", "full") : client.getProfile("profile-1"),
      (error) => {
        assert.equal(error.status, 402);
        assert.deepEqual(error.body, body);
        assert.match(error.message, /HTTP 402.*dashboard\/billing/);
        assert.doesNotMatch(error.message, /sensitive-upstream-value/);
        return true;
      },
    );
    assert.equal(calls.length, 1);
    assert.deepEqual(sleeps, []);
  }
});

test("profile resolution retries reuse one generated request ID and then poll existing work", async () => {
  const calls = [];
  const responses = [
    jsonResponse({ error: "temporary failure" }, 503),
    jsonResponse({ search_id: "search-1", status: "running", results: [] }, 202),
    jsonResponse({ search_id: "search-1", status: "completed", results: [{ profile_id: "profile-1", status: "ready", generation_level: 3, profile: { name: "Ada" } }] }),
  ];
  const client = new ProfileOrbitClient({
    apiKey: "sk_orb_test",
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      const response = responses.shift();
      assert(response, "unexpected request");
      return response;
    },
    sleepImpl: async () => {},
  });
  await client.resolveProfile("Ada Lovelace", "full");
  assert.equal(calls.length, 3);
  assert.match(JSON.parse(calls[0].init.body).request_id, /^[0-9a-f-]{36}$/);
  assert.equal(calls[0].init.body, calls[1].init.body);
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[1].init.method, "POST");
  assert.equal(calls[2].init.body, undefined);
  assert.match(calls[2].url, /\/v3\/search\/search-1$/);
});

test("profile resolution uses v3 Search and returns the embedded profile", async () => {
  const calls = [];
  const responses = [
    jsonResponse({ search_id: "search-1", request_id: "request-1", status: "running", results: [] }, 202),
    jsonResponse({
      search_id: "search-1",
      request_id: "request-1",
      status: "completed",
      results: [{ profile_id: "profile-1", status: "ready", generation_level: 3, profile: { name: "Ada" } }],
    }),
  ];
  const client = new ProfileOrbitClient({
    apiKey: "sk_orb_test",
    baseUrl: "https://api.orbit.test",
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      return responses.shift();
    },
    sleepImpl: async () => {},
  });

  const result = await client.resolveProfile("Ada Lovelace", "full");

  assert.equal(result.profile_id, "profile-1");
  assert.equal(result.profile.name, "Ada");
  assert.equal(calls[0].url, "https://api.orbit.test/v3/search");
  assert.equal(calls[1].url, "https://api.orbit.test/v3/search/search-1");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    request_id: JSON.parse(calls[0].init.body).request_id,
    query: "Ada Lovelace",
    candidate_discovery: true,
    profile_depth: "full",
    include_profile: true,
    limit: 1,
  });
});

test("profile resolution falls back to v3 Enrich when Search omits the embedded profile", async () => {
  const calls = [];
  const responses = [
    jsonResponse({ search_id: "search-2", request_id: "request-2", status: "completed", results: [{ profile_id: "profile/2", status: "ready", generation_level: 2 }] }),
    jsonResponse({ profile_id: "profile/2", generation_level: 2, profile: { name: "Grace" } }),
  ];
  const client = new ProfileOrbitClient({
    apiKey: "sk_orb_test",
    baseUrl: "https://api.orbit.test",
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      return responses.shift();
    },
  });

  const result = await client.resolveProfile("Grace Hopper", "partial");

  assert.equal(result.profile.name, "Grace");
  assert.equal(calls[1].url, "https://api.orbit.test/v3/enrich/profile%2F2");
});

test("email and phone queries use v3 identity signals", async () => {
  const calls = [];
  const responses = [
    jsonResponse({ search_id: "email-search", request_id: "email-job", status: "completed", results: [] }),
    jsonResponse({ search_id: "phone-search", request_id: "phone-job", status: "completed", results: [] }),
  ];
  const client = new ProfileOrbitClient({
    apiKey: "sk_orb_test",
    baseUrl: "https://api.orbit.test",
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      return responses.shift();
    },
  });

  await client.searchAndWait("person@example.com", "partial", "email-job");
  await client.searchAndWait("+1 (415) 555-0123", "partial", "phone-job");

  assert.deepEqual(JSON.parse(calls[0].init.body), {
    request_id: "email-job",
    signals: { email: "person@example.com" },
    candidate_discovery: true,
    profile_depth: "partial",
    include_profile: true,
    limit: 1,
  });
  assert.deepEqual(JSON.parse(calls[1].init.body), {
    request_id: "phone-job",
    signals: { phone: "+1 (415) 555-0123" },
    candidate_discovery: false,
    profile_depth: "partial",
    include_profile: true,
    limit: 1,
  });
});
