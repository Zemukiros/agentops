import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/base.css";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import type { SubtaskView } from "../../api/types";
import { Icon } from "../../components/Icon";
import { StatusBadge } from "../../components/StatusBadge";
import { layout, mergeStatus, type LiveStatus, type LiveSubtask, type PlanNode } from "../../lib/dag";
import { useResolvedTheme } from "../../lib/theme";

const NODE_W = 232;
const NODE_H = 92;
const GAP_X = 72;
const GAP_Y = 24;

const RING: Partial<Record<LiveStatus, string>> = {
  dispatched: "border-info-line",
  working: "border-info-fg ring-2 ring-info-line",
  in_review: "border-info-fg ring-2 ring-info-line",
  revising: "border-warning-fg ring-2 ring-warning-line",
  accepted: "border-success-line",
  failed: "border-danger-line",
  needs_human: "border-warning-fg ring-2 ring-warning-line",
  blocked: "border-line-strong border-dashed",
};

const SPECIALIST_LABEL: Record<string, string> = {
  research: "Research agent",
  writing: "Writing agent",
  analysis: "Analysis agent",
  code: "Code agent",
};

export interface SubtaskNodeData extends Record<string, unknown> {
  id: string;
  specialist: string;
  description: string;
  status: LiveStatus;
  revision: number;
  score: number | null;
  activity: string | null;
  vertical: boolean;
}

const SubtaskNode = memo(function SubtaskNode({ data }: NodeProps<Node<SubtaskNodeData>>) {
  return (
    <div
      className={`rounded-xl border bg-surface px-3 py-2.5 text-left shadow-card transition-[border-color,box-shadow] ${RING[data.status] ?? "border-line"}`}
      style={{ width: NODE_W, height: NODE_H }}
    >
      <Handle type="target" position={data.vertical ? Position.Top : Position.Left} className="!size-2 !border-0 !bg-line-strong" isConnectable={false} />
      <div className="flex items-start justify-between gap-2">
        <span className="truncate font-mono text-[13px] font-semibold text-fg">{data.id}</span>
        <StatusBadge status={data.status} kind="subtask" />
      </div>
      <p className="mt-1 flex items-center gap-1 text-xs text-fg-muted">
        <Icon name="cpu" size={12} />
        {SPECIALIST_LABEL[data.specialist] ?? data.specialist}
      </p>
      <p className="mt-1 truncate text-xs text-fg-subtle">
        {data.activity ??
          [data.revision > 0 ? `revision ${data.revision}` : null, data.score !== null ? `score ${data.score}/5` : null]
            .filter(Boolean)
            .join(" · ") ??
          ""}
      </p>
      <Handle type="source" position={data.vertical ? Position.Bottom : Position.Right} className="!size-2 !border-0 !bg-line-strong" isConnectable={false} />
    </div>
  );
});

const nodeTypes = { subtask: SubtaskNode };

const ACTIVE = new Set<LiveStatus>(["dispatched", "working", "in_review", "revising"]);

export function buildFlow(
  plan: PlanNode[],
  snapshot: SubtaskView[],
  live: Record<string, LiveSubtask>,
  vertical = false,
): { nodes: Node<SubtaskNodeData>[]; edges: Edge[] } {
  const bySnap = new Map(snapshot.map((s) => [s.id, s]));
  const pos = new Map(layout(plan).map((p) => [p.id, p]));
  const status = new Map(plan.map((p) => [p.id, mergeStatus(bySnap.get(p.id), live[p.id])]));
  const nodes = plan.map((p) => {
    const at = pos.get(p.id)!;
    const l = live[p.id];
    const s = bySnap.get(p.id);
    const st = status.get(p.id)!;
    const lastReview = s?.reviews.at(-1);
    return {
      id: p.id,
      type: "subtask",
      // left-to-right on wide screens; top-to-bottom on narrow ones (stays readable)
      position: vertical
        ? { x: at.row * (NODE_W + GAP_Y), y: at.level * (NODE_H + GAP_X / 1.5) }
        : { x: at.level * (NODE_W + GAP_X), y: at.row * (NODE_H + GAP_Y) },
      draggable: false,
      ariaLabel: `Subtask ${p.id}, ${p.specialist}, ${st.replace("_", " ")}`,
      data: {
        id: p.id,
        specialist: p.specialist,
        description: p.description,
        status: st,
        vertical,
        revision: Math.max(l?.revision ?? 0, s?.revisions ?? 0),
        score: l?.score ?? lastReview?.score ?? null,
        activity:
          ACTIVE.has(st) && l?.lastTool && st === "working"
            ? `using ${l.lastTool}`
            : st === "revising"
              ? "revising after review"
              : null,
      },
    };
  });
  const edges: Edge[] = plan.flatMap((p) =>
    p.depends_on
      .filter((d) => pos.has(d))
      .map((d) => {
        const flowing = status.get(d) === "accepted" && ACTIVE.has(status.get(p.id)!);
        return {
          id: `${d}->${p.id}`,
          source: d,
          target: p.id,
          animated: flowing,
          markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
          style: { strokeWidth: 1.5, stroke: flowing ? "var(--info-fg)" : undefined },
        };
      }),
  );
  return { nodes, edges };
}

export function PlanDag({
  plan,
  snapshot,
  live,
}: {
  plan: PlanNode[];
  snapshot: SubtaskView[];
  live: Record<string, LiveSubtask>;
}) {
  const theme = useResolvedTheme();
  const box = useRef<HTMLDivElement>(null);
  const [vertical, setVertical] = useState(false);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setVertical((e?.contentRect.width ?? 1000) < 600));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const { nodes, edges } = useMemo(() => buildFlow(plan, snapshot, live, vertical), [plan, snapshot, live, vertical]);
  const placed = layout(plan);
  const levels = Math.max(1, ...placed.map((p) => p.level + 1));
  const rows = Math.max(1, ...placed.map((p) => p.row + 1));
  const height = vertical
    ? Math.min(560, levels * (NODE_H + GAP_X / 1.5) + 60)
    : Math.min(420, Math.max(190, rows * (NODE_H + GAP_Y) + 70));
  return (
    <div
      ref={box}
      className="w-full overflow-hidden rounded-lg border border-line bg-[radial-gradient(var(--line)_1px,transparent_1px)] [background-size:16px_16px]"
      style={{ height }}
      aria-label={`Plan graph: ${plan.length} subtasks in ${levels} step${levels > 1 ? "s" : ""}`}
      role="group"
    >
      <ReactFlow
        key={vertical ? "v" : "h"}
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        colorMode={theme}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
        minZoom={0.4}
        maxZoom={1.5}
        nodesConnectable={false}
        nodesDraggable={false}
        elementsSelectable={false}
        zoomOnScroll={false}
        preventScrolling={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} size={0} color="transparent" />
        <Controls showInteractive={false} position="bottom-right" />
      </ReactFlow>
    </div>
  );
}
