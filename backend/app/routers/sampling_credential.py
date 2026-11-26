"""采样凭据包接口：轮次采样、定稿、凭据申请、签发与冻结重领。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.sampling_credential import sampling_credential_service as service

router = APIRouter(prefix="/api/sampling-credential", tags=["采样凭据包"])


class ReadingPayload(BaseModel):
    timestamp: str = Field(..., description="读数时间，ISO 8601")
    value: float = Field(..., description="读数值")


class TaskPayload(BaseModel):
    task_id: str
    source_point: str
    granularity: str = "10m"
    agg_function: str = "avg"
    period_start: str
    period_end: str


def _ensure_ok(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "操作失败"))
    return result


@router.post("/reset")
def reset_state():
    """还原演示状态（演示/验收用）。"""
    service.reset()
    return {"ok": True, "message": "采样凭据状态已重置"}


@router.get("/tasks")
def list_tasks():
    return {"ok": True, "tasks": list(service.tasks.values())}


@router.post("/tasks")
def register_task(payload: TaskPayload):
    return _ensure_ok(service.register_task(
        payload.task_id,
        payload.source_point,
        payload.granularity,
        payload.agg_function,
        payload.period_start,
        payload.period_end,
    ))


@router.get("/tasks/{task_id}")
def task_detail(task_id: str):
    return _ensure_ok(service.task_detail(task_id))


@router.post("/tasks/{task_id}/readings")
def ingest_reading(task_id: str, payload: ReadingPayload):
    return _ensure_ok(service.ingest_reading(task_id, payload.timestamp, payload.value))


@router.get("/tasks/{task_id}/rounds")
def list_rounds(task_id: str):
    return _ensure_ok(service.list_rounds(task_id))


@router.post("/tasks/{task_id}/rounds/settle")
def settle_round(task_id: str):
    return _ensure_ok(service.settle_round(task_id))


@router.post("/tasks/{task_id}/packages")
def apply_package(task_id: str):
    return _ensure_ok(service.apply_package(task_id))


@router.get("/tasks/{task_id}/packages")
def list_packages(task_id: str):
    return _ensure_ok(service.list_packages(task_id))


@router.post("/packages/{package_id}/issue")
def issue_package(package_id: str):
    return _ensure_ok(service.issue_package(package_id))


@router.get("/packages/{package_id}/download")
def download_package(package_id: str):
    result = service.download_package(package_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "下载失败"))
    headers = {
        "Content-Disposition": f'attachment; filename="{result["filename"]}"',
        "X-File-Fingerprint": result["fingerprint"],
        "X-Frozen": "1" if result["frozen"] else "0",
    }
    return StreamingResponse(
        iter([result["content"]]),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )
