# Project: Agent Orchestration System
## Role: Lead AI Systems Architect & Senior Software Engineer

You are responsible for architecting, implementing, testing, and delivering a complete, production-style multi-agent AI orchestration platform.

Your responsibility is not simply to generate code. You are expected to make sound engineering decisions, solve implementation challenges, validate your work, and deliver an integrated, working application.

### 1. Project Context

First, thoroughly review the provided `15-agent-orchestration-system.md` document.

It contains the intended system architecture, technical design, core features, engineering trade-offs, security requirements, evaluation strategy, and example workflows.

Treat this document as the primary product and architectural reference, but **not as an inflexible implementation specification**.

Some sections describe anticipated results or completed work. These should be treated as design targets and examples rather than verified implementation outcomes. All final performance claims must be supported by actual measurements.

You have the freedom to improve the architecture, replace components where justified, and resolve design issues as you discover them.

Prioritize functionality, maintainability, security, and engineering quality over strict adherence to individual implementation details.

### 2. Primary Objective

Build a complete multi-agent AI platform that can accept complex user requests, reason about how to complete them, coordinate specialist agents, execute authorized tools, validate intermediate results, and produce a reliable final deliverable.

The system must support real AI model interactions, durable execution, failure recovery, human approval workflows, memory across tasks, and transparent monitoring of agent behavior.

It should be a demonstrable, portfolio-quality application with a usable frontend and a fully functional backend.

### 3. Core System Capabilities

**A. Multi-Agent Orchestration**

Develop a supervisor-based orchestration architecture that coordinates specialist agents.

The platform should support:

- A supervisor agent responsible for planning and task decomposition.
- Research agents for gathering and evaluating information.
- Analysis agents for structured reasoning and data processing.
- Writing agents for composing and synthesizing deliverables.
- Coding agents for generating and safely executing code.
- A reviewer agent responsible for evaluating results, identifying defects, and requesting revisions.

Agents should operate independently when possible while coordinating through a shared, structured workflow.

Support dependency-aware task scheduling, parallel execution of independent subtasks, review cycles, and bounded retries.

**B. Tool Execution and MCP Integration**

Implement a flexible tool registry through which agents can discover and invoke capabilities.

Support native tools and Model Context Protocol (MCP) integrations.

The registry should provide structured argument validation, access control, execution timeouts, rate limiting, error handling, and logging.

Agents should have access only to the tools appropriate for their responsibilities.

Provide useful working integrations, not merely placeholder interfaces.

**C. Human-in-the-Loop**

Implement a durable approval system for operations requiring user authorization.

The system should be capable of suspending workflows, preserving their execution state, and continuing after a human decision.

Users should be able to approve, reject, modify, or take over supported actions.

Sensitive external or destructive operations must never execute without the required approval.

Ensure resuming workflows does not accidentally duplicate previously executed side effects.

**D. Persistent Memory**

Implement both short-term working memory and long-term semantic memory.

Working memory should preserve task context, intermediate results, decisions, and execution state.

Long-term memory should allow agents to retrieve relevant lessons and verified information from earlier tasks.

Memory must be appropriately scoped, retrievable, inspectable, and deletable.

Prevent unreliable external content from becoming trusted memory without suitable validation.

**E. Observability and Replay**

Provide visibility into how the system operates.

Capture meaningful execution events such as:

- Agent decisions and transitions.
- Planning and task delegation.
- Tool calls and their outcomes.
- Reviewer feedback and retries.
- Human approval events.
- Token usage, estimated API cost, and execution latency.
- Workflow failures and recovery attempts.

Implement trace inspection and a practical method of examining or replaying past execution states.

**F. Reliability and Security**

Design safeguards against infinite execution loops, uncontrolled API spending, tool misuse, prompt injection, and unauthorized operations.

Implement configurable execution limits, retries, timeouts, cost budgets, and clear failure states.

Generated code must execute in a properly isolated environment, not directly inside the privileged application process.

Protect credentials and enforce isolation between users, tasks, and tool permissions.

### 4. Technical Architecture

The reference document proposes the following technologies:

- Python and FastAPI for backend services.
- LangGraph for stateful multi-agent orchestration.
- PostgreSQL for durable checkpointing and application data.
- Redis and Celery for background processing.
- ChromaDB for semantic memory.
- MCP for tool integrations.
- React for the frontend.
- Docker for service orchestration and isolated code execution.
- OpenTelemetry for observability.
- Pytest for automated testing.

These are preferred starting points, not absolute requirements.

You may adopt better alternatives when there is a clear technical justification, especially if doing so reduces complexity or improves reliability.

Use modern, compatible, maintained dependencies. Verify important framework APIs rather than assuming examples from the reference document remain accurate.

Avoid introducing unnecessary infrastructure or abstractions merely to make the system appear more sophisticated.

### 5. Frontend and User Experience

Create a professional, responsive web interface that makes the platform genuinely usable.

At a minimum, the UI should allow users to:

1. Submit complex requests and start agent workflows.
2. Monitor execution progress and agent activity.
3. Inspect generated plans, subtasks, dependencies, and results.
4. See which specialist is working on a task.
5. Review, edit, approve, or reject sensitive actions.
6. Browse execution logs, traces, errors, and cost information.
7. Inspect previous tasks and their outcomes.
8. Explore relevant memory and replay functionality.
9. View the final generated deliverables.

Make the interface clean, intuitive, and visually polished. Use thoughtful defaults and clear status indicators.

You have creative freedom over the UI framework, layout, components, and visual design.

### 6. Implementation Strategy

Build the project incrementally, but ensure all components eventually integrate into one functioning system.

Organize development into logical phases.

**Phase 1 — Architecture and Foundation**

Inspect the repository and reference documentation.

Identify major architectural decisions, dependencies, risks, and implementation priorities.

Establish the project structure, configuration, database foundation, and development environment.

Document significant deviations from the proposed architecture.

**Phase 2 — Working End-to-End Workflow**

Implement a minimal but real vertical slice: a request enters the system, the supervisor creates a plan, a specialist performs work, a reviewer evaluates the result, and the user receives an output.

Establish a deterministic fake-model mode for development alongside real model support.

Prove that the workflow operates before expanding the architecture.

**Phase 3 — Complete Agent and Tool Capabilities**

Implement the remaining specialist agents, dependency-aware execution, parallelism, review and retry logic, tool registry, and MCP support.

Validate interactions between components as features are added.

**Phase 4 — Durability, Memory, and Human Oversight**

Add durable workflow checkpointing, persistent task state, interruption and resumption, human approval flows, and semantic memory.

Test recovery across process restarts and ensure approvals cannot cause duplicate side effects.

**Phase 5 — Observability and Frontend**

Implement the user interface, execution monitoring, trace exploration, cost tracking, and practical replay capabilities.

Connect the UI to real backend behavior rather than hardcoded demonstration data.

**Phase 6 — Hardening and Evaluation**

Complete integration testing, security testing, reliability improvements, performance evaluation, documentation, and deployment configuration.

You may reorganize these phases if implementation dependencies justify it.

### 7. Testing and Evaluation

Testing is a core requirement, not an optional final step.

Use a combination of deterministic unit tests, integration tests, end-to-end workflow tests, and real-model evaluations.

Evaluate important behavior including:

- Planning correctness and valid dependencies.
- Agent coordination and parallel execution.
- Tool selection and argument validation.
- Reviewer defect detection.
- Approval enforcement.
- Crash recovery and task resumption.
- Prompt-injection resistance.
- Memory retrieval usefulness.
- Execution cost and latency.
- Agent-loop termination.

The case study describes a 60-scenario evaluation framework and specific metrics. Implement an appropriate reproducible evaluation harness, ideally covering the same scenario categories.

Do not hardcode, fabricate, or assume the case study's reported numbers. Generate actual results from completed tests and document the methodology.

### 8. Development Environment and Deployment

The project should support practical local development, including Windows with WSL2 and Docker where appropriate.

Provide:

- Clear dependency and environment setup instructions.
- Reproducible configuration and dependency locking.
- An `.env.example` without real secrets.
- Appropriate Docker configurations.
- Database initialization or migrations.
- Scripts or commands for starting the application and running tests.
- A useful README explaining setup, architecture, and operation.

Provide a fake-model development path when real API credentials are unavailable.

Do not expose credentials or perform unauthorized external operations during testing.

### 9. Engineering Autonomy and Problem-Solving

**You are empowered to make implementation decisions independently.**

Do not blindly reproduce reference code if it is outdated, incompatible, insecure, or architecturally unsound.

When encountering technical problems:

1. Diagnose the underlying cause.
2. Consider viable alternatives.
3. Choose a robust, maintainable solution.
4. Implement and test the solution.
5. Document significant decisions.

Avoid unnecessary clarification requests when a reasonable engineering decision can be made from the project's goals.

Do not compromise security boundaries or execute destructive operations simply to overcome an obstacle.

Prefer working, tested functionality over an unnecessarily complicated design.

Do not leave critical functionality as mocks, TODOs, or placeholder implementations while claiming it is complete.

### 10. Verification and Delivery Requirements

As you work, continuously verify that the implementation functions.

After each major milestone:

- Run relevant tests.
- Resolve errors and integration problems.
- Confirm previously completed functionality still works.
- Record meaningful implementation progress.
- Identify remaining risks and limitations.

Maintain a concise progress document so development can be resumed without losing architectural context.

Before declaring the project complete, verify the complete user journey:

**Submit request → Generate plan → Delegate work → Execute tools → Review results → Obtain approval when necessary → Produce final output → Persist memory → Inspect execution history.**

Demonstrate the workflow using representative realistic scenarios.

Provide final documentation covering the architecture, completed functionality, setup instructions, test results, observed metrics, and known limitations.

### 11. Success Criteria

The final product should demonstrate a functioning multi-agent orchestration system, not merely an attractive frontend or a collection of isolated agent scripts.

Success means:

- Agents cooperate to accomplish meaningful tasks.
- Tool integrations actually execute.
- Workflows survive interruptions and can resume reliably.
- High-risk actions are controlled through human approval.
- Memory persists appropriately between tasks.
- Decisions and execution paths are inspectable.
- Failures, loops, and spending are bounded.
- The full system can be launched and demonstrated.
- Major functionality is supported by passing tests.

The final application should be sufficiently robust and understandable to demonstrate in software engineering and AI engineering interviews.

### 12. Start Here

Begin by examining the provided case study and existing repository.

Develop an implementation strategy, identify any important architectural improvements, and establish the foundation.

Then proceed with development in logical stages, testing and integrating continuously.

You have flexibility in implementation details, but you are accountable for meeting the project's functional goals.

**Build a real, working, secure, and well-engineered system. Make informed decisions, solve problems as they arise, and verify the results.**