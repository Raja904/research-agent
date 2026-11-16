"""Web search tool supporting multi-query concurrent search via Serper and DuckDuckGo."""

import asyncio
from typing import (
    Any,
    Optional,
)
from duckduckgo_search import DDGS
import httpx
from langchain_core.tools import tool
from tenacity import (
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.langgraph.failure_injector import (
    FailureInjectionError,
    FailureInjector,
)
from app.core.logging import logger
from app.core.metrics import research_tool_calls_total
from app.schemas.research_state import FailureInjectionConfig


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=6),
    retry=retry_if_not_exception_type(FailureInjectionError),
    reraise=True,
)
async def _execute_serper_search(query: str, num_results: int = 5) -> list[dict[str, Any]]:
    """Execute search via Serper API."""
    url = "https://google.serper.dev/search"
    headers = {
        "X-API-KEY": settings.SERPER_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {"q": query, "num": num_results}

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    results: list[dict[str, Any]] = []
    for item in data.get("organic", [])[:num_results]:
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            }
        )
    return results


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=6),
    retry=retry_if_not_exception_type(FailureInjectionError),
    reraise=True,
)
async def _execute_duckduckgo_search(query: str, num_results: int = 5) -> list[dict[str, Any]]:
    """Execute search via DuckDuckGo as reliable fallback."""
    loop = asyncio.get_running_loop()

    def _sync_search() -> list[dict[str, Any]]:
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=num_results))
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                }
                for r in raw
            ]

    return await loop.run_in_executor(None, _sync_search)


async def _search_single_query(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """Execute a single query with Serper or DuckDuckGo fallback."""
    if settings.SERPER_API_KEY:
        try:
            return await _execute_serper_search(query, max_results)
        except FailureInjectionError:
            raise
        except Exception as e:
            logger.warning("serper_search_failed_falling_back_to_ddg", error=str(e), query=query)
            return await _execute_duckduckgo_search(query, max_results)
    return await _execute_duckduckgo_search(query, max_results)


async def execute_web_search(
    queries: list[str] | str,
    max_total_results: int = 5,
    failure_config: Optional[FailureInjectionConfig] = None,
) -> list[dict[str, Any]]:
    """Execute up to 3 web search queries concurrently in one go and return up to 5 results.

    Args:
        queries: List of search query strings (max 3) or single query string.
        max_total_results: Maximum total resources to return (default 5).
        failure_config: Optional failure injection configuration.

    Returns:
        List of dictionaries containing title, url, snippet.
    """
    if isinstance(queries, str):
        query_list = [queries]
    else:
        query_list = list(queries)

    # Agent can send at max 3 queries in form of list
    capped_queries = query_list[:3]
    logger.info("web_search_started", query_count=len(capped_queries), queries=capped_queries)

    async def _run() -> list[dict[str, Any]]:
        # Execute all queries in one go concurrently
        nested_results = await asyncio.gather(
            *[_search_single_query(q, max_results=max_total_results) for q in capped_queries],
            return_exceptions=True,
        )

        seen_urls: set[str] = set()
        deduped_results: list[dict[str, Any]] = []

        for sublist in nested_results:
            if isinstance(sublist, Exception):
                logger.warning("subquery_search_failed", error=str(sublist))
                continue
            for item in sublist:
                url = item.get("url", "").strip()
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    deduped_results.append(item)
                    if len(deduped_results) >= max_total_results:
                        break
            if len(deduped_results) >= max_total_results:
                break

        return deduped_results[:max_total_results]

    try:
        results = await FailureInjector.maybe_intercept_tool("web_search", failure_config, _run)
        logger.info("web_search_completed", result_count=len(results))
        return results
    except Exception as e:
        logger.exception("web_search_failed", error=str(e))
        raise


@tool("web_search")
async def web_search_tool(queries: list[str]) -> str:
    """Search the web for up to 3 queries executed concurrently in one go.

    Args:
        queries: A list of search query strings (at most 3 queries).

    Returns:
        Formatted string containing up to 5 relevant search results with title, URL, and snippet.
    """
    try:
        results = await execute_web_search(queries=queries, max_total_results=5)
        research_tool_calls_total.labels(tool="web_search", status="success").inc()
        if not results:
            return "No search results found."

        lines: list[str] = []
        for idx, r in enumerate(results, start=1):
            lines.append(f"[{idx}] Title: {r.get('title')}\nURL: {r.get('url')}\nSnippet: {r.get('snippet')}\n")
        return "\n".join(lines)
    except Exception as e:
        research_tool_calls_total.labels(tool="web_search", status="error").inc()
        return f"Error executing web search: {str(e)}"
