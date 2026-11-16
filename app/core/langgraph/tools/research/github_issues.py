"""GitHub issue and PR search tool for reliability and bug investigation."""

import re
from typing import (
    Any,
    Optional,
)
from urllib.parse import quote_plus
import httpx
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
from app.schemas.research_state import FailureInjectionConfig


def _parse_repo(repo_str: str) -> tuple[str, str]:
    """Parse owner and repo name from string or URL."""
    cleaned = repo_str.strip().rstrip("/")
    url_match = re.search(r"github\.com/([^/]+)/([^/]+)", cleaned)
    if url_match:
        return url_match.group(1), url_match.group(2)
    parts = cleaned.split("/")
    if len(parts) >= 2:
        return parts[0], parts[1]
    raise ValueError(f"Invalid repository string: {repo_str}")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=6),
    retry=retry_if_not_exception_type(FailureInjectionError),
    reraise=True,
)
async def _search_issues(owner: str, repo: str, query: str, max_results: int) -> list[dict[str, Any]]:
    """Search GitHub repository issues using the GitHub Search API."""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Research-Agent-Evaluation-Harness",
    }
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"token {settings.GITHUB_TOKEN}"

    search_query = f"repo:{owner}/{repo} {query}"
    url = f"https://api.github.com/search/issues?q={quote_plus(search_query)}&per_page={max_results}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.get(url, headers=headers)
        res.raise_for_status()
        data = res.json()

    items = data.get("items", [])
    results: list[dict[str, Any]] = []
    for item in items[:max_results]:
        body = item.get("body", "") or ""
        snippet = body[:400] + ("..." if len(body) > 400 else "")
        results.append(
            {
                "number": item.get("number"),
                "title": item.get("title", ""),
                "state": item.get("state", "open"),
                "url": item.get("html_url", ""),
                "created_at": item.get("created_at", ""),
                "snippet": snippet,
            }
        )
    return results


async def search_github_issues(
    repo_identifier: str,
    query: str,
    max_results: int = 5,
    failure_config: Optional[FailureInjectionConfig] = None,
) -> list[dict[str, Any]]:
    """Search issues and PRs in a repository for bugs, reliability discussions, or feature status.

    Args:
        repo_identifier: Repository 'owner/repo' or URL.
        query: Keywords to search within repo issues (e.g., 'memory leak', 'retry', 'timeout').
        max_results: Maximum issues to return.
        failure_config: Optional failure injection configuration.

    Returns:
        List of matching issue dictionaries.
    """
    owner, repo = _parse_repo(repo_identifier)
    logger.info("github_issue_search_started", owner=owner, repo=repo, query=query)

    async def _run() -> list[dict[str, Any]]:
        return await _search_issues(owner, repo, query, max_results)

    try:
        results = await FailureInjector.maybe_intercept_tool("github_issue", failure_config, _run)
        logger.info("github_issue_search_completed", owner=owner, repo=repo, count=len(results))
        return results
    except Exception as e:
        logger.exception("github_issue_search_failed", owner=owner, repo=repo, query=query, error=str(e))
        raise

