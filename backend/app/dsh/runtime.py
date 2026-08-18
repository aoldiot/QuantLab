from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.llm_config import decrypt_api_key
from app.models import LlmConfiguration

logger = logging.getLogger(__name__)


@dataclass
class AgentEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    agent_role: str = "lead"  # lead, researcher, developer, reviewer, system, tool
    event_type: str = "message"  # message, thought, tool_call, tool_result, status
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.event_id,
            "session_id": self.session_id,
            "role": self.agent_role,
            "type": self.event_type,
            "content": self.content,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


def normalize_llm_endpoint(base_url: str) -> str:
    """Normalize base URL to standard OpenAI/DeepSeek /v1/chat/completions endpoint."""
    url = (base_url or "https://api.deepseek.com/v1").strip().rstrip("/")
    if url.endswith("/chat/completions") or url.endswith("/responses") or url.endswith("/messages"):
        return url
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


class DSHRuntimeManager:
    """Manages DeepSeek Harness runtime, session state, and LLM tool-calling turns."""

    def __init__(self):
        self._sessions: dict[str, list[AgentEvent]] = {}
        self._active_status: dict[str, dict[str, Any]] = {}

    def get_session_events(self, session_id: str) -> list[AgentEvent]:
        return self._sessions.get(session_id, [])

    def record_event(self, session_id: str, event: AgentEvent) -> None:
        if session_id not in self._sessions:
            self._sessions[session_id] = []
        self._sessions[session_id].append(event)

    def set_status(
        self,
        session_id: str,
        stage: str,
        progress: int = 0,
        status: str = "RUNNING",
        thought: str = "",
        agent_role: str = "lead",
    ) -> None:
        self._active_status[session_id] = {
            "session_id": session_id,
            "stage": stage,
            "progress": progress,
            "status": status,
            "thought": thought,
            "agent_role": agent_role,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    def get_status(self, session_id: str) -> dict[str, Any]:
        return self._active_status.get(
            session_id,
            {
                "session_id": session_id,
                "stage": "就绪",
                "progress": 0,
                "status": "IDLE",
                "thought": "",
                "agent_role": "lead",
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    async def call_llm(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
        db_config: LlmConfiguration | None = None,
        temperature: float = 0.2,
    ) -> tuple[str, list[dict[str, Any]], str]:
        """Execute a completion with function tool-calling against configured LLM backend."""
        # 1. Resolve LLM configuration
        base_url = "https://api.deepseek.com/v1"
        api_key = ""
        model = "deepseek-chat"
        timeout_seconds = 120

        if db_config is not None:
            if db_config.base_url and db_config.model:
                base_url = db_config.base_url.rstrip("/")
                api_key = decrypt_api_key(db_config.api_key_encrypted) if db_config.api_key_encrypted else ""
                model = db_config.model
                timeout_seconds = db_config.timeout_seconds or 120

        # Build standard messages payload
        payload_messages = []
        if system_prompt:
            payload_messages.append({"role": "system", "content": system_prompt})
        payload_messages.extend(messages)

        request_body: dict[str, Any] = {
            "model": model,
            "messages": payload_messages,
            "temperature": temperature,
        }

        if tools:
            openai_tools = []
            for t in tools:
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name"),
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                    },
                })
            request_body["tools"] = openai_tools
            request_body["tool_choice"] = "auto"

        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"

        candidate_urls: list[str] = [normalize_llm_endpoint(base_url)]
        clean_url = base_url.rstrip("/")
        if f"{clean_url}/v1/chat/completions" not in candidate_urls:
            candidate_urls.append(f"{clean_url}/v1/chat/completions")
        if f"{clean_url}/chat/completions" not in candidate_urls:
            candidate_urls.append(f"{clean_url}/chat/completions")
        if f"{clean_url}/v1/messages" not in candidate_urls:
            candidate_urls.append(f"{clean_url}/v1/messages")
        if f"{clean_url}/responses" not in candidate_urls:
            candidate_urls.append(f"{clean_url}/responses")

        last_error = ""
        for target_url in candidate_urls:
            try:
                # Prepare payload if Anthropic /messages endpoint
                body = request_body
                if target_url.endswith("/messages"):
                    body = {
                        "model": model,
                        "max_tokens": 4096,
                        "messages": messages,
                        "system": system_prompt,
                        "temperature": temperature,
                    }

                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    response = await client.post(target_url, json=body, headers=headers)
                    if response.status_code == 404 and len(candidate_urls) > 1:
                        last_error = f"[API Error 404]: {response.text}"
                        continue
                    if response.status_code != 200:
                        logger.warning("LLM API (%s) 返回非200状态码: %s %s", target_url, response.status_code, response.text)
                        return f"[API Error {response.status_code}]: {response.text}", [], ""

                    data = response.json()
                    choice = data.get("choices", [{}])[0]
                    msg = choice.get("message", {})
                    content = msg.get("content") or ""
                    reasoning = msg.get("reasoning_content") or ""

                    # Check Anthropic message format if present
                    if not content and "content" in data and isinstance(data["content"], list):
                        texts = []
                        for block in data["content"]:
                            if isinstance(block, dict) and block.get("type") == "text":
                                texts.append(block.get("text", ""))
                        if texts:
                            content = "\n".join(texts)

                    # Extract <think> if embedded
                    if "<think>" in content and "</think>" in content:
                        m = re.search(r"<think>(.*?)</think>", content, flags=re.DOTALL)
                        if m:
                            reasoning = m.group(1).strip()
                            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

                    tool_calls = []
                    for tc in msg.get("tool_calls", []):
                        fn = tc.get("function", {})
                        fn_name = fn.get("name")
                        fn_args_str = fn.get("arguments", "{}")
                        try:
                            parsed_args = json.loads(fn_args_str) if isinstance(fn_args_str, str) else fn_args_str
                        except Exception:
                            parsed_args = {}
                        tool_calls.append({
                            "id": tc.get("id", str(uuid.uuid4())),
                            "name": fn_name,
                            "arguments": parsed_args,
                        })

                    # Handle Anthropic tool_use if present
                    if "content" in data and isinstance(data["content"], list):
                        for block in data["content"]:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tool_calls.append({
                                    "id": block.get("id", str(uuid.uuid4())),
                                    "name": block.get("name"),
                                    "arguments": block.get("input", {}),
                                })

                    return content, tool_calls, reasoning

            except Exception as exc:
                last_error = f"[LLM Exception]: {exc}"
                logger.warning("调用 LLM 服务 (%s) 遇到异常: %s", target_url, exc)
                continue

        return last_error or "[LLM Exception]: 连接失败", [], ""


dsh_runtime = DSHRuntimeManager()
