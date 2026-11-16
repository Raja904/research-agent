"""Pluggable LLM service for the Research Agent supporting OpenRouter (Nemotron free) and other providers."""

import asyncio
from typing import (
    Any,
    Optional,
)
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
)
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import agent_tokens_total
from app.core.observability import langfuse_callback_handler


def _record_tokens(response: BaseMessage, model_name: str) -> None:
    """Extract token counts from response metadata and record to Prometheus."""
    prompt_tokens = 0
    completion_tokens = 0

    if hasattr(response, "usage_metadata") and response.usage_metadata:
        prompt_tokens = response.usage_metadata.get("input_tokens", 0)
        completion_tokens = response.usage_metadata.get("output_tokens", 0)
    elif hasattr(response, "response_metadata") and response.response_metadata:
        token_usage = response.response_metadata.get("token_usage") or {}
        prompt_tokens = token_usage.get("prompt_tokens", 0)
        completion_tokens = token_usage.get("completion_tokens", 0)

    # Fallback estimate (~4 chars per token) if provider omits usage
    if completion_tokens == 0 and hasattr(response, "content") and response.content:
        completion_tokens = max(1, len(str(response.content)) // 4)

    if prompt_tokens > 0:
        agent_tokens_total.labels(type="prompt", model=model_name).inc(prompt_tokens)
    if completion_tokens > 0:
        agent_tokens_total.labels(type="completion", model=model_name).inc(completion_tokens)


class ResearchLLMService:
    """Universal LLM client for Research Agent, defaults to OpenRouter Nemotron 3 Ultra free model."""

    def __init__(self):
        """Initialize the model client based on configuration."""
        self._primary_client: Optional[ChatOpenAI] = None
        self._fallback_clients: list[ChatOpenAI] = []
        self._active_model_name: str = settings.RESEARCH_MODEL_NAME
        self._init_clients()

    def _resolve_credentials(self) -> tuple[str, str, str]:
        """Resolve API key, base URL, and model name from settings and env."""
        provider = settings.RESEARCH_MODEL_PROVIDER.lower()

        # Check explicit research API key first
        api_key = settings.RESEARCH_MODEL_API_KEY

        if provider == "openrouter":
            api_key = api_key or settings.OPENROUTER_API_KEY
            base_url = settings.RESEARCH_MODEL_BASE_URL or "https://openrouter.ai/api/v1"
            model = settings.RESEARCH_MODEL_NAME or "nvidia/nemotron-3.5-lightning:free"
        elif provider == "mistral":
            api_key = api_key or settings.MISTRAL_API_KEY
            base_url = settings.RESEARCH_MODEL_BASE_URL or "https://api.mistral.ai/v1"
            model = settings.RESEARCH_MODEL_NAME or "mistral-large-latest"
        elif provider == "openai":
            api_key = api_key or settings.OPENAI_API_KEY
            base_url = "https://api.openai.com/v1"
            model = settings.RESEARCH_MODEL_NAME or "gpt-4o"
        else:
            api_key = (
                api_key
                or settings.OPENROUTER_API_KEY
                or settings.MISTRAL_API_KEY
                or settings.OPENAI_API_KEY
                or "dummy_key"
            )
            base_url = settings.RESEARCH_MODEL_BASE_URL
            model = settings.RESEARCH_MODEL_NAME

        return api_key, base_url, model

    def _init_clients(self) -> None:
        """Initialize the primary and fallback LLM instances."""
        api_key, base_url, model = self._resolve_credentials()
        self._active_model_name = model

        if not api_key:
            logger.warning(
                "research_llm_api_key_not_configured",
                provider=settings.RESEARCH_MODEL_PROVIDER,
                expected_env="OPENROUTER_API_KEY (or RESEARCH_MODEL_API_KEY)",
            )

        provider = settings.RESEARCH_MODEL_PROVIDER.lower()

        default_headers: dict[str, str] = {}
        model_kwargs: dict[str, Any] = {}

        extra_body: Optional[dict[str, Any]] = None

        if provider == "openrouter":
            default_headers = {
                "HTTP-Referer": "http://localhost:8000",
                "X-Title": "Technical Research & Reliability Agent",
            }
            if settings.RESEARCH_MODEL_REASONING_ENABLED:
                extra_body = {"reasoning": {"enabled": True}}

        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": SecretStr(api_key or "sk-placeholder"),
            "temperature": settings.RESEARCH_MODEL_TEMPERATURE,
            "max_tokens": settings.RESEARCH_MODEL_MAX_TOKENS,
        }
        if base_url:
            kwargs["base_url"] = base_url
        if default_headers:
            kwargs["default_headers"] = default_headers
        if extra_body:
            kwargs["extra_body"] = extra_body
        if model_kwargs:
            kwargs["model_kwargs"] = model_kwargs

        self._primary_client = ChatOpenAI(**kwargs)

        # Fallback models for OpenRouter free tier to guard against 429 rate limits
        if provider == "openrouter":
            free_fallbacks = [
                "meta-llama/llama-3.3-70b-instruct:free",
                "google/gemini-2.0-flash-exp:free",
                "mistralai/mistral-small-24b-instruct-2501:free",
            ]
            for fb_model in free_fallbacks:
                if fb_model != model:
                    fb_kwargs = kwargs.copy()
                    fb_kwargs["model"] = fb_model
                    self._fallback_clients.append(ChatOpenAI(**fb_kwargs))
        elif provider == "mistral":
            fallbacks = ["mistral-small-latest", "codestral-latest"]
            for fb_model in fallbacks:
                if fb_model != model:
                    fb_kwargs = kwargs.copy()
                    fb_kwargs["model"] = fb_model
                    self._fallback_clients.append(ChatOpenAI(**fb_kwargs))

        logger.info(
            "research_llm_service_configured",
            provider=settings.RESEARCH_MODEL_PROVIDER,
            primary_model=model,
            base_url=base_url,
            fallback_count=len(self._fallback_clients),
        )

    def get_model_name(self) -> str:
        """Return the active model name."""
        return self._active_model_name

    def get_client(self) -> ChatOpenAI:
        """Return the reusable singleton ChatOpenAI client instance."""
        if self._primary_client is None:
            self._init_clients()
        assert self._primary_client is not None
        return self._primary_client

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def _invoke_client(self, client: ChatOpenAI, messages: list[BaseMessage]) -> BaseMessage:
        """Invoke a specific ChatOpenAI client with tenacity retry and reasoning detail capture."""
        callbacks = [langfuse_callback_handler] if settings.LANGFUSE_TRACING_ENABLED else []
        response = await client.ainvoke(messages, config={"callbacks": callbacks})

        # OpenRouter reasoning extraction: if content is empty but reasoning is present
        if not response.content and hasattr(response, "additional_kwargs"):
            reasoning_text = response.additional_kwargs.get("reasoning_details") or response.additional_kwargs.get(
                "reasoning"
            )
            if reasoning_text and isinstance(reasoning_text, str):
                logger.info("extracted_content_from_reasoning_details")
                response = AIMessage(
                    content=reasoning_text,
                    additional_kwargs=response.additional_kwargs,
                )

        # Record token usage metrics to Prometheus
        _record_tokens(response, getattr(client, "model_name", self._active_model_name))

        return response

    async def call(self, messages: list[BaseMessage]) -> BaseMessage:
        """Call the LLM with automatic retry and model fallback.

        Args:
            messages: List of BaseMessage prompts.

        Returns:
            BaseMessage response from the model.
        """
        if self._primary_client is None:
            self._init_clients()

        clients = [self._primary_client] + self._fallback_clients
        last_error: Optional[Exception] = None

        for client in clients:
            if client is None:
                continue
            model_name = getattr(client, "model_name", "unknown")
            try:
                logger.info("research_llm_calling_model", model=model_name)
                response = await self._invoke_client(client, messages)
                logger.info("research_llm_call_succeeded", model=model_name)
                return response
            except Exception as e:
                logger.warning("research_llm_model_attempt_failed", model=model_name, error=str(e))
                last_error = e
                # Brief pause before attempting next fallback model
                await asyncio.sleep(0.5)

        logger.exception("research_llm_all_models_failed", last_error=str(last_error))
        raise RuntimeError(f"All research LLM models failed. Last error: {str(last_error)}")


# Singleton instance for the Research Agent
research_llm_service = ResearchLLMService()
