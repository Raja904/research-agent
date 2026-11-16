"""Schemas for the Technical Research and Engineering Investigation Agent."""

from typing import (
    Any,
    Literal,
    Optional,
)
from pydantic import (
    BaseModel,
    Field,
)


class FailureInjectionConfig(BaseModel):
    """Configuration for controlled failure injection during testing."""

    failure_id: Optional[str] = Field(default=None)
    target_node: Optional[str] = Field(default=None)
    target_tool: Optional[str] = Field(default=None)
    failure_mode: Optional[str] = Field(default=None)
    parameters: dict[str, Any] = Field(default_factory=dict)


class EvidenceItem(BaseModel):
    """Stub for backward compatibility."""

    id: str = ""
    source_type: str = "web_search"
    url: str = ""
    title: str = ""
    content: str = ""


class ResearchTask(BaseModel):
    """Stub for backward compatibility."""

    task_id: str = ""
    tool: str = "web_search"
    query_or_url: str = ""
    rationale: str = ""
    status: str = "completed"


class TechnicalReport(BaseModel):
    """Stub for backward compatibility."""

    title: str = ""
    executive_summary: str = ""
    sources_cited: list[str] = Field(default_factory=list)


class ResearchState(BaseModel):
    """Lean 3-node state for the Technical Research Agent."""

    query: str = Field(description="The original user query")
    next_node: Optional[str] = Field(
        default=None,
        description="Routing destination from entry_node: 'execute_agent' or 'generate_report'",
    )
    router_reasoning: Optional[str] = Field(
        default=None,
        description="Reasoning explaining the routing decision",
    )
    agent_output: Optional[str] = Field(
        default=None,
        description="Detailed findings, tool outputs, and scratchpad from the agent",
    )
    sources_cited: list[str] = Field(
        default_factory=list,
        description="List of resource URLs or repositories cited",
    )
    final_report: Optional[str] = Field(
        default=None,
        description="Final synthesized technical report or direct response",
    )
    status: Literal["completed", "degraded", "failed"] = Field(
        default="completed",
        description="Overall execution status",
    )
    error: Optional[str] = Field(
        default=None,
        description="Error details if execution encountered an issue",
    )
    failure_config: Optional[FailureInjectionConfig] = Field(
        default=None,
        description="Optional failure injection settings",
    )


class ResearchRequest(BaseModel):
    """API request payload for research investigation."""

    query: str = Field(..., min_length=1, description="Technical investigation query or question")
    max_iterations: Optional[int] = Field(default=3, description="Optional parameter for backward compatibility")
    failure_injection: Optional[FailureInjectionConfig] = Field(
        default=None, description="Optional failure injection settings"
    )


class ResearchResponse(BaseModel):
    """API response payload returning the investigation results."""

    query: str
    status: Literal["completed", "degraded", "failed"] = "completed"
    next_node: Optional[str] = None
    router_reasoning: Optional[str] = None
    final_report: Optional[str] = None
    sources_cited: list[str] = Field(default_factory=list)
    agent_output: Optional[str] = None
    error: Optional[str] = None
