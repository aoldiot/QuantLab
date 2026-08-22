from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AgentTask, AgentTaskStatus, WorkerType


async def create_task(
    db: AsyncSession,
    *,
    project_id: str,
    worker_type: WorkerType,
    task_type: str,
    input_json: dict[str, Any],
    parent_task_id: str | None = None,
    max_attempts: int = 3,
) -> AgentTask:
    active = await db.scalar(
        select(AgentTask).where(
            AgentTask.project_id == project_id,
            AgentTask.worker_type == worker_type,
            AgentTask.status == AgentTaskStatus.RUNNING,
        ).order_by(AgentTask.created_at.desc())
    )
    if active is not None:
        return active
    pending = await db.scalar(
        select(AgentTask).where(
            AgentTask.project_id == project_id,
            AgentTask.worker_type == worker_type,
            AgentTask.task_type == task_type,
            AgentTask.status == AgentTaskStatus.PENDING,
        ).order_by(AgentTask.created_at.desc())
    )
    if pending is not None:
        pending.input_json = input_json
        pending.parent_task_id = parent_task_id or pending.parent_task_id
        pending.max_attempts = max_attempts
        await db.commit()
        return pending
    task = AgentTask(
        project_id=project_id,
        worker_type=worker_type,
        task_type=task_type,
        input_json=input_json,
        parent_task_id=parent_task_id,
        max_attempts=max_attempts,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def start_task(db: AsyncSession, task: AgentTask, session_id: str) -> AgentTask:
    task.status = AgentTaskStatus.RUNNING
    task.session_id = session_id
    task.attempt += 1
    task.started_at = datetime.now(UTC)
    task.error_code = None
    task.error_message = None
    await db.commit()
    return task


async def complete_task(db: AsyncSession, task: AgentTask, output: dict[str, Any]) -> AgentTask:
    task.status = AgentTaskStatus.COMPLETED
    task.output_json = output
    task.completed_at = datetime.now(UTC)
    await db.commit()
    return task


async def fail_task(db: AsyncSession, task: AgentTask, code: str, message: str) -> AgentTask:
    task.error_code = code
    task.error_message = message
    task.completed_at = datetime.now(UTC)
    task.status = AgentTaskStatus.PENDING if task.attempt < task.max_attempts else AgentTaskStatus.FAILED
    await db.commit()
    return task


async def recover_interrupted_tasks(db: AsyncSession) -> int:
    tasks = list((await db.scalars(select(AgentTask).where(AgentTask.status == AgentTaskStatus.RUNNING))).all())
    for task in tasks:
        task.status = AgentTaskStatus.PENDING
        task.error_code = "PROCESS_INTERRUPTED"
        task.error_message = "服务中断，任务已恢复为待执行并保留原 session_id"
    if tasks:
        await db.commit()
    return len(tasks)
