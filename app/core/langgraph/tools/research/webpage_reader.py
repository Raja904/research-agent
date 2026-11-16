"""Webpage reader tool for extracting clean text content from URLs."""

import re
from typing import (
    Any,
    Optional,
)
import httpx
from langchain_core.tools import tool
from tenacity import (
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.langgraph.failure_injector import (
    FailureInjectionError,
    FailureInjector,
)
from app.core.logging import logger
from app.core.metrics import research_tool_calls_total
from app.schemas.research_state import FailureInjectionConfig


def _clean_html_content(html: str) -> str:
    """Extract clean text content from HTML without heavy external dependencies."""
    # Remove script and style elements
    cleaned = re.sub(r"<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", cleaned)
    # Decode common HTML entities
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    # Normalize excessive whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _should_retry_web_error(exc: BaseException) -> bool:
    """Do not retry client errors like 404 or injected failures."""
    if isinstance(exc, FailureInjectionError):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code in (400, 401, 403, 404, 410):
            return False
    return True


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=6),
    retry=_should_retry_web_error,
    reraise=True,
)
async def _fetch_url(url: str, max_length: int) -> dict[str, Any]:
    """Fetch and parse clean text from a web page."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,text/plain",
    }

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        html = response.text

    # Extract title
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else url

    text = _clean_html_content(html)
    if len(text) > max_length:
        text = text[:max_length] + " ... [Truncated]"

    return {
        "url": url,
        "title": title,
        "content": text,
        "status_code": response.status_code,
    }


async def fetch_webpage_content(
    url: str,
    max_length: int = 8000,
    failure_config: Optional[FailureInjectionConfig] = None,
) -> dict[str, Any]:
    """Fetch and extract clean text from a target web URL.

    Args:
        url: The URL to read.
        max_length: Maximum length of extracted text snippet.
        failure_config: Optional failure injection configuration.

    Returns:
        Dictionary containing url, title, content, status_code.
    """
    logger.info("webpage_fetch_started", url=url, max_length=max_length)

    async def _run() -> dict[str, Any]:
        return await _fetch_url(url, max_length)

    try:
        result = await FailureInjector.maybe_intercept_tool("fetch_webpage", failure_config, _run)
        logger.info(
            "webpage_fetch_completed",
            url=url,
            content_length=len(result.get("content", "")) if isinstance(result, dict) else 0,
        )
        return result
    except Exception as e:
        logger.exception("webpage_fetch_failed", url=url, error=str(e))
        raise


@tool("fetch_webpage")
async def fetch_webpage_tool(url: str) -> str:
    """Fetch and parse clean text from a target web URL.

    Args:
        url: The web URL to inspect and read.

    Returns:
        Cleaned text content of the webpage.
    """
    try:
        data = await fetch_webpage_content(url=url, max_length=6000)
        research_tool_calls_total.labels(tool="fetch_webpage", status="success").inc()
        title = data.get("title", "")
        content = data.get("content", "")
        return f"Title: {title}\nURL: {url}\n\nContent:\n{content}"
    except Exception as e:
        research_tool_calls_total.labels(tool="fetch_webpage", status="error").inc()
        return f"Error fetching webpage {url}: {str(e)}"


