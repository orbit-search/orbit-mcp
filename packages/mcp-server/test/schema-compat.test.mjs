/** Compatibility edge cases validate through Zod 4, with no legacy validator. */
import assert from "node:assert/strict";
import test from "node:test";
import { z } from "zod";
import { integer, uuid, email, publicJsonSchema } from "../build/schema-compat.js";
import { legacyError, normalizeValidationText, scopeValidationErrors } from "../build/validation-errors.js";

test("integer compatibility retains unsafe representable integers and explicit bounds", () => {
  const count = integer().min(0);
  for (const n of [0, Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER + 1, 1e20]) assert.equal(count.parse(n), n);
  for (const n of [-1, 1.5, NaN, Infinity]) assert.equal(count.safeParse(n).success, false);
  assert.deepEqual(publicJsonSchema(count), { type: "integer", minimum: 0 });
  assert.equal(integer().max(50).safeParse(51).success, false);
  assert.equal(integer().max(50).safeParse(1e20).success, false);
});

test("UUID compatibility preserves non-RFC versions/variants but rejects malformed UUIDs", () => {
  for (const id of ["ffffffff-ffff-ffff-0000-ffffffffffff", "12345678-1234-0234-1234-123456789abc", "00000000-0000-0000-0000-000000000000"]) assert.equal(uuid().parse(id), id);
  for (const id of ["", "12345678-1234-1234-1234-123456789abg", "12345678123412341234123456789abc", " 00000000-0000-0000-0000-000000000000"]) assert.equal(uuid().safeParse(id).success, false);
  assert.deepEqual(publicJsonSchema(uuid()), { type: "string", format: "uuid" });
  assert.equal(email().safeParse("first.last+tag@example.com").success, true);
  assert.equal(email().safeParse("first..last@example.com").success, false);
});

test("patched Zod 4 rejects oversized arrays before validating their elements", () => {
  let elements = 0;
  const bounded = z.array(z.string().refine(() => { elements++; return true; })).max(2);
  const result = bounded.safeParse(["a", "b", "c"]);
  assert.equal(result.success, false);
  assert.equal(elements, 0);
  assert.equal(result.error.issues.length, 1);
  assert.equal(result.error.issues[0].code, "too_big");
});

test("error mapping handles enum types and removes only cascading size diagnostics", () => {
  const schema = z.enum(["async", "sync"]);
  assert.equal(schema.safeParse(1, { error: legacyError }).error.issues[0].message, "Expected 'async' | 'sync', received number");
  assert.equal(schema.safeParse("bad", { error: legacyError }).error.issues[0].message, "Invalid enum value. Expected 'async' | 'sync', received 'bad'");
  const head = "MCP error -32602: Input validation error: Invalid arguments for tool example: Expected string, received array at query";
  assert.equal(normalizeValidationText(head + "\nArray must contain at least 1 element(s) at query"), head);
  assert.equal(normalizeValidationText(head + "\nArray must contain at least 1 element(s) at signals"), head + "\nArray must contain at least 1 element(s) at signals");
});

test("error customization is scoped to application schemas, preserving SDK protocol errors", () => {
  const protocolSchema = z.string();
  const appSchema = z.string();
  const priorMessage = protocolSchema.safeParse(42).error.issues[0].message;
  scopeValidationErrors(appSchema);
  assert.equal(appSchema.safeParse(42).error.issues[0].message, "Expected string, received number");
  assert.equal(protocolSchema.safeParse(42).error.issues[0].message, priorMessage);
});
