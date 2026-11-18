"""数据导出接口：导出任务时间线、按范围裁剪的清单下载与 V012 导出文件清单。"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.data_export import (
    STRATEGIES,
    DataExportService,
)

router = APIRouter(prefix="/api/data_export", tags=["数据导出"])
service = DataExportService()
LIST_FIELDS = ["任务编号", "数据来源", "导出格式", "字段清单", "数据范围", "压缩方式", "分片大小", "导出批次"]
STATUSES = ["草稿", "导出中", "已完成", "已取消"]


class ManifestPayload(BaseModel):
    """生成清单的参数：任务编号、导出批次与拆分策略。"""

    values: dict[str, Any] = Field(default_factory=dict)


class ShardPayload(BaseModel):
    """中断/续写的分片定位参数。"""

    分片: int = 1
    remark: str | None = None


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("")
def list_entries(scope: str | None = Query(default=None)) -> dict[str, Any]:
    """列表与详情共用同一套范围裁剪口径。"""
    return {"items": service.get_timeline(scope=scope)}


@router.get("/export")
def export_entries(scope: str | None = Query(default=None, description="允许查看的实验范围，逗号分隔")) -> StreamingResponse:
    """下载导出作业清单：只包含允许范围内的任务，元数据随文件一并给出。"""
    items, allowed, calibers = service.export_rows(scope=scope)
    has_caliber = bool(calibers)
    columns = [*LIST_FIELDS, *(["指标口径"] if has_caliber else []), "实验范围"]
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["# 清单版本", "V012"])
    writer.writerow(["# 允许范围", ",".join(allowed) if allowed else "全部（未声明范围）"])
    writer.writerow(["# 字段清单", ",".join(columns)])
    writer.writerow(["# 指标口径", json.dumps(calibers, ensure_ascii=False) if has_caliber else ""])
    writer.writerow(columns)
    for item in items:
        writer.writerow([csv_cell(item.get(column, "")) for column in columns])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=data_export.csv",
            "X-Export-Scope": ",".join(allowed),
        })


# ---- V012 清单生成与事务 ----

@router.post("/manifests")
def generate_manifest(payload: ManifestPayload) -> ActionResult:
    manifest, message = service.generate_manifest(payload.values)
    if manifest is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=manifest)


@router.get("/manifests")
def list_manifests(scope: str | None = Query(default=None)) -> dict[str, Any]:
    return {"items": service.list_manifests(scope=scope), "清单版本": "V012"}


@router.get("/manifests/archive")
def list_archive(batch: str | None = Query(default=None)) -> dict[str, Any]:
    return {"items": service.list_archive(batch=batch), "清单版本": "V012"}


@router.get("/manifests/{batch}")
def get_manifest(batch: str, scope: str | None = Query(default=None)) -> dict[str, Any]:
    manifest = service.get_manifest_detail(batch, scope=scope)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"导出批次 {batch} 没有清单")
    return manifest


@router.get("/manifests/{batch}/verify")
def verify_manifest(batch: str, scope: str | None = Query(default=None)) -> dict[str, Any]:
    return service.verify_manifest(batch, scope=scope)


@router.post("/manifests/{batch}/files/{file_name}/interrupt")
def interrupt_shard(batch: str, file_name: str, payload: ShardPayload) -> ActionResult:
    file, message = service.interrupt_shard(batch, file_name, payload.分片, reason=payload.remark or "导出中断")
    if file is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"文件名": file["文件名"], "分片": payload.分片})


@router.post("/manifests/{batch}/resume")
def resume_manifest(batch: str, scope: str | None = Query(default=None)) -> ActionResult:
    manifest, message = service.resume_manifest(batch, scope=scope)
    if manifest is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=manifest)


@router.get("/manifests/{batch}/files/{file_name}/download")
def download_file(batch: str, file_name: str, scope: str | None = Query(default=None)) -> StreamingResponse:
    name, text, error = service.render_file(batch, file_name, scope=scope)
    if error or text is None:
        raise HTTPException(status_code=404, detail=error or "导出文件不存在")
    return StreamingResponse(
        iter([text]), media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Export-Version": "V012",
            "X-Export-Batch": batch,
        })


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"导出任务 {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="导出任务已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry_or_none, message = service.run_action(entry_id, payload.action)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)
