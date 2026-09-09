/** Regression coverage for paste-only setup and legacy Bearer compatibility. */
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { extractApiKey, API_KEY_HELP } from "../build/api-key-auth.js";

test("raw and Bearer forms normalize to the same session credential", () => {
  for (const value of ["sk_orb_test", " Bearer sk_orb_test ", "bearer sk_orb_test", "  sk_orb_test  "]) {
    assert.equal(extractApiKey(value), "sk_orb_test");
  }
  assert.equal(extractApiKey("Bearer legacy-token"), "legacy-token");
});

test("missing, unsupported, and ambiguous authorization is rejected without echoing it", () => {
  for (const value of [undefined, "", "Bearer ", "Basic abc", "sk_orb_", "sk_orb_one,sk_orb_two", "Bearer one two", "Bearer one,two"]) {
    assert.equal(extractApiKey(value), null);
  }
  assert.match(API_KEY_HELP, /https:\/\/developer.orbitsearch.com\/dashboard\/keys/);
});

test("Smithery keeps its required secret header while simplifying the label", () => {
  const schema = JSON.parse(readFileSync(new URL("../../../smithery-config.json", import.meta.url)));
  const field = schema.properties.orbitAuthorization;
  assert.deepEqual(schema.required, ["orbitAuthorization"]);
  assert.equal(field.title, "Orbit API key");
  assert(field.description.length < 100);
  assert.deepEqual(field["x-from"], { header: "x-orbit-authorization" });
  assert.deepEqual(field["x-to"], { header: "Authorization" });
  assert.equal(field.default, undefined);
});
