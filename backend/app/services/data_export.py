"""数据导出 业务规则：导出任务、实验范围裁剪与 V012 导出文件清单。

清单生成是一条事务：先在暂存区拼装文件与分片，全部成功后再一次性发布；
任何环节异常都按 V012 回滚，撤销暂存分片并清理残留，同时留档供核对。
已发布的文件只允许续写缺失分片，不能回填越权记录，也不能改写既有分片。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from app.services.ab_result import AbResultService
from app.services.metric_catalog import fallback_metric, metric_by_key, normalize_precision
from app.services.scoping import in_scope, intersect, parse_scope, row_scope
from app.store import store

MODULE = "data_export"
MANIFEST_MODULE = "data_export_manifest"
ARCHIVE_MODULE = "data_export_manifest_archive"
MANIFEST_VERSION = "V012"
REQUIRED_FIELDS = ["任务编号", "数据来源", "导出格式"]
LIST_FIELDS = ["任务编号", "数据来源", "导出格式", "字段清单", "数据范围", "压缩方式", "分片大小", "导出批次"]
STATUS_ORDER = ["草稿", "导出中", "已完成", "已取消"]
ACTION_RULES = {"开始导出": "导出中", "确认完成": "已完成", "取消导出": "已取消"}
NEGATIVE_ACTIONS = []

STRATEGY_SPLIT = "按范围拆分"
STRATEGY_MERGE = "合并文件"
STRATEGIES = [STRATEGY_SPLIT, STRATEGY_MERGE]

EXPORT_FIELDS = [
    "报告编号", "实验编号", "指标编号", "指标值",
    "对照组均值", "实验组均值", "差异", "置信水平", "效果方向", "生成时间",
]
METRIC_FIELDS = {"指标值", "对照组均值", "实验组均值", "差异"}
GENERIC_LABELS = {
    "报告编号": "报告编号",
    "实验编号": "实验编号",
    "指标编号": "指标编号",
    "置信水平": "置信水平",
    "效果方向": "效果方向",
    "生成时间": "生成时间",
}
DEFAULT_SHARD_SIZE = 500

# 暂存区：批次 -> 已写入的临时分片标识；事务失败时按这里清理残留。
_STAGING: dict[str, list[str]] = {}
# 暂存区归属：批次 -> 允许的实验范围，回滚留档时按归属裁剪。
_STAGING_SCOPES: dict[str, list[str]] = {}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_name(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z一-鿿_.-]+", "_", text) or "all"


def _checksum(records: list[dict[str, Any]]) -> str:
    payload = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_metric_keys(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        parts: list[str] = []
        for item in value:
            parts.extend(re.split(r"[,，;；/|、\s]+", str(item)))
    else:
        parts = re.split(r"[,，;；/|、\s]+", str(value))
    seen: set[str] = set()
    keys: list[str] = []
    for part in parts:
        key = part.strip()
        if key and key.upper() not in seen:
            seen.add(key.upper())
            keys.append(key)
    return keys


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


class DataExportService:
    fault_on_generate: bool = False

    # ---------- 导出任务（时间线） ----------

    def get_timeline(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        allowed = parse_scope(scope) if scope else []
        today = date.today()
        items: list[dict] = []
        for i, row in enumerate(rows):
            row_scopes = parse_scope(row.get("数据范围"))
            if allowed and not (set(row_scopes) & set(allowed)):
                # 所属范围完全在允许范围之外：列表页不展示，也无法被改动。
                continue
            visible_scopes = intersect(row_scopes, allowed) if allowed else row_scopes
            start = (today - timedelta(days=7 - i * 2)).isoformat()
            end = (today + timedelta(days=3 - i)).isoformat()
            items.append({
                "id": int(row.get("id", i + 1)),
                "name": str(row.get("任务编号") or row.get("status") or f"导出任务{i + 1}"),
                "start": start,
                "end": end,
                "status": str(row.get("status", STATUS_ORDER[i % len(STATUS_ORDER)])),
                "logs": f"[{start}] 作业启动\n[{start}] 正在处理...\n[{end}] 任务{'完成' if row.get('status') == STATUS_ORDER[-1] else '进行中'}",
                "实验范围": visible_scopes,
                "字段清单": _parse_metric_keys(row.get("字段清单")),
            })
        return items

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        for field in LIST_FIELDS:
            entry.setdefault(field, values.get(field, ""))
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["created_at"] = datetime.now().timestamp()
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"导出任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据导出可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"导出任务已{action}"

    # ---------- 任务清单导出（按范围裁剪） ----------

    def export_rows(self, *, scope: str | None = None) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
        """返回任务清单行、允许范围与指标口径；范围外记录不会出现在结果里。"""
        allowed = parse_scope(scope) if scope else []
        rows = store.rows(MODULE)
        metric_keys: list[str] = []
        items: list[dict[str, Any]] = []
        for row in rows:
            if allowed and not in_scope(row, allowed):
                continue
            row_scope_value = parse_scope(row.get("数据范围"))
            if allowed and not set(row_scope_value) <= set(allowed):
                # 任务自身范围跨出允许范围时，只保留交集，避免把越权范围写进清单。
                visible = intersect(row_scope_value, allowed)
                if not visible:
                    continue
            else:
                visible = row_scope_value
            keys = _parse_metric_keys(row.get("字段清单"))
            metric_keys.extend(k for k in keys if k.upper() not in {m.upper() for m in metric_keys})
            item = {field: row.get(field, "") for field in LIST_FIELDS}
            item["id"] = row.get("id")
            item["实验范围"] = ",".join(visible)
            item["指标口径"] = ",".join(self._caliber_label(key) for key in keys)
            items.append(item)
        caliber = [self._caliber_entry(key) for key in metric_keys]
        return items, allowed, caliber

    @staticmethod
    def _caliber_entry(metric_key: str) -> dict[str, Any]:
        metric = metric_by_key(metric_key) or fallback_metric(metric_key)
        return {
            "指标键": metric["指标键"],
            "字段标签": metric["字段标签"],
            "精度位数": metric["精度位数"],
            "口径版本": metric.get("口径版本"),
        }

    @staticmethod
    def _caliber_label(metric_key: str) -> str:
        metric = metric_by_key(metric_key) or fallback_metric(metric_key)
        return f"{metric['指标键']}@{metric['精度位数']}"

    # ---------- V012 清单生成 ----------

    def _find_task(self, values: dict[str, Any]) -> dict[str, Any] | None:
        code = str(values.get("任务编号") or "").strip()
        if code:
            for row in store.rows(MODULE):
                if str(row.get("任务编号")) == code:
                    return row
            return None
        rows = store.rows(MODULE)
        return rows[0] if rows else None

    def _build_fields(self, metric_keys: list[str]) -> list[dict[str, Any]]:
        """字段清单条目：普通字段直接给标签，指标字段逐个携带指标键与精度位数。"""
        calibers = [self._caliber_entry(key) for key in metric_keys]
        fields: list[dict[str, Any]] = []
        for name in EXPORT_FIELDS:
            if name in METRIC_FIELDS:
                if calibers:
                    for caliber in calibers:
                        fields.append({
                            "字段名": name,
                            "字段标签": caliber["字段标签"],
                            "指标键": caliber["指标键"],
                            "精度位数": caliber["精度位数"],
                        })
                else:
                    fields.append({
                        "字段名": name,
                        "字段标签": GENERIC_LABELS.get(name, name),
                        "指标键": None,
                        "精度位数": None,
                    })
            else:
                fields.append({
                    "字段名": name,
                    "字段标签": GENERIC_LABELS.get(name, name),
                    "指标键": None,
                    "精度位数": None,
                })
        return fields

    def _records_for(
        self,
        results: list[dict[str, Any]],
        scope: str,
        metric_keys: list[str],
    ) -> list[dict[str, Any]]:
        """取某个实验范围内的记录；metric_keys 只决定口径标注，不做记录过滤。"""
        precision_by_key = {
            str(item["指标键"]).upper(): item["精度位数"]
            for item in (self._caliber_entry(key) for key in metric_keys)
        }
        records: list[dict[str, Any]] = []
        for row in results:
            if row_scope(row) != scope.upper():
                continue
            metric_key = str(row.get("指标编号") or "").strip()
            precision = precision_by_key.get(metric_key.upper(), 3)
            metric_value = float(row.get("指标值") or 0.0)
            baseline = float(row.get("对比基准") or 0.0)
            diff = round(metric_value - baseline, 3)
            if isinstance(precision, int):
                metric_value = round(metric_value, precision)
                baseline = round(baseline, precision)
                diff = round(diff, precision)
            records.append({
                "报告编号": row.get("报告编号", ""),
                "实验编号": row.get("实验编号", scope),
                "指标编号": metric_key,
                "指标值": metric_value,
                "对照组均值": baseline,
                "实验组均值": metric_value,
                "差异": diff,
                "置信水平": row.get("置信水平", "95%"),
                "效果方向": "提升" if diff >= 0 else "下降",
                "生成时间": row.get("生成时间", ""),
            })
        return records

    def generate_manifest(self, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        """生成 V012 导出文件清单；失败时回滚并清理残留。"""
        values = values or {}
        batch = ""
        try:
            task = self._find_task(values)
            if task is None:
                return None, "没有可用于生成清单的导出任务"
            batch = str(values.get("导出批次") or task.get("导出批次") or "").strip()
            if not batch:
                return None, "缺少导出批次，无法生成可回查的文件清单"
            if self._manifest_of(batch) is not None:
                return None, f"导出批次 {batch} 已存在已发布清单，不能改写；请续写缺失分片或使用新批次"

            task_scope = parse_scope(task.get("数据范围"))
            requested = parse_scope(values.get("数据范围")) or task_scope
            if not requested:
                return None, "导出任务未声明实验范围，拒绝生成清单"
            # 先用任务自身范围收口；若调用方带了允许范围，再与之取交集。
            allowed = intersect(requested, task_scope) if task_scope else list(requested)
            viewer_scope = parse_scope(values.get("允许范围"))
            if viewer_scope:
                allowed = intersect(allowed, viewer_scope)
            excluded = [code for code in requested if code not in allowed]
            if not allowed:
                return None, "请求的实验范围不在允许查看的范围内"

            strategy = str(values.get("拆分策略") or STRATEGY_SPLIT).strip()
            if strategy not in STRATEGIES:
                return None, f"不支持的拆分策略「{strategy}」，可选：{'、'.join(STRATEGIES)}"

            metric_keys = _parse_metric_keys(values.get("字段清单") or task.get("字段清单"))
            fields = self._build_fields(metric_keys)
            calibers = [self._caliber_entry(key) for key in metric_keys]
            shard_size = _positive_int(values.get("分片大小") or task.get("分片大小"), DEFAULT_SHARD_SIZE)
            results = AbResultService().list_results()

            # 逐范围取数：范围外记录在任何阶段都不会进入暂存区。
            records_by_scope = {code: self._records_for(results, code, metric_keys) for code in allowed}
            excluded_records = sum(
                1 for row in results if row_scope(row) not in set(allowed) and row_scope(row) in set(requested)
            )

            staging: list[str] = []
            _STAGING[batch] = staging
            _STAGING_SCOPES[batch] = list(allowed)
            now = _now()
            if strategy == STRATEGY_SPLIT:
                file_specs = [(f"data_export_{_safe_name(batch)}_{_safe_name(code)}.json", [code]) for code in allowed]
            else:
                file_specs = [(f"data_export_{_safe_name(batch)}_merged.json", list(allowed))]

            files: list[dict[str, Any]] = []
            all_partitions: list[dict[str, Any]] = []
            for file_name, file_scopes in file_specs:
                records: list[dict[str, Any]] = []
                partitions: list[dict[str, Any]] = []
                for code in file_scopes:
                    scope_records = records_by_scope.get(code, [])
                    start = len(records) + 1
                    records.extend(scope_records)
                    partitions.append({
                        "实验范围": code,
                        "起始记录": start,
                        "结束记录": len(records),
                        "记录数": len(scope_records),
                    })
                shards: list[dict[str, Any]] = []
                for offset in range(0, len(records), shard_size):
                    chunk = records[offset:offset + shard_size]
                    shard = {
                        "序号": len(shards) + 1,
                        "记录数": len(chunk),
                        "状态": "已写入",
                        "校验": _checksum(chunk),
                        "记录": chunk,
                    }
                    shards.append(shard)
                    staging.append(f"{file_name}#{shard['序号']}")
                metadata = {
                    "清单版本": MANIFEST_VERSION,
                    "导出批次": batch,
                    "文件名": file_name,
                    "实验范围": file_scopes,
                    "字段清单": [field["字段名"] for field in fields],
                    "指标口径": calibers,
                    "范围分区": partitions,
                    "数据来源": task.get("数据来源", ""),
                    "导出格式": "JSON",
                    "生成时间": now,
                }
                files.append({
                    "文件名": file_name,
                    "实验范围": file_scopes,
                    "导出格式": "JSON",
                    "记录数": len(records),
                    "分片大小": shard_size,
                    "字段清单": [field["字段名"] for field in fields],
                    "指标口径": calibers,
                    "范围分区": partitions,
                    "分片": shards,
                    "发布状态": "已发布",
                    "元数据": metadata,
                })
                all_partitions.extend(partitions)

            if values.get("注入异常") or self.fault_on_generate:
                raise RuntimeError("分片写入被模拟中断")

            manifest = {
                "id": store.next_id(MANIFEST_MODULE),
                "清单编号": f"MF-{MANIFEST_VERSION}-{store.next_id(MANIFEST_MODULE):04d}",
                "清单版本": MANIFEST_VERSION,
                "导出批次": batch,
                "生成时间": now,
                "拆分策略": strategy,
                "请求范围": requested,
                "允许范围": allowed,
                "排除范围": excluded,
                "版本冲突": len(allowed) > 1,
                "冲突处理": "两个实验范围拆成独立范围文件" if strategy == STRATEGY_SPLIT and len(allowed) > 1
                else ("合并为一个文件并附范围分区" if len(allowed) > 1 else "单一实验范围，无需裁决"),
                "字段清单": fields,
                "指标口径": calibers,
                "文件清单": files,
                "分区边界": all_partitions,
                "记录总数": sum(file["记录数"] for file in files),
                "排除记录数": excluded_records,
                "发布状态": "已发布",
                "事务": {"清单版本": MANIFEST_VERSION, "状态": "已提交", "回滚": "未触发"},
                "续写记录": [],
            }
            store.rows(MANIFEST_MODULE).append(manifest)
            _STAGING.pop(batch, None)
            _STAGING_SCOPES.pop(batch, None)
            self._archive(batch, "已提交", "清单生成完成，按 V012 留档核对", [], manifest, allowed)
            message = f"清单 {manifest['清单编号']} 已生成，共 {len(files)} 个文件、{manifest['记录总数']} 条记录"
            if excluded:
                message += f"；已排除范围外请求：{'、'.join(excluded)}"
            if len(allowed) > 1:
                message += f"；检测到同批次多实验范围，已{manifest['冲突处理']}"
            return manifest, message
        except Exception as exc:  # noqa: BLE001 - 事务异常统一回滚
            cleaned = self.rollback_manifest(batch or "", reason=str(exc))
            return None, f"清单生成失败，已按 V012 回滚并清理 {len(cleaned)} 处残留：{exc}"

    def rollback_manifest(self, batch: str, *, reason: str = "清单生成事务异常") -> list[str]:
        """回滚未发布清单：撤销暂存分片、清理半成品文件并留档。"""
        cleaned = list(_STAGING.pop(batch, []))
        scopes = _STAGING_SCOPES.pop(batch, [])
        if batch:
            rows = store.rows(MANIFEST_MODULE)
            for row in [r for r in rows if r.get("导出批次") == batch and r.get("发布状态") != "已发布"]:
                cleaned.append(str(row.get("清单编号") or row.get("导出批次")))
                rows.remove(row)
        self._archive(batch or "UNKNOWN", "已回滚", reason, cleaned, None, scopes)
        return cleaned

    def _archive(
        self,
        batch: str,
        status: str,
        reason: str,
        cleaned: list[str],
        manifest: dict[str, Any] | None,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        archive_rows = store.rows(ARCHIVE_MODULE)
        visible = list(scopes or (manifest or {}).get("允许范围") or [])
        row = {
            "id": max((int(r.get("id", 0)) for r in archive_rows), default=0) + 1,
            "清单版本": MANIFEST_VERSION,
            "导出批次": batch,
            "清单编号": manifest.get("清单编号") if manifest else None,
            "实验范围": visible,
            "状态": status,
            "原因": reason,
            "清理残留": cleaned,
            "留档时间": _now(),
            "核对项": ["清单版本", "实验范围", "字段清单", "指标口径", "分片校验"],
        }
        archive_rows.append(row)
        return row

    # ---------- 清单读取（按范围裁剪） ----------

    def _manifest_of(self, batch: str) -> dict[str, Any] | None:
        for row in store.rows(MANIFEST_MODULE):
            if row.get("导出批次") == batch:
                return row
        return None

    def get_manifest(self, batch: str) -> dict[str, Any] | None:
        return self._manifest_of(batch)

    def list_manifests(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        allowed = parse_scope(scope) if scope else None
        manifests: list[dict[str, Any]] = []
        for row in store.rows(MANIFEST_MODULE):
            if allowed is not None:
                visible = [file for file in row["文件清单"] if set(file["实验范围"]) & set(allowed)]
                if not visible:
                    continue
                copy = dict(row)
                copy["文件清单"] = [
                    self._public_file(file, intersect(file["实验范围"], allowed)) for file in visible
                ]
                copy["分区边界"] = [
                    part for part in row.get("分区边界", []) if part["实验范围"] in set(allowed)
                ]
                copy["可见范围"] = allowed
                manifests.append(copy)
            else:
                copy = dict(row)
                copy["文件清单"] = [self._public_file(file) for file in row["文件清单"]]
                manifests.append(copy)
        return manifests

    @staticmethod
    def _public_file(file: dict[str, Any], visible: list[str] | None = None) -> dict[str, Any]:
        """清单视图不携带记录内容；给了可见范围时再按范围分区裁剪。"""
        view = {key: value for key, value in file.items() if key != "分片"}
        if visible is not None:
            view["可见范围"] = visible
            view["范围分区"] = [
                part for part in file.get("范围分区", []) if part["实验范围"] in set(visible)
            ]
        view["分片"] = [
            {key: value for key, value in shard.items() if key != "记录"}
            for shard in file.get("分片", [])
        ]
        return view

    def get_manifest_detail(self, batch: str, *, scope: str | None = None) -> dict[str, Any] | None:
        manifest = self._manifest_of(batch)
        if manifest is None:
            return None
        allowed = parse_scope(scope) if scope else None
        copy = dict(manifest)
        copy["文件清单"] = []
        for file in manifest["文件清单"]:
            if allowed is not None and not (set(file["实验范围"]) & set(allowed)):
                continue
            visible = intersect(file["实验范围"], allowed) if allowed is not None else None
            copy["文件清单"].append(self._public_file(file, visible))
        if allowed is not None:
            copy["分区边界"] = [part for part in manifest.get("分区边界", []) if part["实验范围"] in set(allowed)]
            copy["可见范围"] = allowed
        return copy

    # ---------- 文件读取 / 下载 ----------

    def find_file(self, batch: str, file_name: str) -> dict[str, Any] | None:
        manifest = self._manifest_of(batch)
        if manifest is None:
            return None
        for file in manifest["文件清单"]:
            if file["文件名"] == file_name:
                return file
        return None

    def render_file(
        self,
        batch: str,
        file_name: str,
        *,
        scope: str | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        """返回 (文件名, JSON 文本, 错误说明)。文件内嵌字段清单、实验归属与指标口径。"""
        manifest = self._manifest_of(batch)
        file = self.find_file(batch, file_name)
        if manifest is None or file is None:
            return None, None, f"导出文件 {file_name} 不存在"
        allowed = parse_scope(scope) if scope else []
        visible_scopes = file["实验范围"]
        if allowed:
            visible_scopes = intersect(file["实验范围"], allowed)
            if not visible_scopes:
                return None, None, f"导出文件 {file_name} 不在允许的实验范围内"
        shards = file.get("分片", [])
        missing = [shard["序号"] for shard in shards if shard.get("状态") != "已写入"]
        if missing:
            return None, None, f"文件 {file_name} 存在缺失分片 {missing}，请先续写再下载"
        visible_scope_set = {code.upper() for code in visible_scopes}
        records: list[dict[str, Any]] = []
        for shard in shards:
            for record in shard.get("记录", []):
                if str(record.get("实验编号", "")).upper() not in visible_scope_set:
                    continue
                records.append(record)
        metadata = dict(file["元数据"])
        if allowed:
            metadata["实验范围"] = visible_scopes
            metadata["文件完整范围"] = file["实验范围"]
            metadata["范围分区"] = [
                part for part in file.get("范围分区", []) if part["实验范围"] in set(visible_scopes)
            ]
            metadata["本次查看范围"] = visible_scopes
        metadata["记录数"] = len(records)
        payload = {
            "metadata": metadata,
            "fields": file["字段清单"] and [
                field for field in manifest["字段清单"]
                if field["字段名"] in set(file["字段清单"])
            ],
            "records": records,
        }
        return file_name, json.dumps(payload, ensure_ascii=False, indent=2), None

    # ---------- 中断 / 续写 ----------

    def interrupt_shard(
        self,
        batch: str,
        file_name: str,
        shard_index: int,
        *,
        reason: str = "导出中断",
    ) -> tuple[dict[str, Any] | None, str]:
        file = self.find_file(batch, file_name)
        if file is None:
            return None, f"导出文件 {file_name} 不存在"
        for shard in file.get("分片", []):
            if int(shard.get("序号", 0)) == shard_index:
                if shard.get("状态") == "缺失":
                    return file, f"分片 {shard_index} 已是缺失状态"
                shard["原校验"] = shard.get("校验")
                shard["状态"] = "缺失"
                shard["记录"] = []
                shard["记录数"] = 0
                shard["校验"] = None
                self._archive(batch, "中断留档", reason, [f"{file_name}#{shard_index}"], self._manifest_of(batch))
                return file, f"已标记 {file_name} 分片 {shard_index} 缺失，可续写"
        return None, f"分片 {shard_index} 不存在"

    def resume_manifest(self, batch: str, *, scope: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """只续写缺失分片；已发布分片保持原样，越权记录一律拒绝写入。"""
        manifest = self._manifest_of(batch)
        if manifest is None:
            return None, f"导出批次 {batch} 没有可续写的清单"
        allowed = parse_scope(scope) if scope else []
        results = AbResultService().list_results()
        metric_keys = _parse_metric_keys([field["指标键"] for field in manifest["字段清单"] if field["指标键"]])
        shard_size = DEFAULT_SHARD_SIZE
        restored = 0
        resumed: list[dict[str, Any]] = []
        for file in manifest["文件清单"]:
            if allowed and not (set(file["实验范围"]) & set(allowed)):
                continue
            missing = [shard for shard in file.get("分片", []) if shard.get("状态") != "已写入"]
            if not missing:
                continue
            file_shard_size = _positive_int(file.get("分片大小"), DEFAULT_SHARD_SIZE)
            rebuilt_by_scope = {code: self._records_for(results, code, metric_keys) for code in file["实验范围"]}
            rebuilt: list[dict[str, Any]] = []
            for code in file["实验范围"]:
                rebuilt.extend(rebuilt_by_scope[code])
            stray = [row for row in rebuilt if str(row.get("实验编号", "")).upper() not in set(file["实验范围"])]
            if stray:
                return None, f"文件 {file['文件名']} 续写数据包含范围外记录，已拒绝写入"
            for shard in missing:
                index = int(shard["序号"])
                chunk = rebuilt[(index - 1) * file_shard_size:index * file_shard_size]
                shard["记录"] = chunk
                shard["记录数"] = len(chunk)
                shard["状态"] = "已写入"
                shard["校验"] = _checksum(chunk)
                restored += 1
                resumed.append({
                    "时间": _now(),
                    "文件": file["文件名"],
                    "分片": index,
                    "写入记录": len(chunk),
                    "说明": "仅续写缺失分片，已发布分片未改动",
                })
        if restored == 0:
            return manifest, "没有缺失分片，无需续写"
        manifest.setdefault("续写记录", []).extend(resumed)
        self._archive(batch, "已续写", f"续写 {restored} 个缺失分片", [], manifest)
        return manifest, f"已续写 {restored} 个缺失分片，已发布分片保持不变"

    # ---------- 核对 ----------

    def verify_manifest(self, batch: str, *, scope: str | None = None) -> dict[str, Any]:
        """按 V012 核对清单：归属、字段清单、指标口径、分区与分片校验。"""
        manifest = self._manifest_of(batch)
        if manifest is None:
            return {"ok": False, "清单版本": MANIFEST_VERSION, "message": f"导出批次 {batch} 没有清单"}
        allowed = parse_scope(scope) if scope else None
        issues: list[str] = []
        checked_files = 0
        checked_records = 0
        for file in manifest["文件清单"]:
            if allowed is not None and not (set(file["实验范围"]) & set(allowed)):
                continue
            checked_files += 1
            visible_scopes = set(intersect(file["实验范围"], allowed)) if allowed is not None else set(file["实验范围"])
            field_names = list(file["字段清单"])
            expected = [field["字段名"] for field in manifest["字段清单"]]
            if field_names != expected:
                issues.append(f"{file['文件名']} 字段清单与清单声明不一致")
            metadata = file.get("元数据", {})
            if not metadata.get("实验范围") or not metadata.get("字段清单") or not metadata.get("指标口径"):
                issues.append(f"{file['文件名']} 元数据缺少实验归属、字段清单或指标口径")
            for shard in file.get("分片", []):
                if shard.get("状态") != "已写入":
                    issues.append(f"{file['文件名']} 分片 {shard.get('序号')} 缺失")
                    continue
                all_records = shard.get("记录", [])
                if shard.get("校验") != _checksum(all_records):
                    issues.append(f"{file['文件名']} 分片 {shard.get('序号')} 校验不匹配")
                for record in all_records:
                    record_scope = str(record.get("实验编号", "")).upper()
                    if record_scope not in {code.upper() for code in file["实验范围"]}:
                        # 不暴露范围外记录内容，只说明存在越权记录。
                        issues.append(f"{file['文件名']} 分片 {shard.get('序号')} 含不属于本文件的记录")
                    if record_scope not in visible_scopes:
                        continue
                    checked_records += 1
                    if set(record.keys()) != set(field_names):
                        issues.append(f"{file['文件名']} 记录字段与字段清单不一致")
            if len(file["实验范围"]) > 1 and not metadata.get("范围分区"):
                issues.append(f"{file['文件名']} 合并文件缺少范围分区")
        return {
            "ok": not issues,
            "清单版本": MANIFEST_VERSION,
            "清单编号": manifest["清单编号"],
            "导出批次": batch,
            "核对文件数": checked_files,
            "核对记录数": checked_records,
            "问题": issues,
            "核对项": ["实验归属", "字段清单", "指标口径", "范围分区", "分片校验"],
        }

    # ---------- 留档 ----------

    def list_archive(self, *, batch: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(ARCHIVE_MODULE)
        if batch:
            return [row for row in rows if row.get("导出批次") == batch]
        return list(rows)
