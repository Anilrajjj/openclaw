"""
Shared execution engine for Lyzr-style agents.

Every agent is a DB record; this module turns one into a live chat turn:
  1. Compose the system prompt from role / goal / instructions / examples.
  2. Pull session history when the MEMORY feature is enabled.
  3. Expose each agent in `managed_agents` as a tool the LLM can call
     (Manager Agent pattern — delegation is decided dynamically by the model).
  4. Loop on tool calls until the model produces a final answer.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import models
from services import encryption
from services.llm_client import LLMError, chat_completion

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5       # tool-call loops per turn
MAX_DELEGATION_DEPTH = 2  # manager -> sub-agent -> sub-sub-agent, then stop
HISTORY_LIMIT = 30        # messages of session memory per turn


class AgentRuntimeError(Exception):
    """User-facing runtime failure (bad config, provider error, etc.)."""


def run_chat_turn(
    db: Session,
    agent: models.Agent,
    user_message: str,
    history: Optional[List[Dict[str, Any]]] = None,
    _depth: int = 0,
) -> Dict[str, Any]:
    """Execute one chat turn. Returns {"response": str, "delegations": [...]}."""
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(agent)}
    ]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})

    sub_agents = _load_sub_agents(db, agent) if _depth < MAX_DELEGATION_DEPTH else {}
    tools = [_delegation_tool(name, sub) for name, sub in sub_agents.items()] or None

    api_key = _resolve_api_key(agent)
    delegations: List[Dict[str, str]] = []

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            result = chat_completion(
                agent.provider,
                agent.model,
                api_key,
                messages,
                tools=tools,
                temperature=agent.temperature,
                top_p=agent.top_p,
            )
        except LLMError as exc:
            raise AgentRuntimeError(str(exc))

        if not result["tool_calls"]:
            return {"response": result["content"] or "", "delegations": delegations}

        # Echo the assistant tool-call message back in OpenAI wire format.
        messages.append({
            "role": "assistant",
            "content": result["content"],
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": _json_dumps(tc["arguments"])},
                }
                for tc in result["tool_calls"]
            ],
        })

        for tc in result["tool_calls"]:
            sub = sub_agents.get(tc["name"])
            if sub is None:
                tool_output = f"Unknown tool '{tc['name']}'."
            else:
                task = str(tc["arguments"].get("message", "")).strip() or user_message
                logger.info(f"[runtime] {agent.name} -> delegating to {sub.name}: {task[:120]}")
                try:
                    sub_result = run_chat_turn(db, sub, task, _depth=_depth + 1)
                    tool_output = sub_result["response"]
                    delegations.append({
                        "agent_id": sub.id,
                        "agent_name": sub.name,
                        "request": task,
                        "response": tool_output,
                    })
                    delegations.extend(sub_result["delegations"])
                except AgentRuntimeError as exc:
                    tool_output = f"Sub-agent '{sub.name}' failed: {exc}"

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": tool_output or "(no response)",
            })

    raise AgentRuntimeError(
        f"Agent '{agent.name}' exceeded {MAX_TOOL_ROUNDS} tool rounds without a final answer."
    )


# ─── Prompt & config helpers ──────────────────────────────────────────────────

def build_system_prompt(agent: models.Agent) -> str:
    parts: List[str] = []
    if agent.agent_role:
        parts.append(f"# Role\n{agent.agent_role.strip()}")
    if agent.agent_goal:
        parts.append(f"# Goal\n{agent.agent_goal.strip()}")
    if agent.agent_instructions:
        parts.append(f"# Instructions\n{agent.agent_instructions.strip()}")
    if agent.examples:
        parts.append(f"# Examples\n{agent.examples.strip()}")
    if agent.managed_agents:
        parts.append(
            "# Delegation\nYou manage a team of specialist agents exposed as tools. "
            "Delegate any sub-task that matches a specialist's expertise by calling its tool "
            "with a clear, self-contained request, then synthesize the results into your answer."
        )
    return "\n\n".join(parts) or f"You are {agent.name}, a helpful AI assistant."


def has_feature(agent: models.Agent, feature_type: str) -> bool:
    return any(
        isinstance(f, dict) and str(f.get("type", "")).upper() == feature_type.upper()
        for f in (agent.features or [])
    )


def load_session_history(session: models.ChatSession) -> List[Dict[str, Any]]:
    history = []
    for msg in session.messages[-HISTORY_LIMIT:]:
        if msg.role in ("user", "assistant") and msg.content:
            history.append({"role": msg.role, "content": msg.content})
    return history


def _resolve_api_key(agent: models.Agent) -> Optional[str]:
    if not agent.api_key_encrypted:
        if agent.provider == "ollama":
            return None  # local, no key needed
        raise AgentRuntimeError(
            f"Agent '{agent.name}' has no API key configured for provider '{agent.provider}'."
        )
    try:
        return encryption.decrypt(agent.api_key_encrypted)
    except Exception:
        raise AgentRuntimeError(f"Failed to decrypt the API key for agent '{agent.name}'.")


def _load_sub_agents(db: Session, agent: models.Agent) -> Dict[str, models.Agent]:
    """Map tool-name -> sub-agent for every valid managed agent."""
    sub_agents: Dict[str, models.Agent] = {}
    for sub_id in agent.managed_agents or []:
        if sub_id == agent.id:
            continue  # no self-delegation
        sub = (
            db.query(models.Agent)
            .filter(models.Agent.id == sub_id, models.Agent.tenant_id == agent.tenant_id)
            .first()
        )
        if sub:
            sub_agents[_tool_name(sub)] = sub
    return sub_agents


def _tool_name(sub: models.Agent) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", sub.name.strip()).strip("_").lower() or "agent"
    return f"delegate_to_{slug}_{sub.id[:8]}"


def _delegation_tool(tool_name: str, sub: models.Agent) -> Dict[str, Any]:
    description = sub.description or sub.agent_role or "A specialist agent."
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": f"Delegate a task to the '{sub.name}' agent. {description}"[:1024],
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "A clear, self-contained task or question for this agent.",
                    }
                },
                "required": ["message"],
            },
        },
    }


def _json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
