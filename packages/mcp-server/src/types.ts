export type JsonObject = Record<string, unknown>;

export type ProfileDepth = "partial" | "full";
export type EnrichOperation = ProfileDepth | "regenerate";

export interface SearchSignals {
  email?: string;
  linkedin_url?: string;
  usernames?: string[];
  urls?: string[];
  address?: string;
  phone?: string;
}

export interface SearchInput {
  request_id?: string;
  query?: string;
  signals?: SearchSignals;
  candidate_discovery?: boolean;
  profile_depth?: ProfileDepth;
  limit?: number;
}

export interface Failure {
  code: string;
  message: string;
  retryable: boolean;
}

export interface SearchResult {
  profile_id: string;
  status: "enriching" | "ready" | "failed";
  generation_level: number | null;
  profile?: JsonObject;
  failure?: Failure;
}

export interface SearchResponse {
  search_id: string;
  request_id: string;
  status: "running" | "completed" | "completed_with_errors" | "failed";
  candidate_discovery: boolean;
  profile_depth: ProfileDepth;
  include_profile: boolean;
  results: SearchResult[];
  created_at: string;
  updated_at: string;
  links: { status: string };
}

export interface ProfileReadResponse {
  profile_id: string;
  generation_level: number;
  profile: JsonObject;
}

export interface EnrichResponse {
  request_id: string;
  profile_id: string;
  status: "running" | "completed" | "failed";
  operation: EnrichOperation;
  include_profile: boolean;
  generation_level?: number | null;
  profile?: JsonObject;
  failure?: Failure;
  links?: { status?: string; profile: string };
}
