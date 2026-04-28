export interface SearchRequestBody {
  query: string;
  numUsers: string;
  isManualInput: boolean;
  searchModel: string;
}

export interface RawSearchUser {
  userId: string;
  displayName?: string;
  username?: string;
  city?: string;
  age?: number;
  matchReason?: string | { reason: string };
  sourceCount?: number;
  [key: string]: unknown;
}

export interface SSEInitialPayload {
  payload: {
    users: RawSearchUser[];
  };
}

export interface SearchResult {
  displayName: string;
  username: string | null;
  userId: string;
  city: string | null;
  age: number | null;
  matchReason: string;
  sourceCount: number;
}

export interface OrbitProfileResponse {
  payload: {
    userId: string;
    socialProfile: RawSocialProfile;
  };
}

export interface RawSocialProfile {
  displayName?: string;
  avatarUrl?: string;
  username?: string;
  location?: {
    city?: string;
    region?: string;
    country?: string;
  };
  widgets?: unknown[];
  socialMediaHandles?: SocialMediaHandle[];
  aiRating?: RawAiRating;
  orbitSources?: OrbitSource[];
  [key: string]: unknown;
}

export interface SocialMediaHandle {
  network?: string;
  url?: string;
  username?: string;
  [key: string]: unknown;
}

export interface OrbitSource {
  source?: string;
  url?: string;
  [key: string]: unknown;
}

export interface RawAiRating {
  bio?: string;
  birthday?: string;
  school?: string;
  jobs?: Array<Record<string, unknown>>;
  education?: Array<Record<string, unknown>>;
  interests?: string[];
  passions?: string[];
  family?: Array<Record<string, unknown>>;
  accomplishments?: Array<Record<string, unknown>>;
  controversies?: Array<Record<string, unknown>>;
  worldview?: string;
  [key: string]: unknown;
}

export interface NormalizedProfile {
  displayName: string;
  avatarUrl: string | null;
  username: string | null;
  location: {
    city: string | null;
    region: string | null;
    country: string | null;
  };
  bio: string | null;
  birthday: string | null;
  school: string | null;
  jobs: Array<Record<string, unknown>>;
  education: Array<Record<string, unknown>>;
  interests: string[];
  family: Array<Record<string, unknown>>;
  accomplishments: Array<Record<string, unknown>>;
  controversies: Array<Record<string, unknown>>;
  socialLinks: Array<{ network: string | null; url: string | null; username: string | null }>;
  worldview: string | null;
  sources: Array<{ source: string | null; url: string | null }>;
}
