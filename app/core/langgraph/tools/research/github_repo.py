"""GitHub repository inspector tool."""

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

from app.core.config import settings
from app.core.langgraph.failure_injector import (
    FailureInjectionError,
    FailureInjector,
)
from app.core.logging import logger
from app.core.metrics import research_tool_calls_total
from app.schemas.research_state import FailureInjectionConfig


def _parse_github_repo(repo_str: str) -> tuple[str, str]:
    """Parse owner and repo name from string or URL."""
    cleaned = repo_str.strip().rstrip("/")
    # Handle full URL
    url_match = re.search(r"github\.com/([^/]+)/([^/]+)", cleaned)
    if url_match:
        return url_match.group(1), url_match.group(2)
    parts = cleaned.split("/")
    if len(parts) >= 2:
        return parts[0], parts[1]
    raise ValueError(f"Invalid GitHub repository format: {repo_str}. Expected 'owner/repo' or URL.")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=6),
    retry=retry_if_not_exception_type(FailureInjectionError),
    reraise=True,
)
async def _inspect_repo(owner: str, repo: str) -> dict[str, Any]:
    """Call GitHub API to inspect repository metadata and structure."""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Research-Agent-Evaluation-Harness",
    }
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"token {settings.GITHUB_TOKEN}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Repo metadata
        meta_res = await client.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
        meta_res.raise_for_status()
        meta = meta_res.json()

        # Top-level contents
        tree_res = await client.get(f"https://api.github.com/repos/{owner}/{repo}/contents", headers=headers)
        top_files: list[str] = []
        if tree_res.status_code == 200:
            top_files = [item.get("path", "") for item in tree_res.json() if isinstance(item, dict)]

    return {
        "full_name": meta.get("full_name", f"{owner}/{repo}"),
        "description": meta.get("description", ""),
        "stars": meta.get("stargazers_count", 0),
        "forks": meta.get("forks_count", 0),
        "open_issues": meta.get("open_issues_count", 0),
        "license": meta.get("license", {}).get("spdx_id") if meta.get("license") else "Unknown",
        "default_branch": meta.get("default_branch", "main"),
        "top_level_files": top_files[:30],
    }


async def inspect_github_repo(
    repo_identifier: str,
    failure_config: Optional[FailureInjectionConfig] = None,
) -> dict[str, Any]:
    """Inspect a GitHub repository's metadata, stats, and top-level directory structure.

    Args:
        repo_identifier: Either 'owner/repo' or full GitHub URL.
        failure_config: Optional failure injection configuration.

    Returns:
        Dictionary containing stars, description, open issues, and directory listing.
    """
    owner, repo = _parse_github_repo(repo_identifier)
    logger.info("github_repo_inspection_started", owner=owner, repo=repo)

    async def _run() -> dict[str, Any]:
        return await _inspect_repo(owner, repo)

    try:
        result = await FailureInjector.maybe_intercept_tool("github_repo", failure_config, _run)
        logger.info("github_repo_inspection_completed", owner=owner, repo=repo)
        return result
    except Exception as e:
        logger.exception("github_repo_inspection_failed", owner=owner, repo=repo, error=str(e))
        raise


@tool("github_repo")
async def github_repo_tool(repo: str) -> str:
    """Inspect a GitHub repository to get description, stars, license, issues, and top-level directory structure.

    Args:
        repo: GitHub repository identifier (e.g., 'owner/repo' or GitHub URL).

    Returns:
        Formatted summary of repository statistics and top files.
    """
    try:
        data = await inspect_github_repo(repo_identifier=repo)
        research_tool_calls_total.labels(tool="github_repo", status="success").inc()
        return (
            f"Repository: {data.get('full_name')}\n"
            f"Stars: {data.get('stars')} | Forks: {data.get('forks')} | Open Issues: {data.get('open_issues')}\n"
            f"License: {data.get('license')} | Default Branch: {data.get('default_branch')}\n"
            f"Description: {data.get('description')}\n"
            f"Top Files: {', '.join(data.get('top_level_files', []))}"
        )
    except Exception as e:
        research_tool_calls_total.labels(tool="github_repo", status="error").inc()
        return f"Error inspecting GitHub repository {repo}: {str(e)}"


