/** Normalize HTTP API-key input without changing upstream credential validation. */
export const API_KEY_URL = "https://developer.orbitsearch.com/dashboard/keys";

/** Accept existing Bearer credentials and pasted Orbit keys from setup forms. */
export function extractApiKey(authorization: string | undefined): string | null {
  const value = authorization?.trim();
  if (!value) return null;
  const bearer = /^Bearer[ \t]+([^\s,]+)$/i.exec(value);
  if (bearer) return bearer[1];
  return /^sk_orb_[A-Za-z0-9_-]+$/.test(value) ? value : null;
}

export const API_KEY_HELP = `An Orbit API key is required. Paste your sk_orb_ key or use Authorization: Bearer <key>. Create a key at ${API_KEY_URL}.`;
