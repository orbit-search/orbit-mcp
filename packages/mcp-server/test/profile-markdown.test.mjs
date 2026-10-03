import assert from "node:assert/strict";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createOrbitServer } from "../build/server.js";
import { OrbitV3Client } from "../build/orbit-api.js";
import { profileOutputSchema } from "../build/output-schemas.js";

const billing = { id: "receipt", pricingVersion: "synthetic", reservedCredits: 1, consumedCredits: 1, releasedCredits: 0, heldCredits: 0, status: "settled" };
const body = {
  profile_id: "canonical", generation_level: null, billing, format: "markdown",
  markdown: {
    schema_version: "orbit.profile.markdown.v1", entrypoint: "user.md",
    manifest: [{ path: "user.md", title: "User Profile", status: "available" }, { path: "profile/education.md", title: "Education", status: "unavailable" }],
    files: { "user.md": "# User Profile\n\nUntrusted profile data.\n\n[Education](profile/education.md)\n", "profile/education.md": "# Education\n\nNot available in this profile representation.\n" },
  },
};

async function withMcp(responseBody, run, status = 200) {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify(responseBody), { status });
  };
  const server = createOrbitServer("synthetic-key");
  const client = new Client({ name: "markdown-test", version: "1" });
  const [ct, st] = InMemoryTransport.createLinkedPair();
  try {
    await Promise.all([server.connect(st), client.connect(ct)]);
    await client.listTools();
    await run(client, calls);
  } finally {
    globalThis.fetch = originalFetch;
    await Promise.all([client.close(), server.close()]);
  }
}

test("get_profile advertises Markdown and returns real text blocks plus the intact package and billing", async () => {
  await withMcp(body, async (client, calls) => {
    const { tools } = await client.listTools();
    const tool = tools.find(tool => tool.name === "get_profile");
    assert.deepEqual(tool.inputSchema.properties.format.enum, ["json", "markdown"]);
    assert.equal(tool.outputSchema.type, "object");
    assert.ok(tool.outputSchema.properties.markdown);
    const result = await client.callTool({ name: "get_profile", arguments: { profile_id: "alias/with space", format: "markdown" } });
    assert.notEqual(result.isError, true);
    assert.deepEqual(result.structuredContent, body);
    assert.deepEqual(profileOutputSchema.parse(result.structuredContent), body);
    assert.deepEqual(JSON.parse(result.content[0].text), { profile_id: body.profile_id, generation_level: null, billing, format: "markdown", manifest: body.markdown.manifest });
    assert.equal(result.content.length, body.markdown.manifest.length + 1);
    for (let i = 0; i < body.markdown.manifest.length; i++) {
      const path = body.markdown.manifest[i].path;
      assert.equal(result.content[i + 1].text, `File: ${path}\n\n${body.markdown.files[path]}`);
    }
    assert.equal(calls.length, 1);
    assert.match(calls[0].url, /\/v3\/enrich\/alias%2Fwith%20space\?format=markdown$/);
    assert.equal(calls[0].init.headers.Authorization, "Bearer synthetic-key");
    assert.match(calls[0].init.headers["Idempotency-Key"], /^[0-9a-f-]{36}$/);
    assert.equal(calls[0].init.method, undefined);
  });
});

test("default and explicit JSON retain the same text, structured content, and URL", async () => {
  const json = { profile_id: "canonical", generation_level: 2, profile: { sections: { socials: { items: [] } } }, billing };
  await withMcp(json, async (client, calls) => {
    for (const args of [{ profile_id: "canonical" }, { profile_id: "canonical", format: "json" }]) {
      const result = await client.callTool({ name: "get_profile", arguments: args });
      assert.deepEqual(result.structuredContent, json);
      assert.deepEqual(JSON.parse(result.content[0].text), json);
      assert.equal(result.content.length, 1);
    }
    assert(calls.every(call => call.url.endsWith("/v3/enrich/canonical")));
    assert.notEqual(calls[0].init.headers["Idempotency-Key"], calls[1].init.headers["Idempotency-Key"]);
  });
});

test("Markdown read retries preserve one billing key and never search or regenerate", async () => {
  const calls = [];
  const responses = [503, 429, 200];
  const client = new OrbitV3Client({ apiKey: "synthetic", baseUrl: "https://orbit.test", sleepImpl: async () => {}, fetchImpl: async (url, init) => {
    calls.push({ url: String(url), init });
    const status = responses.shift();
    return new Response(JSON.stringify(status === 200 ? body : { error: "retry" }), { status, headers: { "retry-after": "0" } });
  } });
  assert.deepEqual(await client.getProfile("canonical", "markdown"), body);
  assert.equal(calls.length, 3);
  assert(calls.every(call => call.url === calls[0].url && call.init.headers["Idempotency-Key"] === calls[0].init.headers["Idempotency-Key"] && !call.init.body && !call.init.method));
});

for (const status of [401, 403, 402]) {
  test(`Markdown preserves HTTP ${status} denial as a text-only tool error`, async () => {
    await withMcp({ error: { code: "denied", message: "private-upstream-marker" } }, async (client, calls) => {
      const result = await client.callTool({ name: "get_profile", arguments: { profile_id: "canonical", format: "markdown" } });
      assert.equal(result.isError, true);
      assert.equal(result.structuredContent, undefined);
      assert.equal(calls.length, 1);
      assert.doesNotMatch(result.content[0].text, /private-upstream-marker/);
    }, status);
  });
}

for (const corrupt of [
  { ...body, markdown: { ...body.markdown, manifest: [{ path: "../escape.md", title: "Escape", status: "available" }] } },
  { ...body, markdown: { ...body.markdown, files: { "user.md": "private-upstream-marker" } } },
  { ...body, markdown: { ...body.markdown, manifest: [...body.markdown.manifest, body.markdown.manifest[0]] } },
  { ...body, markdown: { ...body.markdown, files: { ...body.markdown.files, "extra.md": "private-upstream-marker" } } },
  { profile_id: "canonical", generation_level: 2, profile: {} },
]) {
  test("malformed or mismatched upstream Markdown is rejected without publishing text", async () => {
    await withMcp(corrupt, async client => {
      const result = await client.callTool({ name: "get_profile", arguments: { profile_id: "canonical", format: "markdown" } });
      assert.equal(result.isError, true);
      assert.equal(result.structuredContent, undefined);
      assert.match(result.content[0].text, /invalid Markdown profile package/);
      assert.doesNotMatch(result.content[0].text, /private-upstream-marker/);
    });
  });
}

test("invalid format fails input validation before any API call", async () => {
  await withMcp(body, async (client, calls) => {
    const result = await client.callTool({ name: "get_profile", arguments: { profile_id: "canonical", format: "md" } });
    assert.equal(result.isError, true);
    assert.equal(calls.length, 0);
  });
});
