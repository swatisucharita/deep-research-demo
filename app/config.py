"""Load settings from environment / .env file."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# The OpenAI client reads OPENAI_* env vars directly; an empty value such as
# `OPENAI_BASE_URL=` would be used as-is and break requests, so drop blanks.
for _key in [k for k, v in os.environ.items() if k.startswith("OPENAI_") and not v.strip()]:
    del os.environ[_key]


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None
    openai_base_url: str | None
    openai_model: str
    competitor_model: str
    demand_model: str
    feasibility_model: str
    decision_model: str
    codebase_dir: Path | None
    team_devs: int
    team_qa: int
    hours_per_day: float
    agent_name: str
    agent_instructions: str
    docs_dir: Path
    max_doc_chars: int
    tracing_disabled: bool
    host: str
    port: int
    share: bool


def load_settings() -> Settings:
    docs_dir = Path(os.getenv("DOCS_DIR", "docs"))
    if not docs_dir.is_absolute():
        docs_dir = PROJECT_ROOT / docs_dir

    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_base_url=os.getenv("OPENAI_BASE_URL") or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        competitor_model=os.getenv("COMPETITOR_MODEL", "gpt-6-luna"),
        demand_model=os.getenv("DEMAND_MODEL", "gpt-6-luna"),
        feasibility_model=os.getenv("FEASIBILITY_MODEL", "gpt-6-luna"),
        decision_model=os.getenv("DECISION_MODEL", "gpt-6-luna"),
        codebase_dir=Path(os.environ["CODEBASE_DIR"]) if os.getenv("CODEBASE_DIR") else None,
        team_devs=int(os.getenv("TEAM_DEVS", "1")),
        team_qa=int(os.getenv("TEAM_QA", "1")),
        hours_per_day=float(os.getenv("HOURS_PER_DAY", "5")),
        agent_name=os.getenv("AGENT_NAME", "Research Assistant"),
        agent_instructions=os.getenv(
            "AGENT_INSTRUCTIONS",
            "You are a helpful research assistant. Use the document tools to "
            "list and read files from the local docs folder when the user asks "
            "about them, and cite the file names you used.",
        ),
        docs_dir=docs_dir,
        max_doc_chars=int(os.getenv("MAX_DOC_CHARS", "50000")),
        tracing_disabled=_bool(os.getenv("OPENAI_AGENTS_DISABLE_TRACING"), False),
        host=os.getenv("GRADIO_SERVER_NAME", "127.0.0.1"),
        port=int(os.getenv("GRADIO_SERVER_PORT", "7860")),
        share=_bool(os.getenv("GRADIO_SHARE"), False),
    )


settings = load_settings()
