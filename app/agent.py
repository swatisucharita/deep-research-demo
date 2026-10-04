"""OpenAI Agents SDK setup: the agent and its document tools."""

from agents import (
    Agent,
    AsyncOpenAI,
    OpenAIChatCompletionsModel,
    function_tool,
    set_default_openai_key,
    set_tracing_disabled,
)

from app.config import settings
from app.documents import list_documents, read_document, resolve_document


@function_tool
def list_docs() -> str:
    """List the PDF and text files available in the local docs folder."""
    files = list_documents(settings.docs_dir)
    if not files:
        return "The docs folder is empty."
    return "\n".join(str(f) for f in files)


@function_tool
def read_doc(file_name: str) -> str:
    """Read the text content of a file from the local docs folder.

    Args:
        file_name: File name (or relative path) as returned by list_docs.
    """
    try:
        text = read_document(resolve_document(settings.docs_dir, file_name))
    except (ValueError, FileNotFoundError) as e:
        return f"Error: {e}"
    if len(text) > settings.max_doc_chars:
        text = text[: settings.max_doc_chars] + "\n\n[...truncated...]"
    return text


def _build_model():
    # A custom base URL (Azure, Ollama, OpenRouter, ...) goes through Chat Completions;
    # otherwise use the default OpenAI Responses API with the model name.
    if settings.openai_base_url:
        client = AsyncOpenAI(
            api_key=settings.openai_api_key, base_url=settings.openai_base_url
        )
        return OpenAIChatCompletionsModel(model=settings.openai_model, openai_client=client)
    return settings.openai_model


def build_agent() -> Agent:
    if settings.openai_api_key:
        set_default_openai_key(settings.openai_api_key)
    if settings.tracing_disabled or settings.openai_base_url:
        set_tracing_disabled(True)

    return Agent(
        name=settings.agent_name,
        instructions=settings.agent_instructions,
        model=_build_model(),
        tools=[list_docs, read_doc],
    )
