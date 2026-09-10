/** Real HTTP tests: delegation is verified afresh even within an existing MCP session. */
import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "node:http";
import { once } from "node:events";
import { createHash } from "node:crypto";
import { SignJWT, createLocalJWKSet, exportJWK, generateKeyPair } from "jose";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { createHttpApp } from "../build/http-app.js";
import { verifyDirectoryDelegation } from "../build/directory-auth.js";
import { directoryTools } from "../build/directory-tools.js";
const id = "00000000-0000-4000-8000-000000000001";
const fixtures = { organizationId: id, directoryId: id, personId: "person/with?reserved#chars", sourceId: id, watcherId: id, grantId: id, query: "engineers", status: "canceled",
  name: "Test directory", description: "Fixture", slug: "fixture", visibility: "private", profile_id: id, person_ids: [id], removal_source_id: id,
  request_id: id, selection: { person_ids: [id] }, principal_type: "user", principal_id: id, permissions: ["search"], orbit_ids: [id],
  interval_seconds: 3600, enabled: false, csv_text: "name,email\nSample Person,sample@example.com", filename: "fixture.csv", idempotency_key: id,
  headers: ["name", "email"], operator: "and", filters: [{ type: "company", id: "known-company" }], async: true, emails: ["sample@example.com"] };
const argsFor = def => Object.fromEntries(Object.keys(def.input).filter(key => fixtures[key] !== undefined).map(key => [key, fixtures[key]]));

test("directory tools enforce request-local scopes and route all 38 operations through user auth", async () => {
  const requests = [];
  let status = 200;
  let responseOverride;
  let emptyBody = false;
  const upstream = createServer(async (req, res) => {
    let body = "";
    for await (const chunk of req) body += chunk;
    requests.push({ url: req.url, method: req.method, headers: req.headers, body });
    res.writeHead(status, { "content-type": "application/json" }).end(emptyBody ? undefined : JSON.stringify(responseOverride ?? (status === 200 ? { status: "success", payload: { queued: true } } : { message: "synthetic-private-user-token" })));
  }).listen(0, "127.0.0.1");
  await once(upstream, "listening");
  const savedHost = process.env.ORBIT_API_URL;
  process.env.ORBIT_API_URL = `http://127.0.0.1:${upstream.address().port}`;
  const { privateKey, publicKey } = await generateKeyPair("EdDSA");
  const keys = createLocalJWKSet({ keys: [{ ...await exportJWK(publicKey), kid: "fixture" }] });
  const app = createHttpApp((token, request) => verifyDirectoryDelegation(token, request, keys));
  const listener = app.listen(0, "127.0.0.1");
  await once(listener, "listening");
  const url = new URL(`http://127.0.0.1:${listener.address().port}/mcp`);
  const hash = value => createHash("sha256").update(value).digest("base64url");
  let scopes;
  let expired = false;
  let forged = false;
  const client = new Client({ name: "directory-http", version: "1" });
  const transport = new StreamableHTTPClientTransport(url, {
    requestInit: { headers: { Authorization: "Bearer sk_orb_fixture" } },
    fetch: async (input, init) => {
      const headers = new Headers(init?.headers);
      if (scopes) {
        const now = Math.floor(Date.now() / 1000);
        const proof = await new SignJWT({
          user_token: "synthetic-private-user-token", user_expires_at: expired ? Date.now() - 1 : Date.now() + 3600000,
          scopes, app_id: "fixture-app", api_key_hash: hash("sk_orb_fixture"),
          method: init?.method || "GET", body_hash: hash(init?.body || ""), mcp_session_id: headers.get("mcp-session-id") || ""
        }).setProtectedHeader({ alg: "EdDSA", kid: "fixture", typ: "orbit-mcp-delegation+jwt" })
          .setIssuer("https://api.orbitsearch.com/mcp-auth").setAudience("https://orbit-mcp-k7iyipqilq-uc.a.run.app/mcp")
          .setIssuedAt(now).setExpirationTime(now + 30).sign(privateKey);
        headers.set("x-orbit-mcp-delegation", forged ? proof + "tampered" : proof);
      }
      return fetch(input, { ...init, headers });
    }
  });
  try {
    await client.connect(transport); // Start key-only; subsequent grants need no new MCP session.
    const catalog = (await client.listTools()).tools;
    assert.equal(catalog.length, 41);
    assert.equal(directoryTools.length, 38);
    const payloadSchema = name => catalog.find(tool => tool.name === name).outputSchema.properties.data.properties.payload;
    assert.equal(payloadSchema("count_directory_people").properties.count.type, "number");
    const directorySchema = catalog.find(tool => tool.name === "list_directories").outputSchema;
    const nameField = payloadSchema("list_directories").properties.directories.items.properties.name;
    const resolvedName = nameField.$ref
      ? nameField.$ref.slice(2).split("/").reduce((value, key) => value[key.replaceAll("~1", "/").replaceAll("~0", "~")], directorySchema)
      : nameField;
    assert.equal(resolvedName.type, "string");
    assert.equal(payloadSchema("get_directory_source_status").properties.backfills.type, "array");
    for (const permission of [undefined, "directories.read", "directories.write"]) {
      scopes = permission ? [permission] : undefined;
      for (const def of directoryTools) {
        const before = requests.length;
        const result = await client.callTool({ name: def.name, arguments: argsFor(def) });
        const allowed = permission === (def.read ? "directories.read" : "directories.write");
        assert.equal(result.isError === true, !allowed, def.name);
        assert.equal(requests.length - before, allowed ? 1 : 0, def.name);
        const tool = catalog.find(t => t.name === def.name);
        assert.equal(tool.annotations.readOnlyHint, def.read);
        if (allowed) {
          assert.equal(requests.at(-1).headers.authorization, "Bearer synthetic-private-user-token");
          assert.equal(requests.at(-1).headers["app-id"], "fixture-app");
          assert.equal(requests.at(-1).method, def.method);
          assert.ok(!JSON.stringify(result).includes("synthetic-private-user-token"));
        } else assert.ok(result._meta?.["mcp/www_authenticate"]);
      }
    }
    scopes = ["directories.read", "directories.write"];
    const call = async (name, args) => { const result = await client.callTool({ name, arguments: args }); assert.notEqual(result.isError, true, name); return requests.at(-1); };
    const person = await call("get_directory_person", { organizationId: id, directoryId: id, personId: fixtures.personId });
    assert.equal(person.url, `/v2/organizations/${id}/directories/${id}/people/person%2Fwith%3Freserved%23chars`);
    const search = await call("search_directory", { organizationId: id, directoryId: id, query: "engineers" });
    assert.deepEqual(JSON.parse(search.body), { query: "engineers", numUsers: 20, searchScope: { type: "directory", directoryId: id } });
    const refresh = await call("refresh_directory_profiles", { organizationId: id, directoryId: id, request_id: id, selection: { all: true } });
    assert.deepEqual(JSON.parse(refresh.body), { request_id: id, all: true });
    const csv = await call("upload_directory_csv", argsFor(directoryTools.find(d => d.name === "upload_directory_csv")));
    assert.match(csv.headers["content-type"], /multipart\/form-data; boundary=/);
    assert.ok(csv.body.includes(fixtures.csv_text));
    assert.ok(csv.body.includes(id));
    await call("upload_directory_csv", { organizationId: id, directoryId: id, idempotency_key: id, csv_text: "x".repeat(524288) });
    let before = requests.length;
    for (const [name, args] of [
      ["upload_directory_csv", { idempotency_key: id, csv_text: "é".repeat(300000) }],
      ["update_directory", {}], ["update_directory_watcher", { watcherId: id }],
      ["check_directory_membership", {}], ["restore_directory_people", {}],
      ["refresh_directory_profiles", { request_id: id, selection: { all: true, person_ids: [id] } }],
      ["get_directory", { organizationId: "../escape" }]
    ]) assert.equal((await client.callTool({ name, arguments: { organizationId: id, directoryId: id, ...args } })).isError, true, name);
    assert.equal(requests.length, before);
    status = 403;
    const denied = await client.callTool({ name: "create_directory", arguments: { organizationId: id, name: "Fixture" } });
    assert.equal(denied.isError, true);
    assert.ok(!JSON.stringify(denied).includes("synthetic-private-user-token"));
    assert.equal(requests.length, before + 1, "no automatic retries");
    status = 200;
    for (const envelope of [
      { status: "failed", error: { code: "import_failed", message: "Some rows failed" }, payload: { source_status: "failed", source: { id, error: "Invalid rows", validation_errors: [{ row: 2, reasons: ["Missing identity"] }] } } },
      { status: "success", payload: { source_status: "failed", source: { id, error: "Import stopped" }, backfills: [] } }
    ]) {
      responseOverride = envelope;
      const result = await client.callTool({ name: "get_directory_source_status", arguments: { organizationId: id, directoryId: id, sourceId: id } });
      assert.equal(result.isError, true);
      assert.deepEqual(result.structuredContent, { data: envelope });
      assert.deepEqual(JSON.parse(result.content[0].text), envelope);
    }
    responseOverride = { status: "success", payload: { refreshes: [{ request_id: id, status: "failed", error: "A profile failed", runs: { completed: 2, failed: 1 }, extra_diagnostic: "preserved" }] } };
    const history = await client.callTool({ name: "list_directory_refreshes", arguments: { organizationId: id, directoryId: id } });
    assert.notEqual(history.isError, true, "a history list containing failures is still a successful read");
    assert.deepEqual(history.structuredContent.data, responseOverride);
    responseOverride = undefined;
    emptyBody = true;
    for (const code of [204, 200]) {
      status = code;
      const result = await client.callTool({ name: "delete_directory_watcher", arguments: { organizationId: id, directoryId: id, watcherId: id } });
      assert.notEqual(result.isError, true);
      assert.deepEqual(result.structuredContent, { data: {} });
    }
    emptyBody = false;
    status = 200;
    before = requests.length;
    expired = true;
    await assert.rejects(client.callTool({ name: "list_directory_organizations", arguments: {} }));
    expired = false; forged = true;
    await assert.rejects(client.callTool({ name: "list_directory_organizations", arguments: {} }));
    forged = false; scopes = undefined;
    assert.equal((await client.callTool({ name: "list_directory_organizations", arguments: {} })).isError, true, "previous grant cannot survive in the session");
    assert.equal(requests.length, before);
  } finally {
    scopes = undefined;
    await transport.terminateSession(); await client.close();
    listener.closeAllConnections(); upstream.closeAllConnections();
    await Promise.all([new Promise(resolve => listener.close(resolve)), new Promise(resolve => upstream.close(resolve))]);
    if (savedHost === undefined) delete process.env.ORBIT_API_URL; else process.env.ORBIT_API_URL = savedHost;
  }
});
