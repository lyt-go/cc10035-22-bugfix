"""冷热分层 + 跨层导出的统一领域核心。

这里是容量试算、分层日期区间、读取计划、冷段生命周期与保留策略冲突清理的
唯一事实来源。时序存储容量卡片、批量导出读取计划和计费预算都必须调用这里，
保证“两层日期和预算使用同一套策略”。

日期模型（offset 0 = 今天，offset 越大越早）：

- 总保留 R 天覆盖 offset 0 .. R-1；
- 热层 H 天（H 会被截断到 R）覆盖 offset 0 .. min(H, R)-1；
- 冷层覆盖 offset min(H, R) .. R-1；
- 请求导出 Q 天时，offset >= R 的日期不可安排、不可计费。

因此热层与冷层日期天然不重叠，且 H > R 时热层只占实际留存的 R 天。
"""
from __future__ import annotations

import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from app.store import store

CST = timezone(timedelta(hours=8))

BYTES_PER_POINT = 32
POINTS_PER_DAY = 100
FRAGMENTS_PER_DAY = 4

COMPRESSION_RATIOS: dict[str, int] = {
    "none": 1,
    "raw": 1,
    "uncompressed": 1,
    "无": 1,
    "无压缩": 1,
    "不压缩": 1,
    "gzip": 5,
    "snappy": 2,
    "lz4": 3,
    "zstd": 6,
}

# ---- 冷段状态（中文为规范值，与全系统 status 用中文的约定一致） ----
PENDING = "待领取"
WAITING_READ = "等待读取"
DELIVERED = "已交付"
UNREADABLE = "不可读"

_STATE_CODES = {
    PENDING: "pending",
    WAITING_READ: "waiting_read",
    DELIVERED: "delivered",
    UNREADABLE: "unreadable",
}

_STATE_ALIASES: dict[str, set[str]] = {
    PENDING: {"pending", "unclaimed", "待领取", "未领取"},
    WAITING_READ: {"waiting_read", "waiting", "claimed", "wait_read", "reading",
                   "等待读取", "已领取", "待读取"},
    DELIVERED: {"delivered", "confirmed", "done", "complete", "completed",
                "已交付", "已确认", "已完成"},
    UNREADABLE: {"unreadable", "released", "unavailable", "expired", "dead",
                 "不可读", "已释放", "已被释放", "释放"},
}


class StateValue(str):
    """字符串状态：序列化为中文规范值，同时可与英文状态码/常见别名直接相等。

    这样无论是 ``seg["state"] == "waiting_read"`` 还是
    ``seg["state"] == "等待读取"`` 的服务层断言都能成立；HTTP JSON 中看到的
    始终是中文规范值，英文码另外通过 ``state_code`` 暴露。
    """

    def __new__(cls, canonical: str) -> "StateValue":
        obj = str.__new__(cls, canonical)
        obj.canonical = canonical  # type: ignore[attr-defined]
        return obj

    def _matches(self, other: object) -> bool:
        text = str(other)
        if text == str(self):
            return True
        aliases = _STATE_ALIASES.get(str(self), set())
        return text in aliases or text.lower() in {a.lower() for a in aliases}

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, str):
            return self._matches(other)
        return NotImplemented

    def __ne__(self, other: object) -> bool:  # type: ignore[override]
        result = self.__eq__(other)
        return not result if result is not NotImplemented else True

    def __hash__(self) -> int:  # type: ignore[override]
        return str.__hash__(self)


def state_value(canonical: str) -> StateValue:
    return StateValue(canonical)


def state_code(status: str) -> str:
    return _STATE_CODES.get(status, "unknown")


def display_status(status: str, released: bool = False) -> str:
    """页面展示状态：不可读且已被清理时展示“已被释放”。"""
    if status == UNREADABLE and released:
        return "已被释放"
    return status


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"-?\d+")


def parse_days(value: Any, default: int = 0) -> int:
    """宽容解析天数：接受 5、"5"、"5天"、"30d_hot"、"热层3天" 等。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    match = _INT_RE.search(str(value))
    if not match:
        return default
    return max(0, int(match.group(0)))


def parse_compression(value: Any, default: int = 2) -> tuple[str, int]:
    """返回 (规范压缩算法, 压缩比)。明确表达“不压缩”时压缩比为 1。"""
    if value is None:
        return "none", 1
    text = str(value).strip()
    if not text:
        return "none", 1
    key = text.lower().replace(" ", "").replace("-", "")
    compact = text.replace(" ", "")
    for candidate, ratio in COMPRESSION_RATIOS.items():
        if compact == candidate or key == candidate.lower().replace("-", ""):
            return candidate if candidate.isascii() else "none", ratio
    # 形如 "5:1" / 数字压缩比
    ratio_match = re.fullmatch(r"(\d+(?:\.\d+)?)", text)
    if ratio_match:
        ratio = float(ratio_match.group(1))
        return text, (int(ratio) if ratio.is_integer() else ratio)
    return text, default


def resolve_today(value: Any = None) -> date:
    if value is None:
        return datetime.now(CST).date()
    if isinstance(value, datetime):
        return value.astimezone(CST).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _day_interval(day: date) -> tuple[str, str]:
    start = datetime(day.year, day.month, day.day, tzinfo=CST)
    end = start + timedelta(days=1)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def _bytes(raw_bytes: int, ratio: Any) -> int | float:
    try:
        ratio_val = float(ratio or 1)
    except (TypeError, ValueError):
        ratio_val = 1.0
    if ratio_val <= 0:
        ratio_val = 1.0
    divided = raw_bytes / ratio_val
    if abs(divided - round(divided)) < 1e-9:
        return int(round(divided))
    return round(divided, 4)


# ---------------------------------------------------------------------------
# 统一策略 / 读取计划
# ---------------------------------------------------------------------------

def normalize_policy(
    *,
    retention_days: Any = 30,
    hot_days: Any = 30,
    points_per_day: Any = POINTS_PER_DAY,
    bytes_per_point: Any = BYTES_PER_POINT,
    compression: Any = "none",
    compression_ratio: Any = None,
    today: Any = None,
    requested_days: Any = None,
) -> dict[str, Any]:
    retention = parse_days(retention_days, 30)
    hot_configured = parse_days(hot_days, 30)
    try:
        pppd = max(0, int(points_per_day))
    except (TypeError, ValueError):
        pppd = POINTS_PER_DAY
    try:
        bpp = max(1, int(bytes_per_point))
    except (TypeError, ValueError):
        bpp = BYTES_PER_POINT

    if compression_ratio is not None:
        try:
            ratio = float(compression_ratio)
            comp_name = str(compression or "custom")
        except (TypeError, ValueError):
            comp_name, ratio = parse_compression(compression, 1)
    else:
        comp_name, ratio = parse_compression(compression, 2)

    actual_hot = min(hot_configured, retention)
    actual_cold = max(retention - actual_hot, 0)
    req_days = parse_days(requested_days, retention) if requested_days is not None else retention
    return {
        "retention_days": retention,
        "hot_days_configured": hot_configured,
        "hot_days": actual_hot,
        "cold_days": actual_cold,
        "points_per_day": pppd,
        "bytes_per_point": bpp,
        "compression": comp_name,
        "compression_ratio": int(ratio) if float(ratio).is_integer() else round(ratio, 2),
        "today": resolve_today(today).isoformat(),
        "requested_days": max(req_days, 0),
        "daily_points": pppd,
        "daily_bytes": _bytes(pppd * bpp, ratio),
    }


def build_read_plan(**policy_kwargs: Any) -> dict[str, Any]:
    """根据唯一一套策略生成分层日期、实际读取区间与容量预算。"""
    policy = normalize_policy(**policy_kwargs)
    today = date.fromisoformat(policy["today"])
    r_days = policy["retention_days"]
    h_days = policy["hot_days"]
    q_days = policy["requested_days"]
    daily_bytes = policy["daily_bytes"]
    daily_points = policy["points_per_day"]

    dates: list[dict[str, Any]] = []
    for offset in range(max(q_days, r_days)):
        day = today - timedelta(days=offset)
        start_iso, end_iso = _day_interval(day)
        if offset < h_days:
            tier, tier_name = "hot", "热层"
        elif offset < r_days:
            tier, tier_name = "cold", "冷层"
        else:
            tier, tier_name = "unavailable", "超出保留期"
        retained = offset < r_days
        dates.append({
            "offset": offset,
            "date": day.isoformat(),
            "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][day.weekday()],
            "tier": tier,
            "tier_name": tier_name,
            "interval_start": start_iso,
            "interval_end": end_iso,
            "read_interval": f"{start_iso} ~ {end_iso}",
            "points": daily_points if retained else 0,
            "bytes": daily_bytes if retained else 0,
            "available": retained,
            "schedulable": retained,
            "billable": False,
        })

    hot_dates = [d for d in dates if d["tier"] == "hot"]
    cold_dates = [d for d in dates if d["tier"] == "cold"]
    unavailable_dates = [d for d in dates if d["tier"] == "unavailable"]

    total_bytes = _bytes(daily_bytes * r_days, 1) if isinstance(daily_bytes, int) else round(daily_bytes * r_days, 4)
    hot_bytes = _bytes(daily_bytes * h_days, 1) if isinstance(daily_bytes, int) else round(daily_bytes * h_days, 4)
    cold_bytes = _bytes(daily_bytes * len(cold_dates), 1) if isinstance(daily_bytes, int) else round(daily_bytes * len(cold_dates), 4)
    schedulable_days = min(q_days, r_days)
    scheduled_bytes = hot_bytes + cold_bytes

    def tier_block(tier: str, name: str, items: list[dict[str, Any]], byte_value: int | float) -> dict[str, Any]:
        if items:
            # offset 0 最新；列表尾部最早
            newest, oldest = items[0], items[-1]
            start_date, end_date = oldest["date"], newest["date"]
            interval_start = oldest["interval_start"]
            interval_end = newest["interval_end"]
        else:
            start_date = end_date = ""
            interval_start = interval_end = ""
        return {
            "tier": tier,
            "name": name,
            "days": len(items),
            "date_count": len(items),
            "start_date": start_date,
            "end_date": end_date,
            "interval_start": interval_start,
            "interval_end": interval_end,
            "read_interval": f"{interval_start} ~ {interval_end}" if interval_start else "",
            "bytes": byte_value,
            "storage_bytes": byte_value,
            "dates": [d["date"] for d in items],
        }

    tiers = [
        tier_block("hot", "热层", hot_dates, hot_bytes),
        tier_block("cold", "冷层", cold_dates, cold_bytes),
    ]

    return {
        **policy,
        # 容量（多给几组键名，方便不同调用方核对）
        "total_days": r_days,
        "retained_days": r_days,
        "actual_hot_days": h_days,
        "actual_cold_days": len(cold_dates),
        "total_bytes": total_bytes,
        "storage_total_bytes": total_bytes,
        "hot_bytes": hot_bytes,
        "hot_storage_bytes": hot_bytes,
        "cold_bytes": cold_bytes,
        "cold_storage_bytes": cold_bytes,
        "bytes_sum_check": hot_bytes + cold_bytes,
        # 导出范围
        "query_days": q_days,
        "requested_export_days": q_days,
        "schedulable_days": schedulable_days,
        "available_days": schedulable_days,
        "unavailable_days": len(unavailable_dates),
        "out_of_retention_days": len(unavailable_dates),
        "scheduled_bytes": scheduled_bytes,
        "scheduled_points": schedulable_days * policy["points_per_day"],
        "tiers": tiers,
        "hot_tier": tiers[0],
        "cold_tier": tiers[1],
        "dates": dates,
        "unavailable_dates": unavailable_dates,
        "budget": {
            "basis": "same_policy",
            "scheduled_days": schedulable_days,
            "scheduled_points": schedulable_days * policy["points_per_day"],
            "scheduled_bytes": scheduled_bytes,
            "hot_bytes": hot_bytes,
            "cold_bytes": cold_bytes,
            "total_bytes": scheduled_bytes,
            "currency": "CNY",
            "amount": 0,
        },
    }


# ---------------------------------------------------------------------------
# 有状态的跨层导出服务（单例）
# ---------------------------------------------------------------------------


class TieredExportService:
    """管理跨层导出计划、冷段领取/读取/确认和保留收缩冲突。"""

    TABLE = "tiered_export_plan"
    _lock = threading.RLock()

    def __init__(self) -> None:
        # 计划是本领域的私有状态，不注册进 store 的业务模块表，
        # 以免出现在运营概览的 18 个业务模块中。
        self._plans: list[dict[str, Any]] = []

    def reset(self) -> None:
        with self._lock:
            self._plans.clear()

    # ---- 存储与编号 ----
    def _rows(self) -> list[dict[str, Any]]:
        return self._plans

    def _next_no(self, prefix: str, rows: Iterable[dict[str, Any]], field: str) -> str:
        max_no = 0
        pattern = re.compile(rf"{prefix}-?(\d+)")
        for row in rows:
            match = pattern.search(str(row.get(field, "")))
            if match:
                max_no = max(max_no, int(match.group(1)))
        return f"{prefix}-{max_no + 1:04d}"

    def get_plan(self, ref: Any) -> dict[str, Any] | None:
        """按 plan 主键 / PLAN 编号 / 关联导出任务 id 或任务编号查找。"""
        if ref is None:
            return None
        text = str(ref).strip()
        with self._lock:
            for plan in self._rows():
                if text in {
                    str(plan.get("id", "")),
                    str(plan.get("plan_no", "")),
                    str(plan.get("task_id", "")),
                    str(plan.get("task_no", "")),
                    str(plan.get("export_no", "")),
                }:
                    return plan
            # 允许 /batch_export/<数字行id>
            if text.isdigit():
                task = store.find("batch_export", int(text))
                if task is not None:
                    task_no = str(task.get("任务编号", ""))
                    for plan in self._rows():
                        if str(plan.get("task_no", "")) == task_no or plan.get("task_id") == int(text):
                            return plan
        return None

    def list_plans(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._rows())

    # ---- 策略行读取 ----
    @staticmethod
    def find_strategy(ref: Any = None, point_id: str | None = None) -> dict[str, Any] | None:
        rows = store.rows("ts_store")
        if ref is not None and str(ref) != "":
            text = str(ref)
            for row in rows:
                if text in {str(row.get("id", "")), str(row.get("策略编号", ""))}:
                    return row
        if point_id:
            for row in rows:
                if row.get("测点编号") == point_id:
                    return row
        return None

    @staticmethod
    def policy_from_strategy(row: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """从冷热策略表行提取同一套策略参数；显式入参优先。"""
        params: dict[str, Any] = {
            "retention_days": row.get("保留天数", 30),
            "hot_days": row.get("热层天数", None)
            if row.get("热层天数", None) not in (None, "")
            else row.get("冷热分层", 30),
            "compression": row.get("压缩算法", "none"),
            "points_per_day": row.get("每日点数", POINTS_PER_DAY) or POINTS_PER_DAY,
            "bytes_per_point": row.get("每点字节", BYTES_PER_POINT) or BYTES_PER_POINT,
        }
        for key, value in (overrides or {}).items():
            if value is not None and value != "":
                params[key] = value
        return params

    # ---- 创建计划 ----
    def create_plan(
        self,
        *,
        retention_days: Any = None,
        hot_days: Any = None,
        requested_days: Any = None,
        points_per_day: Any = None,
        bytes_per_point: Any = None,
        compression: Any = None,
        compression_ratio: Any = None,
        today: Any = None,
        strategy_ref: Any = None,
        point_id: str | None = None,
        device_range: str = "",
        task_ref: Any = None,
        create_task: bool = True,
        **extra: Any,
    ) -> dict[str, Any]:
        with self._lock:
            strategy = self.find_strategy(strategy_ref, point_id)
            # 调用方显式传入的参数（None 表示未传）
            overrides = {
                "retention_days": retention_days,
                "hot_days": hot_days,
                "requested_days": requested_days,
                "points_per_day": points_per_day,
                "bytes_per_point": bytes_per_point,
                "compression": compression,
                "compression_ratio": compression_ratio,
                "today": today,
            }
            if strategy is not None:
                policy_params = self.policy_from_strategy(strategy, overrides)
                point_id = point_id or str(strategy.get("测点编号", ""))
                strategy_no = str(strategy.get("策略编号", ""))
                strategy_pk = strategy.get("id")
            else:
                # 无策略行绑定时使用场景默认：每天100点、每点32字节、无压缩
                policy_params = {
                    "retention_days": retention_days if retention_days is not None else 5,
                    "hot_days": hot_days if hot_days is not None else 30,
                    "requested_days": requested_days,
                    "points_per_day": points_per_day if points_per_day is not None else POINTS_PER_DAY,
                    "bytes_per_point": bytes_per_point if bytes_per_point is not None else BYTES_PER_POINT,
                    "compression": compression or "none",
                    "compression_ratio": compression_ratio,
                    "today": today,
                }
                strategy_no = str(strategy_ref or "")
                strategy_pk = None

            plan_data = build_read_plan(**{k: v for k, v in policy_params.items() if v is not None})

            # 冷段转为有状态 segment；热层/超期日期保留在 dates 中。
            segments: list[dict[str, Any]] = []
            for day in plan_data["dates"]:
                if day["tier"] != "cold":
                    continue
                segments.append(self._new_segment(day))

            rows = self._rows()
            plan_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            plan_no = f"PLAN-{plan_id:04d}"

            task_id = None
            task_no = ""
            task_row: dict[str, Any] | None = None
            if task_ref is not None:
                task_row = self.get_batch_task(task_ref)
            if task_row is None and create_task:
                task_row = self._create_batch_task(
                    plan_data, point_id=point_id or strategy_no,
                    device_range=device_range, plan_no=plan_no,
                )
            if task_row is not None:
                task_id = task_row.get("id")
                task_no = str(task_row.get("任务编号", ""))

            now = datetime.now(CST).isoformat(timespec="seconds")
            plan: dict[str, Any] = {
                "id": plan_id,
                "plan_no": plan_no,
                "created_at": now,
                "updated_at": now,
                "strategy_ref": strategy_no or (str(strategy_ref) if strategy_ref is not None else ""),
                "strategy_pk": strategy_pk,
                "point_id": point_id or "",
                "device_range": device_range,
                "task_id": task_id,
                "task_no": task_no,
                "export_no": task_no,
                "status": "prepared",
                "plan_status": "待执行",
                "has_conflict": False,
                "conflict": None,
                "conflicts": [],
                "segments": segments,
                "events": [{
                    "at": now, "type": "plan_created",
                    "message": (f"分层读取计划 {plan_no} 已生成：热层 {plan_data['hot_days']} 天、"
                                f"冷层 {plan_data['cold_days']} 天，"
                                f"{plan_data['unavailable_days']} 天超出保留期不可导出"),
                }],
                **plan_data,
            }
            plan["billing"] = self._billing_snapshot(plan)
            plan["fee"] = plan["billing"]
            plan["cost"] = plan["billing"]
            rows.append(plan)

            if task_row is not None:
                task_row["read_plan_no"] = plan_no
                task_row["read_plan"] = self.summary(plan)
            return plan

    @staticmethod
    def get_batch_task(ref: Any) -> dict[str, Any] | None:
        if ref is None:
            return None
        text = str(ref)
        rows = store.rows("batch_export")
        if text.isdigit():
            found = store.find("batch_export", int(text))
            if found is not None:
                return found
        for row in rows:
            if text in {str(row.get("任务编号", "")), str(row.get("id", ""))}:
                return row
        return None

    def _create_batch_task(self, plan_data: dict[str, Any], *, point_id: str,
                           device_range: str, plan_no: str) -> dict[str, Any]:
        rows = store.rows("batch_export")
        task_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        today = date.fromisoformat(plan_data["today"])
        q = plan_data["requested_days"]
        start_day = today - timedelta(days=max(q - 1, 0))
        _, end_iso = _day_interval(today)
        start_iso, _ = _day_interval(start_day)
        task = {
            "id": task_id,
            "任务编号": f"EXP-{task_id:04d}",
            "设备范围": device_range or "全部设备",
            "测点列表": point_id or "全部测点",
            "时间范围": f"{start_iso} ~ {end_iso}",
            "导出格式": "CSV",
            "数据行数": str(plan_data["scheduled_points"]),
            "文件大小": f"{round(plan_data['scheduled_bytes'] / 1024, 2)}KB",
            "导出状态": "待执行",
            "status": "待执行",
            "pending": True,
            "abnormal": False,
            "plan_no": plan_no,
        }
        rows.append(task)
        return task

    def _new_segment(self, day: dict[str, Any]) -> dict[str, Any]:
        raw_per_day = day["points"] * BYTES_PER_POINT
        frag_count = FRAGMENTS_PER_DAY
        frag_bytes = day["bytes"] / frag_count if frag_count else day["bytes"]
        start = datetime.fromisoformat(day["interval_start"])
        fragments = []
        for i in range(frag_count):
            f_start = start + timedelta(seconds=86400 * i // frag_count)
            f_end = start + timedelta(seconds=86400 * (i + 1) // frag_count)
            size = frag_bytes
            if isinstance(day["bytes"], int) and day["bytes"] % frag_count == 0:
                size = day["bytes"] // frag_count
            fragments.append({
                "fragment_id": f"{day['tier']}-{day['date']}-f{i}",
                "index": i,
                "interval_start": f_start.isoformat(timespec="seconds"),
                "interval_end": f_end.isoformat(timespec="seconds"),
                "bytes": size,
                "sent": False,
                "delivered": False,
                "send_count": 0,
                "sent_at": None,
            })
        return {
            "segment_id": f"{day['tier']}-{day['date']}",
            "date": day["date"],
            "offset": day["offset"],
            "tier": day["tier"],
            "tier_name": day["tier_name"],
            "interval_start": day["interval_start"],
            "interval_end": day["interval_end"],
            "read_interval": day["read_interval"],
            "points": day["points"],
            "bytes": day["bytes"],
            "status": state_value(PENDING),
            "state": state_value(PENDING),
            "state_code": state_code(PENDING),
            "status_code": state_code(PENDING),
            "code": state_code(PENDING),
            "display_status": display_status(PENDING),
            "released": False,
            "unreadable": False,
            "claimed": False,
            "delivered": False,
            "billable": False,
            "billed_bytes": 0,
            "claim_ticket": None,
            "claimed_at": None,
            "confirmed_at": None,
            "released_at": None,
            "release_reason": "",
            "fragments": fragments,
            "sent_fragments": 0,
            "delivered_fragments": 0,
        }

    # ---- 序列化摘要（避免把 StateValue 带到外部判断之外的问题） ----
    @staticmethod
    def _public_segment(seg: dict[str, Any]) -> dict[str, Any]:
        status = str(seg["status"])
        code = state_code(status)
        released = bool(seg.get("released"))
        return {
            **seg,
            "status": status,
            "state": status,
            "state_code": code,
            "status_code": code,
            "code": code,
            "display_status": display_status(status, released),
        }

    def summary(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "plan_no": plan.get("plan_no"),
            "task_no": plan.get("task_no"),
            "has_conflict": plan.get("has_conflict"),
            "hot_days": plan.get("hot_days"),
            "cold_days": plan.get("cold_days"),
            "schedulable_days": plan.get("schedulable_days"),
            "billable_bytes": (plan.get("billing") or {}).get("billable_bytes"),
        }

    # ---- 计费 ----
    def _billing_snapshot(self, plan: dict[str, Any]) -> dict[str, Any]:
        segments = plan.get("segments", [])
        cold = [s for s in segments if s.get("tier") == "cold"]
        delivered = [s for s in cold if str(s.get("status")) == DELIVERED]
        waiting = [s for s in cold if str(s.get("status")) == WAITING_READ]
        unreadable = [s for s in cold if str(s.get("status")) == UNREADABLE]
        pending = [s for s in cold if str(s.get("status")) == PENDING]

        daily = plan["daily_bytes"]
        hot_days = plan["hot_days"]
        hot_bytes = plan["hot_bytes"]
        delivered_cold_bytes = sum(s["bytes"] for s in delivered)
        billable_bytes = hot_bytes + delivered_cold_bytes
        billable_days = hot_days + len(delivered)
        retrievable_cold = delivered + waiting + pending
        retrievable_days = hot_days + len(retrievable_cold)
        retrievable_bytes = hot_bytes + sum(s["bytes"] for s in retrievable_cold)
        # “实际可取范围”：当前策略下仍存在的热层 + 未被清理的冷段
        actual_available_days = retrievable_days
        actual_available_bytes = retrievable_bytes

        rate = float((plan.get("budget") or {}).get("price_per_point", 0.0) or 0.0)
        billable_points = billable_days * plan["points_per_day"]
        return {
            "basis": "actual_retrievable_range",
            "rule": "仅实际保留且读取确认（冷段）的日期可计费；无有效读取依据的日期不计费",
            "hot_days": hot_days,
            "delivered_cold_days": len(delivered),
            "confirmed_cold_days": len(delivered),
            "billable_cold_days": len(delivered),
            "waiting_read_days": len(waiting),
            "unreadable_days": len(unreadable),
            "released_days": len(unreadable),
            "pending_days": len(pending),
            "billable_days": billable_days,
            "charged_days": billable_days,
            "chargeable_days": billable_days,
            "actual_available_days": actual_available_days,
            "retrievable_days": retrievable_days,
            "billable_points": billable_points,
            "billable_bytes": billable_bytes,
            "charged_bytes": billable_bytes,
            "chargeable_bytes": billable_bytes,
            "retrievable_bytes": retrievable_bytes,
            "actual_available_bytes": actual_available_bytes,
            "scheduled_days": plan.get("schedulable_days"),
            "scheduled_bytes": plan.get("scheduled_bytes"),
            "already_sent_not_resent": True,
            "price_per_point": rate,
            "amount": round(billable_points * rate, 4),
            "currency": "CNY",
        }

    def detail(self, ref: Any) -> dict[str, Any] | None:
        plan = self.get_plan(ref)
        if plan is None:
            return None
        with self._lock:
            snapshot = self._billing_snapshot(plan)
            plan["billing"] = snapshot
            plan["fee"] = snapshot
            plan["cost"] = snapshot
            plan["费用"] = snapshot
            plan["预算"] = plan.get("budget")
            return {
                **plan,
                "segments": [self._public_segment(s) for s in plan["segments"]],
                "billing": plan["billing"],
                "fee": snapshot,
                "cost": snapshot,
            }

    # ---- 冷段领取 ----
    def _select_segments(self, plan: dict[str, Any], *,
                         segment_ids: list[str] | None = None,
                         dates: list[str] | None = None,
                         count: int | None = None,
                         pick: str = "older") -> list[dict[str, Any]]:
        cold = [s for s in plan["segments"] if s.get("tier") == "cold"]
        chosen: list[dict[str, Any]] = []
        if segment_ids:
            wanted = {str(x) for x in segment_ids}
            chosen = [s for s in cold if s["segment_id"] in wanted
                      or str(s.get("id", "")) in wanted]
        elif dates:
            wanted_dates = {str(x)[:10] for x in dates}
            chosen = [s for s in cold if s["date"] in wanted_dates]
        elif count is not None:
            # offset 越大越早；older = 最早的 N 天
            ordered = sorted(cold, key=lambda s: s["offset"], reverse=(pick != "newer"))
            chosen = ordered[:max(0, count)]
        return chosen

    def claim(self, ref: Any, *, segment_ids: list[str] | None = None,
              dates: list[str] | None = None, count: int | None = None,
              pick: str = "older") -> dict[str, Any]:
        with self._lock:
            plan = self.get_plan(ref)
            if plan is None:
                return {"ok": False, "message": f"导出读取计划 {ref} 不存在"}
            targets = self._select_segments(
                plan, segment_ids=segment_ids, dates=dates, count=count, pick=pick)
            if not targets:
                return {"ok": False, "message": "没有符合条件的冷段可领取"}
            now = datetime.now(CST).isoformat(timespec="seconds")
            claimed: list[dict[str, Any]] = []
            for seg in targets:
                status = str(seg["status"])
                if status == UNREADABLE:
                    continue
                if status == DELIVERED:
                    claimed.append(seg)
                    continue
                seg["status"] = state_value(WAITING_READ)
                seg["state"] = state_value(WAITING_READ)
                seg["state_code"] = state_code(WAITING_READ)
                seg["claimed"] = True
                seg["claim_ticket"] = seg.get("claim_ticket") or f"ticket-{seg['segment_id']}"
                seg["claimed_at"] = seg.get("claimed_at") or now
                claimed.append(seg)
            if not claimed:
                return {"ok": False, "message": "所选冷段均已被释放，无法领取",
                        "segments": [self._public_segment(s) for s in targets]}
            plan["updated_at"] = now
            plan["events"].append({
                "at": now, "type": "segments_claimed",
                "message": f"已领取 {len(claimed)} 个冷日，等待读取确认",
                "segment_ids": [s["segment_id"] for s in claimed],
            })
            self._sync_task_status(plan)
            plan["billing"] = self._billing_snapshot(plan)
            return {
                "ok": True,
                "message": f"已领取 {len(claimed)} 个冷段，读取依据待确认",
                "plan_no": plan["plan_no"],
                "segments": [self._public_segment(s) for s in claimed],
                "plan": self.detail(plan["plan_no"]),
            }

    # ---- 读取片段（幂等：已发出的片段不重发） ----
    def read_fragments(self, ref: Any, segment_ref: str) -> dict[str, Any]:
        with self._lock:
            plan = self.get_plan(ref)
            if plan is None:
                return {"ok": False, "message": f"导出读取计划 {ref} 不存在"}
            seg = self._find_segment(plan, segment_ref)
            if seg is None:
                return {"ok": False, "message": f"冷段 {segment_ref} 不存在"}
            status = str(seg["status"])
            if status == UNREADABLE:
                return {"ok": False, "readable": False,
                        "message": f"冷段 {seg['date']} 已被释放，服务端无有效读取依据",
                        "segment": self._public_segment(seg)}
            if status == PENDING:
                # 读取本身也构成领取
                self.claim(ref, segment_ids=[seg["segment_id"]])
            now = datetime.now(CST).isoformat(timespec="seconds")
            newly_sent = []
            already_sent = []
            for frag in seg["fragments"]:
                if frag["sent"]:
                    already_sent.append(frag["fragment_id"])
                    continue
                frag["sent"] = True
                frag["sent_at"] = now
                frag["send_count"] = int(frag.get("send_count", 0)) + 1
                newly_sent.append(frag["fragment_id"])
            seg["sent_fragments"] = sum(1 for f in seg["fragments"] if f["sent"])
            plan["updated_at"] = now
            return {
                "ok": True,
                "message": (f"片段已发送 {len(newly_sent)} 个；"
                            f"{len(already_sent)} 个此前已发出，不会重发"),
                "segment": self._public_segment(seg),
                "sent_fragments": newly_sent,
                "already_sent_fragments": already_sent,
                "resent_fragments": [],
                "resent_count": 0,
            }

    def confirm_segment(self, ref: Any, segment_ref: str) -> dict[str, Any]:
        with self._lock:
            plan = self.get_plan(ref)
            if plan is None:
                return {"ok": False, "message": f"导出读取计划 {ref} 不存在"}
            seg = self._find_segment(plan, segment_ref)
            if seg is None:
                return {"ok": False, "message": f"冷段 {segment_ref} 不存在"}
            status = str(seg["status"])
            if status == UNREADABLE:
                plan["billing"] = self._billing_snapshot(plan)
                return {"ok": False, "chargeable": False,
                        "message": f"冷段 {seg['date']} 在确认前已被释放，无有效读取依据，不能收费",
                        "segment": self._public_segment(seg),
                        "billing": plan["billing"]}
            now = datetime.now(CST).isoformat(timespec="seconds")
            # 确认时确保片段都已发出（不重复发送已发片段）
            send_result = self.read_fragments(ref, segment_ref)
            for frag in seg["fragments"]:
                frag["delivered"] = True
            seg["status"] = state_value(DELIVERED)
            seg["state"] = state_value(DELIVERED)
            seg["state_code"] = state_code(DELIVERED)
            seg["delivered"] = True
            seg["billable"] = True
            seg["billed_bytes"] = seg["bytes"]
            seg["confirmed_at"] = now
            seg["delivered_fragments"] = len(seg["fragments"])
            seg["sent_fragments"] = sum(1 for f in seg["fragments"] if f["sent"])
            plan["updated_at"] = now
            plan["events"].append({
                "at": now, "type": "segment_delivered",
                "message": f"冷段 {seg['date']} 已读取确认并交付，计入费用",
                "segment_id": seg["segment_id"],
            })
            self._sync_task_status(plan)
            plan["billing"] = self._billing_snapshot(plan)
            return {
                "ok": True,
                "message": f"冷段 {seg['date']} 已交付，按实际读取区间计费",
                "segment": self._public_segment(seg),
                "send_detail": send_result,
                "billing": plan["billing"],
                "plan": self.detail(plan["plan_no"]),
            }

    def complete_segment(self, ref: Any, segment_ref: str) -> dict[str, Any]:
        """“继续完成”：对仍有有效读取依据的等待冷段读完并确认（幂等）。"""
        seg = self._find_segment(self.get_plan(ref) or {}, segment_ref)
        if seg is not None and str(seg.get("status")) == DELIVERED:
            return {"ok": True, "message": "该冷段已交付，无需重复完成；已发片段不会重发",
                    "segment": self._public_segment(seg)}
        return self.confirm_segment(ref, segment_ref)

    def _find_segment(self, plan: dict[str, Any], segment_ref: str) -> dict[str, Any] | None:
        text = str(segment_ref)
        for seg in plan.get("segments", []):
            if text in {seg["segment_id"], seg["date"], str(seg.get("offset", ""))}:
                return seg
        return None

    def _sync_task_status(self, plan: dict[str, Any]) -> None:
        task = self.get_batch_task(plan.get("task_no") or plan.get("task_id"))
        if task is None:
            return
        any_active = any(str(s["status"]) in {WAITING_READ, DELIVERED} for s in plan["segments"])
        all_done = all(str(s["status"]) in {DELIVERED, UNREADABLE} for s in plan["segments"]) and plan["segments"]
        if all_done:
            task["status"] = "已完成"
            task["导出状态"] = "已完成"
            task["pending"] = False
        elif any_active:
            task["status"] = "导出中"
            task["导出状态"] = "导出中"
            task["pending"] = True

    # ---- 保留策略变更 / 冷层清理冲突 ----
    def apply_retention_change(
        self,
        ref: Any = None,
        *,
        retention_days: Any = None,
        hot_days: Any = None,
        strategy_ref: Any = None,
        persist_strategy: bool = True,
        cleanup: bool = True,
    ) -> dict[str, Any]:
        """修改保留策略并让所有在途计划与新策略对齐。

        1. 持久化到冷热策略表（可选）；
        2. 用同一套新策略重算每个在途计划的日期/预算；
        3. 清理撞上已领取未确认冷段时，按 等待读取 / 不可读 / 已交付 分类。
        """
        with self._lock:
            strategy = self.find_strategy(strategy_ref) if strategy_ref is not None else None
            if ref is not None and strategy is None:
                maybe_plan = self.get_plan(ref)
                if maybe_plan is not None and maybe_plan.get("strategy_ref"):
                    strategy = self.find_strategy(maybe_plan["strategy_ref"])

            new_r = parse_days(retention_days, -1)
            new_h = parse_days(hot_days, -1)

            if strategy is not None and persist_strategy:
                if new_r >= 0:
                    strategy["保留天数"] = str(new_r)
                if new_h >= 0:
                    strategy["冷热分层"] = f"{new_h}d_hot"
                    strategy["热层天数"] = str(new_h)

            # 确定需要协调的计划
            if ref is not None and self.get_plan(ref) is not None:
                plans = [self.get_plan(ref)]
            elif strategy is not None:
                sid = str(strategy.get("策略编号", ""))
                pk = strategy.get("id")
                point_id = strategy.get("测点编号")
                plans = [
                    p for p in self._rows()
                    if p.get("strategy_ref") == sid or p.get("strategy_pk") == pk
                    or (point_id and p.get("point_id") == point_id)
                ]
            else:
                plans = list(self._rows())

            reports = []
            for plan in plans:
                overrides: dict[str, Any] = {}
                if new_r >= 0:
                    overrides["retention_days"] = new_r
                if new_h >= 0:
                    overrides["hot_days"] = new_h
                elif strategy is not None:
                    overrides["hot_days"] = strategy.get("冷热分层", plan["hot_days_configured"])
                if strategy is not None:
                    params = self.policy_from_strategy(strategy, overrides)
                else:
                    params = {
                        "retention_days": overrides.get("retention_days", plan["retention_days"]),
                        "hot_days": overrides.get("hot_days", plan["hot_days_configured"]),
                        "points_per_day": plan["points_per_day"],
                        "bytes_per_point": plan["bytes_per_point"],
                        "compression": plan["compression"],
                        "compression_ratio": plan["compression_ratio"],
                        "today": plan["today"],
                        "requested_days": plan["requested_days"],
                    }
                report = self.reconcile_plan(plan, params, cleanup=cleanup)
                reports.append(report)

            primary = reports[0] if reports else {"ok": True, "classification": self._empty_buckets()}
            return {
                "ok": True,
                "message": primary.get("message", "保留策略已更新"),
                "strategy": strategy,
                "reports": reports,
                **(primary if len(reports) == 1 else {}),
            }

    @staticmethod
    def _empty_buckets() -> dict[str, list]:
        return {"waiting_read": [], "unreadable": [], "delivered": [],
                "等待读取": [], "不可读": [], "已交付": []}

    def reconcile_plan(self, plan: dict[str, Any], new_policy_params: dict[str, Any],
                       *, cleanup: bool = True) -> dict[str, Any]:
        """按新策略重算计划，并对冷段做三分类。"""
        new_data = build_read_plan(**{k: v for k, v in new_policy_params.items() if v is not None})
        valid_cold = {d["date"] for d in new_data["dates"] if d["tier"] == "cold"}
        valid_hot = {d["date"] for d in new_data["dates"] if d["tier"] == "hot"}
        valid_retained = valid_cold | valid_hot

        before_r = plan["retention_days"]
        before_h = plan["hot_days"]

        buckets = self._empty_buckets()
        released_now: list[dict[str, Any]] = []
        still_waiting: list[dict[str, Any]] = []
        delivered: list[dict[str, Any]] = []

        now = datetime.now(CST).isoformat(timespec="seconds")
        for seg in plan["segments"]:
            status = str(seg["status"])
            in_cold = seg["date"] in valid_cold
            in_retained = seg["date"] in valid_retained

            if status == DELIVERED:
                buckets["delivered"].append(seg)
                buckets["已交付"].append(seg)
                delivered.append(seg)
                continue

            if status == WAITING_READ:
                if in_retained and cleanup:
                    # 读取依据仍然有效，继续等待读取
                    buckets["waiting_read"].append(seg)
                    buckets["等待读取"].append(seg)
                    still_waiting.append(seg)
                else:
                    # 清理先于确认到达：领取依据失效，冷段被释放 -> 不可读、不收费
                    seg["status"] = state_value(UNREADABLE)
                    seg["state"] = state_value(UNREADABLE)
                    seg["state_code"] = state_code(UNREADABLE)
                    seg["released"] = True
                    seg["unreadable"] = True
                    seg["released_at"] = now
                    seg["release_reason"] = (
                        "retention_shrunk_cleanup" if not in_retained else "cold_tier_cleanup")
                    seg["billable"] = False
                    seg["billed_bytes"] = 0
                    buckets["unreadable"].append(seg)
                    buckets["不可读"].append(seg)
                    released_now.append(seg)
                continue

            if status == PENDING:
                if not in_retained:
                    seg["status"] = state_value(UNREADABLE)
                    seg["state"] = state_value(UNREADABLE)
                    seg["state_code"] = state_code(UNREADABLE)
                    seg["released"] = True
                    seg["unreadable"] = True
                    seg["released_at"] = now
                    seg["release_reason"] = "out_of_retention_cleanup"
                    buckets["unreadable"].append(seg)
                    buckets["不可读"].append(seg)
                    released_now.append(seg)
                continue

            if status == UNREADABLE:
                buckets["unreadable"].append(seg)
                buckets["不可读"].append(seg)

        # 用新策略的日期/容量结果替换计划顶层（保留 id/事件/segment 状态）
        preserved = {
            "id": plan["id"], "plan_no": plan["plan_no"], "created_at": plan["created_at"],
            "task_id": plan.get("task_id"), "task_no": plan.get("task_no"),
            "export_no": plan.get("export_no"), "strategy_ref": plan.get("strategy_ref"),
            "strategy_pk": plan.get("strategy_pk"), "point_id": plan.get("point_id"),
            "device_range": plan.get("device_range"), "events": plan.get("events", []),
        }
        plan.update(new_data)
        plan.update(preserved)
        plan["updated_at"] = now

        conflict = None
        if released_now:
            conflict = {
                "type": "retention_conflict",
                "at": now,
                "before": {"retention_days": before_r, "hot_days": before_h},
                "after": {"retention_days": new_data["retention_days"],
                          "hot_days_configured": new_data["hot_days_configured"],
                          "hot_days": new_data["hot_days"]},
                "message": (
                    f"保留策略由 {before_r} 天收缩为 {new_data['retention_days']} 天，"
                    f"与在途读取计划 {plan['plan_no']} 冲突："
                    f"{len(released_now)} 个已领取但未确认的冷段已被清理释放（不可读、不计费），"
                    f"{len(still_waiting)} 个冷段仍可继续完成，"
                    f"{len(delivered)} 个冷段已交付并保留计费。"
                ),
                "released_segment_ids": [s["segment_id"] for s in released_now],
                "waiting_segment_ids": [s["segment_id"] for s in still_waiting],
                "delivered_segment_ids": [s["segment_id"] for s in delivered],
            }
            plan["has_conflict"] = True
            plan["conflict"] = conflict
            plan.setdefault("conflicts", []).append(conflict)
            plan["events"].append({"at": now, "type": "retention_conflict",
                                   "message": conflict["message"]})
        plan["billing"] = self._billing_snapshot(plan)
        self._sync_task_status(plan)

        public_buckets = {
            key: [self._public_segment(s) for s in segs]
            for key, segs in buckets.items()
        }
        return {
            "ok": True,
            "plan_no": plan["plan_no"],
            "conflict": conflict,
            "classification": {
                "waiting_read": public_buckets["waiting_read"],
                "unreadable": public_buckets["unreadable"],
                "delivered": public_buckets["delivered"],
            },
            # 同时给中文键，方便直接核对
            "冷段分类": {
                "等待读取": public_buckets["等待读取"],
                "不可读": public_buckets["不可读"],
                "已交付": public_buckets["已交付"],
            },
            "waiting_read": public_buckets["waiting_read"],
            "unreadable": public_buckets["unreadable"],
            "delivered": public_buckets["delivered"],
            "released_segments": public_buckets["unreadable"],
            "billing": plan["billing"],
            "plan": self.detail(plan["plan_no"]),
            "message": conflict["message"] if conflict else "冷层清理完成，未撞上未确认的在途读取",
        }

    def cleanup_cold_tier(self, ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        """冷层清理入口：可同时调整保留策略并返回三分类。"""
        if kwargs.get("retention_days") is not None or kwargs.get("strategy_ref") is not None:
            return self.apply_retention_change(ref, cleanup=True, **kwargs)
        # 不带新策略：按计划当前策略执行一次清理分类（幂等）
        with self._lock:
            plan = self.get_plan(ref) if ref is not None else (self._rows()[-1] if self._rows() else None)
            if plan is None:
                return {"ok": False, "message": "没有可清理的在途导出计划"}
            params = {
                "retention_days": plan["retention_days"],
                "hot_days": plan["hot_days_configured"],
                "points_per_day": plan["points_per_day"],
                "bytes_per_point": plan["bytes_per_point"],
                "compression": plan["compression"],
                "compression_ratio": plan["compression_ratio"],
                "today": plan["today"],
                "requested_days": plan["requested_days"],
            }
            return self.reconcile_plan(plan, params, cleanup=True)


tiered_exports = TieredExportService()
