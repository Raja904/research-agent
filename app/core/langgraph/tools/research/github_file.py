"""GitHub file fetcher tool for inspecting repository code and configs."""

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


def _parse_repo_and_path(input_str: str) -> tuple[str, str, str, str]:
    """Parse owner, repo, branch, and file path from flexible string inputs."""
    # Matches github.com/owner/repo/blob/branch/path/to/file
    blob_match = re.search(r"github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)", input_str)
    if blob_match:
        return blob_match.group(1), blob_match.group(2), blob_match.group(3), blob_match.group(4)

    # Matches owner/repo:path/to/file
    if ":" in input_str:
        repo_part, file_path = input_str.split(":", 1)
        owner, repo = repo_part.strip().split("/", 1)
        return owner.strip(), repo.strip(), "main", file_path.strip().lstrip("/")

    parts = input_str.strip().split("/")
    if len(parts) >= 3:
        owner = parts[0]
        repo = parts[1]
        file_path = "/".join(parts[2:])
        return owner, repo, "main", file_path

    raise ValueError(f"Could not parse repo and file path from: {input_str}. Expected 'owner/repo:path/to/file'")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=6),
    retry=retry_if_not_exception_type((FailureInjectionError, FileNotFoundError)),
    reraise=True,
)
async def _fetch_file_content(owner: str, repo: str, branch: str, file_path: str, max_chars: int) -> dict[str, Any]:
    """Fetch raw file content from GitHub raw content URL."""
    headers = {"User-Agent": "Research-Agent-Evaluation-Harness"}
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"token {settings.GITHUB_TOKEN}"

    # Try requested branch first, fallback to master if main 404s
    branches = [branch] if branch != "main" else ["main", "master"]
    content: Optional[str] = None
    resolved_branch = branch

    async with httpx.AsyncClient(timeout=10.0) as client:
        for b in branches:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{b}/{file_path}"
            res = await client.get(raw_url, headers=headers)
            if res.status_code == 200:
                content = res.text
                resolved_branch = b
                break

    if content is None:
        raise FileNotFoundError(f"File '{file_path}' not found in {owner}/{repo} (tried branches: {branches})")

    if len(content) > max_chars:
        content = content[:max_chars] + f"\n... [Truncated {len(content) - max_chars} characters]"

    return {
        "repo": f"{owner}/{repo}",
        "branch": resolved_branch,
        "file_path": file_path,
        "content": content,
        "length_chars": len(content),
    }


async def fetch_github_file(
    repo_file_identifier: str,
    max_chars: int = 12000,
    failure_config: Optional[FailureInjectionConfig] = None,
) -> dict[str, Any]:
    """Fetch raw content of a specific code or config file from a GitHub repository.

    Args:
        repo_file_identifier: Identifier format 'owner/repo:path/to/file' or GitHub blob URL.
        max_chars: Maximum characters to read.
        failure_config: Optional failure injection configuration.

    Returns:
        Dictionary containing repo, branch, file_path, content.
    """
    owner, repo, branch, file_path = _parse_repo_and_path(repo_file_identifier)
    logger.info("github_file_fetch_started", owner=owner, repo=repo, file_path=file_path)

    async def _run() -> dict[str, Any]:
        return await _fetch_file_content(owner, repo, branch, file_path, max_chars)

    try:
        result = await FailureInjector.maybe_intercept_tool("github_file", failure_config, _run)
        logger.info("github_file_fetch_completed", owner=owner, repo=repo, file_path=file_path)
        return result
    except Exception as e:
        logger.exception("github_file_fetch_failed", owner=owner, repo=repo, file_path=file_path, error=str(e))
        raise


@tool("github_file")
async def github_file_tool(repo: str, file_path: str = "") -> str:
    """Read a specific source code, configuration, or documentation file from a GitHub repository.

    Args:
        repo: Repository identifier (e.g. 'owner/repo' or 'owner/repo:path/to/file').
        file_path: Relative path to the file within the repository (e.g. 'README.md', 'pyproject.toml').

    Returns:
        Content of the requested file.
    """
    try:
        combined = f"{repo}:{file_path}" if file_path and ":" not in repo else (file_path or repo)
        data = await fetch_github_file(repo_file_identifier=combined, max_chars=8000)
        research_tool_calls_total.labels(tool="github_file", status="success").inc()
        return (
            f"File: {data.get('file_path')} (Repo: {data.get('repo')} on branch {data.get('branch')})\n\n"
            f"{data.get('content')}"
        )
    except Exception as e:
        research_tool_calls_total.labels(tool="github_file", status="error").inc()
        return f"Error reading GitHub file {repo}/{file_path}: {str(e)}"


