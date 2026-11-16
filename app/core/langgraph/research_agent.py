"""Simplified 3-node Research Agent workflow for technical investigation and synthesis."""

import json
import re
import time
import uuid
from collections.abc import AsyncGenerator
from typing import (
    Any,
    Optional,
)
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.runnables.config import RunnableConfig
from langgraph.graph import (
    END,
    StateGraph,
)
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent

from app.core.config import settings
from app.core.langgraph.tools.research import AGENT_TOOLS
from app.core.logging import logger
from app.core.metrics import (
    agent_last_response_time_seconds,
    agent_queries_total,
    agent_response_time_seconds,
    agent_sources_discovered_total,
    agent_tokens_total,
)
from app.core.observability import langfuse_callback_handler
from app.core.prompts import (
    AGENT_PROMPT,
    REPORT_PROMPT,
    ROUTER_PROMPT,
    SIMPLE_ANSWER_PROMPT,
)
from app.schemas.research_state import ResearchState
from app.services.research_llm import (
    _record_tokens,
    research_llm_service,
)


def _strip_thought_tags(text: str) -> str:
    """Remove reasoning thought tags from model output."""
    return re.sub(r"<(thought|think)>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


def _extract_json_block(text: str) -> str:
    """Extract json code block or raw json string from LLM output."""
    cleaned = _strip_thought_tags(text)

    # Markdown fence matching
    fence_match = re.search(r"```(?:json)?\s*(.*?)(?:```|$)", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        extracted = fence_match.group(1).strip()
        if extracted.startswith("{"):
            return extracted

    # Locate outer curly braces
    first_brace = cleaned.find("{")
    if first_brace != -1:
        last_brace = cleaned.rfind("}")
        if last_brace != -1 and last_brace > first_brace:
            return cleaned[first_brace : last_brace + 1].strip()
        return cleaned[first_brace:].strip()

    return cleaned


def _extract_urls(text: str) -> list[str]:
    """Extract unique, clean URLs from text."""
    raw_urls = re.findall(r"https?://[^\s)\]\"'>]+", text)
    cleaned: list[str] = []
    seen: set[str] = set()
    for u in raw_urls:
        u_clean = u.rstrip(".,;!?:")
        if u_clean and u_clean not in seen:
            seen.add(u_clean)
            cleaned.append(u_clean)
    return cleaned


class ResearchAgentWorkflow:
    """Orchestrates the 3-node technical investigation workflow using LangGraph."""

    def __init__(self):
        """Initialize the research agent and pre-build the ReAct agent reusing the single LLM client."""
        self.llm_service = research_llm_service
        # Reuse the singleton primary client from research_llm_service
        self._react_agent = create_react_agent(
            model=self.llm_service.get_client(),
            tools=AGENT_TOOLS,
            prompt=AGENT_PROMPT,
        )
        self._graph: Optional[CompiledStateGraph] = None
        logger.info(
            "research_agent_workflow_initialized",
            model=self.llm_service.get_model_name(),
            tools_count=len(AGENT_TOOLS),
            environment=settings.ENVIRONMENT.value,
        )

    async def _entry_node(self, state: ResearchState | dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        """Classify user query and decide next node (execute_agent or generate_report)."""
        query = state.get("query", "") if isinstance(state, dict) else state.query
        logger.info("entry_node_started", query=query)

        system_msg = SystemMessage(content=ROUTER_PROMPT)
        user_msg = HumanMessage(content=f"Query: {query}")

        try:
            response = await self.llm_service.call([system_msg, user_msg])
            raw_content = response.content if isinstance(response.content, str) else str(response.content)
            json_str = _extract_json_block(raw_content)

            data = json.loads(json_str) if json_str else {}
            next_node = data.get("next_node", "execute_agent")
            reasoning = data.get("reasoning", "Standard routing decision.")

            if next_node not in ("execute_agent", "generate_report"):
                next_node = "execute_agent"

            logger.info("entry_node_completed", query=query, next_node=next_node, reasoning=reasoning)
            return {
                "next_node": next_node,
                "router_reasoning": reasoning,
            }
        except Exception as e:
            logger.exception("entry_node_failed_routing_to_agent", error=str(e))
            return {
                "next_node": "execute_agent",
                "router_reasoning": f"Fallback to agent due to routing parsing: {str(e)}",
            }

    def _route_entry(self, state: ResearchState | dict[str, Any]) -> str:
        """Route conditionally based on entry_node classification."""
        next_node = state.get("next_node") if isinstance(state, dict) else getattr(state, "next_node", None)
        if next_node == "generate_report":
            return "generate_report"
        return "execute_agent"

    async def _execute_agent(self, state: ResearchState | dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        """Execute the LangGraph ReAct agent with 4 tools and extract cited resources."""
        query = state.get("query", "") if isinstance(state, dict) else state.query
        logger.info("execute_agent_started", query=query)

        try:
            callbacks = [langfuse_callback_handler] if settings.LANGFUSE_TRACING_ENABLED else []
            agent_config: RunnableConfig = {
                "callbacks": callbacks,
                "metadata": {
                    "step": "execute_agent",
                    "query": query,
                    "environment": settings.ENVIRONMENT.value,
                },
            }

            result = await self._react_agent.ainvoke(
                {"messages": [HumanMessage(content=query)]},
                config=agent_config,
            )

            messages = result.get("messages", [])
            for msg in messages:
                if isinstance(msg, AIMessage):
                    _record_tokens(msg, self.llm_service.get_model_name())

            all_texts: list[str] = []
            for msg in messages:
                if hasattr(msg, "content") and msg.content:
                    all_texts.append(str(msg.content))

            combined_text = "\n".join(all_texts)
            urls = _extract_urls(combined_text)

            final_msg = messages[-1].content if messages else ""
            agent_output = _strip_thought_tags(str(final_msg))

            logger.info(
                "execute_agent_completed",
                query=query,
                sources_count=len(urls),
                output_length=len(agent_output),
            )
            return {
                "agent_output": agent_output,
                "sources_cited": urls,
                "status": "completed",
            }
        except Exception as e:
            logger.exception("execute_agent_failed", query=query, error=str(e))
            return {
                "agent_output": f"Agent execution encountered an error: {str(e)}",
                "sources_cited": [],
                "status": "degraded",
                "error": str(e),
            }

    async def _generate_report(self, state: ResearchState | dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        """Synthesize the final technical report or direct response, always citing sources."""
        query = state.get("query", "") if isinstance(state, dict) else state.query
        next_node = state.get("next_node") if isinstance(state, dict) else state.next_node
        sources_cited = state.get("sources_cited", []) if isinstance(state, dict) else state.sources_cited
        agent_output = state.get("agent_output") if isinstance(state, dict) else state.agent_output
        status = state.get("status", "completed") if isinstance(state, dict) else state.status

        logger.info("generate_report_started", next_node=next_node, sources_count=len(sources_cited))

        try:
            if next_node == "generate_report":
                # Direct response for simple query or greeting
                system_msg = SystemMessage(content=SIMPLE_ANSWER_PROMPT)
                user_msg = HumanMessage(content=query)
                response = await self.llm_service.call([system_msg, user_msg])
                report_text = _strip_thought_tags(str(response.content))
            else:
                # Technical report synthesized from agent findings with citations
                system_msg = SystemMessage(content=REPORT_PROMPT)
                sources_block = (
                    "\n".join(f"- {u}" for u in sources_cited) if sources_cited else "None captured."
                )
                user_prompt = (
                    f"User Query:\n{query}\n\n"
                    f"Agent Research Findings:\n{agent_output or 'No findings available.'}\n\n"
                    f"Cited Sources / URLs:\n{sources_block}"
                )
                user_msg = HumanMessage(content=user_prompt)
                response = await self.llm_service.call([system_msg, user_msg])
                report_text = _strip_thought_tags(str(response.content))

                # Always guarantee a Sources & References section if URLs exist
                if (
                    sources_cited
                    and "sources & references" not in report_text.lower()
                    and "references" not in report_text.lower()
                ):
                    report_text += "\n\n### Sources & References\n" + "\n".join(f"- {u}" for u in sources_cited)

            route_label = next_node if next_node in ("execute_agent", "generate_report") else "execute_agent"
            status_label = "completed" if status == "completed" else "failed"
            agent_queries_total.labels(route=route_label, status=status_label).inc()
            if sources_cited:
                agent_sources_discovered_total.inc(len(sources_cited))

            logger.info("generate_report_completed", query=query, report_length=len(report_text))
            return {"final_report": report_text}
        except Exception as e:
            logger.exception("generate_report_failed", query=query, error=str(e))
            fallback_text = (
                agent_output
                or f"An error occurred while synthesizing the report: {str(e)}"
            )
            return {
                "final_report": fallback_text,
                "status": "degraded",
                "error": str(e),
            }

    def create_graph(self) -> CompiledStateGraph:
        """Compile and return the 3-node LangGraph research workflow."""
        if self._graph is None:
            builder = StateGraph(ResearchState)
            builder.add_node("entry_node", self._entry_node)
            builder.add_node("execute_agent", self._execute_agent)
            builder.add_node("generate_report", self._generate_report)

            builder.set_entry_point("entry_node")
            builder.add_conditional_edges(
                "entry_node",
                self._route_entry,
                {
                    "execute_agent": "execute_agent",
                    "generate_report": "generate_report",
                },
            )
            builder.add_edge("execute_agent", "generate_report")
            builder.add_edge("generate_report", END)

            self._graph = builder.compile()
            logger.info("research_agent_3node_graph_compiled")
        return self._graph

    async def run_investigation(
        self,
        query: str,
        max_iterations: int = 3,
        failure_config: Optional[Any] = None,
        session_id: Optional[str] = None,
    ) -> ResearchState:
        """Run an investigation to completion with tracing and metrics.

        Args:
            query: The technical research query or user question.
            max_iterations: Max iterations (retained for backward compatibility).
            failure_config: Optional failure configuration.
            session_id: Optional session identifier for tracing.

        Returns:
            The final ResearchState.
        """
        graph = self.create_graph()
        initial_state = ResearchState(
            query=query,
            failure_config=failure_config,
        )

        callbacks: list[BaseCallbackHandler] = [langfuse_callback_handler] if settings.LANGFUSE_TRACING_ENABLED else []
        thread_id = session_id or f"research_{uuid.uuid4().hex[:12]}"

        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "callbacks": callbacks,
            "metadata": {
                "session_id": thread_id,
                "environment": settings.ENVIRONMENT.value,
            },
        }

        logger.info("investigation_started", query=query, thread_id=thread_id)
        start_time = time.time()
        final_state_dict = await graph.ainvoke(initial_state.model_dump(), config=config)
        duration = time.time() - start_time
        route_used = final_state_dict.get("next_node") or "execute_agent"
        status_used = final_state_dict.get("status") or "completed"
        agent_response_time_seconds.labels(route=route_used, status=status_used).observe(duration)
        agent_last_response_time_seconds.labels(route=route_used).set(duration)
        return ResearchState(**final_state_dict)

    async def stream_investigation(
        self,
        query: str,
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream node transitions, tool activity, and the final report in real time for UI display.

        Args:
            query: The user inquiry.
            session_id: Optional session ID.

        Yields:
            Dictionaries representing real-time execution events.
        """
        graph = self.create_graph()
        initial_state = ResearchState(query=query)

        callbacks: list[BaseCallbackHandler] = [langfuse_callback_handler] if settings.LANGFUSE_TRACING_ENABLED else []
        thread_id = session_id or f"research_{uuid.uuid4().hex[:12]}"

        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "callbacks": callbacks,
            "metadata": {
                "session_id": thread_id,
                "environment": settings.ENVIRONMENT.value,
            },
        }

        yield {
            "type": "start",
            "query": query,
            "thread_id": thread_id,
        }

        yield {
            "type": "node_start",
            "node": "entry_node",
            "label": "Query Router",
            "message": "Classifying query: evaluating if autonomous research is required...",
        }

        all_sources: list[str] = []
        final_report_text: str = ""
        detected_route: str = "execute_agent"
        stream_start_time = time.time()

        try:
            async for chunk in graph.astream(initial_state.model_dump(), config=config, stream_mode="updates"):
                if "entry_node" in chunk:
                    entry_data = chunk["entry_node"]
                    next_node = entry_data.get("next_node", "execute_agent")
                    detected_route = next_node
                    reasoning = entry_data.get("router_reasoning", "")
                    yield {
                        "type": "node_end",
                        "node": "entry_node",
                        "next_node": next_node,
                        "reasoning": reasoning,
                    }
                    if next_node == "execute_agent":
                        yield {
                            "type": "node_start",
                            "node": "execute_agent",
                            "label": "Research Agent",
                            "message": "Autonomous ReAct agent executing with 4 tools (web search, GitHub)...",
                        }
                    else:
                        yield {
                            "type": "node_start",
                            "node": "generate_report",
                            "label": "Direct Answer Synthesizer",
                            "message": "Direct response selected. Synthesizing answer...",
                        }

                elif "execute_agent" in chunk:
                    agent_data = chunk["execute_agent"]
                    sources = agent_data.get("sources_cited", [])
                    all_sources.extend(sources)
                    yield {
                        "type": "node_end",
                        "node": "execute_agent",
                        "sources_cited": sources,
                        "agent_output": agent_data.get("agent_output", ""),
                    }
                    yield {
                        "type": "node_start",
                        "node": "generate_report",
                        "label": "Report Synthesizer",
                        "message": "Synthesizing comprehensive technical report with citations...",
                    }

                elif "generate_report" in chunk:
                    report_data = chunk["generate_report"]
                    final_report_text = report_data.get("final_report", "")
                    yield {
                        "type": "node_end",
                        "node": "generate_report",
                    }

            stream_duration = time.time() - stream_start_time
            agent_response_time_seconds.labels(route=detected_route, status="completed").observe(stream_duration)
            agent_last_response_time_seconds.labels(route=detected_route).set(stream_duration)

            yield {
                "type": "complete",
                "final_report": final_report_text,
                "sources_cited": list(dict.fromkeys(all_sources)),
                "status": "completed",
            }
        except Exception as e:
            logger.exception("stream_investigation_failed", query=query, error=str(e))
            stream_duration = time.time() - stream_start_time
            agent_response_time_seconds.labels(route=detected_route, status="failed").observe(stream_duration)
            agent_last_response_time_seconds.labels(route=detected_route).set(stream_duration)
            yield {
                "type": "error",
                "error": str(e),
                "status": "failed",
            }
