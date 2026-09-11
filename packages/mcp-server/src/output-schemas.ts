/** Public v3 response envelopes; preserve evolving profile fields and API additions. */
import { z } from "zod";

export const billingSchema = z.object({
  id: z.string(),
  pricingVersion: z.string(),
  reservedCredits: z.number().finite(),
  consumedCredits: z.number().finite(),
  releasedCredits: z.number().finite(),
  heldCredits: z.number().finite(),
  status: z.enum(["open", "settled"]),
}).describe("Authoritative operation billing from Orbit; reserved credits are not necessarily consumed credits.");

export const creditUsageOutputSchema = z.object({
  status: z.literal("success"),
  payload: z.object({
    account_type: z.enum(["user", "organization"]),
    usage_scope: z.literal("current_api_key"),
    balance_scope: z.literal("billing_account"),
    period_start: z.string(),
    period_end: z.string(),
    credits_used: z.number().finite().describe("Net credits consumed by this API key, including refunds."),
    request_count: z.number().int().nonnegative(),
    available_credits: z.number().finite(),
    reserved_credits: z.number().finite(),
  }),
});

const profile = z.object({}).passthrough().describe(
  "Public Orbit person context, including available profile sections and source evidence. Fields vary by generation level; returned fields are preserved.",
);
const generationLevel = z.number().int().nullable().describe(
  "Stored profile generation level: 1 identity, 2 partial, 3 full; null when no level is available.",
);
const failure = z.object({
  code: z.string().describe("Machine-readable failure code."),
  message: z.string().describe("Explanation of the failed work."),
  retryable: z.boolean().describe("Whether the failed work may be retried."),
  reason: z.string().optional().describe("Specific failure reason, when supplied by the API."),
  suggested_inputs: z.array(z.string()).optional().describe("Additional identity inputs that may help."),
}).passthrough();
const source = z.object({
  link: z.string().describe("Discovered source URL."),
  title: z.string().describe("Source page title."),
  sourceImage: z.string().describe("Source site icon URL."),
}).passthrough();

export const searchOutputSchema = z.object({
  billing: billingSchema.optional(),
  search_id: z.string().describe("Server-generated search identifier."),
  request_id: z.string().describe("Idempotency key for this logical search; reuse for retries."),
  status: z.enum(["completed", "completed_with_errors", "failed"]).describe("Terminal search state after polling."),
  candidate_discovery: z.boolean().describe("Whether candidate discovery was enabled."),
  candidate_discovery_completed: z.boolean().optional().describe("Whether discovery can add any more results."),
  profile_depth: z.enum(["partial", "full"]).describe("Requested minimum profile depth."),
  include_profile: z.boolean().describe("Whether results include available profile objects."),
  profile_upgrades_completed: z.boolean().optional().describe("Whether all results reached the requested depth or failed."),
  results: z.array(z.object({
    profile_id: z.string().describe("Canonical ID for get_profile or enrich_profile."),
    status: z.enum(["generating", "enriching", "ready", "failed"]).describe("This person's profile readiness."),
    generation_level: generationLevel,
    sources: z.array(z.enum(["search", "candidate_discovery"])).optional().describe("Result origins, not evidence receipts."),
    candidate_sources: z.array(source).optional().describe("Discovery evidence assigned to this person."),
    preview: z.object({}).passthrough().optional().describe("Available preview while the profile is being prepared."),
    profile: profile.optional(),
    failure: failure.optional(),
  }).passthrough()).describe("Matched people; an empty completed result means no matches."),
  discovered_sources: z.array(source).optional().describe("Discovery evidence not yet assigned to a person."),
  candidate_discovery_failure: failure.optional(),
  created_at: z.string().describe("Search creation timestamp."),
  updated_at: z.string().describe("Last snapshot update timestamp."),
  links: z.object({ status: z.string().describe("Relative v3 search-status URL.") }).passthrough(),
}).passthrough();

export const profileOutputSchema = z.object({
  billing: billingSchema.optional(),
  profile_id: z.string().describe("Canonical Orbit profile identifier."),
  generation_level: generationLevel,
  profile,
}).passthrough();

export const enrichOutputSchema = z.object({
  billing: billingSchema.optional(),
  request_id: z.string().describe("Idempotency key for this enrichment; reuse for retries."),
  profile_id: z.string().describe("Canonical Orbit profile identifier."),
  status: z.enum(["completed", "failed"]).describe("Terminal enrichment state after polling."),
  operation: z.enum(["partial", "full", "regenerate"]).describe("Requested enrichment operation."),
  include_profile: z.boolean().describe("Whether available person context is embedded."),
  generation_level: generationLevel.optional(),
  profile: profile.optional(),
  failure: failure.optional(),
  links: z.object({
    status: z.string().optional().describe("Relative enrichment-status URL, when asynchronous work was needed."),
    profile: z.string().describe("Relative profile-read URL."),
  }).passthrough().optional(),
}).passthrough();
