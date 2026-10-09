# Deep Research Demo

Skeleton chat app: **uv** + **Gradio** + **OpenAI Agents SDK**, with tools to read PDFs/text files from `docs/`.

## Setup

```bash
uv sync
cp .env.example .env   # then set OPENAI_API_KEY
uv run main.py         # http://127.0.0.1:7860
```

## Layout

```
main.py            # Gradio ChatInterface, streams agent output
app/config.py      # settings loaded from .env
app/models.py      # model setup shared by all agents
app/chat_agent.py  # chat Agent + list_docs / read_doc tools
app/intake_agent.py # Intake agent: clarifying questions -> JSON brief
app/documents.py   # PDF (pypdf) and text file readers
docs/              # put your .pdf / .txt / .md files here
```
