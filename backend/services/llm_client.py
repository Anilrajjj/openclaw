"""
Minimal multi-provider chat-completion client used by the shared agent runtime.

Speaks the OpenAI chat-completions dialect for every OpenAI-compatible provider,
plus the native Anthropic Messages API. Tool calls are normalized to the OpenAI
shape so the runtime only handles one format:

    {"content": str | None, "tool_calls": [{"id", "name", "arguments": dict}]}
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(120.0, connect=15.0)

# OpenAI-compatible providers → base URL. Anthropic is handled natively below.
OPENAI_COMPATIBLE: Dict[str, str] = {
    "openai":     "https://api.openai.com/v1",
    "groq":       "https://api.groq.com/openai/v1",
    "mistral":    "https://api.mistral.ai/v1",
    "deepseek":   "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "xai":        "https://api.x.ai/v1",
    "google":     "https://generativelanguage.googleapis.com/v1beta/openai",
    "together":   "https://api.together.xyz/v1",
    "fireworks":  "https://api.fireworks.ai/inference/v1",
    "cerebras":   "https://api.cerebras.ai/v1",
    "ollama":     "http://localhost:11434/v1",
}

SUPPORTED_PROVIDERS = sorted([*OPENAI_COMPATIBLE.keys(), "anthropic"])


class LLMError(Exception):
    """Raised when the upstream provider returns an error."""


def chat_completion(
    provider: str,
    model: str,
    api_key: Optional[str],
    messages: List[Dict[str, Any]],
    *,
    tools: Optional[List[Dict[str, Any]]] = None,
    temperature: float = 0.7,
    top_p: float = 1.0,
) -> Dict[str, Any]:
    """Run one chat completion. `messages` and `tools` use the OpenAI format."""
    provider = (provider or "").lower()
    if provider == "anthropic":
        return _anthropic_chat(model, api_key, messages, tools, temperature, top_p)
    if provider in OPENAI_COMPATIBLE:
        return _openai_chat(provider, model, api_key, messages, tools, temperature, top_p)
    raise LLMError(
        f"Unsupported provider '{provider}'. Supported: {', '.join(SUPPORTED_PROVIDERS)}"
    )


# ─── OpenAI-compatible ────────────────────────────────────────────────────────

def _openai_chat(provider, model, api_key, messages, tools, temperature, top_p):
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
    }
    if tools:
        body["tools"] = tools

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = f"{OPENAI_COMPATIBLE[provider]}/chat/completions"
    data = _post(url, headers, body, provider)

    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"{provider} returned an unexpected response shape: {exc}")

    tool_calls = []
    for tc in message.get("tool_calls") or []:
        try:
            arguments = json.loads(tc["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        tool_calls.append(
            {"id": tc.get("id", ""), "name": tc["function"]["name"], "arguments": arguments}
        )

    return {"content": message.get("content"), "tool_calls": tool_calls}


# ─── Anthropic native ─────────────────────────────────────────────────────────

def _anthropic_chat(model, api_key, messages, tools, temperature, top_p):
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    converted: List[Dict[str, Any]] = []

    for m in messages:
        role = m["role"]
        if role == "system":
            continue
        if role == "tool":
            converted.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": m.get("content") or "",
                }],
            })
        elif role == "assistant" and m.get("tool_calls"):
            blocks: List[Dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": tc["function"]["name"],
                    "input": args,
                })
            converted.append({"role": "assistant", "content": blocks})
        else:
            converted.append({"role": role, "content": m.get("content") or ""})

    body: Dict[str, Any] = {
        "model": model,
        "max_tokens": 4096,
        "messages": converted,
        "temperature": min(temperature, 1.0),
        "top_p": top_p,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    if tools:
        body["tools"] = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}}),
            }
            for t in tools
        ]

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key or "",
        "anthropic-version": "2023-06-01",
    }
    data = _post("https://api.anthropic.com/v1/messages", headers, body, "anthropic")

    content_text = None
    tool_calls = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            content_text = (content_text or "") + block.get("text", "")
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "arguments": block.get("input") or {},
            })

    return {"content": content_text, "tool_calls": tool_calls}


# ─── HTTP helper ──────────────────────────────────────────────────────────────

def _post(url: str, headers: Dict[str, str], body: Dict[str, Any], provider: str) -> Dict[str, Any]:
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach {provider}: {exc}")

    if resp.status_code >= 400:
        detail = resp.text[:500]
        try:
            err = resp.json().get("error")
            if isinstance(err, dict) and err.get("message"):
                detail = err["message"]
            elif isinstance(err, str):
                detail = err
        except Exception:
            pass
        raise LLMError(f"{provider} error ({resp.status_code}): {detail}")

    return resp.json()
