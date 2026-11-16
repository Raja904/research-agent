"""Verification script for the Research Agent and failure injection harness."""

import asyncio
from pathlib import Path
import sys

# Ensure repository root is on sys.path
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from app.main import app
from app.core.langgraph.research_agent import research_workflow
from app.schemas.research_state import (
    EvidenceItem,
    FailureInjectionConfig,
    ResearchRequest,
    ResearchResponse,
    ResearchState,
    ResearchTask,
)
from app.core.langgraph.failure_injector import (
    FailureInjectionError,
    FailureInjector,
)


async def main():
    print("=" * 60)
    print("1. Verifying Schemas...")
    task = ResearchTask(
        task_id="t1",
        tool="web_search",
        query_or_url="LangGraph vs CrewAI",
        rationale="Compare state checkpointing",
    )
    evidence = EvidenceItem(
        id="ev_1",
        source_type="web_search",
        url="https://langchain.com",
        title="LangGraph Docs",
        content="PostgreSQL checkpointing enables durable human-in-the-loop workflows.",
    )
    state = ResearchState(
        query="Compare LangGraph vs CrewAI",
        hypotheses=["LangGraph provides checkpointing"],
        tasks=[task],
        evidence=[evidence],
    )
    assert len(state.evidence) == 1
    assert state.evidence[0].source_type == "web_search"
    print("   Schemas validated successfully!")

    print("\n2. Verifying Failure Interceptor...")
    # Test timeout failure
    timeout_config = FailureInjectionConfig(
        target_tool="test_tool",
        failure_mode="timeout",
        parameters={"delay_seconds": 0.05},
    )

    async def dummy_tool():
        return "success"

    try:
        await FailureInjector.maybe_intercept_tool("test_tool", timeout_config, dummy_tool)
        raise AssertionError("Expected timeout failure not raised")
    except FailureInjectionError as e:
        print(f"   Injected failure intercepted as expected: {e}")

    # Test 429 failure
    rate_limit_config = FailureInjectionConfig(
        target_tool="test_tool",
        failure_mode="rate_limit_429",
    )
    try:
        await FailureInjector.maybe_intercept_tool("test_tool", rate_limit_config, dummy_tool)
        raise AssertionError("Expected 429 failure not raised")
    except FailureInjectionError as e:
        print(f"   Injected failure intercepted as expected: {e}")

    print("\n3. Verifying LangGraph Workflow Compilation...")
    graph = research_workflow.create_graph()
    assert graph is not None
    print("   LangGraph Research Workflow compiled successfully!")

    print("\n4. Verifying FastAPI Route Registrations...")
    route_paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/api/v1/research/investigate" in route_paths, "Route /api/v1/research/investigate not registered"
    assert "/api/v1/research/experiment" in route_paths, "Route /api/v1/research/experiment not registered"
    print("   API routes registered: /api/v1/research/investigate, /api/v1/research/experiment")

    print("\n5. Verifying Research LLM Service (OpenRouter Nemotron / Universal)...")
    from app.services.research_llm import research_llm_service
    model_name = research_llm_service.get_model_name()
    print(f"   Configured Research Agent Model: {model_name}")
    assert model_name is not None
    print("   Research LLM Service configured successfully!")

    print("\nALL VERIFICATION CHECKS PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())

