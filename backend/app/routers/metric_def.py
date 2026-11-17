"""指标定义 分析看板接口：汇总指标、参数运行与结果导出。"""
from __future__ import annotations

import csv
import io
import random
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.metric_def import MetricCaliberService, MetricDefService

router = APIRouter(prefix="/api/metric_def", tags=["指标定义"])
service = MetricDefService()
caliber = MetricCaliberService()
LIST_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "筛选条件", "精度位数", "口径版本"]
STATUSES = ["草稿", "已定义", "已发布", "已废弃"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    return {"metrics": [
        {"label": "覆盖数据源", "value": random.randint(12, 45), "trend": random.randint(-12, 25)},
        {"label": "最新指标值", "value": round(random.uniform(0.72, 0.99), 3), "trend": random.randint(-8, 15)},
        {"label": "异常记录", "value": random.randint(0, 8), "trend": -random.randint(1, 10)},
    ]}


@router.get("/caliber/status-records")
def list_status_records() -> dict[str, Any]:
    """指标状态记录：当前状态、已处理状态与状态水位，用来识别状态流转边界。"""
    items = caliber.status_records()
    return {"items": items, "total": len(items)}


@router.get("/caliber/refresh-list")
def list_refresh_list(
    task_no: str | None = Query(default=None, description="按口径切换任务编号过滤"),
    valid_only: bool = Query(default=False, description="只看当前有效版本"),
) -> dict[str, Any]:
    """指标刷新清单：保留历史版本，可按任务与有效性回查。"""
    items = caliber.refresh_list(task_no, valid_only=valid_only)
    return {"items": items, "total": len(items)}


@router.get("/caliber/results")
def list_caliber_results() -> dict[str, Any]:
    """口径切换后的指标结果：同一指标状态只输出一条。"""
    items = caliber.results()
    return {"items": items, "total": len(items)}


@router.get("/caliber/tasks")
def list_switch_tasks() -> dict[str, Any]:
    items = caliber.tasks()
    return {"items": items, "total": len(items)}


@router.get("/caliber/tasks/{task_id}")
def get_switch_task(task_id: int) -> dict:
    detail = caliber.task_detail(task_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"口径切换任务 {task_id} 不存在")
    return detail


@router.post("/caliber/switch")
def switch_caliber(payload: EntryPayload) -> dict[str, Any]:
    """发起口径切换：按状态流转边界重算已确认指标，复用已确认版本。"""
    result, error = caliber.switch(payload.values)
    if result is None:
        return {"ok": False, "message": error}
    return result


@router.post("/caliber/diff")
def write_caliber_diff(payload: EntryPayload) -> dict[str, Any]:
    """指标差异接口：把核对结果写入指标刷新清单并保留对应版本。"""
    result, error = caliber.apply_diff(payload.values)
    if result is None:
        return {"ok": False, "message": error}
    return result


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """推进指标发布状态；状态变化会在下次口径切换时形成新的流转边界。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("")
def list_results() -> dict[str, Any]:
    return {"items": service.list_results()}



@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    results = service.run_analysis(payload.values)
    return {"items": results}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.list_results()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=metric_def.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"指标 {entry_id} 不存在")
    return entry
