/** Only the pinned public OAuth gateway may delegate directory access per request. */
import { AsyncLocalStorage } from "node:async_hooks";
import { createHash } from "node:crypto";
import { createRemoteJWKSet, jwtVerify } from "jose";

export type DirectoryAuth = { userToken: string; expiresAt: number; scopes: string[]; appId: string };
export const directoryContext = new AsyncLocalStorage<DirectoryAuth | undefined>();
export const delegationHeader = "x-orbit-mcp-delegation";
const issuer = "https://api.orbitsearch.com/mcp-auth";
const audience = "https://orbit-mcp-k7iyipqilq-uc.a.run.app/mcp";
const jwks = createRemoteJWKSet(new URL(`${issuer}/.well-known/mcp-delegation-jwks.json`), { cacheMaxAge: 30_000, cooldownDuration: 5_000, timeoutDuration: 5_000 });
const hash = (value: string | Buffer) => createHash("sha256").update(value).digest("base64url");

export async function verifyDirectoryDelegation(
  token: string, request: { apiKey: string; method: string; body: Buffer; sessionId: string },
  // Explicit injection for local tests only; production never trusts a URL/header/env key.
  keySet: Parameters<typeof jwtVerify>[1] = jwks
): Promise<DirectoryAuth> {
  const { payload } = await jwtVerify(token, keySet, {
    issuer, audience, algorithms: ["EdDSA"], typ: "orbit-mcp-delegation+jwt",
    requiredClaims: ["exp", "iat"], maxTokenAge: "30s"
  });
  if (typeof payload.exp !== "number" || typeof payload.iat !== "number" || payload.exp - payload.iat > 30
    || payload.iat > Date.now() / 1000
    || payload.api_key_hash !== hash(request.apiKey) || payload.method !== request.method
    || payload.body_hash !== hash(request.body) || payload.mcp_session_id !== request.sessionId
    || typeof payload.user_token !== "string" || !payload.user_token
    || typeof payload.user_expires_at !== "number" || !Number.isFinite(payload.user_expires_at) || payload.user_expires_at <= Date.now()
    || typeof payload.app_id !== "string" || !payload.app_id
    || !Array.isArray(payload.scopes) || !payload.scopes.length
    || !payload.scopes.every((scope): scope is string => typeof scope === "string" && ["directories.read", "directories.write"].includes(scope))) {
    throw new Error("invalid_directory_delegation");
  }
  return { userToken: payload.user_token, expiresAt: Math.min(payload.user_expires_at, payload.exp * 1000), scopes: payload.scopes, appId: payload.app_id };
}
