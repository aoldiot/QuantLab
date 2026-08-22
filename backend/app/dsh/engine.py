"""DSH SDK engine for research projects.

Wraps the official DeepSeek Harness Python SDK per research project. One
project == one DSH session: the engine spawns the bundled JS runtime as a
child process (injected env: DSH_CWD workspace, DSH_SESSION_ROOT, DSH_CORDIS_CONFIG
pointing at backend/dsh_runtime/cordis.yml, and DSH_BRIDGE_* / DSH_TOOLS_PLUGIN_PATH
for the HTTP bridge plugin), runs agent turns with real-time notifications, maps the
flat SDK events onto the platform event vocabulary, and persists them into research_messages.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import settings
from ..models import LlmConfiguration, ResearchMessage, ResearchProject

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 32768

TASK_MAX_TOKENS = {
    "WRITE_STRATEGY": 24576,
    "FIX_ERROR": 12000,
    "RUN_BACKTEST": 4096,
    "ANALYZE_BACKTEST": 6000,
}

TASK_TIMEOUT_SECONDS = {
    "WRITE_STRATEGY": 960,
    "FIX_ERROR": 360,
    "RUN_BACKTEST": 120,
    "ANALYZE_BACKTEST": 180,
}

INTENT_NAMES = frozenset(
    {
        "DISCUSS_STRATEGY",
        "MODIFY_STRATEGY_PLAN",
        "START_IMPLEMENTATION",
        "MODIFY_STRATEGY_CODE",
        "REQUEST_BACKTEST",
        "MODIFY_BACKTEST_PARAMS",
        "APPROVE_PENDING_ACTION",
        "REJECT_PENDING_ACTION",
        "ANALYZE_BACKTEST",
        "VIEW_STRATEGY_CODE",
        "UNKNOWN",
    }
)

_MAX_SESSION_EVENTS = 2000
_MAX_LIVE_SESSION_EVENTS = 600


def _dsh_runtime_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "dsh_runtime"


def _cordis_path(phase: str = "IMPLEMENTATION") -> Path:
    phase_normalized = (phase or "").upper()
    if phase_normalized == "INTENT":
        return _dsh_runtime_dir() / "cordis-intent.yml"
    if phase_normalized == "RESEARCH":
        return _dsh_runtime_dir() / "cordis-research.yml"
    if phase_normalized in {"REPAIR", "FIX_ERROR", "IMPLEMENTATION", "IMPLEMENTED"}:
        return _dsh_runtime_dir() / "cordis-coding.yml"
    if phase_normalized in {"BACKTEST", "BACKTEST_RETRY"}:
        return _dsh_runtime_dir() / "cordis-backtest.yml"
    if phase_normalized == "RESULT_REVIEW":
        return _dsh_runtime_dir() / "cordis-analysis.yml"
    return _dsh_runtime_dir() / "cordis.yml"


def _plugin_path() -> Path:
    return _dsh_runtime_dir() / "src" / "quantlab-tools.mjs"


async def _runtime_llm_config() -> dict[str, str]:
    """Load the model credentials configured from the Settings page."""
    from ..db import SessionLocal
    from ..llm_config import decrypt_api_key

    async with SessionLocal() as db:
        saved = await db.get(LlmConfiguration, 1)

    base_url = (saved.base_url or "").strip() if saved else ""
    model = (saved.model or "").strip() if saved else ""
    api_key = decrypt_api_key(saved.api_key_encrypted) if saved else ""
    if not (base_url and model and api_key):
        raise RuntimeError("尚未配置 DSH 模型：请前往「系统设置 - LLM & DSH 配置」保存 Base URL、模型和 API Key")
    return {"base_url": base_url, "api_key": api_key, "model": model}


def _load_dsh_env() -> None:
    """Load DSH overrides from the unified repository-root .env file."""
    env_file = Path(__file__).resolve().parents[3] / ".env"
    # Keep legacy installations functional during the .env.dsh -> .env migration.
    if not env_file.exists():
        env_file = Path(__file__).resolve().parents[2] / ".env.dsh"
    if not env_file.exists():
        return
    raw: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        raw.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    for key, value in raw.items():
        # DSH_PROBE_* is the historical probe naming; engine uses DSH_*.
        engine_key = key.replace("DSH_PROBE_", "DSH_") if key.startswith("DSH_PROBE_") else key
        os.environ.setdefault(engine_key, value)


_load_dsh_env()

_harnesses: dict[str, Any] = {}
_active_turns: dict[str, asyncio.Task] = {}
_status: dict[str, dict[str, Any]] = {}
_session_events: dict[str, list[dict[str, Any]]] = {}
_live_session_events: dict[str, list[dict[str, Any]]] = {}
_session_event_seq: dict[str, int] = {}
_state_lock = threading.RLock()


def _sdk_session_id(
    project: ResearchProject,
    phase: str = "RESEARCH",
    turn_id: str | None = None,
) -> str:
    """Keep one durable session per specialist worker, not per repair turn."""
    del turn_id
    from .profiles import worker_for_phase

    worker = worker_for_phase(phase).value.lower()
    return f"dsh_project_{project.id}_{worker}"


def _build_harness(
    project: ResearchProject,
    runtime_config: dict[str, str],
    system_instructions: str = "",
    phase: str = "RESEARCH",
    task_profile: str = "",
) -> Any:
    from deepseek_harness import DeepSeekHarness  # imported lazily (heavy import)

    base_url = runtime_config["base_url"]
    api_key = runtime_config["api_key"]
    model = runtime_config["model"]
    default_max_tokens = TASK_MAX_TOKENS.get(task_profile, DEFAULT_MAX_TOKENS)
    max_tokens = int(os.environ.get("DSH_MAX_TOKENS", "") or default_max_tokens)

    core_path = _cordis_path(phase)
    if not core_path.exists():
        raise RuntimeError(f"DSH cordis 配置缺失: {core_path}")
    from .profiles import worker_for_phase

    worker = worker_for_phase(phase)
    # Cordis path grants are repository-relative for every worker.  Keep a
    # single, explicit resolution root and let each phase's Cordis profile
    # provide the least-privilege view.  Using an empty per-project cwd made
    # paths such as app/quant and data/backtests point at nonexistent files.
    workspace = settings.strategy_repo_path.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    sessions = (settings.data_root / "dsh" / "sessions").resolve()
    sessions.mkdir(parents=True, exist_ok=True)

    return DeepSeekHarness(
        provider="deepseek-official",
        model=model,
        max_tokens=max_tokens,
        cwd=str(workspace),
        session_root=str(sessions),
        base_url=base_url,
        api_key=api_key,
        cordis=str(core_path),
        env={
            "DSH_PROJECT_ID": project.id,
            "DSH_BRIDGE_URL": settings.dsh_bridge_url,
            "DSH_BRIDGE_TOKEN": settings.dsh_bridge_token,
            "DSH_TOOLS_PLUGIN_PATH": str(_plugin_path()),
            "DSH_RESEARCH_INSTRUCTIONS": system_instructions,
            "DSH_RESEARCH_PHASE": phase,
            "DSH_TASK_PROFILE": task_profile,
            "DSH_WORKER_TYPE": worker.value,
        },
    )


def _parse_intent_response(text: str) -> dict[str, Any]:
    """Parse and validate the JSON decision produced by the DSH intent turn."""
    decoder = json.JSONDecoder()
    payload: dict[str, Any] | None = None

    # 1. Standard raw_decode scan
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[index:])
            if isinstance(candidate, dict):
                payload = candidate
                break
        except json.JSONDecodeError:
            continue

    # 2. Strip markdown fences and try direct json.loads
    if payload is None:
        cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
        cleaned = re.sub(r"```$", "", cleaned.strip(), flags=re.MULTILINE).strip()
        try:
            candidate = json.loads(cleaned)
            if isinstance(candidate, dict):
                payload = candidate
        except Exception:
            pass

    # 3. Try repairing unclosed strings and braces (common when hitting token limits)
    if payload is None:
        trimmed = text.strip()
        for fix_suffix in ['"}', '}', '\"}}', '}}', '":""}']:
            for index, char in enumerate(trimmed):
                if char != "{":
                    continue
                try:
                    candidate = json.loads(trimmed[index:] + fix_suffix)
                    if isinstance(candidate, dict):
                        payload = candidate
                        break
                except Exception:
                    continue
            if payload is not None:
                break

    # 4. Regex fallback extraction for partial/truncated output
    if payload is None:
        intent_match = re.search(r'["\']intent["\']\s*:\s*["\']([A-Za-z0-9_]+)["\']', text)
        if intent_match:
            intent_val = intent_match.group(1).upper()
            conf_match = re.search(r'["\']confidence["\']\s*:\s*([0-9.]+)', text)
            confidence = float(conf_match.group(1)) if conf_match else 0.8
            req_match = re.search(r'["\']normalized_request["\']\s*:\s*"(.*?)(?:",|"\s*}|\Z)', text, re.DOTALL)
            normalized_request = req_match.group(1) if req_match else ""
            clarif_match = re.search(r'["\']needs_clarification["\']\s*:\s*(true|false)', text, re.IGNORECASE)
            needs_clarification = clarif_match.group(1).lower() == "true" if clarif_match else False
            payload = {
                "intent": intent_val,
                "confidence": confidence,
                "normalized_request": normalized_request,
                "needs_clarification": needs_clarification,
                "clarification_question": "",
                "pending_request_id": "",
                "reason": "从截断或非标准响应中容错解析",
            }

    if payload is None:
        raise ValueError("DSH 意图路由未返回 JSON 对象")

    intent = str(payload.get("intent") or "").strip().upper()
    if intent not in INTENT_NAMES:
        empty_str = "<empty>"
        raise ValueError(f"DSH 返回了未知意图: {intent or empty_str}")
    try:
        confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "intent": intent,
        "confidence": confidence,
        "normalized_request": str(payload.get("normalized_request") or "").strip(),
        "needs_clarification": bool(payload.get("needs_clarification", False)),
        "clarification_question": str(payload.get("clarification_question") or "").strip(),
        "pending_request_id": str(payload.get("pending_request_id") or "").strip(),
        "reason": str(payload.get("reason") or "").strip(),
    }


async def classify_intent(
    project: ResearchProject,
    user_message: str,
    recent_messages: list[dict[str, str]],
    pending: list[dict[str, Any]],
) -> dict[str, Any]:
    """Use a dedicated, tool-free DSH turn to manage the user's intent."""
    from deepseek_harness import DeepSeekHarness  # imported lazily (heavy import)

    runtime_config = await _runtime_llm_config()
    base_url = runtime_config["base_url"]
    api_key = runtime_config["api_key"]
    model = runtime_config["model"]

    core_path = _cordis_path("INTENT")
    if not core_path.exists():
        raise RuntimeError(f"DSH 意图路由配置缺失: {core_path}")
    workspace = (settings.data_root / "dsh" / "intent" / project.id).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    sessions = (settings.data_root / "dsh" / "sessions").resolve()
    sessions.mkdir(parents=True, exist_ok=True)
    timeout_seconds = max(10, int(os.environ.get("DSH_INTENT_TIMEOUT_SECONDS", "60")))

    compact_messages = recent_messages[-12:]
    context = json.dumps(compact_messages, ensure_ascii=False, default=str)[-16_000:]
    pending_context = json.dumps(
        [
            {
                "request_id": item.get("request_id"),
                "tool": item.get("tool"),
                "summary": item.get("summary"),
                "status": item.get("status"),
                "arguments": item.get("arguments"),
            }
            for item in pending
        ],
        ensure_ascii=False,
        default=str,
    )[-8000:]
    prompt = f"""你是 QuantLab 的 DSH 意图管理器。只判断用户这次真正希望平台做什么，不执行任务、不调用工具。
必须结合当前项目状态、最近对话和待审批请求理解省略、代词、口语及上下文确认，不能依赖关键词匹配。

只允许选择一个 intent：
- DISCUSS_STRATEGY：提出想法、询问、继续研究或普通讨论
- MODIFY_STRATEGY_PLAN：修改尚未编码的规则、参数或研究方案
- START_IMPLEMENTATION：确认方案并希望首次生成策略代码
- MODIFY_STRATEGY_CODE：要求修改、修复或重写已有/待审批代码
- REQUEST_BACKTEST：要求准备或执行回测，包括确认已编辑的回测参数
- MODIFY_BACKTEST_PARAMS：只调整回测标的、区间、资金、杠杆或参数
- APPROVE_PENDING_ACTION：明确批准当前待审批工具请求
- REJECT_PENDING_ACTION：明确拒绝当前待审批工具请求
- ANALYZE_BACKTEST：分析已经完成的回测结果，不重新回测
- VIEW_STRATEGY_CODE：只查看或解释当前策略代码
- UNKNOWN：上下文仍不足以判断

判定规则：
1. 存在待审批请求时，只有无歧义的同意/拒绝才选择 APPROVE_PENDING_ACTION 或 REJECT_PENDING_ACTION，并返回匹配的 pending_request_id。
2. 不存在待审批请求时，不得输出 APPROVE_PENDING_ACTION 或 REJECT_PENDING_ACTION；对研究方案的确认应输出 START_IMPLEMENTATION，对回测参数的确认应输出 REQUEST_BACKTEST。
3. 用户同时补充参数并要求继续时，以最终要执行的动作作为 intent，把补充内容写入 normalized_request。
4. 若可能导致写代码、回测或审批，但置信度不足，needs_clarification 必须为 true，并给出一句简体中文确认问题。
5. reason 只写一句简短的判定依据，不输出思维过程。

当前阶段：{project.research_phase or 'RESEARCH'}
是否已有策略：{bool(project.strategy_id)}
是否已有回测结果：{bool(project.latest_backtest_id)}
原始需求：{(project.original_idea or '')[:4000]}
待审批请求：{pending_context}
最近对话（时间正序）：{context}
用户本次输入：{user_message}

只输出一个合法 JSON 对象，不要 Markdown：
{{"intent":"DISCUSS_STRATEGY","confidence":0.0,"normalized_request":"","needs_clarification":false,"clarification_question":"","pending_request_id":"","reason":""}}
"""

    max_intent_tokens = int(os.environ.get("DSH_INTENT_MAX_TOKENS", "8192"))
    harness = DeepSeekHarness(
        provider="deepseek-official",
        model=model,
        max_tokens=max_intent_tokens,
        cwd=str(workspace),
        session_root=str(sessions),
        base_url=base_url,
        api_key=api_key,
        cordis=str(core_path),
        request_timeout_seconds=timeout_seconds,
    )

    def _run() -> Any:
        return harness.run(
            prompt,
            session_id=f"dsh_intent_{project.id}_{uuid.uuid4().hex}",
        )

    try:
        result = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_seconds)
        final = (getattr(result, "final_response", "") or "").strip()
        if not final:
            failure = _connectivity_failure_message(result)
            raise RuntimeError(failure or "DSH 意图路由没有返回结果")
        return _parse_intent_response(final)
    except TimeoutError as exc:
        raise RuntimeError(f"DSH 意图判断超过 {timeout_seconds} 秒") from exc
    finally:
        try:
            await asyncio.to_thread(harness.close)
        except Exception:
            logger.warning("关闭 DSH 意图路由 Harness 失败", exc_info=True)


async def _harness_for(
    project: ResearchProject,
    system_instructions: str = "",
    phase: str = "RESEARCH",
    task_profile: str = "",
) -> Any:
    runtime_config = await _runtime_llm_config()
    config_fingerprint = hashlib.sha256(
        f"{runtime_config['base_url']}\0{runtime_config['model']}\0{runtime_config['api_key']}".encode()
    ).hexdigest()[:16]
    from .profiles import worker_for_phase

    worker = worker_for_phase(phase).value
    key = f"{project.id}:{worker}:{task_profile or 'GENERAL'}:{config_fingerprint}"
    h = _harnesses.get(key)
    if h is None:
        # Different phases use different Cordis configurations but resume the
        # same SDK session. Release the previous runtime before opening it from
        # the next phase, otherwise the new Harness can return zero events.
        prefix = f"{project.id}:"
        stale_keys = [item for item in _harnesses if item.startswith(prefix) and item != key]
        for stale_key in stale_keys:
            stale = _harnesses.pop(stale_key)
            try:
                stale.close()
            except Exception:  # noqa: BLE001
                logger.warning("关闭旧阶段 DSH Harness 失败: %s", stale_key, exc_info=True)
        h = _build_harness(
            project,
            runtime_config,
            system_instructions=system_instructions,
            phase=phase,
            task_profile=task_profile,
        )
        _harnesses[key] = h
    return h


def _discard_harness(project_id: str, harness: Any) -> None:
    """Remove a closed harness without relying on its credential fingerprint."""
    for key, item in list(_harnesses.items()):
        if key.startswith(f"{project_id}:") and item is harness:
            _harnesses.pop(key, None)


def _archive_session_directory(session_id_or_pattern: str) -> list[Path]:
    """Archive corrupted, cancelled, or timed-out session directories on disk.

    Prevents subsequent turns from deadlocking on broken session state
    (e.g., 0-event immediate exit after an abrupt process termination or timeout).
    """
    if not session_id_or_pattern:
        return []
    sessions_root = (settings.data_root / "dsh" / "sessions").resolve()
    if not sessions_root.exists():
        return []
    archived_paths: list[Path] = []
    try:
        # Match exact session_id or wildcard patterns (e.g. "*project_id*")
        search_pattern = f"**/{session_id_or_pattern}" if not session_id_or_pattern.startswith("*") else f"**/{session_id_or_pattern}"
        for session_dir in sessions_root.glob(search_pattern):
            if not session_dir.is_dir() or "_archived_" in session_dir.name:
                continue
            timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            archive_dir = session_dir.parent / f"{session_dir.name}_archived_{timestamp}"
            try:
                shutil.move(str(session_dir), str(archive_dir))
                archived_paths.append(archive_dir)
                logger.warning("已归档超时或异常的 DSH Session 目录: %s -> %s", session_dir, archive_dir)
            except Exception:
                logger.warning("归档 DSH Session 目录失败，尝试直接删除: %s", session_dir, exc_info=True)
                shutil.rmtree(str(session_dir), ignore_errors=True)
    except Exception:
        logger.warning("扫描或归档 DSH Session 失败 (target=%s)", session_id_or_pattern, exc_info=True)
    return archived_paths


def get_status(project_id: str) -> dict[str, Any]:
    with _state_lock:
        return dict(
            _status.get(
                project_id,
                {
                    "project_id": project_id,
                    "status": "IDLE",
                    "stage": "",
                    "progress": 0,
                    "error": "",
                    "updated_at": "",
                },
            )
        )


def set_status(
    project_id: str,
    status: str,
    stage: str = "",
    progress: int | None = None,
    error: str = "",
    metrics: dict[str, Any] | None = None,
) -> None:
    with _state_lock:
        prev = _status.get(project_id, {})
        _status[project_id] = {
            "project_id": project_id,
            "status": status,
            "stage": stage,
            "progress": progress if progress is not None else prev.get("progress", 0),
            "error": error,
            "updated_at": datetime.now(UTC).isoformat(),
            "metrics": metrics if metrics is not None else prev.get("metrics", {}),
        }


def get_session_events(project_id: str) -> list[dict[str, Any]]:
    """Return the mapped SDK events recorded for a research project (used by export)."""
    with _state_lock:
        return [dict(event) for event in _session_events.get(project_id, [])]


def get_live_session_events(project_id: str) -> list[dict[str, Any]]:
    """Return the latest turn's mapped SDK events for the live research UI."""
    with _state_lock:
        return [dict(event) for event in _live_session_events.get(project_id, [])]


def _start_live_turn(project_id: str) -> str:
    turn_id = str(uuid.uuid4())
    with _state_lock:
        _live_session_events[project_id] = []
    return turn_id


def _record_live_event(project_id: str, turn_id: str, event: dict[str, Any]) -> dict[str, Any]:
    """Add ordering metadata and publish an SDK event to live/history buffers."""
    with _state_lock:
        seq = _session_event_seq.get(project_id, 0) + 1
        _session_event_seq[project_id] = seq
        enriched = {
            **event,
            "seq": seq,
            "turn_id": turn_id,
            "received_at": datetime.now(UTC).isoformat(),
        }
        live_events = _live_session_events.setdefault(project_id, [])
        stream_key = event.get("stream_key")
        if stream_key and event.get("kind") in {"chunk", "reasoning_chunk"}:
            existing = next((item for item in reversed(live_events) if item.get("stream_key") == stream_key), None)
            if existing is not None:
                existing["text"] = f"{existing.get('text', '')}{event.get('text', '')}"
                existing["seq"] = seq
                existing["received_at"] = enriched["received_at"]
            else:
                live_events.append(enriched)
        else:
            live_events.append(enriched)
        _live_session_events[project_id] = live_events[-_MAX_LIVE_SESSION_EVENTS:]
        _session_events.setdefault(project_id, []).append(enriched)
        _session_events[project_id] = _session_events[project_id][-_MAX_SESSION_EVENTS:]
        return enriched


def _update_live_status(project_id: str, event: dict[str, Any]) -> None:
    kind = event.get("kind")
    if kind == "tool_call":
        tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
        tool_name = tool.get("name") or "工具"
        set_status(project_id, "TOOL_RUNNING", stage=f"正在调用 {tool_name}", progress=45)
    elif kind == "tool_result":
        set_status(project_id, "THINKING", stage="工具返回结果，正在继续推理", progress=65)
    elif kind == "reasoning_chunk":
        set_status(project_id, "THINKING", stage="模型正在推理", progress=35)
    elif kind in {"chunk", "assistant_message"}:
        set_status(project_id, "GENERATING", stage="正在生成研究结论", progress=85)
    elif kind in {"turn_start", "step_start"}:
        set_status(project_id, "THINKING", stage="正在分析任务并规划执行步骤", progress=25)


def _map_notification(notification) -> dict[str, Any]:
    """Map an SDK notification onto the platform event vocabulary.

    Returns a dict shape the frontend and message tables understand, mirroring
    the flat SDK event types (assistant/chunk, assistant/message, tool/call,
    tool/result, step/turn lifecycle, session/status, agent/inbox/spliced).
    """
    method = getattr(notification, "method", "")
    payload = getattr(notification, "payload", {}) or {}
    event = payload.get("event") if isinstance(payload, dict) else None
    etype = event.get("type", method) if isinstance(event, dict) else method
    data = event.get("data", {}) if isinstance(event, dict) else {}
    session_id = payload.get("sessionId", "") if isinstance(payload, dict) else ""
    event_turn = data.get("turn") if isinstance(data, dict) else None
    event_step = data.get("step") if isinstance(data, dict) else None

    if etype == "assistant/chunk":
        chunk = data.get("chunk", {}) if isinstance(data, dict) else {}
        ctype = chunk.get("type")
        if ctype == "text-delta":
            return {
                "type": "sdk_event",
                "kind": "chunk",
                "chunk_type": ctype,
                "text": chunk.get("text", ""),
                "turn": event_turn,
                "step": event_step,
                "stream_key": f"text:{event_turn}:{event_step}",
                "session_id": session_id,
            }
        if ctype == "reasoning-delta":
            return {
                "type": "sdk_event",
                "kind": "reasoning_chunk",
                "chunk_type": ctype,
                "text": chunk.get("text", ""),
                "turn": event_turn,
                "step": event_step,
                "stream_key": f"reasoning:{event_turn}:{event_step}",
                "session_id": session_id,
            }
        if ctype == "block-end":
            block = chunk.get("block", {})
            if isinstance(block, dict) and block.get("type") == "text":
                return {
                    "type": "sdk_event",
                    "kind": "chunk",
                    "chunk_type": ctype,
                    "text": block.get("text", ""),
                    "session_id": session_id,
                }
        if ctype == "usage":
            return {"type": "sdk_event", "kind": "usage", "usage": chunk.get("usage", {}), "session_id": session_id}
        return {"type": "sdk_event", "kind": "chunk_meta", "chunk": chunk, "session_id": session_id}
    if etype == "assistant/message":
        message = data.get("message", {}) if isinstance(data, dict) else {}
        text = "".join(
            b.get("text", "") for b in message.get("content", []) if isinstance(b, dict) and b.get("type") == "text"
        )
        reasoning = "".join(
            b.get("text", "")
            for b in message.get("content", [])
            if isinstance(b, dict) and b.get("type") == "reasoning"
        )
        return {
            "type": "sdk_event",
            "kind": "assistant_message",
            "text": text,
            "reasoning": reasoning,
            "message": message,
            "session_id": session_id,
        }
    if etype == "tool/call":
        raw_arguments = data.get("arguments", "") if isinstance(data, dict) else ""
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) and raw_arguments.strip() else {}
        except json.JSONDecodeError:
            arguments = {"_raw": raw_arguments}
        return {
            "type": "sdk_event",
            "kind": "tool_call",
            "tool": {
                "name": data.get("name", "") if isinstance(data, dict) else "",
                "arguments": arguments,
                "arguments_raw": raw_arguments,
            },
            "call_id": data.get("callId") or data.get("call"),
            "turn": event_turn,
            "step": event_step,
            "session_id": session_id,
        }
    if etype == "tool/result":
        message = data.get("message", {}) if isinstance(data, dict) else {}
        source = message.get("source", {}) if isinstance(message, dict) else {}
        content = message.get("content", []) if isinstance(message, dict) else []
        tool_result_block = next(
            (block for block in content if isinstance(block, dict) and block.get("type") == "tool-result"),
            {},
        )
        result_content = tool_result_block.get("content", []) if isinstance(tool_result_block, dict) else []
        result_text = "".join(
            str(block.get("text") or "")
            for block in result_content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        try:
            result = json.loads(result_text) if result_text.strip() else {"content": result_content}
        except json.JSONDecodeError:
            result = {"output": result_text, "content": result_content}
        if isinstance(result, dict):
            if tool_result_block.get("isError") is not None:
                result.setdefault("is_error", bool(tool_result_block.get("isError")))
            if data.get("error"):
                result.setdefault("error", data.get("error"))
            if data.get("meta") is not None:
                result.setdefault("meta", data.get("meta"))
        return {
            "type": "sdk_event",
            "kind": "tool_result",
            "tool": {},
            "result": result,
            "call_id": source.get("callId") or tool_result_block.get("toolCallId") or data.get("callId"),
            "turn": event_turn,
            "step": event_step,
            "session_id": session_id,
        }
    if etype == "step/start":
        return {"type": "sdk_event", "kind": "step_start", "step": data, "session_id": session_id}
    if etype == "step/end":
        return {"type": "sdk_event", "kind": "step_end", "step": data, "session_id": session_id}
    if etype == "turn/start":
        return {"type": "sdk_event", "kind": "turn_start", "session_id": session_id}
    if etype == "turn/end":
        return {"type": "sdk_event", "kind": "turn_end", "reason": data.get("reason"), "session_id": session_id}
    if etype == "session/status" or method == "session.status":
        status = payload.get("status", "") if isinstance(payload, dict) else event.get("status", "")
        return {"type": "status", "status": status.upper(), "session_id": session_id}
    return {"type": "raw", "method": method, "payload": payload}


async def persist_mapped(project_id: str, mapped: list[dict[str, Any]]) -> None:
    """Persist the mapped events onto research_messages (assistant text + tool cards)."""
    if not mapped:
        return
    from ..db import SessionLocal

    to_save: list[dict[str, Any]] = []
    reasoning_buffer = ""
    for ev in mapped:
        if ev.get("kind") == "reasoning_chunk":
            reasoning_buffer += ev.get("text", "")
        elif ev.get("kind") == "assistant_message":
            text = ev.get("text", "")
            if text:
                reasoning = ev.get("reasoning") or reasoning_buffer
                metadata = {"event": ev, "dsh_event_key": f"{ev.get('turn_id')}:{ev.get('seq')}"}
                if reasoning:
                    metadata["reasoning_content"] = reasoning
                to_save.append({"role": "assistant", "message_type": "message", "content": text, "metadata": metadata})
                reasoning_buffer = ""
        elif ev.get("kind") == "tool_call":
            to_save.append(
                {"role": "tool", "message_type": "tool_call", "content": "", "metadata": {"event": ev, "dsh_event_key": f"{ev.get('turn_id')}:{ev.get('seq')}"}}
            )
        elif ev.get("kind") == "tool_result":
            to_save.append(
                {"role": "tool", "message_type": "tool_result", "content": "", "metadata": {"event": ev, "dsh_event_key": f"{ev.get('turn_id')}:{ev.get('seq')}"}}
            )
    if not to_save:
        return
    async with SessionLocal() as db:
        for row in to_save:
            db.add(
                ResearchMessage(
                    project_id=project_id,
                    role=row["role"],
                    message_type=row["message_type"],
                    content=row["content"],
                    metadata_json=row["metadata"],
                )
            )
        await db.commit()


async def _await_harness_run(run_task: asyncio.Task) -> Any:
    """Await a backgrounded SDK ``harness.run()`` without tearing down its thread.

    The sync harness call runs in a worker thread; cancelling the awaiting task
    must NOT cancel the thread (the SDK subprocess call cannot be interrupted).
    On cancel we wait for the thread to finish, so the harness is quiescent
    before the next turn can start on the same session.
    """
    try:
        return await run_task
    except asyncio.CancelledError:
        try:
            await run_task
        except Exception:  # noqa: BLE001
            pass
        raise


async def _execute_turn(
    project: ResearchProject,
    prompt: str,
    on_event: Callable[[dict[str, Any]], Any] | None = None,
    system_instructions: str = "",
    phase: str = "RESEARCH",
    task_profile: str = "",
    _retry_count: int = 0,
) -> dict[str, Any]:
    """Run one agent turn in the project's DSH session and persist its events."""
    harness_or_awaitable = _harness_for(
        project,
        system_instructions=system_instructions,
        phase=phase,
        task_profile=task_profile,
    )
    # Supporting a direct Harness return keeps this seam easy to fake in tests.
    harness = await harness_or_awaitable if inspect.isawaitable(harness_or_awaitable) else harness_or_awaitable
    mapped_events: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    lock = threading.Lock()
    turn_id = _start_live_turn(project.id)
    sdk_session_id = _sdk_session_id(project, phase, turn_id)
    started_at = datetime.now(UTC)
    event_loop = asyncio.get_running_loop()
    persistence_futures: list[Any] = []
    tool_call_count = 0
    max_step = 0

    from .bridge import reset_turn_budget
    reset_turn_budget(project.id)

    def _collect(n):
        nonlocal tool_call_count, max_step
        try:
            mapped = _map_notification(n)
            call_id = mapped.get("call_id")
            with lock:
                if mapped.get("kind") == "tool_call" and call_id:
                    tool_name = mapped.get("tool", {}).get("name")
                    if tool_name:
                        tool_names[str(call_id)] = str(tool_name)
                elif mapped.get("kind") == "tool_result" and call_id and str(call_id) in tool_names:
                    mapped["tool"] = {"name": tool_names[str(call_id)]}
            enriched = _record_live_event(project.id, turn_id, mapped)
            with lock:
                if mapped.get("kind") == "tool_call":
                    tool_call_count += 1
                step_value = mapped.get("step")
                if isinstance(step_value, int):
                    max_step = max(max_step, step_value)
                mapped_events.append(enriched)
            if enriched.get("kind") in {"assistant_message", "tool_call", "tool_result"}:
                persistence_futures.append(asyncio.run_coroutine_threadsafe(persist_mapped(project.id, [enriched]), event_loop))
            _update_live_status(project.id, enriched)
            if on_event is not None:
                on_event(enriched)
        except Exception:
            logger.warning("DSH 实时事件处理失败（不影响回合执行）", exc_info=True)

    set_status(project.id, "THINKING", stage="正在分析任务并启动 DSH 回合", progress=10, error="")

    def _run_blocking():
        try:
            return harness.run(prompt, session_id=sdk_session_id, on_notification=_collect)
        except BaseException as exc:  # noqa: BLE001
            return {"ok": False, "error": f"DSH 运行失败: {exc}"}

    run_task = asyncio.ensure_future(asyncio.to_thread(_run_blocking))
    timeout_seconds = TASK_TIMEOUT_SECONDS.get(
        task_profile,
        settings.dsh_research_timeout_seconds if phase == "RESEARCH" else max(settings.dsh_research_timeout_seconds, 600),
    )
    done, _ = await asyncio.wait({run_task}, timeout=timeout_seconds)
    if not done:
        set_status(project.id, "FAILED", stage="DSH 回合超时，正在终止", error=f"超过 {timeout_seconds} 秒执行预算")
        await asyncio.to_thread(harness.close)
        _discard_harness(project.id, harness)
        _archive_session_directory(sdk_session_id)
        try:
            await asyncio.wait_for(asyncio.shield(run_task), timeout=10)
        except Exception:  # noqa: BLE001
            pass
        result = {
            "ok": False,
            "error": f"DSH 研究回合超过 {timeout_seconds} 秒，已终止",
            "error_code": "DSH_TURN_TIMEOUT",
        }
    else:
        result = run_task.result()

    if persistence_futures:
        results = await asyncio.gather(*(asyncio.wrap_future(item) for item in persistence_futures), return_exceptions=True)
        if any(isinstance(item, Exception) for item in results):
            logger.warning("部分 DSH 实时事件持久化失败（不影响回合结果）")

    from .bridge import pending_approvals

    pending = pending_approvals(project.id)
    final_response = ""
    if isinstance(result, dict) and not result.get("ok", True):
        error = result.get("error", "")
        set_status(project.id, "FAILED", stage="DSH Agent 运行出错", error=error)
        return {
            "ok": False,
            "error": error,
            "error_code": result.get("error_code", "DSH_RUN_FAILED"),
            "pending": pending,
        }

    final_response = getattr(result, "final_response", "") or (
        result.get("final_response", "") if isinstance(result, dict) else ""
    )

    # A protocol-level completed turn may contain reasoning/tool calls but no
    # user-visible text. Give the model exactly one tool-free synthesis chance.
    recovered = False
    if not final_response.strip() and not pending:
        recovery_prompt = (
            "停止所有工具调用。仅根据本会话已经获得的信息，立即用简体中文输出最终《量化策略设计方案》或当前任务结论。"
            "必须包含用户可见正文；不得描述内部推理、技能加载或工具过程。"
        )
        recovery_task = asyncio.ensure_future(asyncio.to_thread(
            lambda: harness.run(recovery_prompt, session_id=sdk_session_id, on_notification=_collect)
        ))
        recovery_done, _ = await asyncio.wait({recovery_task}, timeout=min(60, timeout_seconds))
        if recovery_done:
            try:
                recovery = recovery_task.result()
            except Exception:  # noqa: BLE001
                recovery = None
            final_response = getattr(recovery, "final_response", "") or ""
            recovered = bool(final_response.strip())
        else:
            await asyncio.to_thread(harness.close)
            _discard_harness(project.id, harness)
            _archive_session_directory(sdk_session_id)

    # Recovery notifications are collected by the same callback; wait for
    # their persistence and return the complete event list.
    if persistence_futures:
        await asyncio.gather(*(asyncio.wrap_future(item) for item in persistence_futures), return_exceptions=True)
    with lock:
        mapped = list(mapped_events)

    elapsed_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
    metrics = {"phase": phase, "elapsed_ms": elapsed_ms, "tool_call_count": tool_call_count, "max_step": max_step, "recovered_empty_response": recovered}
    if not final_response.strip() and not pending:
        has_meaningful_event = any(
            event.get("kind") in {"assistant_message", "tool_call", "tool_result"}
            for event in mapped
        )
        if not has_meaningful_event and _retry_count == 0:
            logger.warning("检测到 DSH 会话未产生有效事件（可能是会话 ID 冲突或 SDK 启动异常），正在自动归档并自愈重试: project=%s, session=%s", project.id, sdk_session_id)
            _archive_session_directory(sdk_session_id)
            _discard_harness(project.id, harness)
            try:
                harness.close()
            except Exception:  # noqa: BLE001
                pass
            return await _execute_turn(
                project,
                prompt,
                on_event=on_event,
                system_instructions=system_instructions,
                phase=phase,
                task_profile=task_profile,
                _retry_count=1,
            )

        start_failed = not has_meaningful_event
        if start_failed:
            _archive_session_directory(sdk_session_id)
            _discard_harness(project.id, harness)
        error = (
            f"DSH {phase} 阶段未成功启动：运行时没有产生任何 SDK 事件"
            if start_failed
            else "DSH 已结束，但未生成最终研究结论；自动收束仍未产生用户可见正文"
        )
        set_status(project.id, "FAILED", stage="DSH 最终输出为空", progress=100, error=error, metrics=metrics)
        return {
            "ok": False,
            "error": error,
            "error_code": "DSH_IMPLEMENTATION_START_FAILED" if start_failed else "DSH_EMPTY_FINAL_RESPONSE",
            "pending": [],
            "events": mapped,
            "metrics": metrics,
        }

    # The turn ended; if the model is blocked on an approval the session is idle
    # and the platform shows the pending request.
    set_status(project.id, "WAITING_APPROVAL" if pending else "IDLE", stage="待审批" if pending else "DSH 回合完成", progress=100, metrics=metrics)
    final_response_persisted = any(
        event.get("kind") == "assistant_message" and event.get("text", "").strip() == final_response.strip()
        for event in mapped
    )
    return {
        "ok": True,
        "final_response": final_response,
        "pending": pending,
        "events": mapped,
        "metrics": metrics,
        "final_response_persisted": final_response_persisted,
    }


async def run_turn(
    project: ResearchProject,
    prompt: str,
    on_event: Callable[[dict[str, Any]], Any] | None = None,
    system_instructions: str = "",
    phase: str = "RESEARCH",
    task_profile: str = "",
) -> dict[str, Any]:
    """Run one agent turn in the project's DSH session, tracked as an active turn."""
    active = _active_turns.get(project.id)
    if active is not None and not active.done():
        return {"ok": False, "error": "该研究项目已有 DSH 回合在运行"}

    task = asyncio.create_task(
        _execute_turn(
            project,
            prompt,
            on_event,
            system_instructions=system_instructions,
            phase=phase,
            task_profile=task_profile,
        )
    )
    _active_turns[project.id] = task
    try:
        return await task
    finally:
        if _active_turns.get(project.id) is task:
            _active_turns.pop(project.id, None)


def cancel_turn(project_id: str) -> None:
    task = _active_turns.get(project_id)
    if task is not None and not task.done():
        task.cancel()
    prefix = f"{project_id}:"
    for key in [item for item in _harnesses if item.startswith(prefix)]:
        harness = _harnesses.pop(key)
        try:
            harness.close()
        except Exception:  # noqa: BLE001
            logger.warning("取消回合时关闭 DSH Harness 失败: %s", key, exc_info=True)
    _archive_session_directory(f"*{project_id}*")
    set_status(project_id, "IDLE", stage="已取消", error="")


async def run_llm_connectivity_test(
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    max_tokens: int = 8192,
    prompt: str = "请只回复 quantlab-ok",
) -> tuple[bool, str]:
    """Run a minimal DSH SDK agent turn to verify LLM connectivity / tool calling.

    Single SDK-backed connectivity probe used by the LLM settings UI; no research
    project is required. Omitted values come from the saved Settings configuration.
    """
    from deepseek_harness import DeepSeekHarness  # imported lazily (heavy import)

    saved_config = await _runtime_llm_config() if not (base_url and api_key and model) else None
    url = (base_url or saved_config["base_url"]).strip()
    key = (api_key or saved_config["api_key"]).strip()
    mdl = (model or saved_config["model"]).strip()

    core_path = _cordis_path()
    if not core_path.exists():
        return False, f"DSH cordis 配置缺失: {core_path}"
    if not key:
        return False, "缺少 DSH API Key：请前往「系统设置 - LLM & DSH 配置」保存配置"

    ws = (settings.data_root / "dsh" / "connectivity_test").resolve()
    ws.mkdir(parents=True, exist_ok=True)
    sessions = (settings.data_root / "dsh" / "sessions").resolve()
    sessions.mkdir(parents=True, exist_ok=True)

    harness = DeepSeekHarness(
        provider="deepseek-official",
        model=mdl,
        max_tokens=max_tokens,
        cwd=str(ws),
        session_root=str(sessions),
        base_url=url,
        api_key=key,
        cordis=str(core_path),
        env={"DSH_TOOLS_PLUGIN_PATH": str(_plugin_path())},
        request_timeout_seconds=120,
    )

    def _run() -> Any:
        session_id = f"dsh_connectivity_test_{uuid.uuid4().hex}"
        return harness.run(prompt, session_id=session_id)

    def _close() -> None:
        try:
            harness.close()
        except Exception:  # noqa: BLE001
            pass

    logger.info("开始 DSH SDK 连通性测试 (model=%s, base_url=%s, prompt=%r)...", mdl, url, prompt)
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(asyncio.to_thread(_run), timeout=60.0)
        duration = time.monotonic() - t0
        logger.info("DSH SDK 连通性测试完成，耗时 %.2fs", duration)
    except asyncio.TimeoutError:
        logger.warning("DSH SDK 连通性测试超时 (60s)")
        return False, "DSH 连通性测试超时（60秒内未完成），请检查上游接口网络延迟或模型响应速度"
    except Exception as exc:  # noqa: BLE001
        logger.warning("DSH SDK 连通性测试异常: %s", exc)
        return False, f"DSH SDK 运行失败: {exc}"
    finally:
        await asyncio.to_thread(_close)

    final = (getattr(result, "final_response", "") or "").strip()
    if not final:
        failure = _connectivity_failure_message(result)
        if failure:
            return False, f"DSH SDK 运行失败: {failure}"
        return False, "DSH 模型未返回任何文本回复"
    return True, final[:500]


def _connectivity_failure_message(result: Any) -> str:
    """Extract the runtime's actual LLM failure from a completed SDK result."""
    if getattr(result, "finish_reason", None) != "error":
        return ""

    for notification in reversed(getattr(result, "notifications", ()) or ()):
        payload = getattr(notification, "payload", None)
        if not isinstance(payload, dict):
            continue
        event = payload.get("event")
        if not isinstance(event, dict):
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue

        reason = data.get("reason")
        if isinstance(reason, dict):
            error = reason.get("error") or reason.get("failure")
            if isinstance(error, dict) and error.get("message"):
                code = str(error.get("code") or "").strip()
                message = str(error["message"]).strip()
                return f"{message} ({code})" if code else message

        chunk = data.get("chunk")
        if isinstance(chunk, dict):
            chunk_reason = chunk.get("reason")
            if isinstance(chunk_reason, dict):
                failure = chunk_reason.get("failure")
                if isinstance(failure, dict) and failure.get("message"):
                    code = str(failure.get("code") or "").strip()
                    message = str(failure["message"]).strip()
                    return f"{message} ({code})" if code else message

    return "DSH runtime 以 error 状态结束"


def shutdown_all() -> None:
    """Shut down all harnesses (best-effort) on app exit."""
    for h in list(_harnesses.values()):
        try:
            h.close()
        except Exception:
            pass
    _harnesses.clear()
    with _state_lock:
        _session_events.clear()
        _live_session_events.clear()
        _session_event_seq.clear()
        _status.clear()
