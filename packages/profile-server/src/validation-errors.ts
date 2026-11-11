/** Translate Zod 4 diagnostics to the established public error vocabulary. */
import { z } from "zod";
const received = (value: unknown) => value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
const quote = (value: unknown) => typeof value === "string" ? `'${value}'` : String(value);

/** Keep historical messages; Zod 4 still produces and owns every validation issue. */
export const legacyError: z.core.$ZodErrorMap = issue => {
  switch (issue.code) {
    case "invalid_type": return issue.input === undefined ? "Required" : `Expected ${issue.expected}, received ${received(issue.input)}`;
    case "invalid_value": {
      const def = issue.inst?._zod.def;
      const enumeration = def && "type" in def && def.type === "enum";
      if (enumeration && typeof issue.input !== "string") return issue.input === undefined ? "Required"
        : `Expected ${issue.values.map(quote).join(" | ")}, received ${received(issue.input)}`;
      return !enumeration && issue.values.length === 1
      ? `Invalid literal value, expected ${JSON.stringify(issue.values[0])}`
      : issue.input === undefined ? "Required" : `Invalid enum value. Expected ${issue.values.map(quote).join(" | ")}, received ${quote(issue.input)}`;
    }
    case "invalid_format": return issue.format === "regex" ? "Invalid" : `Invalid ${issue.format.toLowerCase()}`;
    case "unrecognized_keys": return `Unrecognized key(s) in object: ${issue.keys.map(quote).join(", ")}`;
    case "too_small":
    case "too_big": {
      const small = issue.code === "too_small";
      const bound = small ? issue.minimum : issue.maximum;
      if (issue.origin === "number") return `Number must be ${small ? "greater" : "less"} than ${issue.inclusive ? "or equal to " : ""}${bound}`;
      if (issue.origin === "string") return `String must contain ${small ? "at least" : "at most"} ${bound} character(s)`;
      if (issue.origin === "array") return `Array must contain ${small ? "at least" : "at most"} ${bound} element(s)`;
      return undefined;
    }
    default: return undefined;
  }
};

/** Drop cascading size diagnostics at an already-invalid type path, preserving the primary error. */
export function normalizeValidationText(text: string): string {
  if (!text.startsWith("MCP error -32602: Input validation error:")) return text;
  const lines = text.split("\n");
  const invalidPaths = new Set(lines.filter(line => /(?:Expected \w+, received |Required(?: at |$))/.test(line)).map(line => (line.includes(" at ") ? line.slice(line.lastIndexOf(" at ") + 4) : "")));
  return lines.filter(line => !/^(String|Array) must contain /.test(line) || !invalidPaths.has((line.includes(" at ") ? line.slice(line.lastIndexOf(" at ") + 4) : ""))).join("\n");
}


/** Response validation uses patched Zod 4; only the error serialization uses the legacy field vocabulary. */
export function parseResponse<T extends z.ZodType>(schema: T, value: unknown): z.output<T> {
  const parsed = schema.safeParse(value, { reportInput: true, error: legacyError });
  if (parsed.success) return parsed.data;
  const issues = parsed.error.issues.map(issue => {
    switch (issue.code) {
      case "invalid_value":
        if (issue.message.startsWith("Invalid literal value")) return { received: issue.input, code: "invalid_literal", expected: issue.values[0], path: issue.path, message: issue.message };
        if (issue.message === "Required" || issue.message.startsWith("Expected ")) return { code: "invalid_type", expected: issue.values.map(quote).join(" | "), received: received(issue.input), path: issue.path, message: issue.message };
        return { received: issue.input, code: "invalid_enum_value", options: issue.values, path: issue.path, message: issue.message };
      case "invalid_type": return { code: issue.code, expected: issue.expected, received: received(issue.input), path: issue.path, message: issue.message };
      case "invalid_format": return { validation: issue.format, code: "invalid_string", message: issue.message, path: issue.path };
      case "too_small": return { code: issue.code, minimum: issue.minimum, type: issue.origin, inclusive: issue.inclusive, exact: false, message: issue.message, path: issue.path };
      case "too_big": return { code: issue.code, maximum: issue.maximum, type: issue.origin, inclusive: issue.inclusive, exact: false, message: issue.message, path: issue.path };
      case "custom":
        if (issue.message === "Expected integer, received float") return { code: "invalid_type", expected: "integer", received: "float", message: issue.message, path: issue.path };
        return { code: issue.code, message: issue.message, path: issue.path };
      default: { const { input: _input, ...rest } = issue; return rest; }
    }
  });
  throw new Error(JSON.stringify(issues, null, 2));
}

// Limit the documented global hook to application schema/check instances. SDK
// protocol validation and other consumers retain their existing error formatter.
const ownedSchemas = new WeakSet<object>();
let installed = false;
export function scopeValidationErrors(...schemas: unknown[]): void {
  function collect(value: unknown): void {
    if (!value || typeof value !== "object" || ownedSchemas.has(value)) return;
    ownedSchemas.add(value);
    if (value instanceof z.ZodType) {
      collect(value.def);
    } else {
      for (const child of Object.values(value)) collect(child);
    }
  }
  schemas.forEach(collect);
  if (!installed) {
    const previous = z.config().customError;
    z.config({ customError: issue => issue.inst && ownedSchemas.has(issue.inst) ? legacyError(issue) : previous?.(issue) });
    installed = true;
  }
}
