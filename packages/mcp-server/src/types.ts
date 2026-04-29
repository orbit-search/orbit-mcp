export interface DeveloperSearchUser {
  userId: string;
  displayName?: string;
  username?: string;
  city?: string;
  age?: number;
  matchReason?: string;
  sourceCount?: number;
  [key: string]: unknown;
}

export interface DeveloperSearchResponse {
  status: string;
  searchId: string;
  payload: {
    users: DeveloperSearchUser[];
  };
}

export interface DeveloperProfileResponse {
  status: string;
  payload: {
    id: string;
    orbitId: string;
    displayName: string;
    avatarUrl: string | null;
    profileUrl: string | null;
    verified: boolean;
    location: {
      city: string | null;
    } | null;
    headline: {
      jobTitle: string | null;
      companyName: string | null;
      schoolName: string | null;
    } | null;
    sections: {
      basic: Record<string, unknown> | null;
      personalLife: Record<string, unknown> | null;
      jobs: Record<string, unknown> | null;
      education: Record<string, unknown> | null;
      passions: Record<string, unknown> | null;
      worldview: Record<string, unknown> | null;
      accomplishments: Record<string, unknown> | null;
      controversies: Record<string, unknown> | null;
      bestQualities: Record<string, unknown> | null;
      netWorth: Record<string, unknown> | null;
      portfolio: Record<string, unknown> | null;
      families: Record<string, unknown> | null;
    };
  };
}
