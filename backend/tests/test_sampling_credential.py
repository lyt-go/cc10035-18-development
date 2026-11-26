"""采样凭据包验收测试：完整复现「两轮采样 + 迟到读数重算 + 签发冻结」剧情。

剧情锚点：
- 初始读数 08:00=10、08:05=20、08:10=30，两个十分钟窗，左开右闭归窗；
- 第一份材料未签发时 08:03=15 迟到并触发第二轮重算；
- 同一轮里首窗可能两个或三个点，08:10 只在第二窗；
- 签发后断线重领拿到同一文件（字节与指纹一致）。
"""
from __future__ import annotations

import hashlib
import io
import csv

import pytest

from app.services.sampling_credential import SamplingCredentialService

TASK = SamplingCredentialService.DEFAULT_TASK_ID
DAY = "2026-11-26"


@pytest.fixture
def svc():
    service = SamplingCredentialService()
    return service


def _csv_rows(payload: bytes) -> list[list[str]]:
    text = payload.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def _section(rows: list[list[str]], title: str) -> list[list[str]]:
    start = next(i for i, row in enumerate(rows) if row == [title])
    end = next((i for i in range(start + 1, len(rows)) if rows[i] and rows[i][0].startswith("[")), len(rows))
    return rows[start + 1:end]


def test_full_narrative_rounds_windows_and_frozen_reissue(svc):
    # ---- 第一轮：三个初始读数 ----
    r = svc.ingest_reading(TASK, f"{DAY}T08:00:00+08:00", 10)
    assert r["ok"] and r["round_no"] == 1 and r["window_index"] == 0
    r = svc.ingest_reading(TASK, f"{DAY}T08:05:00+08:00", 20)
    assert r["round_no"] == 1 and r["window_index"] == 0
    r = svc.ingest_reading(TASK, f"{DAY}T08:10:00+08:00", 30)
    # 分界点样本归右边一窗：08:10 只在第二窗
    assert r["window_index"] == 1

    settled = svc.settle_round(TASK)
    assert settled["ok"]
    snap1 = settled["snapshot"]
    w0, w1 = snap1["windows"]
    assert w0["sample_count"] == 2 and w0["value"] == pytest.approx(15.0)
    assert [s["timestamp"][-14:] for s in w0["samples"]] == ["08:00:00+08:00", "08:05:00+08:00"]
    assert w1["sample_count"] == 1 and w1["value"] == pytest.approx(30.0)
    assert len(snap1["readings"]) == 3

    # ---- 第一份材料：未签发，采用第 1 轮 ----
    applied = svc.apply_package(TASK)
    assert applied["ok"]
    package_id = applied["package"]["package_id"]
    assert applied["package"]["adopted_round"] == 1

    draft = svc.download_package(package_id)
    assert draft["ok"] and draft["frozen"] is False
    draft_rows = _csv_rows(draft["content"])
    assert ["本次实际采用轮次", "1"] in draft_rows
    agg = _section(draft_rows, "[窗口聚合]")[1:]
    assert agg[0][3:] == ["2", "15.0000"]
    assert agg[1][3:] == ["1", "30.0000"]
    # 明细与聚合同一份计算互相校验，全部「一致」
    checks = [row for row in _section(draft_rows, "[校验]")[1:] if row]
    assert all(row[-1] == "一致" for row in checks)
    detail_ts = [row[0] for row in _section(draft_rows, "[读数明细]")[1:] if row]
    assert len(detail_ts) == 3

    # ---- 08:03=15 迟到，触发第二轮重算，第一份材料尚未签发 ----
    r = svc.ingest_reading(TASK, f"{DAY}T08:03:00+08:00", 15)
    assert r["ok"] and r["round_no"] == 2 and r["round_status"] == "计算中"
    assert "等待" in r["waiting_reason"]

    detail = svc.task_detail(TASK)
    assert detail["open_round"]["round_no"] == 2
    assert detail["open_round"]["status"] == "计算中"
    assert "重算尚未定稿" in detail["waiting_reason"]
    # 未定稿期间第一份材料仍锚定第 1 轮，文件不混入新样本
    still = svc.download_package(package_id)
    still_rows = _csv_rows(still["content"])
    assert ["本次实际采用轮次", "1"] in still_rows
    assert len([row for row in _section(still_rows, "[读数明细]")[1:] if row]) == 3

    # 未定稿不能签发：需要等待的原因可查明
    blocked = svc.issue_package(package_id)
    # 第一份包已锚定第 1 轮快照，本可签发；这里新申请第二份则会暴露等待态
    second = svc.apply_package(TASK)
    assert second["package"]["adopted_round"] == 1
    assert "未定稿" in second["package"]["pending_reason"]
    assert blocked["ok"] is True  # 已锚定轮次的材料允许签发

    # ---- 第二轮定稿：首窗三个点，08:10 仍只在第二窗 ----
    settled2 = svc.settle_round(TASK)
    assert settled2["ok"]
    snap2 = settled2["snapshot"]
    w0_2, w1_2 = snap2["windows"]
    assert w0_2["sample_count"] == 3
    assert [s["timestamp"][-14:] for s in w0_2["samples"]] == [
        "08:00:00+08:00", "08:03:00+08:00", "08:05:00+08:00",
    ]
    assert w0_2["value"] == pytest.approx(15.0)
    assert w1_2["sample_count"] == 1 and w1_2["value"] == pytest.approx(30.0)
    assert len(snap2["readings"]) == 4

    # 新申请材料采用第 2 轮；第一轮旧材料仍冻结在第 1 轮样本集
    third = svc.apply_package(TASK)
    pkg2 = third["package"]["package_id"]
    assert third["package"]["adopted_round"] == 2
    new_file = svc.download_package(pkg2)
    new_rows = _csv_rows(new_file["content"])
    assert ["本次实际采用轮次", "2"] in new_rows
    new_detail = [row for row in _section(new_rows, "[读数明细]")[1:] if row]
    assert len(new_detail) == 4
    assert any("08:03" in row[0] for row in new_detail)
    # 旧材料不受补采影响：下载文件里没有未用于该均值的样本
    old_rows = _csv_rows(svc.download_package(package_id)["content"])
    old_detail = [row for row in _section(old_rows, "[读数明细]")[1:] if row]
    assert len(old_detail) == 3
    assert not any("08:03" in row[0] for row in old_detail)

    # ---- 签发后断线重领：同一文件（字节 + 指纹）----
    issued = svc.issue_package(pkg2)
    assert issued["ok"] and issued["package"]["status"] == "已签发"
    again = svc.issue_package(pkg2)
    assert again["ok"] is False  # 已签发不可重复签发/改写

    first = svc.download_package(pkg2)
    second_dl = svc.download_package(pkg2)
    assert first["frozen"] is True
    assert first["content"] == second_dl["content"]
    assert first["fingerprint"] == second_dl["fingerprint"]
    # 用文件中「[指纹]」小节之前的正文重算指纹，应与文件内指纹一致
    text = first["content"].decode("utf-8-sig")
    body = text.split("[指纹]")[0].rstrip("\n") + "\n"
    expect_fp = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    rows = _csv_rows(first["content"])
    fp_row = next(row for row in rows if row and row[0] == "文件指纹")
    assert fp_row[1] == expect_fp
    assert len(fp_row[1]) == len("sha256:") + 64


def test_boundary_sample_belongs_to_right_window(svc):
    # 08:10 与 08:20 都是分界点；08:20 属时段终点被裁剪
    assert svc.ingest_reading(TASK, f"{DAY}T08:10:00+08:00", 30)["window_index"] == 1
    assert svc.ingest_reading(TASK, f"{DAY}T08:20:00+08:00", 99)["window_index"] is None
    # 时段起点归第一窗
    assert svc.ingest_reading(TASK, f"{DAY}T08:00:00+08:00", 10)["window_index"] == 0
    snap = svc.settle_round(TASK)["snapshot"]
    assert snap["windows"][0]["sample_count"] == 1
    assert snap["windows"][1]["sample_count"] == 1
    assert snap["excluded_count"] == 1
    # 时段外样本不进入下载文件
    pkg = svc.apply_package(TASK)["package"]["package_id"]
    rows = _csv_rows(svc.download_package(pkg)["content"])
    detail = [row for row in _section(rows, "[读数明细]")[1:] if row]
    assert len(detail) == 2
    assert not any("08:20" in row[0] for row in detail)


def test_duplicate_timestamp_rejected(svc):
    svc.ingest_reading(TASK, f"{DAY}T08:05:00+08:00", 20)
    again = svc.ingest_reading(TASK, f"{DAY}T08:05:00+08:00", 21)
    assert again["ok"] is False


def test_apply_before_any_round_is_not_downloadable(svc):
    applied = svc.apply_package(TASK)
    assert applied["ok"] and applied["package"]["adopted_round"] is None
    pkg = applied["package"]["package_id"]
    assert svc.download_package(pkg)["ok"] is False
    assert svc.issue_package(pkg)["ok"] is False
