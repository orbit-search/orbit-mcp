/** Network Graph tool contract: request shape, one idempotency key across retries, free misses and errors. Local fixtures only. */
import assert from "node:assert/strict";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createOrbitServer } from "../build/server.js";
import { OrbitV3Client } from "../build/orbit-api.js";

const FROM = "11111111-1111-4111-8111-111111111111";
const MIDDLE = "22222222-2222-4222-8222-222222222222";
const TO = "33333333-3333-4333-8333-333333333333";
const billing = { id: "operation-fixture", pricingVersion: "2026-11-03", reservedCredits: 5, consumedCredits: 5, releasedCredits: 0, heldCredits: 0, status: "settled" };
const found = {
  from_profile_id: FROM, to_profile_id: TO, mode: "strongest", found: true, degrees: 2, strength: 0.275,
  people: [{ profile_id: FROM, name: "Ana Ruiz" }, { profile_id: MIDDLE, name: "Ben Cole" }, { profile_id: TO, name: "Cara Diaz" }],
  links: [
    { from_profile_id: FROM, to_profile_id: MIDDLE, strength: 0.55, relationships: ["coworker"], organizations: [{ name: "Acme", overlap_years: 3 }], schools: [] },
    { from_profile_id: MIDDLE, to_profile_id: TO, strength: 0.5, relationships: ["colleague"], organizations: [], schools: [] },
  ],
  reason: null, billing, contract_version: "network-path.v1",
};
const missing = { from_profile_id: FROM, to_profile_id: TO, mode: "shortest", found: false, degrees: null, strength: null,
  people: [], links: [], reason: "not_in_graph", billing: null, contract_version: "network-path.v1" };

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

async function withClient(respond, run) {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, init) => { requests.push({ url: new URL(String(url)), init }); return respond(); };
  const server = createOrbitServer("sk_orb_network_fixture");
  const client = new Client({ name: "network-path-test", version: "1.0.0" });
  const [ct, st] = InMemoryTransport.createLinkedPair();
  try {
    await Promise.all([server.connect(st), client.connect(ct)]);
    await client.listTools(); // enables client-side output validation
    return await run(client, requests);
  } finally {
    globalThis.fetch = originalFetch;
    await Promise.all([client.close(), server.close()]);
  }
}

test("find_network_path advertises profile_id, to and mode, read-only hints and the path output schema", async () => {
  await withClient(() => json(found), async client => {
    const tool = (await client.listTools()).tools.find(item => item.name === "find_network_path");
    assert.ok(tool);
    assert.deepEqual(Object.keys(tool.inputSchema.properties).sort(), ["mode", "profile_id", "to"]);
    assert.deepEqual(tool.inputSchema.required.sort(), ["profile_id", "to"]);
    assert.deepEqual(tool.inputSchema.properties.mode.enum, ["shortest", "strongest"]);
    assert.equal(tool.inputSchema.properties.mode.default, "shortest");
    assert.deepEqual(tool.annotations, { readOnlyHint: true, destructiveHint: false, idempotentHint: false, openWorldHint: false });
    assert.equal(tool.outputSchema.type, "object");
    for (const field of ["from_profile_id", "to_profile_id", "mode", "found", "degrees", "strength", "people", "links", "reason", "billing", "contract_version"]) {
      assert.ok(tool.outputSchema.required.includes(field), field);
    }
    assert.match(tool.description, /Network Graph path rate/);
    assert.match(tool.description, /finds no path is free/);
  });
});

test("a found path calls GET /v3/people/{id}/network/path with an idempotency key and returns the path verbatim", async () => {
  await withClient(() => json(found), async (client, requests) => {
    const result = await client.callTool({ name: "find_network_path", arguments: { profile_id: FROM, to: TO, mode: "strongest" } });
    assert.equal(result.isError, undefined);
    assert.equal(requests.length, 1);
    const [request] = requests;
    assert.equal(request.url.origin + request.url.pathname, `https://api.orbitsearch.com/v3/people/${FROM}/network/path`);
    assert.equal(request.url.searchParams.get("to"), TO);
    assert.equal(request.url.searchParams.get("mode"), "strongest");
    assert.equal(request.init.method, undefined, "a GET read");
    assert.equal(request.init.headers.Authorization, "Bearer sk_orb_network_fixture");
    assert.match(request.init.headers["Idempotency-Key"], /^[0-9a-f-]{36}$/);
    assert.deepEqual(result.structuredContent, found);
    assert.deepEqual(JSON.parse(result.content[0].text), found);
  });
});

test("mode defaults to shortest and a miss is free: billing null with its reason", async () => {
  await withClient(() => json(missing), async (client, requests) => {
    const result = await client.callTool({ name: "find_network_path", arguments: { profile_id: FROM, to: TO } });
    assert.equal(result.isError, undefined);
    assert.equal(requests[0].url.searchParams.get("mode"), "shortest");
    assert.equal(result.structuredContent.found, false);
    assert.equal(result.structuredContent.billing, null);
    assert.equal(result.structuredContent.reason, "not_in_graph");
  });
});

test("API errors keep their code, malformed answers fail, and invalid input never reaches Orbit", async () => {
  let reply = () => json({ error: { code: "network_path_same_person", message: "profile_id and to must be different people" } }, 400);
  await withClient(() => reply(), async (client, requests) => {
    const same = await client.callTool({ name: "find_network_path", arguments: { profile_id: FROM, to: FROM } });
    assert.equal(same.isError, true);
    assert.equal(same.structuredContent, undefined);
    assert.match(JSON.parse(same.content[0].text).error, /HTTP 400: network_path_same_person: profile_id and to must be different people/);
    assert.equal(requests.length, 1, "a 400 is not retried");

    reply = () => json({ found: "yes" });
    const malformed = await client.callTool({ name: "find_network_path", arguments: { profile_id: FROM, to: TO } });
    assert.equal(malformed.isError, true);
    assert.match(JSON.parse(malformed.content[0].text).error, /invalid Network Graph path/);

    reply = () => json({ error: { code: "developer_api_credits_insufficient", message: "private billing detail", remainingCredits: 1 } }, 402);
    const credits = await client.callTool({ name: "find_network_path", arguments: { profile_id: FROM, to: TO } });
    assert.equal(credits.isError, true);
    assert.match(credits.content[0].text, /requires additional credits/);
    assert.doesNotMatch(credits.content[0].text, /private billing detail|developer_api_credits_insufficient/);

    reply = () => json({ error: { code: "forbidden", message: "private access detail" } }, 403);
    const denied = await client.callTool({ name: "find_network_path", arguments: { profile_id: FROM, to: TO } });
    assert.equal(denied.isError, true);
    assert.match(denied.content[0].text, /HTTP 403/);
    assert.doesNotMatch(denied.content[0].text, /private access detail/);

    reply = () => json({ error: { code: "unknown_bad_request", message: "private request detail" } }, 400);
    const unknown = await client.callTool({ name: "find_network_path", arguments: { profile_id: FROM, to: TO } });
    assert.doesNotMatch(unknown.content[0].text, /private request detail/, "only known Network Graph codes are forwarded");

    const before = requests.length;
    const slug = await client.callTool({ name: "find_network_path", arguments: { profile_id: "ana-ruiz", to: TO } });
    assert.equal(slug.isError, true);
    assert.match(slug.content[0].text, /canonical Orbit profile ID \(UUID\)/);
    const badMode = await client.callTool({ name: "find_network_path", arguments: { profile_id: FROM, to: TO, mode: "closest" } });
    assert.equal(badMode.isError, true);
    assert.equal(requests.length, before);
  });
});

test("a 503 is retried with the same Idempotency-Key, so a found path is charged once", async () => {
  const keys = [];
  let calls = 0;
  const client = new OrbitV3Client({
    apiKey: "sk_orb_network_fixture",
    baseUrl: "https://api.example.test",
    sleepImpl: async () => {},
    fetchImpl: async (_url, init) => {
      keys.push(init.headers["Idempotency-Key"]);
      calls += 1;
      return calls === 1 ? json({ error: { code: "network_path_unavailable", message: "Network Graph paths are not available right now" } }, 503) : json(found);
    },
  });
  const result = await client.findNetworkPath(FROM, TO, "strongest");
  assert.equal(result.found, true);
  assert.equal(keys.length, 2);
  assert.equal(keys[0], keys[1]);

  const separate = new OrbitV3Client({ apiKey: "sk_orb_network_fixture", baseUrl: "https://api.example.test", sleepImpl: async () => {},
    fetchImpl: async (_url, init) => { keys.push(init.headers["Idempotency-Key"]); return json(missing); } });
  await separate.findNetworkPath(FROM, TO);
  assert.notEqual(keys[2], keys[0], "a new lookup gets a new key");

  let attempts = 0;
  const down = new OrbitV3Client({ apiKey: "sk_orb_network_fixture", baseUrl: "https://api.example.test", sleepImpl: async () => {},
    fetchImpl: async () => { attempts += 1; return json({ error: { code: "network_path_unavailable" } }, 503); } });
  await assert.rejects(down.findNetworkPath(FROM, TO), error => error.status === 503);
  assert.equal(attempts, 3, "at most three attempts");
});
