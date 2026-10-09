"""Feasibility agent (POC): can the feature be built in our codebase, how, and how long?

Read-only. The agent gets the repo's ARCHITECTURE.md (if present) and a generated map
(files + exported names), then reads the code it needs with three local tools. Code checks
every file/line it cites and turns the S/M/L sizes into hours, people and weeks.

Try it: uv run python -m app.feasibility_agent "Send class reminders on WhatsApp"
"""

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Literal

from agents import Agent, ModelSettings, Runner, custom_span, function_tool, trace
from openai.types.shared import Reasoning
from pydantic import BaseModel, Field

from app.config import settings
from app.models import build_model

MAX_TURNS = 15
MAX_READ_CHARS = 12_000  # per tool result
MAX_RUN_CHARS = 120_000  # all tool results in one run
MAX_FILE_BYTES = 200_000  # bigger files are skipped (generated or data)
SKIP_DIRS = {".git", "node_modules", ".next", "dist", "build", "coverage", "generated", ".venv"}
SKIP = re.compile(r"(^|/)(\.env[^/]*|.*\.(pem|key|png|jpe?g|gif|ico|svg|webp|pdf|db|sqlite|lock)|"
                  r"package-lock\.json)$")
SYMBOL = re.compile(r"^(?:export\s+(?:default\s+)?(?:async\s+)?(?:function|const|class|type|interface)"
                    r"\s+(\w+)|model\s+(\w+)\s*\{)", re.M)

SIZE_HOURS = {"S": (5, 10), "M": (15, 25), "L": (30, 50)}  # developer hours per change
UNVERIFIED = 1.5  # widen the high end of a "modify" with no verified code ref
QA_SHARE, QA_TAIL, BUFFER = 0.3, 0.4, 0.15  # QA hours, QA after dev ends, review/fixes


class CodeRef(BaseModel):
    path: str = Field(description="Repo-relative path, exactly as in the map.")
    start_line: int
    end_line: int
    note: str = Field(description="What this code does, as it relates to the feature.")


class Change(BaseModel):
    area: str = Field(description="e.g. data model, notifications, member page, billing.")
    change: Literal["new", "modify"]
    size: Literal["S", "M", "L"] = Field(
        description="S: small contained change. M: several functions or a new screen. "
        "L: new subsystem, migration or tricky logic."
    )
    what: str = Field(description="One sentence: what to build or change.")
    refs: list[CodeRef] = Field(description="Lines you read that this change touches or hooks into.")


class Component(BaseModel):
    name: str
    status: Literal["new", "changed", "existing"]
    does: str = Field(description="One line: its job in the feature.")


class Edge(BaseModel):
    source: str = Field(description="Component name.")
    target: str = Field(description="Component name.")
    label: str = Field(description="What flows, a few words.")


class Assessment(BaseModel):
    feasibility: Literal["feasible", "feasible with changes", "not feasible"]
    reason: str = Field(description="1-2 sentences, from the code.")
    already_present: list[CodeRef] = Field(description="Existing code the feature can reuse.")
    changes: list[Change]
    components: list[Component] = Field(description="High-level architecture once built.")
    flow: list[Edge]
    open_questions: list[str] = Field(description="Product decisions the code cannot settle.")


class CheckedRef(CodeRef):
    verified: bool  # file exists and the lines are in range


class CheckedChange(Change):
    refs: list[CheckedRef]
    hours: str  # developer hours for this change, after the unverified widening


class Effort(BaseModel):
    dev_hours: str
    qa_hours: str
    people: str
    weeks: str


class FeasibilityReport(BaseModel):
    feature: str
    feasibility: str
    reason: str
    already_present: list[CheckedRef]
    changes: list[CheckedChange]
    components: list[Component]
    flow: list[Edge]
    effort: Effort
    open_questions: list[str]
    files_read: list[str]  # from the tool calls, not the model's account
    tool_calls: int


INSTRUCTIONS = """\
You assess whether a feature can be built in this codebase, and what it takes. You get the
team's ARCHITECTURE.md (if any) and a generated map of the repo. Use them to know where to
look, then confirm with the tools: read the exact lines behind every reference you cite.
Tools are read-only; paths are repo-relative, as in the map.

Return:
- already_present: existing code the feature can reuse (models, functions, patterns).
- changes: each piece of work, with area, new or modify, size S/M/L, what, and refs (the
  lines you read that it touches; for new code, where it hooks in).
- components and flow: the feature's high-level architecture once built. Mark components
  new, changed or existing; flow edges use component names.
- feasibility and reason; open_questions for product decisions the code cannot settle.

Only cite code you read with the tools; never invent files or lines. Give sizes, not hours.
"""


def _load_repo(root: Path) -> dict[str, list[str]]:
    """Text files under `root`, minus SKIP_DIRS, secrets, binaries and very large files."""
    files = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)  # don't descend into skipped dirs
        for name in sorted(names):
            file = Path(folder) / name
            path = file.relative_to(root).as_posix()
            if SKIP.search(path) or file.stat().st_size > MAX_FILE_BYTES:
                continue
            try:
                files[path] = file.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue  # binary file
    return files


def _repo_map(files: dict[str, list[str]]) -> str:
    lines = []
    for path, text in sorted(files.items()):
        names = [a or b for a, b in SYMBOL.findall("\n".join(text))]
        lines.append(f"{path} ({len(text)} lines)" + (f": {', '.join(names)}" if names else ""))
    return "\n".join(lines)


def _tools(files: dict[str, list[str]], log: dict):
    """Read-only tools over `files` only, so nothing outside the repo or in SKIP is reachable."""

    def budget(text: str) -> str:
        text = text[:MAX_READ_CHARS]
        if log["chars"] + len(text) > MAX_RUN_CHARS:
            return "Read budget for this run is used up. Answer with what you have."
        log["chars"] += len(text)
        log["calls"] += 1
        return text

    @function_tool
    def list_files(directory: str) -> str:
        """List repo files under a directory ("" for the whole repo).

        Args:
            directory: Repo-relative directory, e.g. "src/lib".
        """
        prefix = directory.strip("/") + "/" if directory.strip("/") else ""
        return budget("\n".join(p for p in sorted(files) if p.startswith(prefix)) or "No files.")

    @function_tool
    def search_code(pattern: str) -> str:
        """Find a text or regex pattern (case-insensitive) in the repo. Returns path:line: text.

        Args:
            pattern: Text or Python regex, e.g. "notify(" or "whatsapp".
        """
        try:
            rx = re.compile(pattern, re.I)
        except re.error:
            rx = re.compile(re.escape(pattern), re.I)
        hits = [f"{p}:{i}: {line.strip()}" for p, text in sorted(files.items())
                for i, line in enumerate(text, start=1) if rx.search(line)]
        return budget("\n".join(hits[:40]) or "No matches.")

    @function_tool
    def read_file(path: str, start_line: int, end_line: int) -> str:
        """Read lines start_line..end_line (1-based, inclusive) of a repo file, with line numbers.

        Args:
            path: Repo-relative path, as in the map.
            start_line: First line to read.
            end_line: Last line to read.
        """
        if path not in files:
            return f"Not readable: {path}"
        log["files"].add(path)
        text = files[path]
        start, end = max(start_line, 1), min(end_line, len(text))
        return budget("\n".join(f"{i}: {text[i - 1]}" for i in range(start, end + 1)))

    return [list_files, search_code, read_file]


def _check(ref: CodeRef, files: dict[str, list[str]]) -> CheckedRef:
    n = len(files.get(ref.path, []))
    ok = ref.path in files and 1 <= ref.start_line <= ref.end_line <= n
    return CheckedRef(**ref.model_dump(), verified=ok)


def _range(low: float, high: float, step: float = 1) -> str:
    low, high = round(low / step) * step, round(high / step) * step
    fmt = lambda x: f"{x:g}"  # noqa: E731
    return fmt(low) if low == high else f"{fmt(low)}–{fmt(high)}"


def _effort(changes: list[CheckedChange], lows: list[float], highs: list[float]) -> Effort:
    dev_low, dev_high = sum(lows), sum(highs)
    qa_low, qa_high = dev_low * QA_SHARE, dev_high * QA_SHARE
    week = settings.hours_per_day * 5
    # Devs work in parallel; QA mostly overlaps dev, with QA_TAIL of it after dev ends.
    weeks = [(d * (1 + BUFFER) / settings.team_devs + q * QA_TAIL / settings.team_qa) / week
             for d, q in ((dev_low, qa_low), (dev_high, qa_high))]
    return Effort(
        dev_hours=_range(dev_low, dev_high), qa_hours=_range(qa_low, qa_high),
        people=f"{settings.team_devs} dev + {settings.team_qa} QA",
        weeks=_range(*weeks, step=0.5),
    )


async def run_feasibility(feature: str, feature_summary: str, unknowns: list[str]) -> FeasibilityReport:
    root = settings.codebase_dir
    if not root or not root.is_dir():
        raise RuntimeError("CODEBASE_DIR is not set to a local repo")
    # Spans nest under the workflow's "Feasibility" span; the SDK adds the agent run, each
    # model call and each tool call (with its input and output) on its own.
    with custom_span("Load repo") as span:
        files = _load_repo(root)
        arch = files.get("ARCHITECTURE.md")
        repo_map = _repo_map(files)
        span.span_data.data = {"codebase_dir": str(root), "files": len(files),
                               "architecture_md": arch is not None, "map_chars": len(repo_map)}
    log = {"chars": 0, "calls": 0, "files": set()}

    agent = Agent(
        name="Feasibility",
        instructions=INSTRUCTIONS,
        tools=_tools(files, log),
        output_type=Assessment,
        model=build_model(settings.feasibility_model),
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="medium")
            if settings.feasibility_model.startswith(("gpt-5", "gpt-6", "o")) else None,
        ),
    )
    prompt = (
        f"Feature: {feature}\nSummary: {feature_summary}\n"
        f"Open details: {'; '.join(unknowns) or 'none'}\n\n"
        f"ARCHITECTURE.md:\n{chr(10).join(arch) if arch else '(none)'}\n\n"
        f"Repo map:\n{repo_map}"
    )
    out = (await Runner.run(agent, prompt, max_turns=MAX_TURNS)).final_output

    with custom_span("Check refs") as span:
        changes, lows, highs = [], [], []
        for c in out.changes:
            refs = [_check(r, files) for r in c.refs]
            low, high = SIZE_HOURS[c.size]
            if c.change == "modify" and not any(r.verified for r in refs):
                high *= UNVERIFIED
            lows.append(low)
            highs.append(high)
            changes.append(CheckedChange(**c.model_dump(exclude={"refs"}), refs=refs,
                                         hours=_range(low, high)))
        present = [_check(r, files) for r in out.already_present]
        all_refs = present + [r for c in changes for r in c.refs]
        span.span_data.data = {
            "refs": len(all_refs), "verified": sum(r.verified for r in all_refs),
            "unverified": [f"{r.path}:{r.start_line}-{r.end_line}" for r in all_refs if not r.verified],
            "changes": [f"{c.size} {c.change} {c.area} ({c.hours} h)" for c in changes],
        }

    with custom_span("Effort") as span:
        effort = _effort(changes, lows, highs)
        span.span_data.data = {
            **effort.model_dump(), "hours_per_day": settings.hours_per_day,
            "tool_calls": log["calls"], "chars_read": log["chars"], "files_read": len(log["files"]),
        }

    return FeasibilityReport(
        feature=feature, feasibility=out.feasibility, reason=out.reason,
        already_present=present, changes=changes, components=out.components, flow=out.flow,
        effort=effort, open_questions=out.open_questions,
        files_read=sorted(log["files"]), tool_calls=log["calls"],
    )


def architecture_md(report: FeasibilityReport) -> str:
    """The architecture as Markdown (Gradio's chat does not render Mermaid)."""
    mark = {"new": "🟢 new", "changed": "🟡 changed", "existing": "⚪ existing"}
    lines = [f"- **{c.name}** · {mark[c.status]}: {c.does}" for c in report.components]
    lines += ["", "**Flow**", ""] + [f"- {e.source} → {e.target} · _{e.label}_" for e in report.flow]
    return "\n".join(lines)


async def _cli(feature: str) -> FeasibilityReport:
    with trace("Feasibility agent (CLI)"):  # standalone runs get their own trace
        return await run_feasibility(feature, feature, [])


if __name__ == "__main__":
    r = asyncio.run(_cli(sys.argv[1]))
    print(r.model_dump_json(indent=2))
    print(architecture_md(r))
