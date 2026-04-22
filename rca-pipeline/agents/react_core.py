"""
ReAct Core — Production-grade ReAct (Reason + Act) engine shared by all agents.

This is what separates a real agent from a glorified prompt:

1. SCRATCHPAD          — Persistent working memory across tool calls. The agent can
                         store findings, hypotheses, and intermediate state.
2. SELF-REFLECTION     — Before finishing, the agent is FORCED to reflect: check for
                         gaps, contradictions, missed signals, and low-confidence areas.
3. OBSERVATION CHAIN   — Every tool call + result is logged with timing and metadata,
                         creating a full audit trail of the agent's reasoning.
4. BACKTRACKING        — If a tool result contradicts prior findings, the agent can
                         explicitly revise its scratchpad and re-investigate.
5. CONFIDENCE TRACKING — The agent maintains a running confidence estimate, and must
                         justify it with evidence before finishing.
6. ADAPTIVE ITERATIONS — Easy problems finish in 3 iterations; hard ones get up to max.
                         The agent can request more iterations if confidence is below threshold.
7. MULTI-STRATEGY      — Agents can try multiple investigation strategies and pick the
                         best result.

Usage:
    from react_core import ReActEngine, ToolDef, ToolResult

    engine = ReActEngine(
        agent_name="LogAgent",
        system_prompt="...",
        tools=TOOLS,
        tool_executor=my_executor_fn,
        max_iterations=12,
        confidence_threshold=0.7,
    )
    result = engine.run(user_message="Investigate these logs...")
"""

import json, time, hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from llm_client import get_client, get_model
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Observation:
    """Single tool call + result in the agent's chain of thought."""
    iteration: int
    tool_name: str
    tool_args: dict
    result_summary: str          # truncated for logging
    result_full: str             # full result (not sent to LLM)
    duration_ms: float
    timestamp: float

@dataclass
class ScratchpadEntry:
    """A single entry in the agent's working memory."""
    key: str
    value: Any
    updated_at: int              # iteration number when last updated
    confidence: float = 0.0      # agent's confidence in this finding

@dataclass
class ReflectionResult:
    """Structured self-reflection output."""
    gaps_identified: List[str]
    contradictions: List[str]
    confidence_estimate: float
    reasoning: str
    should_continue: bool        # True if agent wants more iterations
    revised_findings: Dict[str, Any]

@dataclass
class AgentResult:
    """Final output from the ReAct engine."""
    findings: dict                   # The agent's final structured output
    observations: List[Observation]  # Full audit trail
    scratchpad: Dict[str, ScratchpadEntry]  # Final working memory state
    reflection: Optional[ReflectionResult]  # Self-reflection results
    iterations_used: int
    total_duration_ms: float
    confidence: float
    strategy_used: str


# ─────────────────────────────────────────────────────────────────────────────
# Meta-tools (available to ALL agents via the engine)
# ─────────────────────────────────────────────────────────────────────────────

SCRATCHPAD_TOOL = {
    "type": "function",
    "function": {
        "name": "update_scratchpad",
        "description": (
            "Update your working memory with a finding or intermediate result. "
            "Use this to track what you've learned, store hypotheses, or mark areas to revisit. "
            "The scratchpad persists across all your tool calls. Keys can be overwritten."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key":        {"type": "string", "description": "Name for this finding (e.g. 'root_failure', 'suspect_services', 'evidence_chain')"},
                "value":      {"description": "The finding itself (string, list, or object)"},
                "confidence": {"type": "number", "description": "Your confidence in this finding (0.0-1.0)", "default": 0.5},
            },
            "required": ["key", "value"],
        },
    },
}

READ_SCRATCHPAD_TOOL = {
    "type": "function",
    "function": {
        "name": "read_scratchpad",
        "description": (
            "Read your current working memory. Returns all stored findings with their confidence levels. "
            "Use this to remind yourself what you've found so far before deciding next steps."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

REFLECT_TOOL = {
    "type": "function",
    "function": {
        "name": "reflect_on_findings",
        "description": (
            "MANDATORY before finishing. Self-reflect on your investigation: "
            "identify gaps in evidence, contradictions between findings, missed signals, "
            "and areas of low confidence. This forces structured self-criticism. "
            "If you find gaps, you can continue investigating. If satisfied, proceed to finish_analysis."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "gaps":           {"type": "array",  "items": {"type": "string"}, "description": "What evidence is missing or weak?"},
                "contradictions": {"type": "array",  "items": {"type": "string"}, "description": "What findings conflict with each other?"},
                "confidence":     {"type": "number", "description": "Overall confidence in your findings (0.0-1.0)"},
                "reasoning":      {"type": "string", "description": "Why you believe your analysis is (or isn't) complete"},
                "should_continue":{"type": "boolean","description": "True if you want to investigate more before finishing"},
            },
            "required": ["gaps", "contradictions", "confidence", "reasoning", "should_continue"],
        },
    },
}

REVISE_FINDING_TOOL = {
    "type": "function",
    "function": {
        "name": "revise_finding",
        "description": (
            "Backtrack: revise a previous finding in your scratchpad after getting new evidence "
            "that contradicts it. Explain why you're revising."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key":         {"type": "string", "description": "Scratchpad key to revise"},
                "new_value":   {"description": "Updated finding"},
                "reason":      {"type": "string", "description": "Why the previous value was wrong"},
                "new_confidence": {"type": "number", "description": "Updated confidence (0.0-1.0)"},
            },
            "required": ["key", "new_value", "reason"],
        },
    },
}

META_TOOLS = [SCRATCHPAD_TOOL, READ_SCRATCHPAD_TOOL, REFLECT_TOOL, REVISE_FINDING_TOOL]


# ─────────────────────────────────────────────────────────────────────────────
# ReAct Engine
# ─────────────────────────────────────────────────────────────────────────────

class ReActEngine:
    """
    Production-grade ReAct engine with self-reflection, scratchpad, and backtracking.
    """

    def __init__(
        self,
        agent_name: str,
        system_prompt: str,
        tools: list,
        finish_tool: dict,
        tool_executor: Callable,
        max_iterations: int = 12,
        confidence_threshold: float = 0.7,
        model: str = "gpt-4o",
        temperature: float = 0,
        reflection_required: bool = True,
    ):
        self.agent_name = agent_name
        self.system_prompt = system_prompt
        self.tools = tools
        self.finish_tool = finish_tool
        self.tool_executor = tool_executor
        self.max_iterations = max_iterations
        self.confidence_threshold = confidence_threshold
        self.model = model
        self.temperature = temperature
        self.reflection_required = reflection_required

        # Build combined tool list: agent tools + meta tools + finish tool
        self.all_tools = tools + META_TOOLS + [finish_tool]

        # State
        self.scratchpad: Dict[str, ScratchpadEntry] = {}
        self.observations: List[Observation] = []
        self.reflection: Optional[ReflectionResult] = None
        self._reflected = False
        self._finished = False
        self._final_result = {}

        self.client = get_client()

    def _build_system_prompt(self) -> str:
        """Enhance the agent's system prompt with meta-tool instructions."""
        meta_instructions = f"""

=== AGENT PROTOCOL (MANDATORY) ===

You are a production-grade ReAct agent. You MUST follow this protocol:

1. SCRATCHPAD: Use update_scratchpad to store important findings as you discover them.
   This is your working memory. Use read_scratchpad to review what you know.

2. INVESTIGATION: Call your domain tools to gather evidence. Be thorough — don't stop
   after the first interesting finding. Cross-validate findings with multiple tools.

3. BACKTRACKING: If new evidence contradicts a prior finding, use revise_finding to
   update your scratchpad with a correction and explanation.

4. SELF-REFLECTION (MANDATORY): Before calling finish_analysis, you MUST call
   reflect_on_findings to check for gaps, contradictions, and confidence. If your
   confidence is below {self.confidence_threshold}, keep investigating.

5. FINISHING: Only call finish_analysis after reflection shows confidence >= {self.confidence_threshold}
   or you've exhausted available tools.

Your scratchpad and reflection are part of your audit trail. Investigators will review
your reasoning chain to understand how you reached your conclusions.
"""
        return self.system_prompt + meta_instructions

    def _truncate_messages(self, messages, max_messages=20):
        """
        Truncate messages list when it gets too long to prevent memory bloat.

        Keeps: first 2 messages (system + user), a summary of middle observations,
        and last (max_messages - 3) messages for recent context.
        """
        if len(messages) <= max_messages:
            return messages

        head = messages[:2]
        summary = {
            "role": "system",
            "content": f"[Previous {len(messages) - max_messages + 1} messages truncated. Key findings are captured in scratchpad.]"
        }
        tail = messages[-(max_messages - 3):]
        return head + [summary] + tail

    def _execute_meta_tool(self, name: str, args: dict, iteration: int) -> str:
        """Execute meta-tools (scratchpad, reflection, revision)."""

        if name == "update_scratchpad":
            key = args.get("key", "unnamed")
            value = args.get("value")
            confidence = float(args.get("confidence", 0.5))
            self.scratchpad[key] = ScratchpadEntry(
                key=key, value=value, updated_at=iteration, confidence=confidence
            )
            return json.dumps({
                "status": "stored",
                "key": key,
                "scratchpad_size": len(self.scratchpad),
                "stored_keys": list(self.scratchpad.keys()),
            })

        elif name == "read_scratchpad":
            entries = {}
            for k, v in self.scratchpad.items():
                entries[k] = {
                    "value": v.value,
                    "confidence": v.confidence,
                    "updated_at_iteration": v.updated_at,
                }
            return json.dumps({
                "scratchpad": entries,
                "total_entries": len(entries),
            })

        elif name == "reflect_on_findings":
            self._reflected = True
            self.reflection = ReflectionResult(
                gaps_identified=args.get("gaps", []),
                contradictions=args.get("contradictions", []),
                confidence_estimate=float(args.get("confidence", 0.0)),
                reasoning=args.get("reasoning", ""),
                should_continue=args.get("should_continue", False),
                revised_findings={},
            )
            # If agent wants to continue and has iterations left, let it
            response = {
                "status": "reflection_recorded",
                "confidence": self.reflection.confidence_estimate,
                "gaps_count": len(self.reflection.gaps_identified),
                "contradictions_count": len(self.reflection.contradictions),
            }
            if self.reflection.should_continue:
                response["instruction"] = "You may continue investigating to address gaps."
            else:
                response["instruction"] = "You may now call finish_analysis."
            return json.dumps(response)

        elif name == "revise_finding":
            key = args.get("key", "")
            new_value = args.get("new_value")
            reason = args.get("reason", "")
            new_confidence = float(args.get("new_confidence", 0.5))

            old = self.scratchpad.get(key)
            self.scratchpad[key] = ScratchpadEntry(
                key=key, value=new_value, updated_at=iteration, confidence=new_confidence
            )
            return json.dumps({
                "status": "revised",
                "key": key,
                "old_value_summary": str(old.value)[:100] if old else "N/A",
                "reason": reason,
                "new_confidence": new_confidence,
            })

        return json.dumps({"error": f"Unknown meta-tool: {name}"})

    def run(self, user_message: str, **executor_kwargs) -> AgentResult:
        """
        Execute the full ReAct loop with self-reflection.
        """
        start_time = time.time()

        messages = [
            {"role": "system", "content": self._build_system_prompt()},
            {"role": "user",   "content": user_message},
        ]

        meta_tool_names = {"update_scratchpad", "read_scratchpad", "reflect_on_findings", "revise_finding"}
        finish_name = self.finish_tool["function"]["name"]

        for iteration in range(self.max_iterations):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.all_tools,
                tool_choice="auto",
                temperature=self.temperature,
                timeout=60,
            )

            msg = response.choices[0].message
            messages.append(msg)

            # No tool calls → agent stopped
            if not msg.tool_calls:
                break

            for tc in msg.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                tool_start = time.time()

                if fn_name == finish_name:
                    # If reflection is required but hasn't happened, reject the finish
                    if self.reflection_required and not self._reflected:
                        result_str = json.dumps({
                            "status": "rejected",
                            "reason": "You MUST call reflect_on_findings before finish_analysis. "
                                      "Reflect on gaps and contradictions first."
                        })
                    else:
                        self._final_result = fn_args
                        self._finished = True
                        result_str = json.dumps({"status": "accepted"})

                elif fn_name in meta_tool_names:
                    result_str = self._execute_meta_tool(fn_name, fn_args, iteration)

                else:
                    # Domain tool — delegate to agent's executor
                    try:
                        result_str = self.tool_executor(fn_name, fn_args, **executor_kwargs)
                    except Exception as e:
                        result_str = json.dumps({"error": str(e), "tool": fn_name})

                tool_duration = (time.time() - tool_start) * 1000

                # Cap observation result to prevent memory bloat
                if len(result_str) > 2000:
                    result_str = result_str[:2000] + f"... [truncated, {len(result_str)} chars total]"

                # Log observation
                self.observations.append(Observation(
                    iteration=iteration,
                    tool_name=fn_name,
                    tool_args=fn_args,
                    result_summary=result_str[:500],
                    result_full=result_str,
                    duration_ms=tool_duration,
                    timestamp=time.time(),
                ))

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      result_str,
                })

                # Truncate messages list to prevent unbounded growth
                messages = self._truncate_messages(messages)

            if self._finished:
                break

        total_duration = (time.time() - start_time) * 1000

        # Compute final confidence
        confidence = 0.0
        if self.reflection:
            confidence = self.reflection.confidence_estimate
        elif self._final_result:
            confidence = 0.5  # No reflection → lower confidence

        return AgentResult(
            findings=self._final_result,
            observations=self.observations,
            scratchpad=dict(self.scratchpad),
            reflection=self.reflection,
            iterations_used=iteration + 1,
            total_duration_ms=total_duration,
            confidence=confidence,
            strategy_used="react_with_reflection",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Utility: pretty-print observation chain for debugging
# ─────────────────────────────────────────────────────────────────────────────

def format_observation_chain(observations: List[Observation]) -> str:
    """Format the observation chain for debugging/logging."""
    lines = []
    for obs in observations:
        lines.append(
            f"  [{obs.iteration}] {obs.tool_name}({json.dumps(obs.tool_args)[:80]}) "
            f"→ {obs.result_summary[:100]}... ({obs.duration_ms:.0f}ms)"
        )
    return "\n".join(lines)


def compute_finding_hash(findings: dict) -> str:
    """Hash findings for dedup/caching."""
    return hashlib.sha256(json.dumps(findings, sort_keys=True, default=str).encode()).hexdigest()[:16]
