"""导出文件清单（V012）：范围裁剪、字段归属、事务回滚与续写。"""
import copy
import json
import sys, os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.seed import SEED_ROWS
from app.services.ab_result import AbResultService
from app.services.data_export import DataExportService
from app.services.metric_def import MetricDefService
from app.store import store


@pytest.fixture(autouse=True)
def _seed_store():
    """其他用例会清空共享仓库，这里恢复示例数据与清单表，保证用例相互独立。"""
    for name, rows in SEED_ROWS.items():
        store._tables[name] = copy.deepcopy(rows)
    store._tables.setdefault("data_export_manifest", []).clear()
    store._tables.setdefault("data_export_manifest_archive", []).clear()
    yield
    for name, rows in SEED_ROWS.items():
        store._tables[name] = copy.deepcopy(rows)
    store._tables["data_export_manifest"] = []
    store._tables["data_export_manifest_archive"] = []


def _reset_export():
    store._tables["data_export_manifest"] = []
    store._tables["data_export_manifest_archive"] = []


def test_export_rows_clip_by_scope():
    """清单下载只保留允许范围内的任务，并带上字段清单与指标口径。"""
    svc = DataExportService()
    items, allowed, calibers = svc.export_rows(scope="EXPE-0001")
    assert allowed == ["EXPE-0001"]
    assert items, "应有允许范围内的导出任务"
    assert all("EXPE-0001" in item["实验范围"] for item in items)
    assert all("EXPE-0002" not in item["实验范围"] for item in items)
    assert calibers and all("精度位数" in item for item in calibers)


def test_timeline_list_hides_out_of_scope_tasks():
    """列表页不再显示范围外任务，跨范围任务只显示交集范围。"""
    svc = DataExportService()
    all_tasks = svc.get_timeline()
    assert len(all_tasks) == 3
    scoped = svc.get_timeline(scope="EXPE-0001")
    assert [item["name"] for item in scoped] == ["DATA-0001", "DATA-0002"]
    merged = [item for item in scoped if item["name"] == "DATA-0002"][0]
    assert merged["实验范围"] == ["EXPE-0001"]
    assert all(item["实验范围"] != ["EXPE-0002"] for item in scoped)


def test_ab_result_detail_matches_list(monkeypatch):
    """看板、列表、详情使用同一套结果值。"""
    svc = AbResultService()
    listed = svc.list_results()
    detail = svc.get_result(listed[0]["id"])
    assert detail["指标值"] == listed[0]["指标值"]
    assert detail["对照组均值"] == listed[0]["对照组均值"]
    assert detail["差异"] == listed[0]["差异"]
    scoped = svc.list_results(scope=["EXPE-0001"])
    assert scoped and all(row["实验编号"] == "EXPE-0001" for row in scoped)


def test_manifest_split_two_scopes():
    """同一批次包含两个实验范围时拆成两个独立文件。"""
    _reset_export()
    svc = DataExportService()
    manifest, message = svc.generate_manifest({
        "任务编号": "DATA-0002", "导出批次": "T-SPLIT", "拆分策略": "按范围拆分",
    })
    assert manifest is not None, message
    files = manifest["文件清单"]
    assert len(files) == 2
    assert {tuple(file["实验范围"]) for file in files} == {("EXPE-0001",), ("EXPE-0002",)}
    assert manifest["版本冲突"] is True
    assert svc.verify_manifest("T-SPLIT")["ok"] is True


def test_manifest_merge_with_partitions():
    """合并文件写出范围分区边界，字段清单与指标口径进入元数据。"""
    _reset_export()
    svc = DataExportService()
    manifest, message = svc.generate_manifest({
        "任务编号": "DATA-0002", "导出批次": "T-MERGE", "拆分策略": "合并文件",
    })
    assert manifest is not None, message
    file = manifest["文件清单"][0]
    assert file["实验范围"] == ["EXPE-0001", "EXPE-0002"]
    assert [part["实验范围"] for part in file["范围分区"]] == ["EXPE-0001", "EXPE-0002"]
    assert file["元数据"]["字段清单"] and file["元数据"]["指标口径"]
    assert file["元数据"]["实验范围"] == ["EXPE-0001", "EXPE-0002"]


def test_manifest_scoped_read_only_visible_scope():
    """共享清单按允许范围裁剪，跨范围部分不可见也不能改动。"""
    _reset_export()
    svc = DataExportService()
    manifest, message = svc.generate_manifest({
        "任务编号": "DATA-0002", "导出批次": "T-SCOPE", "拆分策略": "合并文件",
    })
    assert manifest is not None, message
    before = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    scoped = svc.get_manifest_detail("T-SCOPE", scope="EXPE-0002")
    assert scoped["可见范围"] == ["EXPE-0002"]
    file = scoped["文件清单"][0]
    assert file["可见范围"] == ["EXPE-0002"]
    assert [part["实验范围"] for part in file["范围分区"]] == ["EXPE-0002"]
    assert "分片" in file and all("记录" not in shard for shard in file["分片"])
    assert json.dumps(svc.get_manifest("T-SCOPE"), ensure_ascii=False, sort_keys=True) == before


def test_manifest_download_scoped_and_metadata():
    """下载文件只含获准记录，元数据可回查字段清单、实验范围与指标口径。"""
    _reset_export()
    svc = DataExportService()
    manifest, message = svc.generate_manifest({
        "任务编号": "DATA-0002", "导出批次": "T-DL", "拆分策略": "合并文件",
    })
    assert manifest is not None, message
    file_name = manifest["文件清单"][0]["文件名"]
    _, text, error = svc.render_file("T-DL", file_name, scope="EXPE-0002")
    assert error is None
    payload = json.loads(text)
    assert payload["metadata"]["实验范围"] == ["EXPE-0002"]
    assert payload["metadata"]["字段清单"] == manifest["文件清单"][0]["字段清单"]
    assert payload["metadata"]["指标口径"]
    assert {record["实验编号"] for record in payload["records"]} == {"EXPE-0002"}


def test_manifest_rollback_and_archive():
    """清单生成异常时回滚，清理暂存分片并按 V012 留档。"""
    _reset_export()
    svc = DataExportService()
    manifest, message = svc.generate_manifest({
        "任务编号": "DATA-0002", "导出批次": "T-FAIL", "注入异常": True,
    })
    assert manifest is None and "回滚" in message
    assert svc.get_manifest("T-FAIL") is None
    assert svc.list_manifests() == [] or all(
        item["导出批次"] != "T-FAIL" for item in svc.list_manifests())
    archive = svc.list_archive(batch="T-FAIL")
    assert archive and archive[0]["状态"] == "已回滚"
    assert archive[0]["清理残留"]
    assert archive[0]["清单版本"] == "V012"


def test_manifest_resume_only_missing_shards():
    """中断后只能续写缺失分片，已发布分片内容不被改写。"""
    _reset_export()
    svc = DataExportService()
    manifest, message = svc.generate_manifest({
        "任务编号": "DATA-0002", "导出批次": "T-RESUME", "拆分策略": "合并文件",
    })
    assert manifest is not None, message
    file_name = manifest["文件清单"][0]["文件名"]
    intact = manifest["文件清单"][0]["分片"][1]
    intact_snapshot = json.dumps(intact, ensure_ascii=False, sort_keys=True)
    file, msg = svc.interrupt_shard("T-RESUME", file_name, 1)
    assert file is not None, msg
    _, _, error = svc.render_file("T-RESUME", file_name)
    assert error and "缺失分片" in error
    resumed, msg = svc.resume_manifest("T-RESUME")
    assert resumed is not None, msg
    assert json.dumps(resumed["文件清单"][0]["分片"][1], ensure_ascii=False, sort_keys=True) == intact_snapshot
    assert svc.verify_manifest("T-RESUME")["ok"] is True


def test_metric_def_reads_manifest_calibers():
    """指标定义模块按清单里的指标键与精度位数核对字段标签。"""
    _reset_export()
    svc = DataExportService()
    manifest, message = svc.generate_manifest({
        "任务编号": "DATA-0002", "导出批次": "T-CAL", "拆分策略": "合并文件",
    })
    assert manifest is not None, message
    result = MetricDefService().check_export_calibers(manifest["字段清单"])
    assert result["ok"] is True, result["问题"]
    tampered = [dict(field) for field in manifest["字段清单"]]
    tampered[3]["精度位数"] = 9
    broken = MetricDefService().check_export_calibers(tampered)
    assert broken["ok"] is False
    assert any(issue["问题"].startswith("精度位数") for issue in broken["问题"])
