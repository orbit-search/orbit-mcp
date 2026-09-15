import { randomUUID } from "node:crypto";
import { API_KEY_URL } from "./api-key-auth.js";
import { creditUsageOutputSchema } from "./output-schemas.js";
import type {
  EnrichOperation,
  EnrichResponse,
  JsonObject,
  ProfileReadResponse,
  SearchInput,
  SearchResponse,
  SearchSignals,
  PopulationInput,
  PopulationQuote,
} from "./types.js";

const DEFAULT_BASE_URL = "https://api.orbitsearch.com";
const DEFAULT_POLL_TIMEOUT_MS = 5 * 60_000;
const TERMINAL_SEARCH_STATUSES = new Set(["completed", "completed_with_errors", "failed"]);
const TERMINAL_ENRICH_STATUSES = new Set(["completed", "failed"]);

type Sleep = (milliseconds: number) => Promise<void>;

export interface OrbitV3ClientOptions {
  apiKey: string;
  baseUrl?: string;
  pollTimeoutMs?: number;
  fetchImpl?: typeof fetch;
  sleepImpl?: Sleep;
}

export class OrbitApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: unknown,
    readonly retryAfterMs?: number,
  ) {
    super(message);
    this.name = "OrbitApiError";
  }
}

function retryAfterMs(response: Response): number | undefined {
  const value = response.headers.get("retry-after");
  if (!value) return undefined;
  if (/^\d+(\.\d+)?$/.test(value)) return Number(value) * 1_000;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? undefined : Math.max(0, parsed - Date.now());
}

function cleanSignals(signals?: SearchSignals): SearchSignals | undefined {
  if (!signals) return undefined;
  const clean: SearchSignals = {};
  if (signals.email?.trim()) clean.email = signals.email.trim();
  if (signals.linkedin_url?.trim()) clean.linkedin_url = signals.linkedin_url.trim();
  const usernames = signals.usernames?.map((value) => value.trim()).filter(Boolean);
  if (usernames?.length) clean.usernames = usernames;
  const urls = signals.urls?.map((value) => value.trim()).filter(Boolean);
  if (urls?.length) clean.urls = urls;
  if (signals.address?.trim()) clean.address = signals.address.trim();
  if (signals.phone?.trim()) clean.phone = signals.phone.trim();
  return Object.keys(clean).length ? clean : undefined;
}

export class OrbitV3Client {
  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly pollTimeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly sleepImpl: Sleep;

  constructor(options: OrbitV3ClientOptions) {
    this.apiKey = options.apiKey.trim();
    if (!this.apiKey) throw new Error("An Orbit Developer API key is required");
    this.baseUrl = (options.baseUrl ?? process.env.ORBIT_API_URL ?? DEFAULT_BASE_URL).replace(/\/$/, "");
    this.pollTimeoutMs = options.pollTimeoutMs ?? Number(process.env.ORBIT_POLL_TIMEOUT_MS || DEFAULT_POLL_TIMEOUT_MS);
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
      const apiError = body && typeof body === "object" && "error" in body ? body.error : undefined;
      const invalidKey = response.status === 403 && apiError && typeof apiError === "object" && "code" in apiError && apiError.code === "invalid_api_key";
      const message = response.status === 401 || invalidKey
        ? `Orbit rejected this API key (HTTP ${response.status}). It may be invalid, expired, or revoked. Check your key at ${API_KEY_URL}, then reconnect.`
        : response.status === 403
          ? path === "/v3/credits/usage"
            ? `Orbit denied this credit-usage lookup (HTTP 403). Check the connected key and its account access at ${API_KEY_URL}.`
            : `Orbit denied this operation (HTTP 403). Check the key's permissions at ${API_KEY_URL}: profile reads need profile:read; search and enrichment need search:read, plus profile:read for returned profiles. If scopes are correct, check your account access.`
          : response.status === 402
            ? "Orbit requires additional credits for this operation (HTTP 402). Review your balance at https://developer.orbitsearch.com/dashboard/billing before retrying."
            : `Orbit API request failed with HTTP ${response.status}`;
      throw new OrbitApiError(message, response.status, body, retryAfterMs(response));
    }
    return body as T;
  }

  private async requestWithRetry<T>(path: string, init: RequestInit = {}, maxAttempts = 5): Promise<T> {
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      try {
        return await this.request<T>(path, init);
      } catch (error) {
        if (!(error instanceof OrbitApiError)) throw error;
        const retryable = error.status === 429 || error.status >= 500;
        if (!retryable || attempt === maxAttempts - 1) throw error;
        const backoffMs = Math.min(10_000, 500 * 2 ** attempt);
        const jitteredMs = Math.round(backoffMs * (0.8 + Math.random() * 0.4));
        await this.sleepImpl(error.retryAfterMs ?? jitteredMs);
      }
    }
    throw new Error("Unreachable retry state");
  }

  private async poll<T extends { status: string }>(initial: T, load: () => Promise<T>, terminalStatuses: ReadonlySet<string>): Promise<T> {
    const startedAt = Date.now();
    let attempt = 0;
    let current = initial;
    while (!terminalStatuses.has(current.status)) {
      if (Date.now() - startedAt >= this.pollTimeoutMs) {
        throw new Error(`Orbit operation timed out in state ${current.status}`);
      }
      const backoffMs = Math.min(10_000, 500 * 2 ** attempt);
      await this.sleepImpl(Math.round(backoffMs * (0.8 + Math.random() * 0.4)));
      current = await load();
      attempt += 1;
    }
    return current;
  }

  async searchAndWait(input: SearchInput): Promise<SearchResponse> {
    const query = input.query?.trim() || undefined;
    const signals = cleanSignals(input.signals);
    if (!query && !signals) throw new Error("search_people requires a query or at least one identity signal");

    const request: JsonObject = {
      request_id: input.request_id?.trim() || randomUUID(),
      ...(query ? { query } : {}),
      ...(signals ? { signals } : {}),
      candidate_discovery: signals?.address || signals?.phone ? false : (input.candidate_discovery ?? false),
      profile_depth: input.profile_depth ?? "partial",
      include_profile: true,
      limit: input.limit ?? 10,
    };
    const serializedBody = JSON.stringify(request);
    const initial = await this.requestWithRetry<SearchResponse>("/v3/search", { method: "POST", body: serializedBody });
    if (TERMINAL_SEARCH_STATUSES.has(initial.status)) return initial;
    return this.poll(
      initial,
      () => this.requestWithRetry<SearchResponse>(`/v3/search/${encodeURIComponent(initial.search_id)}`),
      TERMINAL_SEARCH_STATUSES,
    );
  }

  /** The latest snapshot of a search, without starting new work. */
  getSearch(searchId: string): Promise<SearchResponse> {
    return this.requestWithRetry(`/v3/search/${encodeURIComponent(searchId)}`);
  }

  private static populationBody(input: PopulationInput, withRequestId: boolean): JsonObject {
    const name = input.population.name.trim();
    if (!name) throw new Error("population.name is required");
    return {
      ...(withRequestId ? { request_id: input.request_id?.trim() || randomUUID() } : {}),
      population: { kind: input.population.kind, id: input.population.id.trim(), name },
      size: typeof input.size === "number" ? input.size : null,
      profile_depth: input.profile_depth ?? "partial",
    };
  }

  /** The whole price of a population search. Starts no work and reserves no credits. */
  quotePopulation(input: PopulationInput): Promise<PopulationQuote> {
    return this.requestWithRetry("/v3/search/populations/quote", { method: "POST", body: JSON.stringify(OrbitV3Client.populationBody(input, false)) });
  }

  /**
   * Starts a population search and returns its first snapshot at once. The search keeps running
   * in Orbit and adds people as it finds them; read it again with getSearch.
   */
  startPopulation(input: PopulationInput): Promise<SearchResponse> {
    return this.requestWithRetry("/v3/search/populations", { method: "POST", body: JSON.stringify(OrbitV3Client.populationBody(input, true)) });
  }

  getProfile(profileId: string): Promise<ProfileReadResponse> {
    // One key per logical paid read, retained by all transport retries.
    return this.requestWithRetry(`/v3/enrich/${encodeURIComponent(profileId)}`, {
      headers: { "Idempotency-Key": randomUUID() },
    });
  }

  async getCreditUsage() {
    // Subject selection stays entirely upstream; strip any unexpected identity fields.
    return creditUsageOutputSchema.parse(await this.requestWithRetry<unknown>("/v3/credits/usage"));
  }

  async enrichAndWait(profileId: string, operation: EnrichOperation, requestId?: string): Promise<EnrichResponse> {
    const request = {
      request_id: requestId?.trim() || randomUUID(),
      operation,
      include_profile: true,
    };
    const serializedBody = JSON.stringify(request);
    const initial = await this.requestWithRetry<EnrichResponse>(`/v3/enrich/${encodeURIComponent(profileId)}`, {
      method: "POST",
      body: serializedBody,
    });
    if (TERMINAL_ENRICH_STATUSES.has(initial.status)) return initial;
    if (!initial.links?.status) throw new Error(`Orbit enrich response missing status link while in state ${initial.status}`);
    return this.poll(
      initial,
      () => this.requestWithRetry<EnrichResponse>(`/v3/enrich/requests/${encodeURIComponent(initial.request_id)}`),
      TERMINAL_ENRICH_STATUSES,
    );
  }
}
