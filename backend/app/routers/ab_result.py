"""AB结果分析 分析看板接口：汇总指标、参数运行与按范围裁剪的结果导出。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.schemas import ActionResult, EntryPayload
from app.services.ab_result import EXPORTABLE_FIELDS, AbResultService
from app.services.metric_catalog import normalize_precision

router = APIRouter(prefix="/api/ab_result", tags=["AB结果分析"])
service = AbResultService()
LIST_FIELDS = ["报告编号", "实验编号", "指标编号", "分析指标", "对照组均值", "实验组均值", "置信水平", "效果方向", "生成时间"]
STATUSES = ["待分析", "计算中", "已完成", "已归档"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("/summary")
def get_summary(scope: str | None = Query(default=None)) -> dict[str, Any]:
    """看板卡片与列表/详情共用同一套稳定结果，按允许范围裁剪。"""
    return service.get_summary(scope=[scope] if scope else None)


@router.get("")
def list_results(scope: str | None = Query(default=None)) -> dict[str, Any]:
    return {"items": service.list_results(scope=[scope] if scope else None)}


@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    return {"items": service.run_analysis(payload.values)}


@router.get("/export")
def export_entries(scope: str | None = Query(default=None, description="允许查看的实验范围，逗号分隔")) -> StreamingResponse:
    """下载结果清单：只含允许范围内的记录，字段清单与指标口径写入文件头。"""
    results = service.list_results(scope=[scope] if scope else None)
    columns = ["id", *EXPORTABLE_FIELDS]
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["# 清单版本", "V012"])
    writer.writerow(["# 实验范围", scope or "全部"])
    writer.writerow(["# 字段清单", ",".join(columns)])
    writer.writerow(columns)
    for row in results:
        caliber = row.get("指标口径") or {}
        precision = normalize_precision(caliber.get("精度位数"))
        values = [row.get("id")]
        for column in EXPORTABLE_FIELDS:
            value = row.get(column, "")
            if column in {"指标值", "对照组均值", "实验组均值", "差异"} and isinstance(value, (int, float)):
                value = round(value, precision) if isinstance(precision, int) else value
            values.append(value)
        writer.writerow([csv_cell(value) for value in values])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=ab_result.csv",
            "X-Export-Scope": scope or "",
            "X-Export-Version": "V012",
        })


@router.get("/{entry_id}")
def get_entry(entry_id: int, scope: str | None = Query(default=None)) -> dict:
    """详情可直接打开并回查；与列表同口径裁剪，跨范围记录不可见。"""
    entry = service.get_result(entry_id, scope=[scope] if scope else None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分析报告 {entry_id} 不存在或不在允许的实验范围内")
    return entry
