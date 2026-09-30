"""
Base Agent Class for Co-Evolution Database Testing

This module provides the foundation for all agents in the co-evolution system,
following the design patterns from Argus's ModelAPI class.
"""

import logging
import os
from typing import Any

import httpx

from llm.ledger import ledger

LOGGER = logging.getLogger(__name__)


class BaseAgent:
    """
    Base class for all agents in the co-evolution system.

    This class provides common functionality for LLM-based agents,
    including API client management, configuration handling, and metrics tracking.
    """

    def __init__(self, config: dict[str, Any]):
        """
        Initialize the base agent.

        Args:
            config: Configuration dictionary containing agent settings
        """
        self.config = config
        configured_key = config.get("api_key", "")
        if configured_key.startswith("${") and configured_key.endswith("}"):
            configured_key = os.environ.get(configured_key[2:-1], "")
        self.api_key = configured_key
        self.base_url = config.get("base_url", "https://api.deepseek.com/v1")
        self.model = config.get("model", "deepseek-chat")
        # Hard gate: when False every call_llm is blocked and recorded in
        # the ledger without touching the network (true zero-LLM mode).
        self.llm_enabled = not config.get("disable_llm", False)
        self._client: httpx.Client | None = None

        self.history = []
        self.performance_metrics = {
            "total_calls": 0,
            "successful_calls": 0,
            "failed_calls": 0,
            "total_tokens": 0,
            "total_cost": 0.0,
        }

        LOGGER.info(
            "%s initialized with model: %s", self.__class__.__name__, self.model
        )

    def setup_api_client(self) -> httpx.Client:
        """Return the agent's persistent HTTP client (created once)."""
        if self._client is None:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            self._client = httpx.Client(
                base_url=self.base_url, headers=headers, timeout=300.0
            )
        return self._client

    def close_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Execute the agent's primary task.

        Args:
            context: Context information needed for execution

        Returns:
            Dictionary containing execution results
        """
        raise NotImplementedError("Subclasses must implement execute method")

    def learn_from_feedback(self, feedback: dict[str, Any]) -> None:
        """
        Learn from feedback to improve performance.

        Args:
            feedback: Feedback information from previous executions
        """
        raise NotImplementedError(
            "Subclasses must implement learn_from_feedback method"
        )

    def call_llm(
        self,
        prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        event: str = "generic",
    ) -> str | None:
        """Call the LLM API; every attempt is written to the global ledger."""
        agent = self.__class__.__name__
        if not self.llm_enabled:
            ledger.record(
                agent=agent, event=event, model=self.model,
                prompt=prompt, status="blocked",
            )
            return None
        if not self.api_key:
            ledger.record(
                agent=agent, event=event, model=self.model,
                prompt=prompt, status="error",
            )
            self.performance_metrics["failed_calls"] += 1
            LOGGER.error("LLM call skipped: API key is not configured")
            return None
        self.performance_metrics["total_calls"] += 1
        try:
            client = self.setup_api_client()

            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

            response = client.post("/chat/completions", json=payload)
            response.raise_for_status()

            result = response.json()
            content = result["choices"][0]["message"]["content"]
            usage = result.get("usage", {}) or {}

            self.performance_metrics["successful_calls"] += 1
            self.performance_metrics["total_tokens"] += usage.get(
                "total_tokens", 0
            )
            ledger.record(
                agent=agent,
                event=event,
                model=self.model,
                prompt=prompt,
                response=content,
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                status="ok",
            )
            LOGGER.info(
                "LLM call successful. Tokens: %s", usage.get("total_tokens", 0)
            )
            return content

        except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:
            self.performance_metrics["failed_calls"] += 1
            ledger.record(
                agent=agent, event=event, model=self.model,
                prompt=prompt, status="error",
            )
            LOGGER.error("LLM call failed: %s", e)
            return None

    def update_metrics(self, metrics: dict[str, Any]) -> None:
        """
        Update performance metrics.

        Args:
            metrics: Dictionary of metrics to update
        """
        self.performance_metrics.update(metrics)
        LOGGER.info("Metrics updated: %s", metrics)

    def get_metrics(self) -> dict[str, Any]:
        """
        Get current performance metrics.

        Returns:
            Dictionary of current metrics
        """
        return self.performance_metrics.copy()

    def reset_history(self) -> None:
        """Reset conversation history."""
        self.history = []
        LOGGER.info("History reset")

    def add_to_history(self, role: str, content: str) -> None:
        """
        Add entry to conversation history.

        Args:
            role: Role of the message (user/assistant)
            content: Content of the message
        """
        self.history.append({"role": role, "content": content})
