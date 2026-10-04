"""Gradio chat UI backed by an OpenAI Agents SDK agent."""

import socket

import gradio as gr
from agents import Runner
from openai.types.responses import ResponseTextDeltaEvent

from app.agent import build_agent
from app.config import settings

agent = build_agent()


def _text(content) -> str:
    """Gradio message content may be a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content)


async def chat(message: str, history: list[dict]):
    if not settings.openai_api_key:
        yield "OPENAI_API_KEY is not set. Copy `.env.example` to `.env` and add your key."
        return

    inputs = [{"role": m["role"], "content": _text(m["content"])} for m in history]
    inputs.append({"role": "user", "content": message})

    result = Runner.run_streamed(agent, input=inputs)
    reply = ""
    async for event in result.stream_events():
        if event.type == "raw_response_event" and isinstance(
            event.data, ResponseTextDeltaEvent
        ):
            reply += event.data.delta
            yield reply


demo = gr.ChatInterface(
    fn=chat,
    title="Deep Research Demo",
    description=f"Chat with an agent that can read PDFs and text files from `{settings.docs_dir.name}/`.",
    examples=["What documents are available?", "Summarize the documents in the docs folder."],
)


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
    demo.launch(server_name=settings.host, server_port=port, share=settings.share)


if __name__ == "__main__":
    main()
