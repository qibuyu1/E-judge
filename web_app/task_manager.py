from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any, Dict, Optional
from uuid import uuid4

from .config import settings
from .cache import research_cache
from .report_service import research_company
from .schemas import TaskState

logger = logging.getLogger(__name__)


class TaskManager:
    def __init__(self) -> None:
        self._states: Dict[str, TaskState] = {}
        self._reports: Dict[str, Dict[str, Any]] = {}
        self._lock = Lock()
        self._semaphore = asyncio.Semaphore(settings.task_workers)

    def create(self, query: str, force_refresh: bool) -> TaskState:
        task_id = uuid4().hex
        state = TaskState(task_id=task_id, status="queued", progress=0, stage="等待处理", message="任务已进入队列")
        with self._lock:
            self._states[task_id] = state
        asyncio.create_task(self._run(task_id, query, force_refresh))
        return state

    async def _run(self, task_id: str, query: str, force_refresh: bool) -> None:
        async with self._semaphore:
            self._update(task_id, status="running", progress=1, stage="启动", message="正在启动实时公开信息尽调")

            async def progress(progress_value: int, stage: str, message: str) -> None:
                self._update(task_id, status="running", progress=progress_value, stage=stage, message=message)

            try:
                # No artificial global cutoff. Every external stage already has its own
                # short network timeout, and the algorithm bounds query/model counts.
                # The goal is to finish a complete result, not abort at an arbitrary
                # percentage because a wall-clock limit expired.
                report = await research_company(
                    query,
                    force_refresh=force_refresh,
                    progress_callback=progress,
                )
                report_id = report["report_id"]
                with self._lock:
                    self._reports[report_id] = report
                research_cache.archive(report)
                self._update(
                    task_id,
                    status="completed",
                    progress=100,
                    stage="完成",
                    message="尽调报告已生成",
                    report_id=report_id,
                )
            except Exception as exc:
                logger.exception("尽调任务失败")
                with self._lock:
                    current_progress = self._states[task_id].progress
                self._update(
                    task_id,
                    status="failed",
                    progress=min(99, current_progress),
                    stage="失败",
                    message="尽调任务未完成",
                    error=str(exc),
                    error_code=getattr(exc, "code", type(exc).__name__),
                )

    def _update(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            state = self._states[task_id]
            self._states[task_id] = state.model_copy(update=changes)

    def get_state(self, task_id: str) -> Optional[TaskState]:
        with self._lock:
            state = self._states.get(task_id)
            return state.model_copy() if state else None

    def get_report(self, report_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            report = self._reports.get(report_id)
            if report:
                return dict(report)
        archived = research_cache.get_report(report_id)
        return dict(archived) if archived else None


task_manager = TaskManager()
