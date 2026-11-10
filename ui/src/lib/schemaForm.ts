// Form fields generated from a tool's JSON Schema (Pydantic model_json_schema output).
// Supported subset: an object of string / number / integer / boolean / enum / array-of-
// primitive properties, Optional (anyOf with null) unwrapped. Anything else falls back to a
// raw-JSON field, so no argument is ever silently dropped. The server re-validates
// everything (ADR-035); this only gives early, readable errors.

export type FieldKind = "string" | "text" | "number" | "integer" | "boolean" | "enum" | "list" | "json";

export interface FieldSpec {
  name: string;
  label: string;
  kind: FieldKind;
  required: boolean;
  nullable: boolean;
  description?: string;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  minimum?: number;
  maximum?: number;
  options?: (string | number)[];
}

type Schema = Record<string, unknown>;

function unwrap(s: Schema): { schema: Schema; nullable: boolean } {
  const any = (s.anyOf ?? s.oneOf) as Schema[] | undefined;
  if (Array.isArray(any)) {
    const nonNull = any.filter((x) => x.type !== "null");
    if (nonNull.length === 1) return { schema: { ...s, ...nonNull[0], anyOf: undefined }, nullable: nonNull.length < any.length };
  }
  return { schema: s, nullable: false };
}

const titleCase = (n: string) => n.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

export function fieldsFromSchema(schema: Schema | null | undefined): FieldSpec[] {
  if (!schema || schema.type !== "object") return [];
  const props = (schema.properties ?? {}) as Record<string, Schema>;
  const required = new Set((schema.required ?? []) as string[]);
  // Postgres jsonb does not keep key order, so `properties` may come back reordered; the
  // `required` array keeps the model's field order.
  const order = (schema.required ?? []) as string[];
  const rank = (n: string) => (order.includes(n) ? order.indexOf(n) : order.length);
  const entries = Object.entries(props).sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b));
  return entries.map(([name, raw]) => {
    const { schema: s, nullable } = unwrap(raw);
    const base = {
      name,
      label: (s.title as string) ?? titleCase(name),
      required: required.has(name),
      nullable,
      description: s.description as string | undefined,
    };
    if (Array.isArray(s.enum)) return { ...base, kind: "enum", options: s.enum as (string | number)[] };
    switch (s.type) {
      case "string": {
        const maxLength = s.maxLength as number | undefined;
        return {
          ...base,
          kind: maxLength === undefined || maxLength > 300 ? "text" : "string",
          minLength: s.minLength as number | undefined,
          maxLength,
          pattern: s.pattern as string | undefined,
        };
      }
      case "integer":
      case "number":
        return {
          ...base,
          kind: s.type,
          minimum: (s.minimum ?? (s.exclusiveMinimum !== undefined ? Number(s.exclusiveMinimum) : undefined)) as number | undefined,
          maximum: (s.maximum ?? s.exclusiveMaximum) as number | undefined,
        };
      case "boolean":
        return { ...base, kind: "boolean" };
      case "array": {
        const items = (s.items ?? {}) as Schema;
        if (["string", "integer", "number"].includes(items.type as string)) return { ...base, kind: "list" };
        return { ...base, kind: "json" };
      }
      default:
        return { ...base, kind: "json" };
    }
  });
}

export type FormValues = Record<string, string | boolean>;

/** Current args -> editable string values. */
export function initialValues(fields: FieldSpec[], args: Record<string, unknown>): FormValues {
  const out: FormValues = {};
  for (const f of fields) {
    const v = args[f.name];
    if (f.kind === "boolean") out[f.name] = Boolean(v);
    else if (v === undefined || v === null) out[f.name] = "";
    else if (f.kind === "list") out[f.name] = Array.isArray(v) ? v.join("\n") : String(v);
    else if (f.kind === "json") out[f.name] = JSON.stringify(v, null, 2);
    else out[f.name] = String(v);
  }
  return out;
}

/** Validate and convert to tool args. Returns per-field errors (empty object = valid). */
export function toArgs(fields: FieldSpec[], values: FormValues): { args: Record<string, unknown>; errors: Record<string, string> } {
  const args: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const f of fields) {
    const raw = values[f.name];
    if (f.kind === "boolean") {
      args[f.name] = Boolean(raw);
      continue;
    }
    const text = typeof raw === "string" ? raw : "";
    if (text.trim() === "") {
      if (f.required && !f.nullable) errors[f.name] = "Required";
      else if (f.nullable) args[f.name] = null;
      continue;
    }
    switch (f.kind) {
      case "string":
      case "text":
        if (f.minLength !== undefined && text.length < f.minLength) errors[f.name] = `At least ${f.minLength} characters`;
        else if (f.maxLength !== undefined && text.length > f.maxLength) errors[f.name] = `At most ${f.maxLength} characters`;
        else if (f.pattern && !safeTest(f.pattern, text)) errors[f.name] = "Invalid format";
        else args[f.name] = text;
        break;
      case "integer":
      case "number": {
        const n = Number(text);
        if (!Number.isFinite(n) || (f.kind === "integer" && !Number.isInteger(n))) errors[f.name] = f.kind === "integer" ? "Must be a whole number" : "Must be a number";
        else if (f.minimum !== undefined && n < f.minimum) errors[f.name] = `Must be ≥ ${f.minimum}`;
        else if (f.maximum !== undefined && n > f.maximum) errors[f.name] = `Must be ≤ ${f.maximum}`;
        else args[f.name] = n;
        break;
      }
      case "enum": {
        const opt = f.options?.find((o) => String(o) === text);
        if (opt === undefined) errors[f.name] = "Choose an option";
        else args[f.name] = opt;
        break;
      }
      case "list":
        args[f.name] = text.split("\n").map((s) => s.trim()).filter(Boolean);
        break;
      case "json":
        try {
          args[f.name] = JSON.parse(text);
        } catch {
          errors[f.name] = "Invalid JSON";
        }
        break;
    }
  }
  return { args, errors };
}

function safeTest(pattern: string, value: string): boolean {
  try {
    // JSON Schema patterns are ECMA-262 compatible; an unsupported one is left to the server
    return new RegExp(pattern, "u").test(value);
  } catch {
    return true;
  }
}
