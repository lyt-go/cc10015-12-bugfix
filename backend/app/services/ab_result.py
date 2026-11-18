"""AB结果分析 分析看板服务：给出可复现的对比结果，并按实验范围裁剪。

结果值由记录本身（报告编号/实验编号/分析指标）稳定派生，不再每次随机，
这样工作台面板、列表页和详情页看到的是同一份结果；共享的示例数据只做只读访问。
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

from app.services.metric_catalog import fallback_metric, metric_by_key
from app.services.scoping import in_scope, parse_scope, row_scope
from app.store import store

MODULE = "ab_result"
REQUIRED_FIELDS = ["报告编号", "实验编号", "分析指标"]
STATUS_ORDER = ["待分析", "计算中", "已完成", "已归档"]
ACTION_RULES = {"开始计算": "计算中", "确认完成": "已完成", "归档报告": "已归档"}
NEGATIVE_ACTIONS = []
RESULT_FIELDS = ["报告编号", "实验编号", "指标编号", "分析指标", "对照组均值", "实验组均值", "置信水平", "效果方向", "生成时间"]
EXPORTABLE_FIELDS = RESULT_FIELDS


def _digest(seed: str) -> bytes:
    return hashlib.sha256(seed.encode("utf-8")).digest()


def _unit(seed: str, offset: int = 0) -> float:
    """把摘要片段映射到 [0, 1)，用于稳定派生模拟指标。"""
    chunk = _digest(seed)[offset:offset + 4]
    return int.from_bytes(chunk, "big") / 2 ** 32


def _computed_values(row: dict[str, Any]) -> dict[str, float]:
    seed = "|".join(str(row.get(field) or "") for field in ("报告编号", "实验编号", "分析指标"))
    metric_value = round(0.65 + _unit(seed, 0) * 0.33, 3)
    baseline = round(0.60 + _unit(seed, 4) * 0.32, 3)
    return {
        "指标值": metric_value,
        "对比基准": baseline,
        "差异": round(metric_value - baseline, 3),
    }


class AbResultService:
    def list_results(self, *, scope: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """返回裁剪后的结果列表；未指定范围时读取全部（仅限服务内部/管理视图）。"""
        rows = store.rows(MODULE)
        allowed = None if scope is None else parse_scope(list(scope))
        results: list[dict] = []
        for row in rows:
            if allowed is not None and not in_scope(row, allowed):
                continue
            results.append(self._present(row))
        return results

    def _present(self, row: dict[str, Any]) -> dict[str, Any]:
        """组装可展示结果：原始记录 + 稳定派生值 + 指标口径。"""
        item = dict(row)
        values = _computed_values(row)
        item.update(values)
        # 三种视图使用同一组结果值，避免列表与详情各算一套。
        item["对照组均值"] = values["对比基准"]
        item["实验组均值"] = values["指标值"]
        item["效果方向"] = "提升" if values["差异"] >= 0 else "下降"
        metric_key = str(row.get("指标编号") or "").strip()
        if metric_key:
            item["指标口径"] = metric_by_key(metric_key) or fallback_metric(
                metric_key, str(row.get("分析指标") or ""))
        return item

    def get_result(self, entry_id: int, *, scope: Iterable[str] | None = None) -> dict[str, Any] | None:
        """详情与列表使用同一套派生逻辑；给范围时范围外记录不可见。"""
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        if scope is not None:
            allowed = parse_scope(list(scope))
            if not allowed or not in_scope(row, allowed):
                return None
        return self._present(row)

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_summary(self, *, scope: Iterable[str] | None = None) -> dict[str, Any]:
        results = self.list_results(scope=scope)
        if not results:
            return {"metrics": [
                {"label": "覆盖实验", "value": 0, "trend": 0},
                {"label": "最新指标值", "value": 0, "trend": 0},
                {"label": "异常记录", "value": 0, "trend": 0},
            ]}
        experiments = {row_scope(row) for row in results if row_scope(row)}
        values = [float(row["指标值"]) for row in results]
        latest = round(sum(values) / len(values), 3)
        anomalies = sum(1 for row in results if abs(float(row["差异"])) > 0.05)
        return {"metrics": [
            {"label": "覆盖实验", "value": len(experiments), "trend": 0},
            {"label": "最新指标值", "value": latest, "trend": round((latest - 0.8) * 100, 1)},
            {"label": "异常记录", "value": anomalies, "trend": -anomalies},
        ]}

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        count = int(params.get("样本范围", params.get("并行数量", "6")))
        experiment = str(params.get("实验编号") or "").strip() or "EXPE-0001"
        metric_key = str(params.get("指标编号") or "METR-0001").strip()
        metric = metric_by_key(metric_key) or fallback_metric(metric_key, str(params.get("分析指标") or ""))
        results: list[dict] = []
        for i in range(count):
            seed = f"{experiment}|{metric_key}|{params.get('报告编号') or ''}|{i}"
            metric_value = round(0.62 + _unit(seed, 0) * 0.40, 3)
            baseline = round(0.58 + _unit(seed, 4) * 0.37, 3)
            results.append({
                "id": i + 1,
                "分组": f"分组{(i % 3) + 1}",
                "实验编号": experiment,
                "指标编号": metric_key,
                "指标值": metric_value,
                "对比基准": baseline,
                "差异": round(metric_value - baseline, 3),
                "指标口径": metric,
            })
        return results
