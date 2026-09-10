/** API-owned billing receipts and key-scoped usage; no local tariff or ledger. */
import { z } from "zod";

export const billingSchema = z.object({
  id: z.string(), pricingVersion: z.string(), reservedCredits: z.number().finite(),
  consumedCredits: z.number().finite(), releasedCredits: z.number().finite(),
  heldCredits: z.number().finite(), status: z.enum(["open", "settled"]),
});
export type OperationBilling = z.infer<typeof billingSchema>;

export const creditUsageOutputSchema = z.object({
  status: z.literal("success"),
  payload: z.object({
    account_type: z.enum(["user", "organization"]),
    usage_scope: z.literal("current_api_key"), balance_scope: z.literal("billing_account"),
    period_start: z.string(), period_end: z.string(), credits_used: z.number().finite(),
    request_count: z.number().int().nonnegative(), available_credits: z.number().finite(),
    reserved_credits: z.number().finite(),
  }),
});
