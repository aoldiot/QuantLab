from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pathlib import Path

from .agent.service import cancel_active_session, create_worktree, session_out
from .backtest_service import create_backtest_run
from .config import settings
from .db import get_db
from .git_versions import code_hash, manifest_hash
from .llm_config import get_hermes_config
from .models import (
    AgentMessage,
    AgentSession,
    BacktestRun,
    DecisionStatus,
    ResearchDecision,
    ResearchMessage,
    ResearchProject,
    ResearchStatus,
    SpecificationStatus,
    Strategy,
    StrategySpecification,
    StrategyVersion,
)
from .schemas import (
    BacktestCreate,
    ResearchConclusionUpdate,
    ResearchDecisionResolve,
    ResearchImplementationCreate,
    ResearchIterationCreate,
    ResearchMessageCreate,
    ResearchProjectCreate,
    StrategySpecificationUpdate,
)
from .strategy_contract import load_manifest
from .strategy_files import _path, _template

router = APIRouter(prefix="/api/research", tags=["strategy-research"])

RESEARCH_INSTRUCTIONS = """你是 QuantLab 的首席量化研究员 Hermes。你的职责是与用户深入研讨金融量化策略、质疑假设、识别数据偏差和未来函数、设计可证伪实验，并在回测后依据客观数据分析结果。使用简体中文。你不编写或修改正式策略代码，不直接启动正式回测；需要实现时形成清晰策略规格，交由 QuantLab 和 Claude Code 执行。不要把高 Sharpe 直接等同于有效策略。

这是面向人的研究对话，不是机器接口。回复必须使用结构清晰的 Markdown，禁止输出 JSON、Python 字典或把全部内容压成一个段落。先用一两句话给出判断，再按需使用二到四个简短小节；列表每项只表达一个观点。公式、字段名或参数可用行内代码。信息不足时优先提出最有价值的三个决策问题，不要自行补齐大量未经确认的规则。

待决策项机器块：只有当确实存在必须由用户拍板、且你无法自行决定的策略设计选项时，才在回复最末尾追加一个 quantlab-questions 代码块，块内是 JSON 数组，每项包含 question、options、recommendation、impact 四个字段。没有需要用户拍板的决策时，绝对不要输出这个块，也不要输出空数组——不必为了凑格式而制造问题。每轮最多三项。

块内只能放用户不依赖未来回测结果即可直接选择的策略设计决策，例如“是否加入 KDJ 过滤器”。禁止放入必须由实验回答的经验问题，例如“加入某过滤器能否提升胜率/盈亏比/Sharpe”或“某方案是否有效”；这类内容写入正文的研究假设与实验计划，由回测验证。

示例（仅在确有待拍板决策时）：

```quantlab-questions
[{"question": "是否加入 KDJ 过滤器", "options": ["加入", "暂不加入"], "recommendation": "暂不加入", "impact": "先保持基准模型简单，便于归因"}]
```"""

SPEC_FIELDS = (
    "strategy_name", "title", "hypothesis", "market", "data", "signal",
    "entry_rules", "exit_rules", "position_sizing", "risk_management",
    "fees_and_slippage", "backtest_plan", "acceptance_criteria",
    "known_risks", "open_questions",
)

SPEC_INSTRUCTIONS = """你正在为 QuantLab 的机器接口生成策略规格。只返回一个严格有效的 JSON 对象：不要 Markdown 代码围栏、解释、注释或前后缀。所有字段名必须使用提示中给出的英文 snake_case，字段值可以使用简体中文。字符串必须使用 JSON 双引号；不得使用尾随逗号、NaN、Infinity 或 Python 语法。不要调用工具。

用户已在研讨阶段逐项拍板了所有待决策问题，这些决策是既定前提，必须直接写入对应规则字段，不得再次询问。因此 open_questions 通常应为空数组 []。只有当研讨确实没有覆盖、且必须由用户拍板才能继续实现的新决策出现时才填写；每一项都必须让用户不依赖未来回测结果即可直接选择，并写成“决策项：选项 A / 选项 B（推荐项及简短影响）”。禁止把经验问题写进 open_questions，例如“加入某过滤器是否能提升胜率/盈亏比/Sharpe”或“某方案是否有效”；这类内容应写入 hypothesis、backtest_plan 和 acceptance_criteria，由实验验证。"""

EMPIRICAL_QUESTION_PATTERN = re.compile(
    r"(?:能否|是否能|可否).*(?:提升|提高|改善|降低|增加|有效|优于)|"
    r"是否.*(?:提升|提高|改善|降低|增加).*(?:胜率|收益|盈亏比|夏普|Sharpe)",
    re.IGNORECASE,
)

DECISION_BLOCK_PATTERN = re.compile(r"```quantlab-questions\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _normalize_question(question: str) -> str:
    return re.sub(r"\s+", "", question).strip("？?。.：:").lower()


def _split_decisions(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Separate the optional machine block from the human-facing Markdown reply.

    The block is optional by contract: no block means no decision is pending.
    Malformed content is dropped rather than raised — a consulting reply must
    never fail just because the model produced an unparsable block.
    """
    blocks = DECISION_BLOCK_PATTERN.findall(text)
    clean = DECISION_BLOCK_PATTERN.sub("", text).strip()
    decisions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in blocks:
        try:
            parsed = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        items = parsed if isinstance(parsed, list) else [parsed]
        for item in items:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question", "")).strip()
            if not question or EMPIRICAL_QUESTION_PATTERN.search(question):
                continue
            key = _normalize_question(question)
            if not key or key in seen:
                continue
            seen.add(key)
            raw_options = item.get("options")
            options = [str(option).strip() for option in raw_options if str(option).strip()] if isinstance(raw_options, list) else []
            decisions.append({
                "question": question,
                "options": options,
                "recommendation": str(item.get("recommendation", "")).strip() or None,
                "impact": str(item.get("impact", "")).strip() or None,
            })
    return clean or text.strip(), decisions


def _extract_text(payload: dict[str, Any]) -> str:
    texts: list[str] = []
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    if not texts and isinstance(payload.get("output_text"), str):
        texts.append(payload["output_text"])
    return "\n".join(texts).strip()


async def call_hermes(
    project: ResearchProject,
    prompt: str,
    instructions: str = RESEARCH_INSTRUCTIONS,
    db: AsyncSession | None = None,
) -> str:
    base_url, api_key, model, timeout_seconds = await get_hermes_config(db)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "conversation": project.hermes_conversation,
        "input": prompt,
        "instructions": instructions,
        "store": True,
    }
    timeout = httpx.Timeout(timeout_seconds)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{base_url.rstrip('/')}/responses", headers=headers, json=body)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Hermes 调用失败：{exc}") from exc
    text = _extract_text(response.json())
    if not text:
        raise HTTPException(502, "Hermes 未返回可显示的研究内容")
    return text


async def _project(project_id: str, db: AsyncSession) -> ResearchProject:
    project = await db.get(ResearchProject, project_id)
    if not project:
        raise HTTPException(404, "研究项目不存在")
    return project


def _ensure_active(project: ResearchProject) -> None:
    if project.status == ResearchStatus.ARCHIVED:
        raise HTTPException(409, "研究项目已归档，请先重新打开")


def _has_open_questions(content: dict[str, Any]) -> bool:
    questions = content.get("open_questions")
    return isinstance(questions, list) and any(str(item).strip() for item in questions)


def _invalid_open_questions(content: dict[str, Any]) -> list[str]:
    questions = content.get("open_questions")
    if not isinstance(questions, list):
        return []
    return [str(item).strip() for item in questions if EMPIRICAL_QUESTION_PATTERN.search(str(item))]


def _decision_out(decision: ResearchDecision) -> dict[str, Any]:
    return {"id": decision.id, "question": decision.question, "options": decision.options or [],
            "recommendation": decision.recommendation, "impact": decision.impact,
            "status": decision.status.value, "answer": decision.answer, "origin": decision.origin,
            "source_message_id": decision.source_message_id,
            "created_at": decision.created_at, "resolved_at": decision.resolved_at}


async def _decisions(project_id: str, db: AsyncSession) -> list[ResearchDecision]:
    return list(await db.scalars(select(ResearchDecision).where(ResearchDecision.project_id == project_id).order_by(ResearchDecision.created_at)))


async def _pending_decisions(project_id: str, db: AsyncSession) -> list[ResearchDecision]:
    return list(await db.scalars(select(ResearchDecision).where(
        ResearchDecision.project_id == project_id,
        ResearchDecision.status == DecisionStatus.PENDING,
    ).order_by(ResearchDecision.created_at)))


async def _record_decisions(project_id: str, raised: list[dict[str, Any]], db: AsyncSession,
                            origin: str = "DISCUSSION", source_message_id: str | None = None) -> list[ResearchDecision]:
    """Persist newly raised decisions, skipping ones already awaiting the user."""
    if not raised:
        return []
    open_keys = {_normalize_question(item.question) for item in await _pending_decisions(project_id, db)}
    created: list[ResearchDecision] = []
    for item in raised:
        key = _normalize_question(item["question"])
        if key in open_keys:
            continue
        open_keys.add(key)
        decision = ResearchDecision(project_id=project_id, question=item["question"], options=item["options"],
                                    recommendation=item.get("recommendation"), impact=item.get("impact"),
                                    origin=origin, source_message_id=source_message_id)
        db.add(decision)
        created.append(decision)
    return created


def _resolved_decision_brief(decisions: list[ResearchDecision]) -> str:
    settled = [item for item in decisions if item.status == DecisionStatus.RESOLVED and item.answer]
    if not settled:
        return ""
    lines = "\n".join(f"- {item.question} → {item.answer}" for item in settled)
    return f"\n\n用户已拍板的策略设计决策（既定前提，必须直接落实到规则字段，不要再次询问）：\n{lines}"


async def _latest_spec(project_id: str, db: AsyncSession) -> StrategySpecification | None:
    return await db.scalar(select(StrategySpecification).where(StrategySpecification.project_id == project_id).order_by(StrategySpecification.version.desc()).limit(1))


def _next_version(version: str) -> str:
    parts = version.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    except (ValueError, IndexError):
        return f"{version}.1"


def _research_strategy_out(strategy: Strategy, version: StrategyVersion) -> dict[str, Any]:
    return {"id": strategy.id, "name": strategy.name, "slug": strategy.slug, "description": strategy.description,
            "category": strategy.category, "status": strategy.status.value, "latest_version_id": version.id,
            "version": version.version, "version_count": len(strategy.versions), "module": version.entrypoint,
            "parameter_schema": version.parameter_schema, "data_requirements": version.data_requirements,
            "created_at": strategy.created_at, "updated_at": strategy.updated_at}


def _project_out(project: ResearchProject, spec: StrategySpecification | None = None) -> dict[str, Any]:
    return {
        "id": project.id,
        "client_id": project.client_id,
        "title": project.title,
        "original_idea": project.original_idea,
        "status": project.status.value,
        "strategy_id": project.strategy_id,
        "implementation_session_id": project.implementation_session_id,
        "latest_backtest_id": project.latest_backtest_id,
        "conclusion": None if not project.conclusion_verdict else {
            "verdict": project.conclusion_verdict,
            "summary": project.conclusion_summary or "",
            "next_step": project.conclusion_next_step or "",
        },
        "archived_at": project.archived_at,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "specification": None if not spec else {
            "id": spec.id, "version": spec.version, "status": spec.status.value,
            "content": spec.content, "created_at": spec.created_at, "approved_at": spec.approved_at,
        },
    }


@router.get("")
async def list_projects(client_id: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(ResearchProject).where(ResearchProject.client_id == client_id).order_by(ResearchProject.updated_at.desc()))).all()
    return [_project_out(row, await _latest_spec(row.id, db)) for row in rows]


@router.post("")
async def create_project(data: ResearchProjectCreate, db: AsyncSession = Depends(get_db)):
    project = ResearchProject(client_id=data.client_id, title=data.title, original_idea=data.original_idea,
                              hermes_conversation=f"quantlab-research-{uuid.uuid4()}")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return _project_out(project)


@router.get("/{project_id}")
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    return _project_out(project, await _latest_spec(project.id, db))


@router.get("/{project_id}/messages")
async def list_messages(project_id: str, db: AsyncSession = Depends(get_db)):
    await _project(project_id, db)
    rows = (await db.scalars(select(ResearchMessage).where(ResearchMessage.project_id == project_id).order_by(ResearchMessage.created_at))).all()
    return [{"id": row.id, "role": row.role, "content": row.content, "message_type": row.message_type,
             "metadata": row.metadata_json, "created_at": row.created_at} for row in rows]


@router.post("/{project_id}/messages")
async def send_message(project_id: str, data: ResearchMessageCreate, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    _ensure_active(project)
    db.add(ResearchMessage(project_id=project.id, role="user", content=data.content))
    await db.commit()
    answer = await call_hermes(project, data.content, db=db)
    content, raised = _split_decisions(answer)
    message = ResearchMessage(project_id=project.id, role="assistant", content=content)
    db.add(message)
    await db.flush()
    created = await _record_decisions(project.id, raised, db, source_message_id=message.id)
    project.updated_at = datetime.now(UTC)
    await db.commit()
    for decision in created:
        await db.refresh(decision)
    return {"role": "assistant", "content": content, "decisions": [_decision_out(item) for item in created]}


@router.get("/{project_id}/decisions")
async def list_decisions(project_id: str, db: AsyncSession = Depends(get_db)):
    await _project(project_id, db)
    return [_decision_out(item) for item in await _decisions(project_id, db)]


async def _decision(project_id: str, decision_id: str, db: AsyncSession) -> ResearchDecision:
    decision = await db.get(ResearchDecision, decision_id)
    if not decision or decision.project_id != project_id:
        raise HTTPException(404, "待决策项不存在")
    if decision.status != DecisionStatus.PENDING:
        raise HTTPException(409, "该决策项已经处理过")
    return decision


@router.post("/{project_id}/decisions/{decision_id}/resolve")
async def resolve_decision(project_id: str, decision_id: str, data: ResearchDecisionResolve,
                           db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    _ensure_active(project)
    decision = await _decision(project.id, decision_id, db)
    decision.answer = data.answer.strip()
    decision.status = DecisionStatus.RESOLVED
    decision.resolved_at = datetime.now(UTC)
    db.add(ResearchMessage(project_id=project.id, role="user", content=f"决策：{decision.question} → {decision.answer}",
                           message_type="decision", metadata_json={"decision_id": decision.id}))
    project.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(decision)
    return _decision_out(decision)


@router.post("/{project_id}/decisions/{decision_id}/dismiss")
async def dismiss_decision(project_id: str, decision_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    _ensure_active(project)
    decision = await _decision(project.id, decision_id, db)
    decision.status = DecisionStatus.DISMISSED
    decision.resolved_at = datetime.now(UTC)
    project.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(decision)
    return _decision_out(decision)


def _parse_json(text: str) -> dict[str, Any]:
    """Extract the first valid JSON object even when the model adds prose/fences."""
    decoder = json.JSONDecoder()
    value: Any = None
    for match in re.finditer(r"\{", text):
        try:
            candidate, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            value = candidate
            break
    if not isinstance(value, dict):
        raise HTTPException(422, "Hermes 返回的策略规格不是有效 JSON")
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", str(value.get("strategy_name", ""))):
        raise HTTPException(422, "策略规格缺少合法的 strategy_name")
    missing = [field for field in SPEC_FIELDS if field not in value]
    if missing:
        raise HTTPException(422, f"策略规格缺少字段：{', '.join(missing)}")
    invalid_questions = _invalid_open_questions(value)
    if invalid_questions:
        raise HTTPException(422, "open_questions 包含应由回测回答的效果问题；请改为用户可以直接选择的策略设计决策")
    return value


async def _generate_spec_content(project: ResearchProject, settled: str = "", db: AsyncSession | None = None) -> dict[str, Any]:
    fields = ", ".join(SPEC_FIELDS)
    prompt = f"""根据当前全部研讨生成可交给 Claude Code 实现的策略规格。
必须且只能包含这些顶层字段：{fields}。
strategy_name 只能使用小写字母、数字和下划线，并以字母开头。规则必须明确可计算，不使用“明显”“适当”等模糊词。用户已在研讨阶段拍板全部待决策项，open_questions 通常应为空数组；只有研讨确实未覆盖、且必须由用户拍板的新决策才写入 open_questions，并给出明确选项、推荐项和简短影响。不要询问某项改动能否提升胜率、收益、盈亏比或 Sharpe；这类问题放入 hypothesis、backtest_plan 和 acceptance_criteria，交给实验回答。{settled}"""
    raw = await call_hermes(project, prompt, SPEC_INSTRUCTIONS, db=db)
    try:
        return _parse_json(raw)
    except HTTPException as first_error:
        repair_prompt = f"""上一次输出未通过机器校验：{first_error.detail}。
请重新生成完整规格。严格只输出有效 JSON 对象，英文顶层字段必须为：{fields}。
不要复述错误，不要输出 Markdown。"""
        repaired = await call_hermes(project, repair_prompt, SPEC_INSTRUCTIONS, db=db)
        try:
            return _parse_json(repaired)
        except HTTPException as exc:
            raise HTTPException(422, f"Hermes 两次返回的策略规格均未通过校验：{exc.detail}") from exc


@router.post("/{project_id}/specification/generate")
async def generate_specification(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    _ensure_active(project)
    pending = await _pending_decisions(project.id, db)
    if pending:
        raise HTTPException(409, f"还有 {len(pending)} 项待决策问题需要你拍板，请先在研讨阶段完成决策")
    content = await _generate_spec_content(project, _resolved_decision_brief(await _decisions(project.id, db)), db=db)
    current = await _latest_spec(project.id, db)
    version = 1 if not current else current.version + 1
    spec = StrategySpecification(project_id=project.id, version=version, content=content)
    db.add(spec)
    await db.flush()
    # A spec that still carries open questions is kept as a draft, but the
    # questions become discussion decisions so the "no open questions past the
    # discussion stage" invariant closes without discarding Hermes' output.
    raised = [{"question": str(item).strip(), "options": [], "recommendation": None, "impact": None}
              for item in content.get("open_questions") or [] if str(item).strip()]
    created = await _record_decisions(project.id, raised, db, origin="SPECIFICATION") if raised else []
    if created:
        project.status = ResearchStatus.DISCUSSING
        db.add(ResearchMessage(project_id=project.id, role="assistant",
                               content=f"策略规格 V{version} 中出现 {len(created)} 项新的待决策问题，已回到研讨阶段等待你拍板。",
                               message_type="specification", metadata_json={"specification_id": spec.id, "version": version}))
    else:
        project.status = ResearchStatus.SPEC_REVIEW
        db.add(ResearchMessage(project_id=project.id, role="assistant", content=f"已形成策略规格 V{version}，等待确认。", message_type="specification", metadata_json={"specification_id": spec.id, "version": version}))
    await db.commit()
    # `updated_at` uses a SQL expression on update, so SQLAlchemy expires just
    # that attribute after the flush even though the session itself does not
    # expire objects on commit. Refresh before serializing in async code.
    await db.refresh(project)
    await db.refresh(spec)
    return _project_out(project, spec)


@router.put("/{project_id}/specification/{spec_id}")
async def update_specification(project_id: str, spec_id: str, data: StrategySpecificationUpdate, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    _ensure_active(project)
    spec = await db.get(StrategySpecification, spec_id)
    if not spec or spec.project_id != project.id:
        raise HTTPException(404, "策略规格不存在")
    if spec.status != SpecificationStatus.DRAFT:
        raise HTTPException(409, "已确认的策略规格不能修改")
    _parse_json(json.dumps(data.content))
    spec.content = data.content
    await db.commit()
    return _project_out(project, spec)


@router.post("/{project_id}/specification/{spec_id}/approve")
async def approve_specification(project_id: str, spec_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    _ensure_active(project)
    spec = await db.get(StrategySpecification, spec_id)
    if not spec or spec.project_id != project.id:
        raise HTTPException(404, "策略规格不存在")
    if spec.status != SpecificationStatus.DRAFT:
        raise HTTPException(409, "只有草稿规格可以确认")
    if _has_open_questions(spec.content):
        raise HTTPException(409, "规格仍有未解决问题，请先清空 open_questions")
    others = (await db.scalars(select(StrategySpecification).where(StrategySpecification.project_id == project.id, StrategySpecification.status == SpecificationStatus.APPROVED))).all()
    for item in others:
        item.status = SpecificationStatus.SUPERSEDED
    spec.status = SpecificationStatus.APPROVED
    spec.approved_at = datetime.now(UTC)
    project.status = ResearchStatus.SPEC_REVIEW
    await db.commit()
    await db.refresh(project)
    await db.refresh(spec)
    return _project_out(project, spec)


@router.post("/{project_id}/implementation")
async def create_implementation(project_id: str, data: ResearchImplementationCreate, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    _ensure_active(project)
    spec = await _latest_spec(project.id, db)
    if not spec or spec.status != SpecificationStatus.APPROVED:
        raise HTTPException(409, "请先确认策略规格")
    if project.implementation_session_id:
        if not data.force:
            existing = await db.get(AgentSession, project.implementation_session_id)
            if existing and existing.specification_id == spec.id:
                raise HTTPException(409, "当前规格已经创建开发会话")
        else:
            await cancel_active_session(project.implementation_session_id)
    name = str(spec.content["strategy_name"])
    path = _path(name)
    if not path.exists():
        path.write_text(_template(name, "PORTFOLIO", str(spec.content.get("hypothesis", project.title)), "研究策略"), encoding="utf-8")
    session = AgentSession(client_id=data.client_id, strategy_name=name, permission_mode=data.permission_mode,
                           workspace_path="pending", research_project_id=project.id, specification_id=spec.id)
    db.add(session)
    await db.flush()
    try:
        session.workspace_path = str(await asyncio.to_thread(create_worktree, session.id, name))
    except Exception as exc:
        await db.rollback()
        raise HTTPException(500, f"创建策略开发工作区失败：{exc}") from exc
    prompt = "请严格按照已确认的QuantLab策略规格实现和测试当前策略。规格如下：\n" + json.dumps(spec.content, ensure_ascii=False, indent=2)
    db.add(AgentMessage(session_id=session.id, role="system", event_type="research_handoff", content={"text": prompt, "specification_id": spec.id}))
    project.implementation_session_id = session.id
    project.status = ResearchStatus.IMPLEMENTING
    await db.commit()
    await db.refresh(session)
    return {"session": session_out(session), "strategy_name": name, "prompt": prompt}


@router.post("/{project_id}/backtests/{run_id}/repair")
async def repair_failed_backtest(project_id: str, run_id: str, data: ResearchImplementationCreate,
                                 db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    _ensure_active(project)
    run = await db.get(BacktestRun, run_id)
    if not run or run.research_project_id != project.id:
        raise HTTPException(404, "该研究的回测记录不存在")
    if run.status.value != "FAILED":
        raise HTTPException(409, "只有失败的回测才能发起策略修复")
    spec = await _latest_spec(project.id, db)
    if not spec or not spec.content.get("strategy_name"):
        raise HTTPException(409, "找不到该研究对应的策略规格")
    name = str(spec.content["strategy_name"])
    if not _path(name).exists():
        raise HTTPException(409, "找不到该研究对应的策略文件")
    session = AgentSession(client_id=data.client_id, strategy_name=name, permission_mode=data.permission_mode,
                           workspace_path="pending", research_project_id=project.id, specification_id=spec.id)
    db.add(session)
    await db.flush()
    try:
        session.workspace_path = str(await asyncio.to_thread(create_worktree, session.id, name))
    except Exception as exc:
        await db.rollback()
        raise HTTPException(500, f"创建策略修复工作区失败：{exc}") from exc
    prompt = f"""请处理这次失败的 QuantLab 回测。第一步必须先根据堆栈、回测配置和策略代码判断责任类型：

1. 只有错误根因位于当前策略 Python 文件的字段、参数、指标、订单或交易逻辑时，才归类为 STRATEGY，并修改策略、执行语法和相关测试验证。
2. 如果根因属于 QuantLab 回测构建器、NautilusTrader 框架兼容、Catalog 数据、运行环境、基础设施或用户回测配置，必须归类为 NON_STRATEGY。此时严禁修改策略文件，明确回复“拒绝修改策略”，说明原因、责任模块和建议处理方式。
3. 不得为了绕过框架或数据错误而给策略增加无业务意义的兼容字段。
4. 回复开头必须明确写出“责任判断：策略问题”或“责任判断：非策略问题”。

回测任务：{run.name}
回测配置：
{json.dumps(run.config, ensure_ascii=False, indent=2)}

错误日志：
{run.error_message or '未记录错误日志'}

已确认策略规格：
{json.dumps(spec.content, ensure_ascii=False, indent=2)}"""
    db.add(AgentMessage(session_id=session.id, role="system", event_type="backtest_repair_handoff",
                        content={"text": prompt, "specification_id": spec.id, "run_id": run.id}))
    project.implementation_session_id = session.id
    project.status = ResearchStatus.IMPLEMENTING
    await db.commit()
    await db.refresh(session)
    return {"session": session_out(session), "strategy_name": name, "prompt": prompt}


@router.get("/{project_id}/strategy-preview")
async def research_strategy_preview(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    _ensure_active(project)
    spec = await _latest_spec(project.id, db)
    if not spec or not spec.content.get("strategy_name"):
        raise HTTPException(409, "请先生成并确认策略规格")
    module = f"app.strategies.{spec.content['strategy_name']}"
    try:
        manifest = load_manifest(module)
    except (ImportError, AttributeError, TypeError) as exc:
        raise HTTPException(409, f"Claude 尚未生成可用策略：{exc}") from exc
    return {"module": module, "name": manifest.name, "parameter_schema": manifest.parameter_schema(),
            "data_requirements": manifest.data_requirements()}


@router.post("/{project_id}/publish")
async def publish_research_strategy(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    _ensure_active(project)
    spec = await _latest_spec(project.id, db)
    if not spec or spec.status != SpecificationStatus.APPROVED:
        raise HTTPException(409, "请先确认策略规格")
    module = f"app.strategies.{spec.content['strategy_name']}"
    try:
        manifest = load_manifest(module)
    except (ImportError, AttributeError, TypeError) as exc:
        raise HTTPException(409, f"Claude 尚未生成可发布策略：{exc}") from exc
    strategy = await db.scalar(select(Strategy).where(Strategy.slug == manifest.slug))
    if strategy:
        await db.refresh(strategy, ["versions"])

    strategy_name = spec.content['strategy_name']
    source_path = Path(__file__).resolve().parent / "strategies" / f"{strategy_name}.py"
    code = source_path.read_text(encoding="utf-8") if source_path.exists() else ""
    c_hash = code_hash(code) if code else None
    m_hash = manifest_hash(manifest)

    if strategy is None:
        strategy = Strategy(name=manifest.name, slug=manifest.slug, description=manifest.description, category=manifest.category)
        db.add(strategy)
        await db.flush()
        await db.refresh(strategy, ["versions"])

    if strategy.versions:
        latest = max(strategy.versions, key=lambda item: item.created_at)
        if latest.code_hash and latest.code_hash == c_hash:
            project.strategy_id = strategy.id
            project.status = ResearchStatus.READY_FOR_BACKTEST
            await db.commit()
            await db.refresh(project)
            return _research_strategy_out(strategy, latest)

    version_name = manifest.version
    if any(item.version == version_name for item in strategy.versions):
        version_name = _next_version(max(strategy.versions, key=lambda item: item.created_at).version)
    version = StrategyVersion(
        strategy_id=strategy.id,
        version=version_name,
        entrypoint=module,
        code=code,
        code_hash=c_hash,
        parameter_schema=manifest.parameter_schema(),
        data_requirements=manifest.data_requirements(),
        manifest_hash=m_hash,
        description=f"研究项目：{project.title}",
    )
    db.add(version)
    project.strategy_id = strategy.id
    project.status = ResearchStatus.READY_FOR_BACKTEST
    await db.commit()
    await db.refresh(project)
    await db.refresh(strategy, ["versions"])
    return _research_strategy_out(strategy, version)


@router.post("/{project_id}/backtests")
async def create_research_backtest(project_id: str, data: BacktestCreate, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    _ensure_active(project)
    if not project.strategy_id:
        raise HTTPException(409, "请先发布研究策略")
    run = await create_backtest_run(data, db, research_project_id=project.id)
    project.latest_backtest_id = run.id
    project.status = ResearchStatus.BACKTESTING
    await db.commit()
    return {"id": run.id, "status": run.status.value, "name": run.name}


@router.get("/{project_id}/backtests")
async def list_research_backtests(project_id: str, db: AsyncSession = Depends(get_db)):
    await _project(project_id, db)
    rows = (await db.scalars(select(BacktestRun).where(BacktestRun.research_project_id == project_id).order_by(BacktestRun.created_at.desc()))).all()
    return [{"id": row.id, "name": row.name, "status": row.status.value, "stage": row.stage, "progress": row.progress,
             "metrics": row.metrics, "created_at": row.created_at} for row in rows]


@router.post("/{project_id}/backtests/{run_id}/analyze")
async def analyze_backtest(project_id: str, run_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    _ensure_active(project)
    run = await db.get(BacktestRun, run_id)
    if not run or run.research_project_id != project.id:
        raise HTTPException(404, "该研究项目中不存在此回测")
    if run.status.value != "COMPLETED":
        raise HTTPException(409, "回测尚未完成")
    spec = await _latest_spec(project.id, db)
    project.status = ResearchStatus.ANALYZING
    await db.commit()
    prompt = "请分析本次正式回测。必须区分客观事实与推断，判断是否支持原始假设，并提出最有信息价值的下一步实验。\n策略规格：\n" + json.dumps(spec.content if spec else {}, ensure_ascii=False) + "\n回测配置：\n" + json.dumps(run.config, ensure_ascii=False) + "\n指标：\n" + json.dumps(run.metrics, ensure_ascii=False) + "\n结果摘要：\n" + json.dumps(run.result, ensure_ascii=False)[:60000]
    try:
        answer = await call_hermes(project, prompt, db=db)
    except Exception:
        project.status = ResearchStatus.READY_FOR_ANALYSIS
        await db.commit()
        raise
    content, _ = _split_decisions(answer)
    db.add(ResearchMessage(project_id=project.id, role="assistant", content=content, message_type="analysis", metadata_json={"run_id": run.id}))
    project.status = ResearchStatus.RESULT_REVIEW
    project.updated_at = datetime.now(UTC)
    await db.commit()
    return {"role": "assistant", "content": content, "run_id": run.id}


@router.put("/{project_id}/conclusion")
async def save_conclusion(project_id: str, data: ResearchConclusionUpdate, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    _ensure_active(project)
    analysis = await db.scalar(select(ResearchMessage.id).where(
        ResearchMessage.project_id == project.id,
        ResearchMessage.message_type == "analysis",
    ).limit(1))
    if not analysis:
        raise HTTPException(409, "请先完成至少一次回测分析")
    project.conclusion_verdict = data.verdict
    project.conclusion_summary = data.summary
    project.conclusion_next_step = data.next_step
    project.status = ResearchStatus.RESULT_REVIEW
    project.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(project)
    return _project_out(project, await _latest_spec(project.id, db))


@router.post("/{project_id}/archive")
async def archive_project(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    if project.status == ResearchStatus.ARCHIVED:
        return _project_out(project, await _latest_spec(project.id, db))
    if not project.conclusion_verdict:
        raise HTTPException(409, "请先保存研究结论")
    project.status = ResearchStatus.ARCHIVED
    project.archived_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(project)
    return _project_out(project, await _latest_spec(project.id, db))


@router.post("/{project_id}/reopen")
async def reopen_project(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    if project.status != ResearchStatus.ARCHIVED:
        raise HTTPException(409, "研究项目尚未归档")
    project.status = ResearchStatus.RESULT_REVIEW if project.conclusion_verdict else ResearchStatus.DISCUSSING
    project.archived_at = None
    await db.commit()
    await db.refresh(project)
    return _project_out(project, await _latest_spec(project.id, db))


@router.post("/{project_id}/iterate")
async def iterate_project(project_id: str, data: ResearchIterationCreate, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    _ensure_active(project)
    target = ResearchStatus(data.target)
    current_spec = await _latest_spec(project.id, db)
    if target == ResearchStatus.SPEC_REVIEW and not current_spec:
        raise HTTPException(409, "尚无可迭代的策略规格")
    if target == ResearchStatus.READY_FOR_BACKTEST and not project.strategy_id:
        raise HTTPException(409, "尚无已发布策略可继续验证")
    previous_conclusion = {
        "verdict": project.conclusion_verdict,
        "summary": project.conclusion_summary,
        "next_step": project.conclusion_next_step,
    }
    project.status = target
    project.conclusion_verdict = None
    project.conclusion_summary = None
    project.conclusion_next_step = None
    if target == ResearchStatus.SPEC_REVIEW and current_spec:
        db.add(StrategySpecification(project_id=project.id, version=current_spec.version + 1,
                                     status=SpecificationStatus.DRAFT, content=dict(current_spec.content)))
    iteration_content = data.reason
    if previous_conclusion["summary"]:
        iteration_content += f"\n\n上一轮结论：{previous_conclusion['summary']}"
        if previous_conclusion["next_step"]:
            iteration_content += f"\n上一轮建议：{previous_conclusion['next_step']}"
    db.add(ResearchMessage(project_id=project.id, role="system", content=iteration_content,
                           message_type="iteration", metadata_json={
                               "target": data.target,
                               "previous_conclusion": previous_conclusion,
                           }))
    await db.commit()
    await db.refresh(project)
    return _project_out(project, await _latest_spec(project.id, db))
