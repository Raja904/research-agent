"""API endpoints for the Technical Research and Engineering Investigation Agent."""

import json
from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.core.langgraph.research_agent import ResearchAgentWorkflow
from app.core.limiter import limiter
from app.core.logging import logger
from app.core.observability import flush_langfuse
from app.schemas.research_state import (
    ResearchRequest,
    ResearchResponse,
)

router = APIRouter()
research_workflow = ResearchAgentWorkflow()


@router.post("/investigate", response_model=ResearchResponse)
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["research"][0])
async def investigate_technical_query(
    request: Request,
    payload: ResearchRequest,
) -> ResearchResponse:
    """Run an autonomous technical research and engineering investigation.

    Args:
        request: FastAPI request object for rate limiting.
        payload: Research request specifying the technical query and options.

    Returns:
        ResearchResponse: Investigation response with routing reasoning, report, and sources.

    Raises:
        HTTPException: On internal processing errors.
    """
    logger.info("research_investigate_request_received", query=payload.query)

    try:
        final_state = await research_workflow.run_investigation(
            query=payload.query,
            max_iterations=payload.max_iterations or 3,
            failure_config=payload.failure_injection,
        )

        logger.info(
            "research_investigate_request_completed",
            query=payload.query,
            status=final_state.status,
            next_node=final_state.next_node,
            sources_count=len(final_state.sources_cited),
        )

        return ResearchResponse(
            query=payload.query,
            status=final_state.status,
            next_node=final_state.next_node,
            router_reasoning=final_state.router_reasoning,
            final_report=final_state.final_report,
            sources_cited=final_state.sources_cited,
            agent_output=final_state.agent_output,
            error=final_state.error,
        )
    except Exception as e:
        logger.exception("research_investigate_request_failed", query=payload.query, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        flush_langfuse()


@router.post("/experiment", response_model=ResearchResponse)
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["research"][0])
async def run_reliability_experiment(
    request: Request,
    payload: ResearchRequest,
) -> ResearchResponse:
    """Run a technical investigation with failure injection for reliability testing."""
    if not payload.failure_injection:
        raise HTTPException(
            status_code=400,
            detail="failure_injection field is required for /experiment endpoint",
        )

    logger.info(
        "research_experiment_request_received",
        query=payload.query,
        failure_mode=payload.failure_injection.failure_mode,
        target_tool=payload.failure_injection.target_tool,
        target_node=payload.failure_injection.target_node,
    )

    try:
        final_state = await research_workflow.run_investigation(
            query=payload.query,
            max_iterations=payload.max_iterations or 3,
            failure_config=payload.failure_injection,
        )

        return ResearchResponse(
            query=payload.query,
            status=final_state.status,
            next_node=final_state.next_node,
            router_reasoning=final_state.router_reasoning,
            final_report=final_state.final_report,
            sources_cited=final_state.sources_cited,
            agent_output=final_state.agent_output,
            error=final_state.error,
        )
    except Exception as e:
        logger.exception("research_experiment_request_failed", query=payload.query, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        flush_langfuse()


@router.post("/stream")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["research"][0])
async def stream_technical_investigation(
    request: Request,
    payload: ResearchRequest,
) -> StreamingResponse:
    """Stream real-time node transitions, tool calls, and final report for UI."""
    logger.info("research_stream_request_received", query=payload.query)

    async def event_generator():
        try:
            async for event in research_workflow.stream_investigation(query=payload.query):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            logger.exception("stream_generator_failed", error=str(e))
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            flush_langfuse()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/stream")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["research"][0])
async def stream_technical_investigation_get(
    request: Request,
    query: str,
) -> StreamingResponse:
    """GET SSE stream for real-time UI investigation updates."""
    logger.info("research_get_stream_request_received", query=query)

    async def event_generator():
        try:
            async for event in research_workflow.stream_investigation(query=query):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            logger.exception("stream_generator_failed", error=str(e))
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            flush_langfuse()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
