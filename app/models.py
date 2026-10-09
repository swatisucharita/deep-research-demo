"""Model setup shared by all agents."""

from agents import (
    AsyncOpenAI,
    OpenAIChatCompletionsModel,
    set_default_openai_key,
    set_tracing_disabled,
)

from app.config import settings

if settings.openai_api_key:
    set_default_openai_key(settings.openai_api_key)
# Traces go to the OpenAI dashboard, so they're off for other providers.
TRACING_ENABLED = not (settings.tracing_disabled or settings.openai_base_url)
set_tracing_disabled(not TRACING_ENABLED)


def build_model(model: str | None = None):
    """`model` defaults to OPENAI_MODEL."""
    model = model or settings.openai_model
    # A custom base URL (Azure, Ollama, OpenRouter, ...) goes through Chat Completions;
    # otherwise use the default OpenAI Responses API with the model name.
    if settings.openai_base_url:
        client = AsyncOpenAI(
            api_key=settings.openai_api_key, base_url=settings.openai_base_url
        )
        return OpenAIChatCompletionsModel(model=model, openai_client=client)
    return model
