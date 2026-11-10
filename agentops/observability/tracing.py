"""OpenTelemetry tracing (ADR-019, Phase 5). Off unless OTEL_ENABLED=true; then spans are
exported over OTLP/HTTP (Jaeger under the `observability` compose profile).

One trace per task: the trace id IS the task UUID (uuid.hex), so a task is found in Jaeger by
its id. Spans:
- run.segment   one per worker run segment (root span of the task's trace)
- node.<name>   one per main-graph node execution
- llm.call      one per model attempt   (agentops.llm_call_id  = llm_calls.id)
- tool.call     one per tool invocation (agentops.tool_call_id = tool_calls.id)
Every event row is also added to the current span as a span event, and call spans carry
agentops.event_id = the id of their llm.call / tool.call event. So ids match our tables.

No prompts, outputs or arguments go into spans: only ids, names, counts, costs, timings.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.trace import Status, StatusCode

from agentops.settings import Settings

_task_trace_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "agentops_task_trace_id", default=None
)
_provider: TracerProvider | None = None


class TaskTraceIdGenerator(RandomIdGenerator):
    """Root spans started inside `task_trace(task_id)` get trace id = the task's UUID."""

    def generate_trace_id(self) -> int:
        forced = _task_trace_id.get()
        return forced if forced else super().generate_trace_id()


def make_provider(service_name: str, *processors: SpanProcessor) -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}),
        id_generator=TaskTraceIdGenerator(),
    )
    for p in processors:
        provider.add_span_processor(p)
    return provider


def use_provider(provider: TracerProvider | None) -> None:
    """Install (or, with None, remove) the provider our tracer uses. Tests use this with an
    in-memory exporter; the global OTel provider is never touched."""
    global _provider  # noqa: PLW0603
    _provider = provider


def setup(settings: Settings, service_name: str) -> TracerProvider | None:
    if not settings.otel_enabled:
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

    endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
    provider = make_provider(service_name, BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    use_provider(provider)
    return provider


def tracer() -> trace.Tracer:
    if _provider is None:
        return trace.NoOpTracer()
    return _provider.get_tracer("agentops")


def trace_id_for(task_id: uuid.UUID) -> str:
    """The Jaeger trace id of a task."""
    return task_id.hex


@contextmanager
def task_trace(task_id: uuid.UUID, name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Root span of one run segment, in the task's trace."""
    token = _task_trace_id.set(task_id.int)
    try:
        with tracer().start_as_current_span(
            name,
            context=trace.set_span_in_context(trace.INVALID_SPAN),  # always a root span
            attributes={"agentops.task_id": str(task_id), **_clean(attributes)},
        ) as span:
            yield span
    finally:
        _task_trace_id.reset(token)


def traced_node(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a graph node in a span. functools.wraps keeps the signature LangGraph inspects
    (state, runtime). An interrupt is a pause, not an error."""

    def _finish(span: trace.Span, exc: BaseException) -> None:
        from langgraph.errors import GraphBubbleUp  # noqa: PLC0415

        if isinstance(exc, GraphBubbleUp):
            span.set_attribute("agentops.paused", True)
        else:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with tracer().start_as_current_span(
                f"node.{name}",
                attributes={"agentops.node": name},
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                try:
                    return await fn(*args, **kwargs)
                except BaseException as exc:
                    _finish(span, exc)
                    raise

        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        with tracer().start_as_current_span(
            f"node.{name}",
            attributes={"agentops.node": name},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                return fn(*args, **kwargs)
            except BaseException as exc:
                _finish(span, exc)
                raise

    return sync_wrapper


def call_span(
    name: str, latency_ms: int, attributes: dict[str, Any], error: str | None = None
) -> None:
    """A finished span for an LLM/tool call that just ended (end = now, start = end - latency)."""
    end = time.time_ns()
    span = tracer().start_span(
        name, start_time=end - max(0, latency_ms) * 1_000_000, attributes=_clean(attributes)
    )
    if error:
        span.set_status(Status(StatusCode.ERROR, error[:200]))
    span.end(end_time=end)


def add_event(type_: str, event_id: int | None, node: str | None, task_id: uuid.UUID) -> None:
    span = trace.get_current_span()
    if not span.is_recording():
        return
    span.add_event(
        type_,
        _clean(
            {"agentops.event_id": event_id, "agentops.node": node, "agentops.task_id": str(task_id)}
        ),
    )


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    """OTel attributes must be str/bool/int/float (or sequences of them); drop None."""
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if v is None:
            continue
        out[k] = v if isinstance(v, (str, bool, int, float)) else str(v)
    return out
