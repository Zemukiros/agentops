import { describe, expect, it } from "vitest";
import type { CheckpointFields } from "../../api/types";
import { buildFork } from "./ReplayPanel";

const base: CheckpointFields = {
  checkpoint_id: "cp1",
  step: 9,
  created_at: null,
  next: ["final_review"],
  wave: 2,
  forkable: true,
  reason: null,
  as_node: "synthesize",
  editable: ["final"],
  subtasks: { a: "accepted" },
  has_final: true,
  request: "Summarize",
  plan: { subtasks: [{ id: "a", description: "d", specialist: "writing", depends_on: [], expected_output: "o", complexity: "low" }], confidence: 0.8, rationale: "r" },
  outputs: { a: "A" },
  final: "Final v1",
};
const form = (over = {}) => ({
  request: "Summarize",
  plan: null,
  outputs: { a: "A" },
  final: "Final v1",
  budget: { max_usd: "", max_llm_calls: "", max_tokens: "" },
  ...over,
});

describe("buildFork", () => {
  it("sends only changed fields that are editable here (no budget = the original limits)", () => {
    expect(buildFork(base, form()).body).toEqual({ checkpoint_id: "cp1", edits: {} });
    expect(buildFork(base, form({ final: "Final v2", request: "changed but not editable here" })).body).toEqual({
      checkpoint_id: "cp1",
      edits: { final: "Final v2" },
    });
  });
  it("sends output edits at a between-waves point, and a budget override", () => {
    const f = { ...base, next: ["synthesize"], editable: ["outputs" as const] };
    expect(buildFork(f, form({ outputs: { a: "A2" }, budget: { max_usd: "0.2", max_llm_calls: "", max_tokens: "" } })).body).toEqual({
      checkpoint_id: "cp1",
      edits: { outputs: { a: "A2" } },
      budget: { max_usd: 0.2 },
    });
    expect(buildFork(f, form({ budget: { max_usd: "-1", max_llm_calls: "", max_tokens: "" } })).error).toMatch(/positive/);
  });
});
