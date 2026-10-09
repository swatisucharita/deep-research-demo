"""Research workflow: runs the agents in order for one chat message.

Steps: Intake (clarify the request) -> Competitor, Demand and Feasibility, in parallel
-> Decision (go / no go).
Intake may ask a clarifying question; the workflow then returns it with `pending` set,
and the user's next message (the answer) continues that conversation.

Each message is one OpenAI trace ("Deep research workflow") with a span per step and the
agent runs nested under them. Messages of one conversation share a group_id.
"""

import asyncio
import re
from datetime import date
from pathlib import Path
from collections.abc import Callable
from dataclasses import dataclass, field

from agents import TResponseInputItem, custom_span, gen_trace_id, trace

from app.config import PROJECT_ROOT, settings
from app.competitor_agent import run_competitor
from app.decision_agent import run_decision
from app.demand_agent import run_demand
from app.documents import list_documents
from app.feasibility_agent import run_feasibility
from app.intake_agent import MAX_QUESTIONS, IntakeResult, load_context, run_intake
from app.models import TRACING_ENABLED

TRACE_URL = "https://platform.openai.com/traces/trace?trace_id={}"
REPORTS_DIR = PROJECT_ROOT / "reports"  # each finished report is saved here for download

# Reports progress to the UI: (step id, status, detail). Status is "running", "done" or
# "waiting" (a question for the user) or "error". Step ids: context, intake, question,
# brief, competitor, demand, feasibility, decision.
StepCallback = Callable[[str, str, str], None]


@dataclass
class WorkflowResult:
    answer: str  # Markdown shown in the chat
    pending: list[TResponseInputItem] = field(default_factory=list)  # Intake Q&A to continue
    intake: IntakeResult | None = None
    trace_url: str = ""  # this run in the OpenAI dashboard; empty when tracing is off
    report_path: str = ""  # the downloadable report (.md); empty until a run finishes


async def run_workflow(
    message: str,
    pending: list[TResponseInputItem],
    conversation_id: str | None = None,
    on_step: StepCallback = lambda *_: None,
) -> WorkflowResult:
    """Run the workflow for `message`.

    `pending` is the Intake conversation so far (empty for a new request); `message` is
    then the user's answer to the last question. `conversation_id` groups the traces of
    one chat conversation. `on_step` is called as each step starts and finishes.
    """
    if not settings.openai_api_key:
        return WorkflowResult("OPENAI_API_KEY is not set. Copy `.env.example` to `.env` and add your key.")

    trace_id = gen_trace_id()
    with trace(
        "Deep research workflow",
        trace_id=trace_id,
        group_id=conversation_id,
        metadata={"message": message[:500], "turn": str(len(pending) // 2 + 1)},
    ):
        result = await _run(message, pending, on_step)
    if TRACING_ENABLED:
        result.trace_url = TRACE_URL.format(trace_id)
    return result


async def _run(
    message: str, pending: list[TResponseInputItem], on_step: StepCallback
) -> WorkflowResult:
    on_step("context", "running", "")
    with custom_span("Load product context") as span:
        context = load_context()
        span.span_data.data = {"docs_dir": str(settings.docs_dir), "chars": len(context)}
    docs = ", ".join(str(d) for d in list_documents(settings.docs_dir)) or "no documents"
    on_step("context", "done", f"{docs} · {len(context):,} chars")

    # Step 1: Intake
    on_step("intake", "running", settings.openai_model)
    with custom_span("Intake") as span:
        intake, conversation = await run_intake(
            pending + [{"role": "user", "content": message}], context
        )
        span.span_data.data = {"status": intake.status}
    on_step("intake", "done", f"{settings.openai_model} · {intake.status}")
    if intake.status == "needs_clarification":
        asked = sum(1 for item in conversation if item.get("role") == "assistant")
        on_step("question", "waiting", f"question {asked} of {MAX_QUESTIONS} · waiting for your answer")
        return WorkflowResult(intake.question, pending=conversation, intake=intake)
    on_step("brief", "done", f"{len(intake.unknowns)} unknown(s) listed")

    # Steps 2-4 need only the brief, so they run at the same time. Each returns its report;
    # a failed step is noted in the report and the others continue.
    async def competitor():
        on_step("competitor", "running", f"{settings.competitor_model} · search, then recheck unclear")
        with custom_span("Competitor") as span:
            report = await run_competitor(intake.summary.app_summary, intake.refined_query)
            span.span_data.data = {"competitors": len(report.competitors), "searches": report.searches}
        on_step("competitor", "done", f"{settings.competitor_model} · {report.summary} · "
                f"{report.searches} search{'es' if report.searches != 1 else ''}")
        return report

    async def demand():
        on_step("demand", "running", f"{settings.demand_model} · {len(intake.sub_queries)} queries, 1 search")
        with custom_span("Demand") as span:
            report = await run_demand(intake.summary.app_summary, intake.refined_query, intake.sub_queries)
            span.span_data.data = {"score": report.score, "signals": len(report.signals)}
        on_step("demand", "done", f"{settings.demand_model} · score {report.score}/5 · {len(report.signals)} signals")
        return report

    async def feasibility():
        on_step("feasibility", "running", f"{settings.feasibility_model} · reading {settings.codebase_dir}")
        with custom_span("Feasibility") as span:
            report = await run_feasibility(
                intake.refined_query, intake.summary.feature_summary, intake.unknowns
            )
            span.span_data.data = {"feasibility": report.feasibility, "tool_calls": report.tool_calls}
        on_step("feasibility", "done", f"{settings.feasibility_model} · {report.feasibility} · "
                f"{report.effort.dev_hours} dev h · {report.tool_calls} tool calls")
        return report

    errors: dict[str, str] = {}

    async def run_step(step_id: str, step):
        try:
            return await step()
        except Exception as e:  # noqa: BLE001 - one failed step shouldn't sink the others
            on_step(step_id, "error", str(e))
            errors[step_id] = f"{type(e).__name__}: {e}"
            return None

    reports = await asyncio.gather(
        run_step("competitor", competitor), run_step("demand", demand),
        run_step("feasibility", feasibility),
    )

    if not any(reports):
        return WorkflowResult(_failed("all three research steps failed", errors), intake=intake)

    # Step 5: the Decision agent decides go / no go and writes the business report.
    on_step("decision", "running", f"{settings.decision_model} · deciding and writing the report")
    try:
        with custom_span("Decision") as span:
            decision = await run_decision(intake, *reports, errors)
            span.span_data.data = {"decision": decision.decision, "confidence": decision.confidence,
                                   "report_chars": len(decision.report)}
        on_step("decision", "done", f"{settings.decision_model} · {decision.decision.upper()} · "
                f"confidence {decision.confidence}")
    except Exception as e:  # noqa: BLE001 - say what failed instead of a blank chat
        on_step("decision", "error", str(e))
        return WorkflowResult(_failed(f"the Decision agent failed ({e})", errors), intake=intake)

    path = _save_report(decision.report, intake.refined_query)
    return WorkflowResult(decision.report, intake=intake, report_path=str(path))


def _failed(why: str, errors: dict[str, str]) -> str:
    lines = [f"⚠️ No report: {why}.", ""] + [f"- {step}: {err}" for step, err in errors.items()]
    return "\n".join(lines)


def _save_report(markdown: str, feature: str) -> Path:
    """Save the report as reports/<date>-<feature>.md for the download button."""
    REPORTS_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", feature.lower()).strip("-")[:60] or "feature"
    path = REPORTS_DIR / f"{date.today():%Y-%m-%d}-{slug}.md"
    path.write_text(markdown, encoding="utf-8")
    return path
