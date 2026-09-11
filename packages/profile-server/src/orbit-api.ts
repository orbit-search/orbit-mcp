import { randomUUID } from "node:crypto";
import { billingSchema, creditUsageOutputSchema, type OperationBilling } from "./billing.js";

const DEFAULT_BASE_URL = "https://api.orbitsearch.com";
const TERMINAL_SEARCH_STATUSES = new Set(["completed", "completed_with_errors", "failed"]);

type JsonObject = Record<string, unknown>;
type ProfileDepth = "partial" | "full";
type Sleep = (milliseconds: number) => Promise<void>;

export interface ProfileSearchResult {
  profile_id: string;
  status: "enriching" | "ready" | "failed";
  generation_level: number | null;
  profile?: JsonObject;
  failure?: { code: string; message: string; retryable: boolean };
}

export interface ProfileSearchResponse {
  billing?: OperationBilling;
  search_id: string;
  request_id: string;
  status: "running" | "completed" | "completed_with_errors" | "failed";
  results: ProfileSearchResult[];
}

export interface ProfileReadResponse {
  billing?: OperationBilling;
  search_billing?: OperationBilling;
  profile_id: string;
  generation_level: number;
  profile: JsonObject;
}

export interface ProfileOrbitClientOptions {
  apiKey: string;
  baseUrl?: string;
  pollTimeoutMs?: number;
  fetchImpl?: typeof fetch;
  sleepImpl?: Sleep;
}

class OrbitApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: unknown,
    readonly retryAfterMs?: number,
  ) {
    super(message);
  }
}

export class ProfileResolutionError extends Error {
  constructor(error: unknown, readonly search_billing?: OperationBilling) {
    super(error instanceof Error ? error.message : "Orbit profile resolution failed");
  }
}

function retryAfterMs(response: Response): number | undefined {
  const value = response.headers.get("retry-after");
  if (!value) return undefined;
  if (/^\d+(\.\d+)?$/.test(value)) return Number(value) * 1_000;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? undefined : Math.max(0, parsed - Date.now());
}

function searchTarget(query: string): { query?: string; signals?: Record<string, unknown>; candidateDiscovery: boolean } {
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(query)) {
    return { signals: { email: query }, candidateDiscovery: true };
  }
  try {
    const url = new URL(query);
    if (["http:", "https:"].includes(url.protocol)) {
      const linkedin = url.hostname === "linkedin.com" || url.hostname.endsWith(".linkedin.com");
      return {
        signals: linkedin ? { linkedin_url: query } : { urls: [query] },
        candidateDiscovery: true,
      };
    }
  } catch {
    // Not a URL; continue with phone or plain-query classification.
  }
  const digits = query.replace(/\D/g, "");
  if (/^\+?[\d\s().-]+$/.test(query) && digits.length >= 7) {
    return { signals: { phone: query }, candidateDiscovery: false };
  }
  return { query, candidateDiscovery: true };
}

export class ProfileOrbitClient {
  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly pollTimeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly sleepImpl: Sleep;

  constructor(options: ProfileOrbitClientOptions) {
    this.apiKey = options.apiKey.trim();
    if (!this.apiKey) throw new Error("An Orbit Developer API key is required");
    this.baseUrl = (options.baseUrl ?? process.env.ORBIT_API_URL ?? DEFAULT_BASE_URL).replace(/\/$/, "");
    this.pollTimeoutMs = options.pollTimeoutMs ?? Number(process.env.ORBIT_POLL_TIMEOUT_MS || 5 * 60_000);
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.sleepImpl = options.sleepImpl ?? ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)));
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
    });
    const text = await response.text();
    let body: unknown;
    try {
      body = text ? JSON.parse(text) : undefined;
    } catch {
      body = text;
    }
    if (!response.ok) {
      const message = response.status === 402
        ? "Orbit requires additional credits for this operation (HTTP 402). Review your balance at https://developer.orbitsearch.com/dashboard/billing before retrying."
        : `Orbit API request failed with HTTP ${response.status}`;
      throw new OrbitApiError(message, response.status, body, retryAfterMs(response));
    }
    return body as T;
  }

  private async requestWithRetry<T>(path: string, init: RequestInit = {}): Promise<T> {
    for (let attempt = 0; attempt < 5; attempt += 1) {
      try {
        return await this.request<T>(path, init);
      } catch (error) {
        if (!(error instanceof OrbitApiError)) throw error;
        const retryable = error.status === 429 || error.status >= 500;
        if (!retryable || attempt === 4) throw error;
        const backoffMs = Math.min(10_000, 500 * 2 ** attempt);
        await this.sleepImpl(error.retryAfterMs ?? Math.round(backoffMs * (0.8 + Math.random() * 0.4)));
      }
    }
    throw new Error("Unreachable retry state");
  }

  async searchAndWait(query: string, profileDepth: ProfileDepth, requestId?: string): Promise<ProfileSearchResponse> {
    const cleanQuery = query.trim();
    if (!cleanQuery) throw new Error("A profile identity query is required");
    const target = searchTarget(cleanQuery);
    const request = {
      request_id: requestId?.trim() || randomUUID(),
      ...(target.query ? { query: target.query } : {}),
      ...(target.signals ? { signals: target.signals } : {}),
      candidate_discovery: target.candidateDiscovery,
      profile_depth: profileDepth,
      include_profile: true,
      limit: 1,
    };
    const initial = await this.requestWithRetry<ProfileSearchResponse>("/v3/search", {
      method: "POST",
      body: JSON.stringify(request),
    });
    if (TERMINAL_SEARCH_STATUSES.has(initial.status)) return initial;

    const startedAt = Date.now();
    let attempt = 0;
    let current = initial;
    while (!TERMINAL_SEARCH_STATUSES.has(current.status)) {
      if (Date.now() - startedAt >= this.pollTimeoutMs) throw new Error(`Orbit search timed out in state ${current.status}`);
      const backoffMs = Math.min(10_000, 500 * 2 ** attempt);
      await this.sleepImpl(Math.round(backoffMs * (0.8 + Math.random() * 0.4)));
      current = await this.requestWithRetry<ProfileSearchResponse>(`/v3/search/${encodeURIComponent(initial.search_id)}`);
      attempt += 1;
    }
    return current;
  }

  getProfile(profileId: string): Promise<ProfileReadResponse> {
    // One key per logical paid read, retained by all transport retries.
    return this.requestWithRetry(`/v3/enrich/${encodeURIComponent(profileId)}`, {
      headers: { "Idempotency-Key": randomUUID() },
    });
  }

  async getCreditUsage() {
    return creditUsageOutputSchema.parse(await this.requestWithRetry<unknown>("/v3/credits/usage"));
  }

  async resolveProfile(query: string, profileDepth: ProfileDepth, requestId?: string): Promise<ProfileReadResponse | { message: string; search_billing?: OperationBilling }> {
    const search = await this.searchAndWait(query, profileDepth, requestId);
    const searchBilling = search.billing ? { search_billing: billingSchema.parse(search.billing) } : {};
    const result = search.results.find((item) => item.status === "ready");
    if (!result) {
      const failure = search.results.find((item) => item.failure)?.failure;
      if (search.status === "failed" || failure) throw new ProfileResolutionError(new Error(failure?.message || "Orbit profile resolution failed"), searchBilling.search_billing);
      return { message: "No matching person found.", ...searchBilling };
    }
    if (result.profile) {
      return {
        profile_id: result.profile_id,
        generation_level: result.generation_level ?? 0,
        profile: result.profile,
        ...searchBilling,
      };
    }
    try {
      return { ...await this.getProfile(result.profile_id), ...searchBilling };
    } catch (error) {
      throw new ProfileResolutionError(error, searchBilling.search_billing);
    }
  }
}
