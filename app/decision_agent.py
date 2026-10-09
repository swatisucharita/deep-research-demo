"""Decision agent: decides go / no go and writes the business report.

One model call, no tools. It reads the Intake brief and the Competitor, Demand and
Feasibility reports, decides with the feature's goal weighted first, and returns the
decision, its confidence and the full report in Markdown (shown in the chat, downloadable).
Code adds two guards: "not feasible" can never be "go", and confidence is "low" when a
research report is missing.
"""

import json
from datetime import date
from typing import Literal

from agents import Agent, ModelSettings, Runner
from openai.types.shared import Reasoning
from pydantic import BaseModel, Field

from app.competitor_agent import CompetitorReport
from app.config import settings
from app.demand_agent import DemandReport
from app.feasibility_agent import FeasibilityReport
from app.intake_agent import IntakeResult
from app.models import build_model


class Decision(BaseModel):
    decision: Literal["go", "no go"]
    confidence: Literal["low", "medium", "high"]
    report: str = Field(description="The full business report in Markdown, in the required structure.")


INSTRUCTIONS = """\
You make the final go / no go decision on a feature and write it up as a business report
for product managers and founders.

## How to decide
Weigh the inputs in this order of importance:
1. Goal (most important). Does the feature solve a real problem for the product's users,
   as described in the brief and product summary? A feature that does not serve a clear
   goal is "no go", however cheap or common it is.
2. Demand. Evidence that people want it. A low score with few signals means "not found",
   not "no demand".
3. Competitors. Most having it suggests a gap to close; few having it suggests a
   differentiator. Neither alone decides.
4. Feasibility and effort. Only a real blocker overrides a strong goal. If feasibility is
   "not feasible", the decision must be "no go".
If a research report is missing, say so in its section and set confidence to "low".

## How to write the report
Plain business language, short sentences, no JSON. Use only the inputs: never add facts,
competitors, numbers or links, and copy scores, hours, weeks, file paths and links exactly.
The recommendation heading and confidence must match your decision and confidence fields.
Use exactly this structure:

# Feature assessment: <feature>
*<date> · FitSlot feature research*

## Recommendation: ✅ GO   (or: ## Recommendation: ⛔ NO GO)
**Confidence:** High / Medium / Low
**Goal.** Whose problem it solves, and how.
**Why.** 2-3 sentences, led by the goal, then the evidence that decided it.

### Key evidence
A table with columns: (mark) | Area | Finding. Mark ✅ supports go, ⛔ supports no go,
➖ neutral (missing evidence, e.g. no signals found, is neutral). Areas: Goal, Demand,
Competitors, Feasibility; most important first.

### Risks
Bullets: what could make this the wrong call.

### Before we build   (for go)  /  ### What would change the answer   (for no go)
Bullets.

---

## Market: what competitors offer
The competitor summary line and number of searches, then a table: Competitor (linked to its
website) | Has it (✅ Yes, 🟡 Partial, ❌ No, ❔ Unclear) | What the source says | Source (link).

## Demand: do people want it?
The demand score out of 5 and its reason, the queries searched, then a table of signals if
any: Source | Kind | Who | What they said | Link.

## Feasibility and effort
The assessment and reason; an effort table (Development hours, QA hours, Team, Calendar
time); "What already exists" bullets with file paths and lines; a "What needs to change"
table (Area | Type | Size | Hours | Work); an "Architecture" table (Component | Status as
🟢 New, 🟡 Changed or ⚪ Existing | Role) followed by the flow as bullets; open questions.

---

## About this assessment
Bullets: the original request, the product, details not specified, and the method: web
search for competitors and demand, a read-only review of the codebase, and this decision
weighing the goal first; effort uses fixed hours per change size, so it is a range, not a quote.
"""


async def run_decision(
    intake: IntakeResult,
    competitor: CompetitorReport | None,
    demand: DemandReport | None,
    feasibility: FeasibilityReport | None,
    errors: dict[str, str],
) -> Decision:
    """`errors` explains any missing report (step id → why it failed)."""
    reports = {"competitor": competitor, "demand": demand, "feasibility": feasibility}
    missing = [name for name, r in reports.items() if r is None]
    inputs = {
        "date": f"{date.today():%d %B %Y}",
        "feature": intake.refined_query,
        "original_request": intake.original_query,
        "feature_summary": intake.summary.feature_summary,
        "product_summary": intake.summary.app_summary,
        "details_not_specified": intake.unknowns,
        **{f"{name}_report": r.model_dump() if r else f"missing: {errors.get(name, 'step failed')}"
           for name, r in reports.items()},
    }
    agent = Agent(
        name="Decision",
        instructions=INSTRUCTIONS,
        output_type=Decision,
        model=build_model(settings.decision_model),
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="medium")
            if settings.decision_model.startswith(("gpt-5", "gpt-6", "o")) else None,
        ),
    )
    result = await Runner.run(agent, json.dumps(inputs, ensure_ascii=False), max_turns=1)
    decision = result.final_output

    # Guards in code, so they hold whatever the model says.
    if feasibility and feasibility.feasibility == "not feasible" and decision.decision == "go":
        decision.decision = "no go"
        decision.report = ("> ⛔ **Changed to NO GO:** the codebase review found this feature not "
                           "feasible.\n\n" + decision.report)
    if missing:
        decision.confidence = "low"
    return decision
