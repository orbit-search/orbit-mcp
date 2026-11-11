/** Zod 4 validation with Orbit's existing integer, UUID and JSON Schema vocabulary. */
import { z } from "zod";
import type { $ZodTypes } from "zod/v4/core";

// These are the previously accepted syntaxes, not Zod 4's stricter RFC UUID/version policy.
const uuidPattern = /^[0-9a-fA-F]{8}\b-[0-9a-fA-F]{4}\b-[0-9a-fA-F]{4}\b-[0-9a-fA-F]{4}\b-[0-9a-fA-F]{12}$/i;
const emailPattern = /^(?!\.)(?!.*\.\.)([A-Z0-9_'+\-\.]*)[A-Z0-9_+-]@([A-Z0-9][A-Z0-9\-]*\.)+[A-Z]{2,}$/i;
export const uuid = () => z.string().regex(uuidPattern, "Invalid uuid");
export const email = () => z.string().regex(emailPattern, "Invalid email");
// The old API accepts representable integer numbers outside Number.MAX_SAFE_INTEGER.
// Keep that domain, retaining every separately specified min/max constraint.
export const integer = () => z.number().refine(Number.isInteger, "Expected integer, received float");

/** Project schemas, not data, retaining historical local references and unknown-key declarations. */
export function publicJsonSchema(schema: z.ZodType): Record<string, unknown> {
  const seen = new Map<object, string>();
  function visit(node: z.ZodType, path: string, property = false): Record<string, unknown> {
    const prior = seen.get(node);
    if (prior) return { $ref: prior };
    seen.set(node, path);
    const def = (node as unknown as $ZodTypes)._zod.def;
    let result: Record<string, unknown>;
    const child = (value: unknown, suffix = "", prop = false) => visit(value as z.ZodType, path + suffix, prop);
    switch (def.type) {
      case "object": {
        const properties: Record<string, unknown> = {};
        const required: string[] = [];
        for (const [key, value] of Object.entries(def.shape)) {
          properties[key] = child(value, "/properties/" + key.replace(/~/g, "~0").replace(/\//g, "~1"), true);
          if (!z.safeParse(value, undefined).success) required.push(key);
        }
        result = { type: "object", properties, ...(required.length ? { required } : {}),
          additionalProperties: !def.catchall || def.catchall._zod.def.type === "never" ? false
            : def.catchall._zod.def.type === "unknown" ? true : child(def.catchall, "/additionalProperties") };
        break;
      }
      case "optional": result = property ? child(def.innerType, "", property)
        : { anyOf: [{ not: {} }, child(def.innerType, "/anyOf/1")] }; break;
      case "default": result = { ...child(def.innerType, "", property), default: def.defaultValue }; break;
      case "nullable": {
        const inner = def.innerType;
        const d = inner._zod.def;
        result = ["string", "number", "boolean", "null"].includes(d.type) && !d.checks?.length
          ? { type: [d.type, "null"] } : { anyOf: [child(inner, "/anyOf/0"), { type: "null" }] };
        break;
      }
      case "array": {
        const json = z.toJSONSchema(node, { target: "draft-7", io: "input" });
        result = { type: "array", items: child(def.element, "/items"),
          ...(json.minItems === undefined ? {} : { minItems: json.minItems }),
          ...(json.maxItems === undefined ? {} : { maxItems: json.maxItems }) };
        break;
      }
      case "record": result = { type: "object", additionalProperties: child(def.valueType, "/additionalProperties") }; break;
      case "union": result = { anyOf: def.options.map((option, index) => child(option, "/anyOf/" + index)) }; break;
      default: {
        const { $schema: _dialect, ...json } = z.toJSONSchema(node, { target: "draft-7", io: "input" });
        result = json;
        if (def.type === "number" && def.checks?.some(check => {
          const d = check._zod.def;
          return d.check === "custom" && "fn" in d && d.fn === Number.isInteger;
        })) result.type = "integer";
        if (def.type === "string") {
          if (result.pattern === uuidPattern.source) { delete result.pattern; result.format = "uuid"; }
          if (result.pattern === emailPattern.source) { delete result.pattern; result.format = "email"; }
        }
      }
    }
    if (node.description) result.description = node.description;
    return result;
  }
  return visit(schema, "#");
}

/** Metadata controls publication only; the original Zod 4 schema still validates all values. */
export function publicToolSchema<T>(value: T): T {
  if (value === undefined || (value && typeof value === "object" && !(value instanceof z.ZodType) && !Object.keys(value).length)) return value;
  const schema = value instanceof z.ZodType ? value : z.object(value as z.ZodRawShape);
  return schema.meta(publicJsonSchema(schema)) as unknown as T;
}
