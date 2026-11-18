"""指标口径目录：把指标定义整理成导出清单可复用的口径条目。

导出任务生成文件清单、AB 结果标注指标键、指标定义反向核对清单，
都从这里取同一份口径（指标键、字段标签、精度位数），避免三处各写一套。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "metric_def"


def normalize_precision(value: Any, default: int = 3) -> int | str:
    """精度位数统一成整数；历史数据里的非数字值保留原文，供核对时提示。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value >= 0 else default
    text = str(value).strip() if value is not None else ""
    if not text:
        return default
    try:
        number = int(text)
    except ValueError:
        return text
    return number if number >= 0 else default


def metric_catalog() -> list[dict[str, Any]]:
    """按指标定义顺序返回口径条目。"""
    catalog: list[dict[str, Any]] = []
    for row in store.rows(MODULE):
        metric_key = str(row.get("指标编号") or "").strip()
        if not metric_key:
            continue
        catalog.append({
            "指标键": metric_key,
            "字段标签": str(row.get("指标名称") or metric_key).strip(),
            "精度位数": normalize_precision(row.get("精度位数")),
            "口径版本": row.get("口径版本"),
            "状态": row.get("status"),
        })
    return catalog


def metric_by_key(metric_key: str) -> dict[str, Any] | None:
    for item in metric_catalog():
        if item["指标键"].upper() == str(metric_key).strip().upper():
            return item
    return None


def fallback_metric(metric_key: str, label: str = "") -> dict[str, Any]:
    """指标定义缺失时给出可回查的占位口径，并标记来源，便于核对时发现。"""
    return {
        "指标键": metric_key,
        "字段标签": label or metric_key,
        "精度位数": 3,
        "口径版本": None,
        "状态": "未登记",
    }
