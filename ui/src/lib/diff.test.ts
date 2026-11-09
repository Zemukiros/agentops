import { describe, expect, it } from "vitest";
import { diffStats, nodeLabel, parseUnifiedDiff } from "./diff";

describe("parseUnifiedDiff", () => {
  it("types lines and numbers both sides", () => {
    const rows = parseUnifiedDiff(["--- task:a", "+++ replay:b", "@@ -1,2 +1,2 @@", " # Title", "-old line", "+new line"]);
    expect(rows.map((r) => r.kind)).toEqual(["file", "file", "hunk", "ctx", "del", "add"]);
    expect(rows[3]).toMatchObject({ left: 1, right: 1 });
    expect(rows[4]).toMatchObject({ left: 2, right: null, text: "old line" });
    expect(rows[5]).toMatchObject({ left: null, right: 2, text: "new line" });
    expect(diffStats(rows)).toEqual({ added: 1, removed: 1 });
  });
  it("handles an empty diff", () => {
    expect(parseUnifiedDiff([])).toEqual([]);
  });
  it("labels fork points", () => {
    expect(nodeLabel("final_review")).toMatch(/before final review/);
    expect(nodeLabel("custom")).toBe("custom");
  });
});
