/** Exercise delegation trust and every request binding without production credentials. */
import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { SignJWT, createLocalJWKSet, exportJWK, generateKeyPair } from "jose";
import { verifyDirectoryDelegation } from "../build/directory-auth.js";

test("only current pinned-issuer, audience and request-bound delegations authorize", async () => {
  const { privateKey, publicKey } = await generateKeyPair("EdDSA");
  const keys = createLocalJWKSet({ keys: [{ ...await exportJWK(publicKey), kid: "fixture" }] });
  const hash = value => createHash("sha256").update(value).digest("base64url");
  const request = { apiKey: "sk_orb_fixture", method: "POST", body: Buffer.from('{"method":"tools/call"}'), sessionId: "session" };
  const now = Math.floor(Date.now() / 1000);
  const claims = { iss: "https://api.orbitsearch.com/mcp-auth", aud: "https://orbit-mcp-k7iyipqilq-uc.a.run.app/mcp", iat: now, exp: now + 30,
    api_key_hash: hash(request.apiKey), method: request.method, body_hash: hash(request.body), mcp_session_id: request.sessionId,
    user_token: "synthetic-session", user_expires_at: Date.now() + 3600000, app_id: "fixture", scopes: ["directories.read"] };
  const sign = (updates = {}, header = {}) => new SignJWT({ ...claims, ...updates }).setProtectedHeader({ alg: "EdDSA", kid: "fixture", typ: "orbit-mcp-delegation+jwt", ...header }).sign(privateKey);
  assert.deepEqual((await verifyDirectoryDelegation(await sign(), request, keys)).scopes, ["directories.read"]);
  for (const update of [{ iss: "https://evil.example" }, { aud: "https://chatgpt.orbitsearch.com" }, { exp: now - 1 }, { exp: now + 3600 }, { iat: now + 10 },
    { scopes: ["admin"] }, { scopes: [] }, { user_expires_at: Date.now() - 1 }, { user_token: "" }, { app_id: "" }]) {
    await assert.rejects(verifyDirectoryDelegation(await sign(update), request, keys));
  }
  for (const update of [{ apiKey: "sk_orb_other" }, { method: "DELETE" }, { body: Buffer.from("changed") }, { sessionId: "other-session" }]) {
    await assert.rejects(verifyDirectoryDelegation(await sign(), { ...request, ...update }, keys));
  }
  await assert.rejects(verifyDirectoryDelegation(await sign({}, { typ: "at+jwt" }), request, keys));
  const other = await generateKeyPair("EdDSA");
  const forged = await new SignJWT(claims).setProtectedHeader({ alg: "EdDSA", kid: "fixture", typ: "orbit-mcp-delegation+jwt" }).sign(other.privateKey);
  await assert.rejects(verifyDirectoryDelegation(forged, request, keys));
});
