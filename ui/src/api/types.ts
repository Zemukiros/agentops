// Shapes returned by the AgentOps API (agentops/api/routes/*.py, agentops/api/views.py).

export type Role = "user" | "reviewer";
export interface Me {
  id: string;
  name: string;
  role: Role;
}

export type TaskStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  | "completed"
  | "completed_partial"
  | "failed"
  | "rejected"
  | "budget_exceeded"
  | "cancelled";

export const TERMINAL: ReadonlySet<TaskStatus> = new Set([
  "completed",
  "completed_partial",
  "failed",
  "rejected",
  "budget_exceeded",
  "cancelled",
]);

export interface Task {
  id: string;
  request: string;
  graph: string;
  status: TaskStatus;
  kind: "task" | "replay";
  parent_task_id: string | null;
  root_task_id: string | null;
  result: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface TaskListItem {
  id: string;
  request: string;
  status: TaskStatus;
  kind: "task" | "replay";
  root_task_id: string | null;
  scenario: string | null;
  cost_usd: number;
  llm_calls: number;
  duration_s: number;
  pending_approvals: number;
  branches: number;
  created_at: string;
  updated_at: string;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface Verdict {
  verdict: "accept" | "revise" | "escalate";
  score: number;
  issues: string[];
  feedback_for_specialist: string | null;
  unsupported_claims: string[];
}

export interface SubtaskView {
  id: string;
  description: string;
  specialist: string;
  depends_on: string[];
  expected_output: string;
  complexity: string;
  status: string;
  revisions: number;
  output: string | null;
  reason: string | null;
  human_override: boolean;
  reviews: (Verdict & { revision: number })[];
}

export interface ActionOutcome {
  hash: string;
  tool: string;
  status: "done" | "failed" | "unknown" | "rejected" | "human_done" | "not_considered";
  idempotency_key: string | null;
  detail: string | null;
}

export interface ActionView {
  hash: string;
  tool: string;
  risk: string;
  agent: string;
  subtask_id: string;
  args: Record<string, unknown>;
  rationale: string | null;
  approval: { id: string; status: string; version: number; decision: unknown } | null;
  decision: string | null;
  outcome: ActionOutcome | null;
  side_effect_status: string | null;
  mailpit_url: string | null;
}

export interface ApprovalSummary {
  id: string;
  kind: string;
  key: string;
  status: string;
  version: number;
  source: string;
  blocking: boolean;
  created_at: string;
  decided_at: string | null;
  expires_at: string;
}

export interface Budget {
  max_usd: number;
  max_tokens: number;
  max_calls: number;
  used_usd: number;
  used_tokens: number;
  used_calls: number;
}

export interface TaskDetail {
  id: string;
  request: string;
  graph: string;
  status: TaskStatus;
  kind: "task" | "replay";
  parent_task_id: string | null;
  root_task_id: string | null;
  fork_checkpoint_id: string | null;
  scenario: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  budget: Budget | null;
  plan: { confidence: number; rationale: string } | null;
  plan_confidence: {
    self_reported: number;
    structural: number;
    effective: number;
    threshold: number;
  } | null;
  memories: { id: string; kind: string; text: string; outcome: string | null; similarity: number }[];
  subtasks: SubtaskView[];
  actions: ActionView[];
  approvals: ApprovalSummary[];
  final: string | null;
  final_review: Verdict | null;
  partial: boolean;
  outcome: { status: string; reason: string | null } | null;
  failure_reason: string | null;
  explanation: string | null;
}

export interface TaskEvent {
  id: number;
  type: string;
  node: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Approval {
  id: string;
  task_id: string;
  kind: string;
  key: string;
  status: string;
  version: number;
  payload: Record<string, unknown>;
  decision: Record<string, unknown> | null;
  source: string;
  blocking: boolean;
  expires_at: string;
  created_at: string;
  decided_at: string | null;
  decided_by: string | null;
  task_kind: "task" | "replay";
  task_request: string;
}

export interface TraceItem {
  id: number;
  type: string;
  node: string | null;
  at: string;
  kind: "event" | "llm" | "tool";
  agent?: string;
  purpose?: string;
  model?: string;
  attempt?: number;
  ok?: boolean;
  retry?: boolean;
  error?: unknown;
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number | null;
  latency_ms?: number;
  output?: unknown;
  input?: unknown;
  tool?: string;
  risk?: string;
  status?: string;
  subtask_id?: string | null;
  llm_call_id?: number;
  tool_call_id?: number;
  payload?: unknown;
}

export interface CostBucket {
  calls: number;
  errors: number;
  retries: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  latency_ms: number;
}

export interface CostSummary {
  task_id: string;
  total: CostBucket;
  by_agent: Record<string, CostBucket>;
  by_model: Record<string, CostBucket>;
  tools: Record<string, { calls: number; errors: number; latency_ms: number }>;
  budget: Budget | null;
  time: { wall_s: number; compute_s: number; human_wait_s: number; queue_s: number };
  approvals: { kind: string; key: string; status: string; blocking: boolean; wait_s: number }[];
}
