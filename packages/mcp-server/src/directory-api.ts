/** Fixed directory routes use the gateway-verified user session, never the search key. */
import type { DirectoryAuth } from "./directory-auth.js";

export class DirectoryApiError extends Error {
  constructor(public readonly status: number) { super("Orbit directory request failed"); }
}

export async function requestDirectory(path: string, options: { method: string; body?: unknown; form?: FormData }, auth: DirectoryAuth): Promise<unknown> {
  const base = process.env.ORBIT_API_URL || "https://api.orbitsearch.com";
  const response = await fetch(`${base.replace(/\/$/, "")}${path}`, {
    method: options.method,
    headers: { Authorization: `Bearer ${auth.userToken}`, "App-Id": auth.appId, "App-Version": "1.1.0", ...(!options.form ? { "Content-Type": "application/json" } : {}) },
    body: options.form ?? (options.body === undefined ? undefined : JSON.stringify(options.body)),
    redirect: "error", signal: AbortSignal.timeout(25_000)
  });
  // Never echo upstream error bodies or retry an ambiguous write automatically.
  if (!response.ok) { await response.body?.cancel(); throw new DirectoryApiError(response.status); }
  return response.json();
}
