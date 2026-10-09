"""Gradio UI: chat on the left, live agent execution graph on the right.

Each chat message runs the research workflow; its steps are drawn as they run.
"""

import asyncio
import socket
import time
import uuid

import gradio as gr

from app.config import settings
from app.execution_graph import CSS, render_graph
from app.workflow import run_workflow


async def chat(message: str, history: list[dict], pending: list, conversation_id: str):
    """`pending` is the Intake conversation waiting for the user's answers, or empty.
    `conversation_id` groups this chat's traces.
    Yields (reply, pending, id, graph, download button)."""
    if not history:  # new or cleared chat: drop any unanswered questions
        pending, conversation_id = [], f"chat_{uuid.uuid4().hex}"

    title = "Your answer" if pending else "Feature request"
    preview = message if len(message) <= 80 else message[:77] + "…"
    steps = {"request": {"status": "done", "title": title, "detail": preview}}
    started = time.monotonic()

    def graph(meta: str, trace_url: str = "") -> str:
        return render_graph(steps, f"{meta} · {time.monotonic() - started:.1f}s", trace_url)

    # The workflow reports steps through a queue while it runs in its own task.
    queue: asyncio.Queue[tuple[str, str, str] | None] = asyncio.Queue()
    task = asyncio.create_task(
        run_workflow(message, pending, conversation_id,
                     on_step=lambda *step: queue.put_nowait(step))
    )
    task.add_done_callback(lambda _: queue.put_nowait(None))

    no_file = gr.DownloadButton(visible=False)
    yield "_Running…_", pending, conversation_id, graph("running"), no_file
    try:
        while (step := await queue.get()) is not None:
            step_id, status, detail = step
            steps[step_id] = {**steps.get(step_id, {}), "status": status, "detail": detail}
            yield "_Running…_", pending, conversation_id, graph("running"), no_file
        result = await task
    except Exception as e:  # noqa: BLE001 - show workflow failures in the chat and graph
        for step in steps.values():
            if step["status"] == "running":
                step.update(status="error", detail=str(e))
        yield f"⚠️ Error: {e}", pending, conversation_id, graph("failed"), no_file
        return
    finally:
        task.cancel()  # no-op when finished; stops the agents if the user leaves

    state = "waiting for your answer" if result.pending else "done"
    download = (gr.DownloadButton(value=result.report_path, visible=True)
                if result.report_path else no_file)
    yield result.answer, result.pending, conversation_id, graph(state, result.trace_url), download


with gr.Blocks(title="Deep Research Demo") as demo:
    pending = gr.State([])  # Intake conversation while it waits for answers
    conversation_id = gr.State("")  # groups the traces of one chat
    gr.Markdown(
        "## Deep Research Demo\n"
        f"Describe a feature. The workflow uses the files in `{settings.docs_dir.name}/` "
        "as product context."
    )
    with gr.Row(equal_height=False):
        with gr.Column(scale=3):
            graph = gr.HTML(render_graph({}), render=False)
            download = gr.DownloadButton("⬇ Download report (.md)", visible=False, render=False)
            chat_ui = gr.ChatInterface(
                fn=chat,
                additional_inputs=[pending, conversation_id],
                additional_outputs=[pending, conversation_id, graph, download],
                examples=[
                    ["Add membership pause"],
                    ["Send class reminders on WhatsApp"],
                    ["Let members bring a guest to class"],
                ],
            )
        with gr.Column(scale=2):
            gr.Markdown("### Agent execution")
            graph.render()
            download.render()
    chat_ui.chatbot.clear(lambda: (render_graph({}), gr.DownloadButton(visible=False)),
                          outputs=[graph, download])


def _free_port(host: str, start: int, tries: int = 100) -> int:
    """Return the first free port at or after `start`."""
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, port)) != 0:
                return port
    raise OSError(f"No free port in range {start}-{start + tries - 1}")


def main():
    port = _free_port(settings.host, settings.port)
    if port != settings.port:
        print(f"Port {settings.port} is in use, using {port} instead.")
    demo.launch(server_name=settings.host, server_port=port, share=settings.share, css=CSS)


if __name__ == "__main__":
    main()
