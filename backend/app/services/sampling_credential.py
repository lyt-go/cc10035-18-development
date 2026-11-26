"""采样凭据包领域服务：为降采样复核提供可互证、可追溯、可冻结的交付材料。

业务模型
--------
1. 采样轮次（round）
   - 读数进入时归入当前「计算中」的轮次；没有打开的轮次则自动新开一轮。
   - 轮次定稿后生成不可变快照（snapshot）：按实际时间窗裁剪读数并计算窗口聚合，
     之后任何迟到读数都只能进入下一轮，不能改写已定稿的快照。
2. 归窗规则
   - 窗口为左开右闭 ``(window_start, window_end]``：正落在分界点的样本归右边一窗；
     时段起点上的样本归入第一窗；时段外读数被裁剪，不进入任何快照与下载文件。
3. 采样凭据包（package）
   - 操作人为同一时段申请材料，材料锁定「本次实际采用的轮次」。
   - 草稿文件始终由该轮不可变快照渲染，明细与聚合来自同一份计算，可互相校验；
     一次补采不会让文件里出现未用于该均值的样本。
   - 正式签发时冻结文件字节与指纹；之后下载中断、重新进入详情页再领，拿到的都是同一文件。
"""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime, timedelta, timezone
from typing import Any

CST = timezone(timedelta(hours=8))

GRANULARITY_SECONDS = {
    "1m": 60, "5m": 300, "10m": 600, "15m": 900,
    "30m": 1800, "1h": 3600,
}


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def parse_ts(value: str) -> datetime:
    """解析时间字符串；无时区信息时按东八区处理。"""
    dt = datetime.fromisoformat(value.strip().replace("/", "-"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(CST).isoformat(timespec="seconds")


class SamplingCredentialService:
    """采样凭据包服务：状态保存在进程内存，测试可通过 reset 还原。"""

    DEFAULT_TASK_ID = "DOWN-CRED-01"

    def __init__(self) -> None:
        self.reset()

    # ------------------------------------------------------------------
    # 状态初始化
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """清空全部轮次、读数与凭据包，恢复内置演示任务。"""
        period_date = "2026-11-26"
        self.tasks: dict[str, dict[str, Any]] = {
            self.DEFAULT_TASK_ID: {
                "task_id": self.DEFAULT_TASK_ID,
                "source_point": "TP-DEMO-01",
                "granularity": "10m",
                "agg_function": "avg",
                "period_start": f"{period_date}T08:00:00+08:00",
                "period_end": f"{period_date}T08:20:00+08:00",
                "created_at": _now(),
            }
        }
        # 读数台账：每条记录到达轮次与按规则算出的窗口序号（None 表示时段外被裁剪）
        self.readings: dict[str, list[dict[str, Any]]] = {self.DEFAULT_TASK_ID: []}
        # 轮次列表：最后一个可能处于「计算中」，其余均已定稿
        self.rounds: dict[str, list[dict[str, Any]]] = {self.DEFAULT_TASK_ID: []}
        # 凭据包列表
        self.packages: dict[str, list[dict[str, Any]]] = {self.DEFAULT_TASK_ID: []}
        self._package_seq = 0

    # ------------------------------------------------------------------
    # 任务管理
    # ------------------------------------------------------------------
    def register_task(
        self,
        task_id: str,
        source_point: str,
        granularity: str,
        agg_function: str,
        period_start: str,
        period_end: str,
    ) -> dict[str, Any]:
        if task_id in self.tasks:
            return {"ok": False, "message": f"降采样任务 {task_id} 已存在"}
        if granularity not in GRANULARITY_SECONDS:
            return {"ok": False, "message": f"不支持的目标粒度：{granularity}"}
        start = parse_ts(period_start)
        end = parse_ts(period_end)
        if end <= start:
            return {"ok": False, "message": "时段终点必须晚于起点"}
        task = {
            "task_id": task_id,
            "source_point": source_point,
            "granularity": granularity,
            "agg_function": agg_function or "avg",
            "period_start": fmt_ts(start),
            "period_end": fmt_ts(end),
            "created_at": _now(),
        }
        self.tasks[task_id] = task
        self.readings[task_id] = []
        self.rounds[task_id] = []
        self.packages[task_id] = []
        return {"ok": True, "message": f"采样凭据任务 {task_id} 已登记", "task": task}

    def _get_task(self, task_id: str) -> dict[str, Any] | None:
        return self.tasks.get(task_id)

    def _window_seconds(self, task: dict[str, Any]) -> int:
        return GRANULARITY_SECONDS.get(task["granularity"], 600)

    def _build_windows(self, task: dict[str, Any]) -> list[tuple[datetime, datetime]]:
        """按时段起点对齐生成完整窗口序列，窗口覆盖到时段终点。"""
        start = parse_ts(task["period_start"])
        end = parse_ts(task["period_end"])
        step = timedelta(seconds=self._window_seconds(task))
        windows: list[tuple[datetime, datetime]] = []
        cur = start
        while cur < end:
            nxt = min(cur + step, end)
            windows.append((cur, nxt))
            cur = nxt
        return windows

    def _assign_window(
        self, ts: datetime, task: dict[str, Any]
    ) -> int | None:
        """归窗：左开右闭 ``(start, end]``，分界点样本归右窗；时段外返回 None。"""
        start = parse_ts(task["period_start"])
        end = parse_ts(task["period_end"])
        if ts < start or ts >= end:
            return None
        step = self._window_seconds(task)
        index = int((ts - start).total_seconds() // step)
        return max(0, index)

    # ------------------------------------------------------------------
    # 读数接入与轮次定稿
    # ------------------------------------------------------------------
    def ingest_reading(self, task_id: str, timestamp: str, value: float) -> dict[str, Any]:
        task = self._get_task(task_id)
        if task is None:
            return {"ok": False, "message": f"降采样任务 {task_id} 不存在"}
        try:
            ts = parse_ts(timestamp)
            val = float(value)
        except (ValueError, TypeError):
            return {"ok": False, "message": "读数时间或数值格式无效"}

        ledger = self.readings[task_id]
        if any(item["timestamp"] == fmt_ts(ts) for item in ledger):
            return {"ok": False, "message": f"时间戳 {fmt_ts(ts)} 的读数已存在，不能重复计入"}

        task_rounds = self.rounds[task_id]
        if not task_rounds or task_rounds[-1]["status"] == "已定稿":
            round_no = len(task_rounds) + 1
            task_rounds.append({
                "round_no": round_no,
                "status": "计算中",
                "reason": self._waiting_reason(round_no),
                "opened_at": _now(),
                "settled_at": None,
                "snapshot": None,
                "incoming": [],
            })
        current = task_rounds[-1]

        window_index = self._assign_window(ts, task)
        entry = {
            "timestamp": fmt_ts(ts),
            "value": val,
            "arrived_round": current["round_no"],
            "window_index": window_index,
            "included": window_index is not None,
        }
        ledger.append(entry)
        current["incoming"].append({"timestamp": entry["timestamp"], "value": val})

        return {
            "ok": True,
            "message": "读数已计入" + (
                f"第 {current['round_no']} 轮" if window_index is not None
                else f"第 {current['round_no']} 轮，但落在时段外已被裁剪"
            ),
            "task_id": task_id,
            "round_no": current["round_no"],
            "round_status": current["status"],
            "waiting_reason": current["reason"],
            "window_index": window_index,
            "reading": entry,
        }

    @staticmethod
    def _waiting_reason(round_no: int) -> str:
        return (
            f"第 {round_no} 轮采样仍在计算中：迟到读数触发的重算尚未定稿，"
            "需等待该轮完整一轮计算结束后，才能作为凭据包签发依据"
        )

    def settle_round(self, task_id: str) -> dict[str, Any]:
        task = self._get_task(task_id)
        if task is None:
            return {"ok": False, "message": f"降采样任务 {task_id} 不存在"}
        task_rounds = self.rounds[task_id]
        if not task_rounds or task_rounds[-1]["status"] == "已定稿":
            return {"ok": False, "message": "当前没有计算中的采样轮次，无需定稿"}

        current = task_rounds[-1]
        snapshot = self._build_snapshot(task, current["round_no"])
        current["status"] = "已定稿"
        current["reason"] = None
        current["settled_at"] = _now()
        current["snapshot"] = snapshot
        return {
            "ok": True,
            "message": f"第 {current['round_no']} 轮采样已定稿",
            "round_no": current["round_no"],
            "snapshot": snapshot,
        }

    def _build_snapshot(self, task: dict[str, Any], round_no: int) -> dict[str, Any]:
        """以截至本轮到达的全部台账读数构建不可变快照，时段外读数裁剪掉。"""
        window_bounds = self._build_windows(task)
        buckets: list[list[dict[str, Any]]] = [[] for _ in window_bounds]
        excluded: list[dict[str, Any]] = []
        for entry in self.readings[task["task_id"]]:
            if entry["arrived_round"] > round_no or not entry["included"]:
                if entry["arrived_round"] <= round_no and not entry["included"]:
                    excluded.append(dict(entry))
                continue
            idx = entry["window_index"]
            buckets[idx].append({"timestamp": entry["timestamp"], "value": entry["value"]})

        windows: list[dict[str, Any]] = []
        for idx, ((w_start, w_end), samples) in enumerate(zip(window_bounds, buckets)):
            samples.sort(key=lambda item: item["timestamp"])
            agg_value = self._aggregate(task["agg_function"], [s["value"] for s in samples])
            windows.append({
                "window_index": idx,
                "window_start": fmt_ts(w_start),
                "window_end": fmt_ts(w_end),
                "sample_count": len(samples),
                "agg_function": task["agg_function"],
                "value": agg_value,
                "samples": samples,
            })

        included_readings = [
            {"timestamp": s["timestamp"], "value": s["value"], "window_index": idx}
            for idx, bucket in enumerate(buckets) for s in bucket
        ]
        included_readings.sort(key=lambda item: item["timestamp"])

        return {
            "task_id": task["task_id"],
            "round_no": round_no,
            "source_point": task["source_point"],
            "granularity": task["granularity"],
            "agg_function": task["agg_function"],
            "period_start": task["period_start"],
            "period_end": task["period_end"],
            "windows": windows,
            "readings": included_readings,
            "excluded_count": len(excluded),
            "settled_at": _now(),
        }

    @staticmethod
    def _aggregate(func: str, values: list[float]) -> float | None:
        if not values:
            return None
        if func == "max":
            return round(max(values), 4)
        if func == "min":
            return round(min(values), 4)
        if func == "sum":
            return round(sum(values), 4)
        return round(sum(values) / len(values), 4)

    def _latest_settled(self, task_id: str) -> dict[str, Any] | None:
        for round_item in reversed(self.rounds[task_id]):
            if round_item["status"] == "已定稿":
                return round_item
        return None

    def _open_round(self, task_id: str) -> dict[str, Any] | None:
        task_rounds = self.rounds[task_id]
        if task_rounds and task_rounds[-1]["status"] == "计算中":
            return task_rounds[-1]
        return None

    # ------------------------------------------------------------------
    # 查询：轮次与详情
    # ------------------------------------------------------------------
    def list_rounds(self, task_id: str) -> dict[str, Any]:
        task = self._get_task(task_id)
        if task is None:
            return {"ok": False, "message": f"降采样任务 {task_id} 不存在"}
        items = [self._round_summary(item) for item in self.rounds[task_id]]
        latest = self._latest_settled(task_id)
        return {
            "ok": True,
            "task_id": task_id,
            "rounds": items,
            "latest_settled_round": latest["round_no"] if latest else None,
            "open_round": self._round_summary(self._open_round(task_id)) if self._open_round(task_id) else None,
        }

    def _round_summary(self, round_item: dict[str, Any]) -> dict[str, Any]:
        snapshot = round_item.get("snapshot")
        window_values = None
        sample_total = 0
        if snapshot is not None:
            window_values = [
                {
                    "window_index": w["window_index"],
                    "window_start": w["window_start"],
                    "window_end": w["window_end"],
                    "sample_count": w["sample_count"],
                    "value": w["value"],
                }
                for w in snapshot["windows"]
            ]
            sample_total = len(snapshot["readings"])
        return {
            "round_no": round_item["round_no"],
            "status": round_item["status"],
            "reason": round_item.get("reason"),
            "opened_at": round_item.get("opened_at"),
            "settled_at": round_item.get("settled_at"),
            "sample_total": sample_total,
            "windows": window_values,
        }

    def task_detail(self, task_id: str) -> dict[str, Any]:
        task = self._get_task(task_id)
        if task is None:
            return {"ok": False, "message": f"降采样任务 {task_id} 不存在"}
        latest = self._latest_settled(task_id)
        open_round = self._open_round(task_id)
        return {
            "ok": True,
            "task": task,
            "rounds": [self._round_summary(item) for item in self.rounds[task_id]],
            "latest_settled_round": latest["round_no"] if latest else None,
            "open_round": self._round_summary(open_round) if open_round else None,
            "waiting_reason": open_round["reason"] if open_round else None,
            "latest_snapshot": latest["snapshot"] if latest else None,
            "readings_ledger": self.readings[task_id],
            "packages": [self._package_summary(item) for item in self.packages[task_id]],
        }

    # ------------------------------------------------------------------
    # 凭据包：申请、签发、下载
    # ------------------------------------------------------------------
    def apply_package(self, task_id: str) -> dict[str, Any]:
        task = self._get_task(task_id)
        if task is None:
            return {"ok": False, "message": f"降采样任务 {task_id} 不存在"}

        settled = self._latest_settled(task_id)
        open_round = self._open_round(task_id)
        self._package_seq += 1
        package_id = f"SC-{datetime.now(CST).strftime('%Y%m%d')}-{self._package_seq:04d}"
        package = {
            "package_id": package_id,
            "task_id": task_id,
            "status": "草稿",
            "applied_at": _now(),
            "issued_at": None,
            "adopted_round": settled["round_no"] if settled else None,
            "snapshot": settled["snapshot"] if settled else None,
            "pending_reason": (
                self._waiting_reason(open_round["round_no"]) if open_round else None
            ),
            "frozen_bytes": None,
            "fingerprint": None,
            "filename": None,
        }
        self.packages[task_id].append(package)
        if settled is None:
            message = "凭据包已申请，但当前没有已定稿的完整采样轮次，暂不可下载，需等待轮次定稿"
        elif open_round:
            message = (
                f"凭据包已申请，采用已定稿的第 {settled['round_no']} 轮；"
                f"第 {open_round['round_no']} 轮重算未定稿，正式签发前可查明等待原因"
            )
        else:
            message = f"凭据包已申请，采用第 {settled['round_no']} 轮定稿快照"
        return {"ok": True, "message": message, "package": self._package_summary(package)}

    def issue_package(self, package_id: str) -> dict[str, Any]:
        package = self._find_package(package_id)
        if package is None:
            return {"ok": False, "message": f"凭据包 {package_id} 不存在"}
        if package["status"] == "已签发":
            return {"ok": False, "message": "凭据包已正式签发，内容不可更改"}
        if package["snapshot"] is None:
            return {
                "ok": False,
                "message": "没有可采用的已定稿完整采样轮次，无法签发；请先完成一轮采样定稿",
            }
        package["status"] = "已签发"
        package["issued_at"] = _now()
        package["pending_reason"] = None
        # 先写签发时间再渲染，随后冻结：之后重领永远返回这一份字节
        filename, payload, fingerprint = self._render_csv(package, issued=True)
        package["frozen_bytes"] = payload
        package["fingerprint"] = fingerprint
        package["filename"] = filename
        return {"ok": True, "message": f"凭据包 {package_id} 已正式签发并冻结",
                "package": self._package_summary(package)}

    def download_package(self, package_id: str) -> dict[str, Any]:
        package = self._find_package(package_id)
        if package is None:
            return {"ok": False, "message": f"凭据包 {package_id} 不存在"}
        if package["status"] == "已签发":
            return {
                "ok": True,
                "frozen": True,
                "filename": package["filename"],
                "content": package["frozen_bytes"],
                "fingerprint": package["fingerprint"],
                "package": self._package_summary(package),
            }
        if package["snapshot"] is None:
            return {
                "ok": False,
                "message": "材料尚未生成：没有已定稿的完整采样轮次，请等待计算定稿后再下载",
            }
        # 草稿：每次都从同一不可变快照渲染，轮次不变则文件内容与指纹稳定
        filename, payload, fingerprint = self._render_csv(package, issued=False)
        return {
            "ok": True,
            "frozen": False,
            "filename": filename,
            "content": payload,
            "fingerprint": fingerprint,
            "package": self._package_summary(package),
        }

    def list_packages(self, task_id: str) -> dict[str, Any]:
        task = self._get_task(task_id)
        if task is None:
            return {"ok": False, "message": f"降采样任务 {task_id} 不存在"}
        return {
            "ok": True,
            "task_id": task_id,
            "packages": [self._package_summary(item) for item in self.packages[task_id]],
        }

    def _find_package(self, package_id: str) -> dict[str, Any] | None:
        for task_packages in self.packages.values():
            for package in task_packages:
                if package["package_id"] == package_id:
                    return package
        return None

    @staticmethod
    def _package_summary(package: dict[str, Any]) -> dict[str, Any]:
        return {
            "package_id": package["package_id"],
            "task_id": package["task_id"],
            "status": package["status"],
            "applied_at": package["applied_at"],
            "issued_at": package["issued_at"],
            "adopted_round": package["adopted_round"],
            "pending_reason": package["pending_reason"],
            "fingerprint": package["fingerprint"],
            "filename": package["filename"],
        }

    # ------------------------------------------------------------------
    # CSV 渲染：同一份快照同时产出聚合与明细，文件自带校验与指纹
    # ------------------------------------------------------------------
    def _render_csv(
        self, package: dict[str, Any], *, issued: bool
    ) -> tuple[str, bytes, str]:
        snapshot = package["snapshot"]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["# 采样凭据包"])
        writer.writerow(["凭据编号", package["package_id"]])
        writer.writerow(["任务编号", snapshot["task_id"]])
        writer.writerow(["源测点", snapshot["source_point"]])
        writer.writerow(["聚合函数", snapshot["agg_function"]])
        writer.writerow(["窗口粒度", snapshot["granularity"]])
        writer.writerow(["时段起点", snapshot["period_start"]])
        writer.writerow(["时段终点", snapshot["period_end"]])
        writer.writerow(["本次实际采用轮次", snapshot["round_no"]])
        writer.writerow(["签发状态", "已签发" if issued else "草稿"])
        writer.writerow(["申请时间", package["applied_at"]])
        writer.writerow(["签发时间", package["issued_at"] or ""])
        if package["pending_reason"]:
            writer.writerow(["等待原因", package["pending_reason"]])

        writer.writerow([])
        writer.writerow(["[窗口聚合]"])
        writer.writerow(["窗口序号", "窗口起点", "窗口终点", "样本数", "聚合值"])
        for window in snapshot["windows"]:
            writer.writerow([
                window["window_index"],
                window["window_start"],
                window["window_end"],
                window["sample_count"],
                "" if window["value"] is None else f"{window['value']:.4f}",
            ])

        writer.writerow([])
        writer.writerow(["[读数明细]"])
        writer.writerow(["时间戳", "读数", "所属窗口序号"])
        for reading in snapshot["readings"]:
            writer.writerow([reading["timestamp"], reading["value"], reading["window_index"]])

        writer.writerow([])
        writer.writerow(["[校验]"])
        writer.writerow(["窗口序号", "明细样本数", "明细重算值", "聚合值", "校验结论"])
        for window in snapshot["windows"]:
            detail_values = [
                item["value"]
                for item in snapshot["readings"]
                if item["window_index"] == window["window_index"]
            ]
            detail_agg = self._aggregate(snapshot["agg_function"], detail_values)
            consistent = (
                detail_agg == window["value"]
                and len(detail_values) == window["sample_count"]
            )
            writer.writerow([
                window["window_index"],
                len(detail_values),
                "" if detail_agg is None else f"{detail_agg:.4f}",
                "" if window["value"] is None else f"{window['value']:.4f}",
                "一致" if consistent else "不一致",
            ])

        body_text = buffer.getvalue()
        fingerprint = "sha256:" + hashlib.sha256(body_text.encode("utf-8")).hexdigest()
        writer.writerow([])
        writer.writerow(["[指纹]"])
        writer.writerow(["文件指纹", fingerprint])
        final_text = buffer.getvalue()
        payload = b"\xef\xbb\xbf" + final_text.encode("utf-8")
        filename = (
            f"{package['package_id']}_task-{snapshot['task_id']}"
            f"_round{snapshot['round_no']}.csv"
        )
        return filename, payload, fingerprint


sampling_credential_service = SamplingCredentialService()
