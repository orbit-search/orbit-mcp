/** Exercise the HTTP transport boundary without live credentials or API work. */
import assert from "node:assert/strict";
import test from "node:test";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

async function stopChild(child) {
  if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
  const exited = once(child, "exit");
  child.kill();
  await exited;
}

test("HTTP cleanup returns after a startup failure or signal exit", { timeout: 5000 }, async () => {
  for (const signal of [false, true]) {
    const child = spawn(process.execPath, ["-e", signal ? "setInterval(() => {}, 1000)" : "process.exit(1)"], { stdio: "ignore" });
    const exited = once(child, "exit");
    if (signal) child.kill();
    await exited;
    await stopChild(child);
  }
});

test("HTTP setup accepts raw keys, retains Bearer sessions, and rejects key changes", async () => {
  const child = spawn(process.execPath, ["build/http.js"], {
    cwd: new URL("..", import.meta.url),
    env: { ...process.env, PORT: "0" }, stdio: ["ignore", "ignore", "pipe"],
  });
  let client;
  let transport;
  try {
    const url = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("HTTP startup timed out")), 10_000);
      child.once("error", error => { clearTimeout(timeout); reject(error); });
      child.once("exit", () => { clearTimeout(timeout); reject(new Error("HTTP process exited")); });
      child.stderr.on("data", chunk => {
        const match = /http:\/\/localhost:(\d+)\/mcp/.exec(String(chunk));
        if (match) { clearTimeout(timeout); resolve(new URL(`http://localhost:${match[1]}/mcp`)); }
      });
    });
    const missing = await fetch(url);
    assert.equal(missing.status, 401);
    assert.match((await missing.json()).error.message, /Create a key/);
    client = new Client({ name: "http-auth-test", version: "1.0.0" });
    transport = new StreamableHTTPClientTransport(url, { requestInit: { headers: { Authorization: "sk_orb_test" } } });
    await client.connect(transport);
    assert.equal((await client.listTools()).tools.length, 3);
    for (const [authorization, expected] of [["Bearer sk_orb_test", 200], ["sk_orb_other", 403]]) {
      const response = await fetch(url, {
        method: "POST",
        headers: { Authorization: authorization, "Mcp-Session-Id": transport.sessionId, "Content-Type": "application/json", Accept: "application/json, text/event-stream" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 12, method: "tools/list" }),
      });
      assert.equal(response.status, expected);
      const body = await response.text();
      if (expected === 403) assert.match(body, /Reconnect/);
    }
  } finally {
    try {
      if (transport?.sessionId) await transport.terminateSession();
      await client?.close();
    } finally {
      await stopChild(child);
    }
  }
});
