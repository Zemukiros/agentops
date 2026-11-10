import { describe, expect, it } from "vitest";
import { findCycle, newSubtask, removeSubtask, renameSubtask, validatePlan, type EditablePlan } from "./planEdit";
import { fieldsFromSchema, initialValues, toArgs } from "./schemaForm";

// The real send_email schema (EmailIn.model_json_schema())
const EMAIL = {
  additionalProperties: false,
  properties: {
    to: { maxLength: 254, pattern: "^[^@\\s<>,;]{1,64}@[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)*\\.[A-Za-z]{2,63}$", title: "To", type: "string" },
    subject: { maxLength: 200, minLength: 1, pattern: "^[^\\r\\n]+$", title: "Subject", type: "string" },
    body: { maxLength: 20000, minLength: 1, title: "Body", type: "string" },
  },
  required: ["to", "subject", "body"],
  title: "EmailIn",
  type: "object",
};

describe("schema form", () => {
  it("derives fields with kinds and constraints", () => {
    const f = fieldsFromSchema(EMAIL);
    expect(f.map((x) => [x.name, x.kind, x.required])).toEqual([
      ["to", "string", true],
      ["subject", "string", true],
      ["body", "text", true],
    ]);
  });

  it("round-trips current args and validates edits like the server", () => {
    const f = fieldsFromSchema(EMAIL);
    const v = initialValues(f, { to: "team@example.com", subject: "Hi", body: "B" });
    expect(toArgs(f, v)).toEqual({ args: { to: "team@example.com", subject: "Hi", body: "B" }, errors: {} });
    expect(toArgs(f, { ...v, to: "a@b.co, x@evil.example" }).errors.to).toBe("Invalid format");
    expect(toArgs(f, { ...v, subject: "line\r\nBcc: x@evil.example" }).errors.subject).toBe("Invalid format");
    expect(toArgs(f, { ...v, body: "" }).errors.body).toBe("Required");
  });

  it("handles integers with minimums, optionals, enums, lists and unknown shapes", () => {
    const f = fieldsFromSchema({
      type: "object",
      required: ["note_id"],
      properties: {
        note_id: { type: "integer", minimum: 1 },
        tag: { anyOf: [{ type: "string" }, { type: "null" }], default: null },
        mode: { enum: ["a", "b"] },
        labels: { type: "array", items: { type: "string" } },
        meta: { type: "object" },
        on: { type: "boolean" },
      },
    });
    // required first (model order), then the rest by name
    expect(f.map((x) => [x.name, x.kind])).toEqual([
      ["note_id", "integer"],
      ["labels", "list"],
      ["meta", "json"],
      ["mode", "enum"],
      ["on", "boolean"],
      ["tag", "text"],
    ]);
    expect(toArgs(f, { note_id: "0", tag: "", mode: "b", labels: "x\n y \n", meta: '{"k":1}', on: true })).toEqual({
      args: { tag: null, mode: "b", labels: ["x", "y"], meta: { k: 1 }, on: true },
      errors: { note_id: "Must be ≥ 1" },
    });
    expect(toArgs(f, { note_id: "2.5", meta: "{", mode: "", labels: "", tag: "", on: false }).errors).toEqual({
      note_id: "Must be a whole number",
      meta: "Invalid JSON",
    });
  });

  it("returns no fields for a missing or non-object schema", () => {
    expect(fieldsFromSchema(null)).toEqual([]);
    expect(fieldsFromSchema({ type: "string" })).toEqual([]);
  });
});

const st = (id: string, depends_on: string[] = []) => ({
  id,
  description: "d",
  specialist: "writing",
  depends_on,
  expected_output: "o",
  complexity: "low",
});
const plan = (...subtasks: ReturnType<typeof st>[]): EditablePlan => ({ subtasks, confidence: 0.8, rationale: "" });

describe("plan editor", () => {
  it("accepts a valid DAG", () => {
    expect(validatePlan(plan(st("a"), st("b", ["a"])))).toEqual({ plan: [], rows: {} });
  });

  it("reports cycles, duplicates, bad ids, unknown deps and limits", () => {
    expect(findCycle(plan(st("a", ["c"]), st("b", ["a"]), st("c", ["b"])).subtasks)).toEqual(["a", "c", "b", "a"]);
    const e = validatePlan(plan(st("a", ["b"]), st("b", ["a"]), st("a"), st("Bad Id"), st("x", ["ghost"]), st("y", ["y"]), st("z")));
    expect(e.plan.join(" ")).toMatch(/At most 6/);
    expect(e.plan.join(" ")).toMatch(/cycle/);
    expect(e.rows[2]?.id).toBe("Duplicate id");
    expect(e.rows[3]?.id).toMatch(/Lowercase/);
    expect(e.rows[4]?.depends_on).toBe("Unknown: ghost");
    expect(e.rows[5]?.depends_on).toMatch(/itself/);
    expect(validatePlan(plan()).plan).toContain("A plan needs at least one subtask.");
  });

  it("keeps dependencies consistent on rename and remove", () => {
    const p = plan(st("a"), st("b", ["a"]));
    expect(renameSubtask(p, 0, "research").subtasks[1]!.depends_on).toEqual(["research"]);
    expect(removeSubtask(p, 0).subtasks).toEqual([st("b")]);
    expect(newSubtask(p).id).toBe("step-3");
  });
});

describe("schema field order", () => {
  it("follows the required list even when jsonb reordered the properties", () => {
    const reordered = { ...EMAIL, properties: { to: EMAIL.properties.to, body: EMAIL.properties.body, subject: EMAIL.properties.subject } };
    expect(fieldsFromSchema(reordered).map((f) => f.name)).toEqual(["to", "subject", "body"]);
  });
});
