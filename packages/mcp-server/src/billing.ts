const BILLING_API_URL =
  process.env.BILLING_API_URL ?? "https://api.orbitsearch.com/v1/mcp/billing";

interface CreditsResponse {
  valid: boolean;
  credits: number;
}

interface DeductResponse {
  success: boolean;
  remaining: number;
  error?: string;
}

export async function getCredits(apiKey: string): Promise<CreditsResponse> {
  const res = await fetch(`${BILLING_API_URL}/credits`, {
    method: "GET",
    headers: { "x-api-key": apiKey },
  });

  if (!res.ok) {
    if (res.status === 401 || res.status === 403) {
      return { valid: false, credits: 0 };
    }
    throw new Error(`Billing API error ${res.status}: ${res.statusText}`);
  }

  return (await res.json()) as CreditsResponse;
}

export async function deductCredits(
  apiKey: string,
  credits: number,
  tool: string,
): Promise<DeductResponse> {
  const res = await fetch(`${BILLING_API_URL}/deduct`, {
    method: "POST",
    headers: {
      "x-api-key": apiKey,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ credits, tool }),
  });

  if (!res.ok) {
    throw new Error(`Billing API error ${res.status}: ${res.statusText}`);
  }

  return (await res.json()) as DeductResponse;
}
