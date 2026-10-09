"""Demand agent (POC): one web search runs Intake's 3 sub-queries across all domains and
collects evidence that people want the feature.

Code drops signals the search did not return, sets each signal's source from its URL
(forum, review, news or other; "other" does not count) and computes the score 1-5.

Try it: uv run python -m app.demand_agent "members pause membership from the app" \
  "can't pause gym membership" "freeze plan app review" "gym freeze trend India"
"""

import asyncio
import sys
from collections import Counter
from typing import Literal

from agents import Agent, ModelSettings, Runner, WebSearchTool
from openai.types.responses import ResponseFunctionWebSearch
from openai.types.shared import Reasoning
from pydantic import BaseModel, Field

from app.competitor_agent import _domain, _url
from app.config import settings
from app.models import build_model

MAX_SIGNALS = 8
Source = Literal["forum", "review", "news", "other"]

# Sources are set from the URL, not by the model. Subdomains match too.
FORUMS = ("reddit.com", "quora.com")
REVIEWS = ("play.google.com", "apps.apple.com", "g2.com", "capterra.com", "capterra.in",
           "softwaresuggest.com", "trustpilot.com", "getapp.com")
NEWS = ("economictimes.indiatimes.com", "livemint.com", "business-standard.com",
        "yourstory.com", "inc42.com", "thehindu.com", "moneycontrol.com", "statista.com")


class Signal(BaseModel):
    sub_query: int = Field(description="1, 2 or 3: which query found it.")
    kind: Literal["request", "complaint", "workaround", "competitor praise", "market data"]
    who: Literal["member", "studio staff", "unknown"]
    quote: str = Field(description="One sentence, quoted or closely paraphrased from the page.")
    url: str = Field(description="The search result URL.")
    date: str = Field(description="Date shown on the page, or empty.")


class SignalList(BaseModel):
    signals: list[Signal]


class SourcedSignal(Signal):
    source: Source


class DemandReport(BaseModel):
    feature: str
    sub_queries: list[str]  # the queries the search actually ran
    score: int
    score_reason: str
    counts: dict[str, int]
    who: dict[str, int]
    signals: list[SourcedSignal]
    searches: int


INSTRUCTIONS = """\
You look for evidence that people want one feature. Make exactly one web search containing
the queries you are given, as separate queries, word for word.

Record a signal for each result that shows demand: someone asking for the feature,
complaining about the problem it solves, describing a workaround, praising a product that
has it, or market data about interest in it.
- Only results about this feature or its problem; skip loosely related pages and pages that
  just sell software.
- who: "member" for a gym or studio customer; "studio staff" for an owner, manager, front
  desk or trainer; "unknown" if you cannot tell.
- Finding nothing is a valid result: return an empty list. Never use your own knowledge.
"""


def _source(url: str) -> Source:
    d = _domain(url)
    match = lambda domains: any(d == x or d.endswith("." + x) for x in domains)  # noqa: E731
    if match(FORUMS):
        return "forum"
    if match(REVIEWS):
        return "review"
    if match(NEWS):
        return "news"
    return "other"


def _score(signals: list[SourcedSignal]) -> tuple[int, str]:
    """Score 1-5 from the signals that count (not "other")."""
    counted = [s for s in signals if s.source != "other"]
    sources = {s.source for s in counted}
    names = " and ".join(sorted(sources))
    pain = any(s.kind in ("complaint", "workaround") for s in counted)
    if not counted:
        return 1, "No signals found in forums, reviews or news (not the same as no demand)"
    if len(sources) == 1:
        return (3, f"{len(counted)} signals, {names} only") if len(counted) >= 3 \
            else (2, f"{len(counted)} signal(s), {names} only")
    if not pain:
        return 3, f"{len(counted)} signals from {names}, but no complaints or workarounds"
    if "news" in sources:
        return 5, f"{len(counted)} signals from {names}, incl. complaints and market data"
    return 4, f"{len(counted)} signals from {names}, incl. complaints"


async def run_demand(app_summary: str, feature: str, sub_queries: list[str]) -> DemandReport:
    agent = Agent(
        name="Demand",
        instructions=INSTRUCTIONS,
        tools=[WebSearchTool(user_location={"type": "approximate", "country": "IN"},
                             search_context_size="low")],
        output_type=SignalList,
        model=build_model(settings.demand_model),
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="low")
            if settings.demand_model.startswith(("gpt-5", "gpt-6", "o")) else None,
            extra_args={"max_tool_calls": 1},
            response_include=["web_search_call.action.sources"],
        ),
    )
    queries = "\n".join(f"{i}. {q}" for i, q in enumerate(sub_queries, start=1))
    result = await Runner.run(
        agent, f"Product: {app_summary}\nFeature: {feature}\nQueries:\n{queries}"
    )

    calls = [i.raw_item for i in result.new_items
             if isinstance(getattr(i, "raw_item", None), ResponseFunctionWebSearch)]
    seen = {_url(s.url) for c in calls for s in (getattr(c.action, "sources", None) or [])}
    ran = [q for c in calls for q in (getattr(c.action, "queries", None)
                                      or [getattr(c.action, "query", None)]) if q]

    signals, urls = [], set()
    for s in result.final_output.signals:
        if _url(s.url) not in seen or _url(s.url) in urls:
            continue  # not returned by the search, or a duplicate
        urls.add(_url(s.url))
        signals.append(SourcedSignal(**s.model_dump(), source=_source(s.url)))
    signals = signals[:MAX_SIGNALS]

    score, reason = _score(signals)
    return DemandReport(
        feature=feature, sub_queries=ran, score=score, score_reason=reason,
        counts=dict(Counter(s.source for s in signals)), who=dict(Counter(s.who for s in signals)),
        signals=signals, searches=len(calls),
    )


if __name__ == "__main__":
    print(asyncio.run(run_demand("", sys.argv[1], sys.argv[2:])).model_dump_json(indent=2))
