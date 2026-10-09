"""Competitor agent (POC): one web search finds up to 5 competitors and checks each one
for the feature; competitors left "unclear" get one more search on their own website.

Input is the app summary and the refined feature from the Intake brief. Code drops
competitors the search did not return, marks unverified evidence "unclear" and writes the
summary.

Try it: uv run python -m app.competitor_agent "FitSlot: booking software for fitness studios in India" "membership pause"
"""

import asyncio
import sys
from collections import Counter
from typing import Literal

from agents import Agent, ModelSettings, Runner, WebSearchTool
from openai.types.responses import ResponseFunctionWebSearch
from openai.types.shared import Reasoning
from pydantic import BaseModel, Field

from app.config import settings
from app.models import build_model

MAX_COMPETITORS = 5


class Check(BaseModel):
    has_feature: Literal["yes", "partial", "no", "unclear"]
    evidence: str = Field(description="One sentence, close to the source's wording.")
    not_confirmed: list[str] = Field(
        description="Core parts (who, what, where) the source does not confirm or that "
        "differ, e.g. 'staff do it, not members'. No numbers or limits. Empty only if every "
        "core part is confirmed."
    )
    source_url: str = Field(description="The search result URL the evidence comes from.")


class Product(BaseModel):
    name: str
    website: str = Field(description="Homepage URL.")


class Competitor(Check, Product):  # Product's fields come first
    pass


class CompetitorList(BaseModel):
    competitors: list[Competitor]


class CompetitorReport(BaseModel):
    feature: str
    competitors: list[Competitor]
    summary: str
    searches: int


RATING = """\
Rate has_feature against the core parts of the feature: who uses it (e.g. members or staff),
what it does, and where it happens (e.g. in the app, on WhatsApp). Numbers and limits such
as "up to 3 months" do not decide the rating; mention them in the evidence if they differ.
- "yes": a search result explicitly confirms every core part. If any part is only implied,
  assumed or different, it is not "yes".
- "partial": a result shows a related capability, but at least one core part differs or is
  not confirmed (e.g. members can only request it and staff approve; another channel).
- "no": a result explicitly says the competitor does not have it.
- "unclear": no result says anything about it. Not finding it is not evidence it is missing.
List every core part (who, what, where) that is not confirmed or differs in not_confirmed;
it must be empty for "yes". Never list numbers or limits there. Use only what the results state, never your own knowledge.
"""

INSTRUCTIONS = f"""\
You get a product summary and a feature. Find up to {MAX_COMPETITORS} direct competitors of
the product (same customers, same job) and check whether each one has the feature.

You get exactly one web search, so write one query that finds competitors and the feature
at once: the product's category and market plus the feature, in the words product websites
use for it (e.g. "freeze" or "hold" for a pause).

Only name products that appear in the search results, never from memory, and never the
product itself.
{RATING}"""

RECHECK = f"""\
Check whether one competitor has one feature. You get exactly one web search, limited to the
competitor's own website. Search in the words product websites use for the feature
(e.g. "freeze" or "hold" for a pause).
{RATING}"""


def _url(u: str) -> str:
    return u.lower().split("://")[-1].removeprefix("www.").split("?")[0].rstrip("/")


def _domain(u: str) -> str:
    return _url(u).split("/")[0]


async def _search(instructions: str, output_type: type, prompt: str, domain: str = ""):
    """Run an agent allowed one web search (only on `domain` and its subdomains, if given).
    Returns (output, URLs the search returned, number of searches)."""
    agent = Agent(
        name="Competitor" if not domain else f"Recheck {domain}",
        instructions=instructions,
        tools=[WebSearchTool(user_location={"type": "approximate", "country": "IN"},
                             filters={"allowed_domains": [domain]} if domain else None,
                             search_context_size="low")],
        output_type=output_type,
        model=build_model(settings.competitor_model),
        model_settings=ModelSettings(
            # Reasoning models (gpt-5/6, o-series) take an effort; others reject it.
            reasoning=Reasoning(effort="low")
            if settings.competitor_model.startswith(("gpt-5", "gpt-6", "o")) else None,
            extra_args={"max_tool_calls": 1},
            response_include=["web_search_call.action.sources"],
        ),
    )
    result = await Runner.run(agent, prompt)
    calls = [i.raw_item for i in result.new_items
             if isinstance(getattr(i, "raw_item", None), ResponseFunctionWebSearch)]
    seen = {_url(s.url) for c in calls for s in (getattr(c.action, "sources", None) or [])}
    return result.final_output, seen, len(calls)


def _verify(check: Check, seen: set[str]) -> None:
    """Enforce the rating rules: evidence the search never returned doesn't count, and
    "yes" with an unconfirmed part is only "partial"."""
    if _url(check.source_url) not in seen:
        check.has_feature = "unclear"
    if check.has_feature == "yes" and check.not_confirmed:
        check.has_feature = "partial"
    if check.has_feature == "unclear":
        check.evidence, check.source_url, check.not_confirmed = "", "", []


async def _recheck(c: Competitor, feature: str) -> int:
    """One search on the competitor's own site; updates `c`. Returns searches made."""
    try:
        check, seen, searches = await _search(
            RECHECK, Check, f"Competitor: {c.name} ({c.website})\nFeature: {feature}",
            domain=_domain(c.website),
        )
    except Exception:  # noqa: BLE001 - a failed recheck leaves the competitor "unclear"
        return 0
    _verify(check, seen)
    for field in Check.model_fields:
        setattr(c, field, getattr(check, field))
    return searches


async def run_competitor(app_summary: str, feature: str) -> CompetitorReport:
    found, seen, searches = await _search(
        INSTRUCTIONS, CompetitorList, f"Product: {app_summary}\nFeature: {feature}"
    )
    domains = {_domain(u) for u in seen}
    competitors = []
    for c in found.competitors[:MAX_COMPETITORS]:
        if not {_domain(c.website), _domain(c.source_url)} & domains:
            continue  # not in the search results
        _verify(c, seen)
        competitors.append(c)

    # Step 2: one search each, in parallel, for the competitors still unclear.
    unclear = [c for c in competitors if c.has_feature == "unclear"]
    searches += sum(await asyncio.gather(*(_recheck(c, feature) for c in unclear)))

    counts = Counter(c.has_feature for c in competitors)
    summary = f"{counts.pop('yes', 0)} of {len(competitors)} have it" + "".join(
        f" · {n} {level}" for level, n in counts.items()
    ) if competitors else "No competitors found in the search results."
    return CompetitorReport(feature=feature, competitors=competitors, summary=summary,
                            searches=searches)


if __name__ == "__main__":
    print(asyncio.run(run_competitor(sys.argv[1], sys.argv[2])).model_dump_json(indent=2))
