import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TimeBar } from "./TracePanel";

describe("TimeBar", () => {
  it("states every number in text (never color alone) and in the bar's label", () => {
    render(<TimeBar time={{ wall_s: 75, compute_s: 9, human_wait_s: 60, queue_s: 6 }} />);
    expect(screen.getByRole("img")).toHaveAccessibleName("Compute 9.0 s, Human wait 1 min, Queue 6.0 s");
    expect(screen.getByText("Human wait").nextSibling).toHaveTextContent("1 min");
    expect(screen.getByText("Wall clock").nextSibling).toHaveTextContent("1 min 15 s");
  });
  it("omits empty segments from the bar but keeps them in the legend", () => {
    const { container } = render(<TimeBar time={{ wall_s: 3, compute_s: 3, human_wait_s: 0, queue_s: 0 }} />);
    expect(container.querySelectorAll('[role="img"] > div')).toHaveLength(1);
    expect(screen.getByText("Queue")).toBeInTheDocument();
  });
});
