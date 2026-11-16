"""Observability module for the application."""

import os
from typing import Optional

from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

from app.core.config import settings
from app.core.logging import logger

# Ensure Langfuse environment variables are populated for OpenTelemetry and internal get_client()
if settings.LANGFUSE_HOST:
    os.environ["LANGFUSE_HOST"] = settings.LANGFUSE_HOST
    os.environ["LANGFUSE_BASE_URL"] = settings.LANGFUSE_HOST
    os.environ["LANGFUSE_BASEURL"] = settings.LANGFUSE_HOST
if settings.LANGFUSE_PUBLIC_KEY:
    os.environ["LANGFUSE_PUBLIC_KEY"] = settings.LANGFUSE_PUBLIC_KEY
if settings.LANGFUSE_SECRET_KEY:
    os.environ["LANGFUSE_SECRET_KEY"] = settings.LANGFUSE_SECRET_KEY


_langfuse_client: Optional[Langfuse] = None


def langfuse_init(perform_auth_check: bool = False) -> Optional[Langfuse]:
    """Initialize Langfuse client singleton."""
    global _langfuse_client
    if not settings.LANGFUSE_TRACING_ENABLED:
        logger.debug("langfuse_tracing_disabled")
        return None

    if _langfuse_client is not None:
        return _langfuse_client

    _langfuse_client = Langfuse(
        tracing_enabled=settings.LANGFUSE_TRACING_ENABLED,
        public_key=settings.LANGFUSE_PUBLIC_KEY,
        secret_key=settings.LANGFUSE_SECRET_KEY,
        host=settings.LANGFUSE_HOST,
        environment=settings.ENVIRONMENT.value,
        debug=settings.DEBUG,
    )

    if perform_auth_check:
        try:
            if _langfuse_client.auth_check():
                logger.debug("langfuse_auth_success")
            else:
                logger.warning("langfuse_auth_failure")
        except Exception:
            logger.exception("langfuse_auth_check_failed")

    return _langfuse_client


# Initialize client singleton without blocking network call on module import
langfuse_init(perform_auth_check=False)


def get_langfuse_callback_handler() -> CallbackHandler:
    """Create a Langfuse CallbackHandler for tracking LLM interactions.

    Returns:
        CallbackHandler: Configured Langfuse callback handler.
    """
    if not settings.LANGFUSE_TRACING_ENABLED:
        return CallbackHandler()

    return CallbackHandler()


langfuse_callback_handler = get_langfuse_callback_handler()


def flush_langfuse() -> None:
    """Flush pending Langfuse events to ensure traces appear immediately in dashboard."""
    if not settings.LANGFUSE_TRACING_ENABLED:
        return

    try:
        if _langfuse_client:
            _langfuse_client.flush()
            logger.debug("langfuse_traces_flushed")
        elif langfuse_callback_handler and hasattr(langfuse_callback_handler, "client"):
            langfuse_callback_handler.client.flush()
            logger.debug("langfuse_traces_flushed")
    except Exception as e:
        logger.warning("langfuse_flush_failed", error=str(e))
