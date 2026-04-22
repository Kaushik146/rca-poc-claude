"""Base class for RCA pipeline agents with shared ReAct loop logic."""
import json
import logging
from llm_client import get_client, get_model

logger = logging.getLogger(__name__)


class BaseAgent:
    """Base class providing shared ReAct loop infrastructure for all RCA agents.

    Subclasses should override:
        - SYSTEM_PROMPT: str - the agent's system prompt
        - TOOLS: list - OpenAI function-calling tool definitions
        - _execute_tool(name, args) -> str - tool dispatch logic
    """

    SYSTEM_PROMPT = ""
    TOOLS = []
    MAX_ITERATIONS = 12

    def __init__(self):
        self.client = get_client()
        self.model = get_model()
        self.logger = logging.getLogger(self.__class__.__name__)

    def run(self, user_prompt: str) -> dict:
        """Execute the ReAct loop with the given user prompt.

        Returns:
            dict with 'result' key containing the agent's final analysis.
        """
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        for iteration in range(self.MAX_ITERATIONS):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=self.TOOLS if self.TOOLS else None,
                    tool_choice="auto" if self.TOOLS else None,
                    temperature=0,
                    timeout=60,
                )
            except Exception as e:
                self.logger.error("LLM API call failed on iteration %d: %s", iteration, type(e).__name__)
                return {"error": f"LLM call failed: {type(e).__name__}"}

            msg = response.choices[0].message
            messages.append(msg)

            # Check for finish
            if not msg.tool_calls:
                return {"result": msg.content or ""}

            # Execute tools
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                if fn_name == "finish_analysis":
                    return args

                try:
                    result = self._execute_tool(fn_name, args)
                except Exception as e:
                    self.logger.warning("Tool %s failed: %s", fn_name, type(e).__name__)
                    result = json.dumps({"error": f"Tool execution failed: {type(e).__name__}"})

                # Truncate large results
                if len(result) > 4000:
                    result = result[:4000] + f"... [truncated, {len(result)} chars]"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            # Truncate message history if too long
            if len(messages) > 30:
                head = messages[:2]
                summary = {"role": "system", "content": f"[{len(messages) - 20} earlier messages truncated]"}
                tail = messages[-17:]
                messages = head + [summary] + tail

        return {"error": "Max iterations reached without conclusion"}

    def _execute_tool(self, name: str, args: dict) -> str:
        """Dispatch tool call. Override in subclasses."""
        return json.dumps({"error": f"Unknown tool: {name}"})
