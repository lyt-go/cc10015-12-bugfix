"""实验范围（数据归属）解析与裁剪工具。

导出任务、AB 结果和导出文件清单都使用同一套范围口径：
- 范围值允许用中英文逗号、分号、斜杠、竖线或空白分隔；
- 比较前统一去掉首尾空白并转大写，避免 ``expe-0001`` 与 ``EXPE-0001`` 被当成两个范围；
- 裁剪只产生新的列表/字典副本，绝不修改共享数据本身。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

_SPLIT_PATTERN = re.compile(r"[,，;；/|、\s]+")


def parse_scope(value: Any) -> list[str]:
    """把任务上的范围声明解析成去重后的范围列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_parts: list[str] = []
        for item in value:
            raw_parts.extend(_SPLIT_PATTERN.split(str(item)))
    else:
        raw_parts = _SPLIT_PATTERN.split(str(value))
    seen: set[str] = set()
    scopes: list[str] = []
    for part in raw_parts:
        code = part.strip().upper()
        if code and code not in seen:
            seen.add(code)
            scopes.append(code)
    return scopes


def row_scope(row: dict[str, Any]) -> str:
    """读取一条业务记录所属的实验范围。"""
    for key in ("实验编号", "实验范围", "数据范围"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip().upper()
    return ""


def in_scope(row: dict[str, Any], allowed: Iterable[str]) -> bool:
    """记录是否落在允许范围内；没有声明范围时视为不可导出。"""
    allowed_set = {item.upper() for item in allowed}
    if not allowed_set:
        return False
    return row_scope(row) in allowed_set


def clip_rows(rows: Iterable[dict[str, Any]], allowed: Iterable[str]) -> list[dict[str, Any]]:
    """按范围裁剪记录，返回浅拷贝列表，不触碰原始共享数据。"""
    allowed_set = {item.upper() for item in allowed}
    return [dict(row) for row in rows if row_scope(row) in allowed_set]


def intersect(left: Iterable[str], right: Iterable[str]) -> list[str]:
    """求两个范围集合的交集，保留 ``left`` 的原始顺序。"""
    right_set = {item.upper() for item in right}
    seen: set[str] = set()
    result: list[str] = []
    for item in left:
        code = item.upper()
        if code in right_set and code not in seen:
            seen.add(code)
            result.append(item)
    return result
