"""This file contains the prompts for the agent."""

import os
from datetime import datetime
from typing import Optional

from app.core.config import settings

_PROMPTS_DIR = os.path.dirname(__file__)

# Read templates once at module load — no file I/O per request
with open(os.path.join(_PROMPTS_DIR, "system.md"), "r") as _f:
    _SYSTEM_PROMPT_TEMPLATE = _f.read()

with open(os.path.join(_PROMPTS_DIR, "session_title.md"), "r") as _f:
    SESSION_TITLE_PROMPT = _f.read()

with open(os.path.join(_PROMPTS_DIR, "memory_capture.md"), "r") as _f:
    _MEMORY_CAPTURE_PROMPT_TEMPLATE = _f.read()

_RESEARCH_PROMPTS_DIR = os.path.join(_PROMPTS_DIR, "research")

with open(os.path.join(_RESEARCH_PROMPTS_DIR, "router.md"), "r") as _f:
    ROUTER_PROMPT = _f.read()

with open(os.path.join(_RESEARCH_PROMPTS_DIR, "agent.md"), "r") as _f:
    AGENT_PROMPT = _f.read()

with open(os.path.join(_RESEARCH_PROMPTS_DIR, "report.md"), "r") as _f:
    REPORT_PROMPT = _f.read()

with open(os.path.join(_RESEARCH_PROMPTS_DIR, "simple_answer.md"), "r") as _f:
    SIMPLE_ANSWER_PROMPT = _f.read()

# Backward compatible stubs for existing tests or imports
DECOMPOSE_PROMPT = ROUTER_PROMPT
ANALYZE_PROMPT_TEMPLATE = ""
FOLLOWUP_PROMPT_TEMPLATE = ""


def load_analyze_prompt(query: str, hypotheses: list[str], evidence: str) -> str:
    """Render the evidence gap analysis prompt."""
    return (
        ANALYZE_PROMPT_TEMPLATE.replace("{query}", query)
        .replace("{hypotheses}", "\n".join(f"- {h}" for h in hypotheses))
        .replace("{evidence}", evidence)
    )


def load_followup_prompt(query: str, gaps: list[str], previous_tasks: list[str]) -> str:
    """Render the targeted followup prompt."""
    return (
        FOLLOWUP_PROMPT_TEMPLATE.replace("{query}", query)
        .replace("{gaps}", "\n".join(f"- {g}" for g in gaps))
        .replace("{previous_tasks}", "\n".join(f"- {t}" for t in previous_tasks))
    )


def load_system_prompt(username: Optional[str] = None, **kwargs):
    """Load the system prompt from the cached template."""
    user_context = f"# User\nYou are talking to {username}.\n" if username else ""
    return _SYSTEM_PROMPT_TEMPLATE.format(
        agent_name=settings.PROJECT_NAME + " Agent",
        current_date_and_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        user_context=user_context,
        **kwargs,
    )


def load_memory_capture_prompt() -> str:
    """Render the memory-capture prompt with today's date.

    Uses ``str.replace`` rather than ``str.format`` because the prompt body
    contains literal JSON examples with curly braces that would break format().
    """
    return _MEMORY_CAPTURE_PROMPT_TEMPLATE.replace("{current_date}", datetime.now().strftime("%Y-%m-%d"))
