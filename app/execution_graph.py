"""Renders the workflow's progress as a flowchart (HTML) for the right-hand panel.

`steps` maps a step id to {"status": ..., "detail": ...}; ids and statuses come from
`app.workflow`. Steps not in the dict are drawn as pending.
"""

import html

# Main path, top to bottom: (id, title).
PATH = [
    ("request", "Feature request"),
    ("context", "Load product context"),
    ("intake", "Intake agent → OpenAI"),
]
# Intake's two outcomes, side by side: (id, edge label, title).
OUTCOMES = [
    ("question", "needs_clarification", "Ask a clarifying question"),
    ("brief", "ready", "Brief ready"),
]
# After the "ready" outcome, side by side since they run in parallel: (id, title).
# Skipped when Intake asks a question.
AFTER_BRIEF = [("competitor", "Competitor agent"), ("demand", "Demand agent"),
               ("feasibility", "Feasibility agent")]
ICONS = {"done": "✓", "error": "!", "waiting": "?", "running": "", "pending": "", "skipped": ""}

CSS = """
.flow { display: flex; flex-direction: column; align-items: stretch; font-size: 14px; }
.node { display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px;
        border: 1.5px solid var(--border-color-primary); border-radius: 10px;
        background: var(--background-fill-secondary); transition: all .2s; }
.node .title { font-weight: 600; }
.node .detail { font-family: var(--font-mono); font-size: 12px; opacity: .75; margin-top: 2px;
                word-break: break-word; }
.dot { width: 20px; height: 20px; border-radius: 50%; flex-shrink: 0; display: flex;
       align-items: center; justify-content: center; font-size: 12px; font-weight: 700;
       box-sizing: border-box; border: 2px solid var(--border-color-primary); }
.node.pending, .node.skipped { opacity: .45; border-style: dashed; }
.node.running { border-color: #3b82f6; }
.node.running .dot { border-color: #3b82f6; }
.node.running .dot::after { content: ""; width: 8px; height: 8px; border-radius: 50%;
                            background: #3b82f6; animation: flow-pulse 1s infinite; }
.node.done { border-color: #22c55e; }
.node.done .dot { background: #22c55e; border-color: #22c55e; color: #fff; }
.node.waiting { border-color: #f59e0b; }
.node.waiting .dot { background: #f59e0b; border-color: #f59e0b; color: #fff; }
.node.error { border-color: #ef4444; }
.node.error .dot { background: #ef4444; border-color: #ef4444; color: #fff; }
.edge { width: 2px; height: 18px; margin: 2px auto; background: var(--border-color-primary); }
.edge.lit { background: #22c55e; }
.edge.right { margin-left: calc(75% + 2px); }  /* below the "ready" column */
.branches { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.branches.three { grid-template-columns: 1fr 1fr 1fr; }
.branch { display: flex; flex-direction: column; }
.branch .label { font-family: var(--font-mono); font-size: 11px; text-align: center;
                 opacity: .7; margin-bottom: 4px; }
.fork { display: block; width: 100%; height: 22px; }
.fork path { fill: none; stroke: var(--border-color-primary); stroke-width: 2; }
.fork path.lit { stroke: #22c55e; }
.branch-label { font-family: var(--font-mono); font-size: 11px; text-align: center;
                opacity: .7; margin-bottom: 4px; }
.flow-meta { font-family: var(--font-mono); font-size: 12px; opacity: .75; margin-top: 14px; }
.flow-meta a { margin-left: 8px; }
@keyframes flow-pulse { 50% { opacity: .3; } }
"""


def _node(status: str, title: str, detail: str) -> str:
    detail_html = f'<div class="detail">{html.escape(detail)}</div>' if detail else ""
    return (
        f'<div class="node {status}"><div class="dot">{ICONS[status]}</div>'
        f'<div><div class="title">{html.escape(title)}</div>{detail_html}</div></div>'
    )


def render_graph(steps: dict[str, dict], meta: str = "", trace_url: str = "") -> str:
    def status(step_id: str) -> str:
        return steps.get(step_id, {}).get("status", "pending")

    def detail(step_id: str) -> str:
        return steps.get(step_id, {}).get("detail", "")

    parts = []
    for i, (step_id, title) in enumerate(PATH):
        if i:
            parts.append(f'<div class="edge{" lit" if status(step_id) != "pending" else ""}"></div>')
        parts.append(_node(status(step_id), steps.get(step_id, {}).get("title", title), detail(step_id)))

    taken = next((o for o, *_ in OUTCOMES if o in steps), None)
    lit = {o: ' class="lit"' if o == taken else "" for o, *_ in OUTCOMES}
    stem = ' class="lit"' if taken else ""
    # Stem down from Intake, then one leg to the centre of each outcome column.
    parts.append(
        '<svg class="fork" viewBox="0 0 100 22" preserveAspectRatio="none">'
        f'<path{stem} d="M50 0V10" vector-effect="non-scaling-stroke"/>'
        f'<path{lit["question"]} d="M50 10H25V22" vector-effect="non-scaling-stroke"/>'
        f'<path{lit["brief"]} d="M50 10H75V22" vector-effect="non-scaling-stroke"/>'
        '</svg><div class="branches">'
    )
    for step_id, label, title in OUTCOMES:
        st = status(step_id) if step_id == taken or not taken else "skipped"
        parts.append(
            f'<div class="branch"><div class="label">{label}</div>'
            f"{_node(st, title, detail(step_id))}</div>"
        )
    parts.append("</div>")

    after = {i: "skipped" if taken == "question" else status(i) for i, _ in AFTER_BRIEF}
    lit = " lit" if any(st not in ("pending", "skipped") for st in after.values()) else ""
    parts.append(f'<div class="edge right{lit}"></div><div class="branch-label">in parallel</div>'
                 '<div class="branches three">')
    parts += [_node(after[i], title, detail(i)) for i, title in AFTER_BRIEF]
    parts.append("</div>")

    # Final step, after the parallel ones.
    st = "skipped" if taken == "question" else status("decision")
    parts.append(f'<div class="edge{" lit" if st not in ("pending", "skipped") else ""}"></div>')
    parts.append(_node(st, "Decision agent · go / no go", detail("decision")))

    if meta or trace_url:
        link = (
            f'<a href="{html.escape(trace_url)}" target="_blank" rel="noopener">View trace ↗</a>'
            if trace_url else ""
        )
        parts.append(f'<div class="flow-meta">{html.escape(meta)}{link}</div>')
    return '<div class="flow">' + "".join(parts) + "</div>"
