"""Research tools package providing 4 core technical investigation tools."""

from app.core.langgraph.tools.research.github_file import (
    fetch_github_file,
    github_file_tool,
)
from app.core.langgraph.tools.research.github_repo import (
    github_repo_tool,
    inspect_github_repo,
)
from app.core.langgraph.tools.research.web_search import (
    execute_web_search,
    web_search_tool,
)
from app.core.langgraph.tools.research.webpage_reader import (
    fetch_webpage_content,
    fetch_webpage_tool,
)

# The 4 LangGraph ReAct agent tools
AGENT_TOOLS = [
    web_search_tool,
    fetch_webpage_tool,
    github_repo_tool,
    github_file_tool,
]

__all__ = [
    "execute_web_search",
    "fetch_webpage_content",
    "inspect_github_repo",
    "fetch_github_file",
    "web_search_tool",
    "fetch_webpage_tool",
    "github_repo_tool",
    "github_file_tool",
    "AGENT_TOOLS",
]
