"""指标定义 业务规则：分析看板结果、口径版本切换与指标刷新清单。

口径切换链路的几个关键约定：

* 指标状态记录（``metric_status_record``）保存每个指标当前的发布状态、已处理状态、
  状态水位与已确认结果；发布状态变化时只会在下一轮切换里表现为一次“状态流转边界”。
* 指标刷新清单（``metric_refresh_list``）按生命周期状态流转，旧版本记录只失效不删除，
  同一 (指标编号, 生命周期状态) 提交后只保留一条有效记录。
* 状态推进范围决定重算集合，复用边界决定哪些已确认结果不用再刷新：
  - ``按发布日期水位继续``：状态边界变化或水位早于新版本生效时间才重算，其余复用；
  - ``按业务窗口全量重放``：窗口内全部重放，不复用。
"""
from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timezone
from typing import Any

from app.store import store

MODULE = "metric_def"
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
STATUS_ORDER = ["草稿", "已定义", "已发布", "已废弃"]
ACTION_RULES = {"提交定义": "已定义", "发布指标": "已发布", "废弃指标": "已废弃"}
NEGATIVE_ACTIONS = []

STATUS_MODULE = "metric_status_record"
REFRESH_MODULE = "metric_refresh_list"
TASK_MODULE = "caliber_switch_task"

STRATEGY_WATERMARK = "按发布日期水位继续"
STRATEGY_REPLAY = "按业务窗口全量重放"
STRATEGIES = [STRATEGY_WATERMARK, STRATEGY_REPLAY]

# 刷新清单的生命周期：重算与复用走两条不同的流转分支，最终状态都是终态。
# 核对结论由指标差异接口单独写入，不靠状态名兜底。
REFRESH_FLOW: dict[str, list[str]] = {
    "待重算": ["重算中"],
    "重算中": ["已生效"],
    "已生效": [],
    "已确认": ["已复用"],
    "已复用": [],
    "已失效": [],
}

# 演示环境用记录数折算扫描耗时：重算要扫业务窗口，复用只读已确认结果。
RECOMPUTE_COST_MS = 12.0
REUSE_COST_MS = 0.8
DIFF_TOLERANCE = 0.001


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_day(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def next_id(module: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1


def metric_value(metric_no: str, status: str, version: str, watermark: str) -> float:
    """按指标状态与版本做确定性取数，重算多次结果一致，便于核对。"""
    digest = hashlib.sha256(f"{metric_no}|{status}|{version}|{watermark}".encode()).hexdigest()
    ratio = int(digest[:8], 16) / 0xFFFFFFFF
    return round(0.62 + ratio * 0.36, 3)


class MetricDefService:
    def list_results(self) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        caliber = MetricCaliberService()
        confirmed = {item["指标编号"]: item for item in caliber.results()}
        results: list[dict] = []
        for row in rows:
            d = dict(row)
            snapshot = confirmed.get(str(row.get("指标编号", "")))
            if snapshot and snapshot.get("指标值") is not None:
                # 口径切换后的结果优先展示，未切换过的指标仍给出演示取值。
                d["指标值"] = snapshot["指标值"]
                d["对比基准"] = round(float(snapshot["指标值"]) * 0.94, 3)
                d["口径版本"] = snapshot["口径版本"]
            else:
                d["指标值"] = round(random.uniform(0.65, 0.98), 3)
                d["对比基准"] = round(random.uniform(0.60, 0.92), 3)
            d["差异"] = round(float(d["指标值"]) - float(d["对比基准"]), 3)
            results.append(d)
        return results

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        count = int(params.get("样本范围", params.get("并行数量", "6")))
        results: list[dict] = []
        for i in range(count):
            results.append({
                "id": i + 1,
                "分组": f"分组{(i%3)+1}",
                "指标值": round(random.uniform(0.62, 1.02), 3),
                "对比基准": round(random.uniform(0.58, 0.95), 3),
                "差异": round(random.uniform(-0.2, 0.3), 3),
            })
        return results

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标 {entry_id} 不存在或已废弃"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标定义可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if target == entry.get("status"):
            return entry, f"指标当前已是「{target}」，无需重复流转"
        entry["status"] = target
        entry["pending"] = target in ("草稿", "已定义")
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 只推进“当前状态”，已处理状态保持不变，切换时据此识别状态流转边界。
        MetricCaliberService().sync_from_metrics()
        labels = {"提交定义": "已提交定义", "发布指标": "已发布", "废弃指标": "已废弃"}
        return entry, f"指标{labels.get(action, action)}"


class MetricCaliberService:
    """口径切换：读取指标状态记录与切换任务，按状态流转边界重算并复用已确认版本。"""

    # ---------- 读取 ----------

    def status_records(self) -> list[dict[str, Any]]:
        self.sync_from_metrics()
        return [dict(row) for row in store.rows(STATUS_MODULE)]

    def refresh_list(self, task_no: str | None = None, *, valid_only: bool = False) -> list[dict[str, Any]]:
        rows = store.rows(REFRESH_MODULE)
        if task_no:
            rows = [row for row in rows if str(row.get("任务编号")) == task_no]
        if valid_only:
            rows = [row for row in rows if row.get("有效")]
        return [dict(row) for row in rows]

    def tasks(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(TASK_MODULE)]

    def task_detail(self, task_id: int) -> dict[str, Any] | None:
        task = store.find(TASK_MODULE, task_id)
        if task is None:
            return None
        detail = dict(task)
        detail["刷新清单"] = self.refresh_list(str(task.get("任务编号", "")))
        detail["指标结果"] = self.task_results(str(task.get("任务编号", "")))
        return detail

    def task_results(self, task_no: str) -> list[dict[str, Any]]:
        """本次切换产出的指标结果：按刷新清单里的有效记录还原，同一状态一条。"""
        rows = [
            row for row in store.rows(REFRESH_MODULE)
            if str(row.get("任务编号")) == task_no and row.get("有效")
        ]
        return [
            {
                "指标编号": row.get("指标编号"),
                "指标名称": row.get("指标名称"),
                "生命周期状态": row.get("生命周期状态"),
                "指标值": row.get("指标值"),
                "刷新方式": row.get("刷新方式"),
                "口径版本": row.get("目标版本"),
                "来源版本": row.get("来源版本"),
                "状态水位": row.get("本次水位"),
                "核对结果": row.get("核对结果"),
                "记录版本": row.get("记录版本"),
            }
            for row in self._unique_valid_rows(rows)
        ]

    def results(self) -> list[dict[str, Any]]:
        """口径切换后的指标结果：同一指标状态只输出一条（最新水位）。"""
        self.sync_from_metrics()
        target_versions: dict[tuple[str, str], Any] = {}
        for row in store.rows(REFRESH_MODULE):
            if row.get("有效"):
                key = (str(row.get("指标编号")), str(row.get("生命周期状态")))
                target_versions[key] = row.get("目标版本")
        results: list[dict[str, Any]] = []
        for (_metric_no, _status), records in self._groups().items():
            canonical = records[0]
            key = (str(canonical.get("指标编号")), str(canonical.get("当前状态")))
            results.append({
                "指标编号": canonical.get("指标编号"),
                "指标名称": canonical.get("指标名称"),
                "生命周期状态": canonical.get("当前状态"),
                "指标值": canonical.get("已确认指标值"),
                "口径版本": canonical.get("已确认口径版本"),
                "目标版本": target_versions.get(key),
                "状态水位": canonical.get("状态水位"),
                "状态版本": canonical.get("状态版本"),
                "来源任务": canonical.get("来源任务"),
                "更新时间": canonical.get("更新时间"),
            })
        return results

    # ---------- 状态同步与分组 ----------

    def sync_from_metrics(self) -> None:
        """按指标当前发布状态维护状态记录；状态推进时追加一条新记录。

        旧状态记录保留为历史水位，只有与当前状态一致的记录参与本轮切换，
        同一状态若被重复回写出多条记录，会在分组时合并为一次重算。
        """
        records = store.rows(STATUS_MODULE)
        by_metric: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            by_metric.setdefault(str(record.get("指标编号", "")), []).append(record)
        for metric in store.rows(MODULE):
            metric_no = str(metric.get("指标编号", ""))
            status = str(metric.get("status", STATUS_ORDER[0]))
            history = by_metric.get(metric_no, [])
            active = [r for r in history if str(r.get("当前状态", "")) == status]
            if active:
                for record in active:
                    record["指标名称"] = metric.get("指标名称") or record.get("指标名称")
                continue
            latest = max(
                history,
                key=lambda row: (
                    parse_day(row.get("状态水位")) or date.min,
                    int(row.get("状态版本", 0)),
                    int(row.get("id", 0)),
                ),
                default=None,
            )
            records.append({
                "id": next_id(STATUS_MODULE),
                "指标编号": metric_no,
                "指标名称": metric.get("指标名称"),
                "当前状态": status,
                # 继承上一条记录已确认的水位与版本，已处理状态仍停在上一个生命周期，
                # 这样状态推进会被识别为一次流转边界。
                "已处理状态": (latest or {}).get("当前状态", status),
                "状态水位": (latest or {}).get("状态水位") or str(metric.get("时间窗口") or ""),
                "已确认口径版本": (latest or {}).get("已确认口径版本"),
                "已确认指标值": (latest or {}).get("已确认指标值"),
                "状态版本": int((latest or {}).get("状态版本", 1)),
                "来源任务": (latest or {}).get("来源任务", ""),
                "更新时间": now_iso(),
            })

    def _active_records(self) -> list[dict[str, Any]]:
        """只取与指标当前发布状态一致的状态记录。"""
        current = {
            str(metric.get("指标编号", "")): str(metric.get("status", ""))
            for metric in store.rows(MODULE)
        }
        return [
            record for record in store.rows(STATUS_MODULE)
            if str(record.get("当前状态", "")) == current.get(str(record.get("指标编号", "")))
        ]

    def _groups(self) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """按 (指标编号, 生命周期状态) 分组，去重后每条状态只算一次。

        同组内可能存在多条来源状态记录（重复回写），按水位、版本、id 选主记录，
        其余记录只做结果回填，不重复参与重算。
        """
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in self._active_records():
            key = (str(record.get("指标编号", "")), str(record.get("当前状态", "")))
            groups.setdefault(key, []).append(record)
        for records in groups.values():
            records.sort(
                key=lambda row: (
                    parse_day(row.get("状态水位")) or date.min,
                    int(row.get("状态版本", 0)),
                    int(row.get("id", 0)),
                ),
                reverse=True,
            )
        return groups

    # ---------- 口径切换 ----------

    def switch(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        version = str(
            values.get("目标版本") or values.get("目标口径版本") or values.get("口径版本") or ""
        ).strip()
        if not version:
            return None, "请填写目标口径版本"
        effective_day = parse_day(values.get("生效时间") or values.get("生效日期"))
        if effective_day is None:
            return None, "生效时间需为 YYYY-MM-DD 格式"
        strategy, error = self._normalize_strategy(values.get("切换策略"))
        if error:
            return None, error
        effective = effective_day.isoformat()

        self.sync_from_metrics()
        groups = self._groups()
        task_no = self._next_task_no(effective_day)
        task: dict[str, Any] = {
            "id": next_id(TASK_MODULE),
            "任务编号": task_no,
            "目标版本": version,
            "生效时间": effective,
            "切换策略": strategy,
            "状态": "进行中",
            "来源记录数": sum(len(rows) for rows in groups.values()),
            "指标状态数": len(groups),
            "状态边界数": 0,
            "重算指标数": 0,
            "复用指标数": 0,
            "刷新耗时毫秒": 0.0,
            "来源输入": [
                {
                    "指标编号": rows[0].get("指标编号"),
                    "生命周期状态": rows[0].get("当前状态"),
                    "来源状态": rows[0].get("已处理状态"),
                    "来源版本": rows[0].get("已确认口径版本"),
                    "来源水位": rows[0].get("状态水位"),
                }
                for rows in groups.values()
            ],
            "备注": str(values.get("备注") or ""),
            "创建时间": now_iso(),
            "完成时间": "",
        }
        store.rows(TASK_MODULE).append(task)

        # 提交前先把同键的历史有效版本收敛为唯一一条，切换阶段保留原记录。
        self._collapse_valid()

        recomputed = 0
        reused = 0
        boundary_count = 0
        elapsed = 0.0
        refresh_rows: list[dict[str, Any]] = []
        for (metric_no, status), records in groups.items():
            canonical = records[0]
            source_version = canonical.get("已确认口径版本")
            source_watermark = canonical.get("状态水位")
            source_status = canonical.get("已处理状态")
            confirmed_value = canonical.get("已确认指标值")
            boundary_changed = str(source_status or "") != status
            watermark_behind = self._behind(source_watermark, effective)
            # 状态推进范围：状态流转边界、水位早于新版本生效时间、或从未确认过的才重算；
            # 其余命中复用边界，直接沿用已确认版本，不再扫历史窗口。
            need_recompute = (
                strategy == STRATEGY_REPLAY
                or confirmed_value is None
                or boundary_changed
                or watermark_behind
            )
            if boundary_changed:
                boundary_count += 1

            method = "重算" if need_recompute else "复用"
            if need_recompute:
                recomputed += 1
                new_value = metric_value(metric_no, status, version, effective)
                new_watermark = effective
                covered_days = self._window_days(canonical, effective)
                elapsed += RECOMPUTE_COST_MS + covered_days * 0.2
                next_status = "待重算"
            else:
                reused += 1
                new_value = confirmed_value
                # 复用已确认版本时水位不推进，避免把未重算的区间算成已覆盖。
                new_watermark = str(source_watermark or effective)
                covered_days = 0
                elapsed += REUSE_COST_MS
                next_status = "已确认"

            row = {
                "id": next_id(REFRESH_MODULE),
                "任务编号": task_no,
                "指标编号": metric_no,
                "指标名称": canonical.get("指标名称"),
                "生命周期状态": status,
                "刷新方式": method,
                "刷新状态": next_status,
                "来源状态": source_status,
                "来源版本": source_version,
                "来源水位": source_watermark,
                "目标版本": version,
                "本次水位": new_watermark,
                "指标值": new_value if need_recompute else confirmed_value,
                "原指标值": confirmed_value,
                "差异": (
                    round(float(new_value) - float(confirmed_value), 3)
                    if need_recompute and confirmed_value is not None
                    else 0.0
                ),
                "核对结果": "待核对",
                "核对版本": source_version,
                "记录版本": self._next_record_version(metric_no, status),
                "合并来源数": len(records),
                "覆盖天数": covered_days,
                "有效": True,
                "状态轨迹": "",
                "创建时间": now_iso(),
                "更新时间": now_iso(),
                "备注": "",
            }
            refresh_rows.append(row)
            store.rows(REFRESH_MODULE).append(row)

            # 旧版本只失效不删除：同键旧记录、以及指标上一生命周期状态的记录都退为历史，
            # 保证每个指标当前只有一份有效版本。
            for old in store.rows(REFRESH_MODULE):
                if old is row or not old.get("有效"):
                    continue
                if str(old.get("指标编号")) != metric_no:
                    continue
                if str(old.get("生命周期状态")) == status:
                    old["备注"] = f"被 {task_no} 的记录版本 {row['记录版本']} 取代"
                else:
                    old["备注"] = f"指标状态已推进到「{status}」，随 {task_no} 失效"
                old["有效"] = False
                old["刷新状态"] = "已失效"
                old["更新时间"] = now_iso()

            if need_recompute:
                self._advance(row, "重算中")
                self._advance(row, "已生效")
            else:
                self._advance(row, "已复用")

            if need_recompute:
                self._update_status_records(records, version, new_value, new_watermark, task_no)

        elapsed += random.uniform(0.2, 0.9)
        task["状态"] = "已完成"
        task["状态边界数"] = boundary_count
        task["重算指标数"] = recomputed
        task["复用指标数"] = reused
        task["刷新耗时毫秒"] = round(elapsed, 1)
        task["完成时间"] = now_iso()

        self._collapse_valid()
        rows = [row for row in refresh_rows]
        return {
            "ok": True,
            "message": (
                f"口径切换完成：重算 {recomputed} 个指标，复用 {reused} 个指标，"
                f"耗时 {task['刷新耗时毫秒']}ms"
            ),
            "task": dict(task),
            "items": [dict(row) for row in rows],
            "results": self.results(),
            "task_results": self.task_results(task_no),
            "重算指标数": recomputed,
            "复用指标数": reused,
            "刷新耗时毫秒": task["刷新耗时毫秒"],
            "状态边界数": boundary_count,
        }, ""

    def _update_status_records(
        self,
        records: list[dict[str, Any]],
        version: str,
        value: Any,
        watermark: str,
        task_no: str,
    ) -> None:
        for record in records:
            # 同组重复来源只回填同一份结果，不重复计算。
            record["已确认口径版本"] = version
            record["已确认指标值"] = value
            record["状态水位"] = watermark
            record["已处理状态"] = record.get("当前状态")
            record["状态版本"] = int(record.get("状态版本", 1)) + 1
            record["来源任务"] = task_no
            record["更新时间"] = now_iso()

    # ---------- 指标差异核对 ----------

    def apply_diff(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        task = self._resolve_task(values)
        if task is None:
            return None, "暂无口径切换任务，请先发起口径切换"
        self._collapse_valid()
        rows = [
            row for row in store.rows(REFRESH_MODULE)
            if str(row.get("任务编号")) == str(task.get("任务编号")) and row.get("有效")
        ]
        if not rows:
            return None, f"口径切换任务 {task.get('任务编号')} 没有可核对的刷新记录"

        supplied, conflict_count = self._normalize_diff_input(values)
        consistent = 0
        different = 0
        skipped = 0
        for row in self._unique_valid_rows(rows):
            key = (str(row.get("指标编号")), str(row.get("生命周期状态")))
            candidate = supplied.get(key)
            if candidate is None:
                candidate = supplied.get((key[0], ""))
            base = row.get("指标值")
            if base is None:
                base = self._status_value(*key)
            if candidate is None:
                verdict = "一致"
                delta = 0.0
            elif base is None:
                skipped += 1
                continue
            else:
                delta = round(float(candidate) - float(base), 3)
                verdict = "一致" if abs(delta) <= DIFF_TOLERANCE else "有差异"
            row["核对结果"] = verdict
            row["核对值"] = float(candidate) if candidate is not None else base
            # 核对结果保留产生该结果的版本，来源版本不动；冲突入参只落第一条。
            row["核对版本"] = row.get("目标版本")
            row["核对差异"] = delta
            row["核对时间"] = now_iso()
            row["更新人"] = str(values.get("操作人") or "值班管理员")
            row["备注"] = "差异入参存在冲突，已按首条核对" if conflict_count and candidate is not None else row.get("备注", "")
            if verdict == "一致":
                consistent += 1
            else:
                different += 1

        unique_rows = self._unique_valid_rows(rows)
        return {
            "ok": True,
            "message": (
                f"指标差异已写入刷新清单：核对 {len(unique_rows)} 个指标状态，"
                f"一致 {consistent} 个，有差异 {different} 个"
            ),
            "任务编号": task.get("任务编号"),
            "核对数": len(unique_rows),
            "一致数": consistent,
            "差异数": different,
            "冲突归并数": conflict_count,
            "跳过数": skipped,
            "有效记录数": len(unique_rows),
            "items": [dict(row) for row in unique_rows],
        }, ""

    # ---------- 内部工具 ----------

    def _normalize_strategy(self, raw: Any) -> tuple[str, str]:
        text = str(raw or "").strip()
        if not text:
            return STRATEGY_WATERMARK, ""
        for strategy in STRATEGIES:
            if text == strategy:
                return strategy, ""
        if "水位" in text:
            return STRATEGY_WATERMARK, ""
        if "全量" in text or "重放" in text:
            return STRATEGY_REPLAY, ""
        return "", f"切换策略需为「{STRATEGY_WATERMARK}」或「{STRATEGY_REPLAY}」"

    def _next_task_no(self, day: date) -> str:
        prefix = f"CAL-{day.strftime('%Y%m%d')}"
        existing = [
            str(row.get("任务编号", ""))
            for row in store.rows(TASK_MODULE)
            if str(row.get("任务编号", "")).startswith(prefix)
        ]
        return f"{prefix}-{len(existing) + 1:02d}"

    def _next_record_version(self, metric_no: str, status: str) -> int:
        versions = [
            int(row.get("记录版本", 0))
            for row in store.rows(REFRESH_MODULE)
            if str(row.get("指标编号")) == metric_no
            and str(row.get("生命周期状态")) == status
        ]
        return max(versions, default=0) + 1

    def _behind(self, watermark: Any, effective: str) -> bool:
        day = parse_day(watermark)
        if day is None:
            return True
        return day < parse_day(effective)

    def _window_days(self, record: dict[str, Any], effective: str) -> int:
        start = parse_day(record.get("状态水位"))
        end = parse_day(effective)
        if start is None or end is None:
            return 1
        return max(1, abs((end - start).days))

    def _resolve_task(self, values: dict[str, Any]) -> dict[str, Any] | None:
        task_ref = values.get("任务编号") or values.get("task_id")
        tasks = store.rows(TASK_MODULE)
        if task_ref not in (None, ""):
            for task in tasks:
                if str(task.get("任务编号")) == str(task_ref):
                    return task
                if str(task.get("id")) == str(task_ref):
                    return task
            return None
        return tasks[-1] if tasks else None

    def _status_value(self, metric_no: str, status: str) -> Any:
        matches = [
            record for record in store.rows(STATUS_MODULE)
            if str(record.get("指标编号")) == metric_no and str(record.get("当前状态")) == status
        ]
        matches.sort(
            key=lambda row: (
                parse_day(row.get("状态水位")) or date.min,
                int(row.get("状态版本", 0)),
                int(row.get("id", 0)),
            ),
            reverse=True,
        )
        return matches[0].get("已确认指标值") if matches else None

    def _advance(self, row: dict[str, Any], target: str, **extra: Any) -> None:
        current = str(row.get("刷新状态") or "待重算")
        allowed = REFRESH_FLOW.get(current, [])
        if target not in allowed:
            raise ValueError(f"刷新清单状态不能从「{current}」流转到「{target}」")
        trail = [part for part in str(row.get("状态轨迹") or "").split("→") if part]
        trail.append(target)
        row["刷新状态"] = target
        row["状态轨迹"] = "→".join(trail)
        row["更新时间"] = now_iso()
        for key, value in extra.items():
            row[key] = value

    def _collapse_valid(self) -> None:
        """同一 (指标编号, 生命周期状态) 只允许一条有效记录，冲突时保留最高版本。"""
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in store.rows(REFRESH_MODULE):
            if not row.get("有效"):
                continue
            key = (str(row.get("指标编号")), str(row.get("生命周期状态")))
            holder = latest.get(key)
            if holder is None or (
                int(row.get("记录版本", 0)), int(row.get("id", 0))
            ) > (
                int(holder.get("记录版本", 0)), int(holder.get("id", 0))
            ):
                if holder is not None:
                    holder["有效"] = False
                    holder["备注"] = holder.get("备注") or "同键存在更新版本，已失效"
                latest[key] = row
            else:
                row["有效"] = False
                row["备注"] = row.get("备注") or "同键存在更新版本，已失效"
        return None

    def _unique_valid_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, Any]] = []
        for row in rows:
            key = (str(row.get("指标编号")), str(row.get("生命周期状态")))
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        return unique

    def _normalize_diff_input(
        self, values: dict[str, Any]
    ) -> tuple[dict[tuple[str, str], float], int]:
        """归一化差异入参；同键冲突时以第一条为准并统计冲突数。"""
        supplied: dict[tuple[str, str], float] = {}
        conflicts = 0

        def add(metric_no: Any, status: Any, raw: Any) -> None:
            nonlocal conflicts
            try:
                number = float(raw)
            except (TypeError, ValueError):
                return
            key = (str(metric_no), str(status or ""))
            if key in supplied and abs(supplied[key] - number) > DIFF_TOLERANCE:
                conflicts += 1
                return
            supplied.setdefault(key, number)

        items = values.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    add(item.get("指标编号"), item.get("生命周期状态"), item.get("指标值"))
        raw_values = values.get("指标值") or values.get("values") or {}
        if isinstance(raw_values, dict):
            for raw_key, raw_value in raw_values.items():
                key = str(raw_key)
                if "|" in key:
                    metric_no, status = key.split("|", 1)
                else:
                    metric_no, status = key, ""
                add(metric_no, status, raw_value)
        return supplied, conflicts
