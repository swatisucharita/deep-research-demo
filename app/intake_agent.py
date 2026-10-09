"""Intake agent: turns a feature request into a clear brief before research starts.

Loads the product docs, asks clarifying questions one at a time (up to 5) when the request
is unclear,
then returns JSON with a brief app summary and a refined feature description.

Try it: uv run python -m app.intake_agent "Add membership pause"
"""

import asyncio
import sys
import unicodedata
from typing import Literal

from agents import Agent, ModelBehaviorError, Runner, TResponseInputItem
from pydantic import BaseModel, Field

from app.config import settings
from app.documents import list_documents, read_document
from app.models import build_model

MAX_QUESTIONS = 5  # one per reply; after this many it must answer


class Summary(BaseModel):
    app_summary: str = Field(
        description="2-3 sentences on what the product is and who uses it, only from the docs."
    )
    feature_summary: str = Field(description="1-2 sentences restating refined_query. Nothing new.")
    category: str = Field(description="Short market category, e.g. 'fitness studio booking software'.")


class IntakeResult(BaseModel):
    status: Literal["needs_clarification", "ready"]
    question: str = Field(description="needs_clarification only: one short question. Empty when ready.")
    original_query: str
    refined_query: str = Field(
        description="ready only: the feature in the user's words plus product terms."
    )
    summary: Summary | None = Field(description="ready only. Null when asking questions.")
    unknowns: list[str] = Field(description="ready only: details the user did not give.")
    sub_queries: list[str] = Field(
        description="ready only: exactly 3 web search queries for demand research, in this "
        "order: pain point, user voice, market. Empty when asking questions."
    )


INSTRUCTIONS = """\
You are the intake step of a feature-research workflow. Understand exactly which feature
the user wants before any research starts.

Every request is for a new feature the product does not have yet. Never ask whether it is
new or a change to an existing feature.

Sources: the product documents below for facts about the product, and the user's own words
(the request and their answers) for what they want. Nothing else.

Before asking anything, work out what is already known:
- What the user stated, or what follows directly from their words, is known. For example,
  "let members pause their membership from the app" already says who it is for, what it
  does and where it lives.
- What the product documents say is known. Use it; do not ask the user to confirm it.
- What the user already answered in this conversation is known. Never ask it again, in any
  wording.

Ask a question only if something is genuinely unclear and the answer would change what gets
researched. That means the core of the feature: who uses it, and what it lets them do. If
either is unclear, ask. Finer details (limits, durations, fees, edge cases) do not change the
research: do not ask about them; list the open ones under unknowns.

Do not assume. Never fill a gap with what seems sensible, never add requirements, limits,
numbers or behaviour the user did not state, and never state a product fact the documents
do not contain. Every open detail is either asked about or listed under unknowns.

{question_rule}

When you ask (status "needs_clarification"): ask exactly one short question, the most
important open one, offering choices from the documents in brackets where it helps. Leave
refined_query empty, summary null, and unknowns and sub_queries empty.

When the request is clear (status "ready"): leave question empty.
- refined_query: the feature in the user's words and answers plus product terms from the
  documents. Nothing the user did not say: no added user group, app or screen, limit or
  behaviour.
- summary: app_summary only from the documents; feature_summary restates refined_query.
- unknowns: every open detail you did not ask about, and every question the user did not
  answer. List them; do not resolve them.
- sub_queries: exactly 3 short web search queries, at most 6 words each, that find
  evidence of demand for refined_query, one per angle, in this order:
  1. pain point: the problem in a customer's own words, like a forum post title
     (e.g. "can't pause gym membership").
  2. user voice: how users ask for it in app reviews, with a common synonym
     (e.g. "freeze membership app review").
  3. market: interest in this kind of feature, naming the product's country from the
     documents (e.g. "gym membership freeze India").
  Only the market query names a country; the first two use plain customer words, no
  product names.
  They are search wording only: no new requirements, and nothing the user did not ask for.

Product documents:
{context}
"""

ASK_IF_NEEDED = (
    "If nothing important is unclear, set status to \"ready\" straight away, even on the "
    "first message."
)
NO_MORE_QUESTIONS = (
    "You have asked enough questions. Set status to \"ready\" and list anything still "
    "unclear under unknowns."
)


def load_context() -> str:
    """Text of every document in the docs folder."""
    parts = []
    for name in list_documents(settings.docs_dir):
        text = unicodedata.normalize("NFKC", read_document(settings.docs_dir / name))
        parts.append(f"## {name}\n{text.strip()}")
    return "\n\n".join(parts)

def intake_agent(context: str, allow_questions: bool) -> Agent:
    return Agent(
        name="Intake",
        instructions=INSTRUCTIONS.format(
            question_rule=ASK_IF_NEEDED if allow_questions else NO_MORE_QUESTIONS,
            context=context or "(no documents found)",
        ),
        output_type=IntakeResult,
        model=build_model(),
    )


def _usable(out: IntakeResult, allow_questions: bool) -> bool:
    if out.status == "needs_clarification":
        return allow_questions and bool(out.question.strip())
    return out.summary is not None and bool(out.refined_query) and len(out.sub_queries) >= 3


async def run_intake(
    conversation: list[TResponseInputItem], context: str
) -> tuple[IntakeResult, list[TResponseInputItem]]:
    """Run Intake on the conversation so far (the request, then any Q&A).

    Returns the result and the conversation including the agent's reply, so the user's
    answers can be appended and passed back in. Retries once on unusable output.
    """
    asked = sum(1 for item in conversation if item.get("role") == "assistant")
    allow_questions = asked < MAX_QUESTIONS
    agent = intake_agent(context, allow_questions)
    for _ in range(2):
        try:
            result = await Runner.run(agent, conversation, max_turns=1)
        except ModelBehaviorError:
            continue
        out = result.final_output_as(IntakeResult)
        if _usable(out, allow_questions):
            out.original_query = str(conversation[0]["content"])
            out.sub_queries = out.sub_queries[:3]
            return out, result.to_input_list()
    raise RuntimeError("Intake returned unusable output twice")


async def _cli(request: str) -> None:
    context = load_context()
    conversation: list[TResponseInputItem] = [{"role": "user", "content": request}]
    while True:
        out, conversation = await run_intake(conversation, context)
        if out.status == "ready":
            print(out.model_dump_json(indent=2))
            return
        conversation.append({"role": "user", "content": input(f"{out.question}\n> ")})


if __name__ == "__main__":
    asyncio.run(_cli(" ".join(sys.argv[1:]) or input("Feature request: ")))
