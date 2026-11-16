"""Failure injection interceptor for agent reliability experiments."""

import asyncio
from typing import (
    Any,
    Callable,
    Optional,
)

from app.core.logging import logger
from app.schemas.research_state import FailureInjectionConfig


class FailureInjectionError(Exception):
    """Exception raised when an injected failure occurs."""

    pass


class FailureInjector:
    """Interception manager that evaluates active failure configurations."""

    @staticmethod
    async def maybe_intercept_tool(
        tool_name: str,
        config: Optional[FailureInjectionConfig],
        tool_func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Conditionally intercepts a tool execution to inject configured failure modes.

        Args:
            tool_name: Name of the tool being executed.
            config: The active failure configuration, if any.
            tool_func: The actual async callable of the tool.
            *args: Positional arguments for the tool.
            **kwargs: Keyword arguments for the tool.

        Returns:
            The tool result (or modified/simulated result).

        Raises:
            FailureInjectionError: When simulating hard exceptions (timeouts, 429s).
        """
        if not config or not config.failure_mode:
            return await tool_func(*args, **kwargs)

        # Check if this tool is targeted (or if all tools are targeted)
        if config.target_tool and config.target_tool != tool_name:
            return await tool_func(*args, **kwargs)

        mode = config.failure_mode
        logger.warning(
            "failure_injection_triggered",
            tool=tool_name,
            failure_mode=mode,
            target_tool=config.target_tool,
        )

        if mode == "timeout":
            delay = config.parameters.get("delay_seconds", 5.0)
            await asyncio.sleep(delay)
            raise FailureInjectionError(f"Simulated timeout error in tool: {tool_name}")

        elif mode == "rate_limit_429":
            raise FailureInjectionError(f"Simulated HTTP 429 Too Many Requests in tool: {tool_name}")

        elif mode == "empty_response":
            logger.info("simulating_empty_tool_response", tool=tool_name)
            return [] if "search" in tool_name or "issues" in tool_name else ""

        elif mode == "corrupted_data":
            logger.info("simulating_corrupted_tool_data", tool=tool_name)
            return {"error": "CORRUPTED_PAYLOAD_UNPARSEABLE", "raw_corrupt_bytes": "0xDEADBEEF"}

        # If no tool-specific failure matched, proceed normally
        return await tool_func(*args, **kwargs)

    @staticmethod
    def should_inject_node_failure(
        node_name: str,
        config: Optional[FailureInjectionConfig],
    ) -> Optional[str]:
        """Check if a specific node should alter its behavior due to failure injection.

        Args:
            node_name: The name of the currently executing graph node.
            config: Active failure injection config.

        Returns:
            The failure mode string if matched, otherwise None.
        """
        if not config or not config.failure_mode:
            return None

        if config.target_node and config.target_node == node_name:
            logger.warning(
                "node_failure_injection_matched",
                node=node_name,
                failure_mode=config.failure_mode,
            )
            return config.failure_mode

        return None

