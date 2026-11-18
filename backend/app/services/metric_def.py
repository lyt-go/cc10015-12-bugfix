"""指标定义业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.metric_catalog import metric_by_key, normalize_precision
from app.store import store

MODULE = "metric_def"
LIST_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "基准取值", "精度位数", "口径版本"]
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []


class MetricDefService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("指标编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标定义可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"指标定义已{action}"

    def check_export_calibers(self, fields: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """V012 跨模块核对：用清单里的指标键和精度位数反查字段标签。

        ``fields`` 为导出文件清单中的字段条目（含字段名/字段标签/指标键/精度位数）；
        未提供时退化为核对全部指标定义自己的口径完整性。
        """
        issues: list[dict[str, Any]] = []
        checked = 0
        for field in fields or []:
            metric_key = str(field.get("指标键") or "").strip()
            if not metric_key:
                continue
            checked += 1
            metric = metric_by_key(metric_key)
            if metric is None:
                issues.append({
                    "指标键": metric_key,
                    "字段名": field.get("字段名"),
                    "问题": "指标定义中不存在该指标键",
                })
                continue
            if str(field.get("精度位数")) != str(normalize_precision(metric.get("精度位数"))):
                issues.append({
                    "指标键": metric_key,
                    "字段名": field.get("字段名"),
                    "问题": "精度位数与指标定义不一致",
                    "清单值": field.get("精度位数"),
                    "指标定义值": normalize_precision(metric.get("精度位数")),
                })
            if field.get("字段标签") and str(field.get("字段标签")) != str(metric.get("字段标签")):
                issues.append({
                    "指标键": metric_key,
                    "字段名": field.get("字段名"),
                    "问题": "字段标签与指标定义不一致",
                    "清单值": field.get("字段标签"),
                    "指标定义值": metric.get("字段标签"),
                })
        if fields is None:
            for row in store.rows(MODULE):
                checked += 1
                precision = normalize_precision(row.get("精度位数"), default=-1)
                if not isinstance(precision, int) or precision < 0:
                    issues.append({
                        "指标键": row.get("指标编号"),
                        "问题": "精度位数不是有效的非负整数",
                        "指标定义值": row.get("精度位数"),
                    })
        return {
            "ok": not issues,
            "核对版本": "V012",
            "核对字段数": checked,
            "问题": issues,
        }
