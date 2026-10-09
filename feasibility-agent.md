# Feasibility agent: detailed logic

Code: `app/feasibility_agent.py` · Diagram: `feasibility-agent.png` · Step 4 of the workflow.

## Purpose

Answers three questions about a feature request, using the product's own codebase:

1. **Can we build it?** `feasible`, `feasible with changes` or `not feasible`, with a reason.
2. **How?** What infrastructure already exists, what must change, and the high-level
   architecture of the feature once built.
3. **How long?** Developer and QA hours, people, and calendar weeks.

It is **read-only**: it never writes to the codebase, and it sends OpenAI only the code
map and the lines it chooses to read, never the whole repo.

## Where it runs

- Called by `app/workflow.py` after the Intake agent returns `ready`.
- Runs **in parallel** with the Competitor and Demand agents (`asyncio.gather`); all three
  need only the Intake brief.
- Has its own trace span (`Feasibility`) and its own box in the Agent execution panel.
- If it fails, its box turns red and the Decision agent is told it is missing; the report
  says so in the Feasibility section and confidence is set to low.

## Inputs

| Input | From | Used for |
|---|---|---|
| `feature` | Intake `refined_query` | what to assess |
| `feature_summary` | Intake `summary.feature_summary` | extra context |
| `unknowns` | Intake `unknowns` | seeds `open_questions` |
| the codebase | `CODEBASE_DIR` (local folder) | everything else |

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `CODEBASE_DIR` | none (required) | local repo to assess, e.g. `/Users/swati/projects/fitslot-app` |
| `FEASIBILITY_MODEL` | `gpt-6-luna` | model for the agent; reasoning models get `medium` effort |
| `TEAM_DEVS` | `1` | developers working in parallel |
| `TEAM_QA` | `1` | QA people |
| `HOURS_PER_DAY` | `5` | focused hours per person per day |

Constants in the code:

| Constant | Value | Meaning |
|---|---|---|
| `MAX_TURNS` | 15 | maximum model calls in one run |
| `MAX_READ_CHARS` | 12,000 | maximum characters in one tool result |
| `MAX_RUN_CHARS` | 120,000 | maximum characters across all tool results in one run |
| `MAX_FILE_BYTES` | 200,000 | larger files are not loaded (generated code, data) |
| `SKIP_DIRS` | `.git`, `node_modules`, `.next`, `dist`, `build`, `coverage`, `generated`, `.venv` | folders never entered |
| `SKIP` | `.env*`, `.pem`, `.key`, images, `.pdf`, `.db`, `.sqlite`, `.lock`, `package-lock.json` | files never loaded |
| `SIZE_HOURS` | S 5–10, M 15–25, L 30–50 | developer hours per change |
| `UNVERIFIED` | 1.5 | widens the high end of an unverified `modify` change |
| `QA_SHARE` | 0.3 | QA hours as a share of developer hours |
| `QA_TAIL` | 0.4 | share of QA that happens after development ends |
| `BUFFER` | 0.15 | review and fixes, added to developer time |

## Step by step

### 1. Check the repo setting (code)
If `CODEBASE_DIR` is not set or is not a folder, raise
`RuntimeError("CODEBASE_DIR is not set to a local repo")`. The step fails; the others continue.

### 2. Load the repo into memory (code, `_load_repo`)
- Walks `CODEBASE_DIR` with `os.walk`, **without entering** any folder in `SKIP_DIRS`
  (so `node_modules` is never walked; loading takes milliseconds).
- Skips files matching `SKIP` (secrets, keys, binaries, lockfiles) and files over
  `MAX_FILE_BYTES`.
- Reads each remaining file as UTF-8; files that are not valid text are skipped.
- Result: a dictionary `path → list of lines`, with repo-relative paths
  (e.g. `src/lib/billing.ts`). FitSlot: 38 files.
- **Nothing is sent anywhere in this step.** Every later step works from this dictionary,
  so a file that is not loaded here cannot be mapped, read or cited.

### 3. Pick out the hand-written map (code)
If the repo has `ARCHITECTURE.md` at its root, its full text goes into the prompt. It is
assumed to be up to date. FitSlot's is about 7,000 tokens and covers modules, data model,
flows, known gaps and extension points.

### 4. Build the generated map (code, `_repo_map`)
One line per loaded file: `path (N lines): names`, where names are found with the `SYMBOL`
pattern:
- TypeScript/JavaScript: `export [default] [async] function|const|class|type|interface X`
- Prisma: `model X {`

Example: `src/lib/notifications/channels.ts (60 lines): ChannelName, MemberWithStudio,
NotificationChannel, CHANNELS, notify`. FitSlot's map is about 1,800 characters.

### 5. Build the prompt (code)
```
Feature: <refined_query>
Summary: <feature_summary>
Open details: <unknowns joined by "; ", or "none">

ARCHITECTURE.md:
<full text, or "(none)">

Repo map:
<generated map>
```

### 6. Run the agent (model + local tools)
- Agent: `gpt-6-luna`, `medium` reasoning, `output_type=Assessment`, `max_turns=15`.
- Instructions: use the two maps to know where to look, confirm with the tools, read the
  exact lines behind every reference, only cite code that was read, never invent files or
  lines, and give **sizes, not hours**.
- Each turn is one model call. The model either returns the final `Assessment` or asks
  for tools; the SDK runs the tools locally and sends the results back as the next turn.
  One turn can ask for several tools at once.

**Tools** (`_tools`; all read-only, all limited to the loaded files):

| Tool | Arguments | Returns |
|---|---|---|
| `list_files` | `directory` (`""` = whole repo) | file paths under that folder |
| `search_code` | `pattern` (regex, falls back to plain text if invalid) | up to 40 `path:line: text` matches, case-insensitive |
| `read_file` | `path`, `start_line`, `end_line` | those lines, numbered; `Not readable: …` for anything not loaded |

**Budget** (`budget()`, applied to every tool result):
- each result is cut to `MAX_READ_CHARS`;
- once the run's results would pass `MAX_RUN_CHARS`, tools reply
  "Read budget for this run is used up. Answer with what you have.", so the agent
  finishes instead of failing;
- every tool call that returns content is counted in `tool_calls`; every loaded file
  opened with `read_file` is recorded in `files_read`.

**What the model returns** (`Assessment`):
- `feasibility`: `feasible` | `feasible with changes` | `not feasible`, and `reason`.
- `already_present`: list of `CodeRef` (`path`, `start_line`, `end_line`, `note`):
  existing code the feature can reuse.
- `changes`: list of `Change` (`area`, `change` = `new`|`modify`, `size` = `S`|`M`|`L`,
  `what`, `refs`):
  - S = small contained change; M = several functions or a new screen;
    L = new subsystem, migration or tricky logic.
- `components`: list of `Component` (`name`, `status` = `new`|`changed`|`existing`, `does`).
- `flow`: list of `Edge` (`source`, `target`, `label`), using component names.
- `open_questions`: product decisions the code cannot settle.

### 7. Check every code reference (code, `_check`)
A reference is `verified` when its `path` is a loaded file and
`1 ≤ start_line ≤ end_line ≤ number of lines in that file`. Applied to `already_present`
and to every change's `refs`.

### 8. Size each change (code)
For each change:
- `low, high = SIZE_HOURS[size]`;
- if `change == "modify"` and **none** of its refs is verified: `high × UNVERIFIED (1.5)`
  (a `new` change may legitimately have no existing code to point to, so it is not widened);
- the change's `hours` is shown as `low–high`.

### 9. Compute effort (code, `_effort`)
```
dev_hours = sum of all lows  –  sum of all highs
qa_hours  = dev_hours × QA_SHARE (0.3)
week      = HOURS_PER_DAY × 5
weeks     = (dev × (1 + BUFFER) / TEAM_DEVS  +  qa × QA_TAIL / TEAM_QA) / week
people    = "<TEAM_DEVS> dev + <TEAM_QA> QA"
```
Developers work in parallel; most QA overlaps development and only `QA_TAIL` of it adds
calendar time. Hours are rounded to whole numbers, weeks to the nearest 0.5.

**Worked example** (WhatsApp reminders, 6 changes: S, S, M, S, M, S; all refs verified;
1 dev + 1 QA, 5 h/day):
- dev = 5+5+15+5+15+5 = **50** to 10+10+25+10+25+10 = **90** hours
- QA = 50 × 0.3 = **15** to 90 × 0.3 = **27** hours
- week = 5 × 5 = 25 focused hours
- low weeks = (50 × 1.15 / 1 + 15 × 0.4 / 1) / 25 = (57.5 + 6) / 25 = 2.54 → **2.5**
- high weeks = (90 × 1.15 + 27 × 0.4) / 25 = (103.5 + 10.8) / 25 = 4.57 → **4.5**

### 10. Build the report (code)
`FeasibilityReport`: `feature`, `feasibility`, `reason`, `already_present` (checked refs),
`changes` (checked, with hours), `components`, `flow`, `effort`, `open_questions`,
`files_read` and `tool_calls`. The last two come from the tool log, not from the model.

### 11. Hand it to the Decision agent (workflow)
- The `FeasibilityReport` goes to the Decision agent with the Competitor and Demand reports.
  The Decision agent writes the business report, including the "Feasibility and effort"
  section: effort table, what exists, what needs to change, architecture (🟢 new,
  🟡 changed, ⚪ existing, plus the flow) and open questions.
- Panel: `gpt-6-luna · <feasibility> · <dev hours> dev h · <tool calls> tool calls`.

## What leaves the machine

Sent to OpenAI: the Intake brief, `ARCHITECTURE.md`, the generated map, and the results
of the tool calls (file lists, search matches, the lines read). Never sent: anything in
`SKIP_DIRS` or matching `SKIP` (including `.env` files and keys), files over 200 KB, and
any code the agent does not ask to read.

## Failure modes

| Situation | What happens |
|---|---|
| `CODEBASE_DIR` missing or not a folder | step fails with a clear message; other reports still show |
| agent needs more than 15 turns | `MaxTurnsExceeded`; step fails; other reports still show |
| read budget used up | tools tell the agent to answer with what it has; the run completes |
| model cites a wrong file or line | the ref is marked `verified: false`; a `modify` change with no verified ref is widened ×1.5 |
| no `ARCHITECTURE.md` | prompt says `(none)`; the agent relies on the generated map and the tools |
| file is binary or not UTF-8 | skipped while loading |

## Typical run (FitSlot, WhatsApp reminders)

About 25–35 seconds, 9–11 tool calls, all references verified. Estimates vary between
runs because the model may list more or fewer changes (seen: 50–90 and 75–130 developer
hours); the sizes are judgement, the hour maths is fixed.

## Known limitations

- The generated map only finds names in TypeScript/JavaScript exports and Prisma models;
  other languages appear in the map without names (the tools still read them).
- `ARCHITECTURE.md` is trusted as up to date; if it is stale, the tools are the check.
- S/M/L sizes come from the model; the hours per size are fixed and may need calibrating
  against the team's real numbers.
- Generated or build folders not in `SKIP_DIRS` would be loaded; add their names there.

## Run it on its own

```bash
uv run python -m app.feasibility_agent "Send class reminders on WhatsApp"
```
Prints the full report as JSON, then the architecture list. Needs `CODEBASE_DIR` and
`OPENAI_API_KEY` in `.env`.
