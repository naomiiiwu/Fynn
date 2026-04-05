"""Base class shared by all Fynn sub-agents."""

import json
import os
from typing import Any, Callable

import anthropic


class BaseAgent:
    model = "claude-sonnet-4-6"

    def __init__(self) -> None:
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else None

    def _run_tool_loop(
        self,
        system: str,
        user_message: str,
        tools: list[dict],
        tool_executor: Callable[[str, dict], Any],
        max_turns: int = 10,
    ) -> str:
        """
        Generic Claude tool-calling loop.

        Returns the final text response from Claude, or empty string on failure.
        """
        if not self.client:
            return ""

        messages = [{"role": "user", "content": user_message}]

        for _ in range(max_turns):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=system,
                tools=tools,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        return block.text
                return ""

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = tool_executor(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        })
                messages.append({"role": "user", "content": tool_results})

        return ""
