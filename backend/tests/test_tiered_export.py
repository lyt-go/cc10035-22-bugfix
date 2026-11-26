"""冷热分层容量与跨层导出冲突流程的回归测试。

覆盖任务描述中的验收点：
- 5 天保留 + 30 天热层、每天100点、每点32字节、无压缩 -> 热16000 / 冷0；
- 两层日期不重叠、热层截断、字节和等于总量；
- 8/3 策略准备 10 天导出 -> 热3、冷5、2 天超期；
- 领取 3 个较早冷日未确认时保留改 5 -> 三分类正确、释放段不计费、片段不重发。
"""
from __future__ import annotations

import pytest

from app.services.tiered_plan import (
    DELIVERED,
    UNREADABLE,
    WAITING_READ,
    build_read_plan,
    tiered_exports,
)
from app.services.ts_store import TsStoreService
from app.store import store


@pytest.fixture(autouse=True)
def _isolated_plans():
    """每个测试前后清空跨层计划，避免单例状态互相干扰。"""
    tiered_exports.reset()
    yield
    tiered_exports.reset()


def test_capacity_five_retention_thirty_hot():
    plan = build_read_plan(
        retention_days=5, hot_days=30,
        points_per_day=100, bytes_per_point=32, compression="none",
        today="2026-11-26",
    )
    assert plan["hot_bytes"] == 16000
    assert plan["cold_bytes"] == 0
    assert plan["total_bytes"] == 16000
    assert plan["hot_days"] == 5
    assert plan["cold_days"] == 0
    assert plan["bytes_sum_check"] == plan["total_bytes"]


def test_tier_dates_do_not_overlap_and_sum():
    plan = build_read_plan(
        retention_days=8, hot_days=3,
        points_per_day=100, bytes_per_point=32, compression="none",
        requested_days=10, today="2026-11-26",
    )
    hot_dates = set(plan["tiers"][0]["dates"])
    cold_dates = set(plan["tiers"][1]["dates"])
    assert len(hot_dates) == 3
    assert len(cold_dates) == 5
    assert hot_dates.isdisjoint(cold_dates)
    assert plan["unavailable_days"] == 2
    assert plan["hot_bytes"] + plan["cold_bytes"] == plan["total_bytes"] == 25600
    # 每个日期都带可核对的读取区间
    assert all("T00:00:00" in d["interval_start"] for d in plan["dates"] if d["available"])


def test_estimate_storage_fixes_negative_cold():
    rows = store.rows("ts_store")
    rows.append({
        "id": 9001, "status": "已生效", "pending": False, "abnormal": False,
        "策略编号": "TS-TEST-CAP", "测点编号": "TP-TEST-CAP",
        "存储引擎": "tsdb", "压缩算法": "none", "降采样规则": "",
        "保留天数": "5", "分段策略": "daily", "冷热分层": "30d_hot",
    })
    try:
        result = TsStoreService().estimate_storage("TP-TEST-CAP", data_points_per_day=100)
        assert result["ok"] is True
        assert result["hot_storage_bytes"] == 16000
        assert result["cold_storage_bytes"] == 0
        assert result["compressed_bytes_estimate"] == 16000
    finally:
        rows[:] = [r for r in rows if r.get("id") != 9001]


def test_conflict_flow_three_claimed_older_days_released():
    plan = tiered_exports.create_plan(
        retention_days=8, hot_days=3, requested_days=10,
        points_per_day=100, bytes_per_point=32, compression="none",
        today="2026-11-26", create_task=False,
    )
    assert plan["hot_days"] == 3 and plan["cold_days"] == 5

    claimed = tiered_exports.claim(plan["plan_no"], count=3, pick="older")
    assert claimed["ok"] is True
    assert len(claimed["segments"]) == 3
    assert all(str(s["status"]) == WAITING_READ for s in claimed["segments"])

    report = tiered_exports.apply_retention_change(plan["plan_no"], retention_days=5)
    clf = report["classification"]
    assert len(clf["unreadable"]) == 3
    assert len(clf["waiting_read"]) == 0
    assert len(clf["delivered"]) == 0

    detail = tiered_exports.detail(plan["plan_no"])
    assert detail["has_conflict"] is True
    # 未确认即被释放的日期不收费：只有热层 3 天
    assert detail["billing"]["billable_days"] == 3
    assert detail["billing"]["billable_bytes"] == 9600

    # 对已释放段做确认必须失败且不产生费用
    dead_id = clf["unreadable"][0]["segment_id"]
    bad = tiered_exports.confirm_segment(plan["plan_no"], dead_id)
    assert bad["ok"] is False
    assert bad["chargeable"] is False

    # 仍在保留期内的两个冷日可以继续完成并计费
    survivors = [s for s in detail["segments"]
                 if s["state_code"] == "pending"]
    assert len(survivors) == 2
    ok = tiered_exports.complete_segment(plan["plan_no"], survivors[0]["segment_id"])
    assert ok["ok"] is True
    detail = tiered_exports.detail(plan["plan_no"])
    assert detail["billing"]["billable_days"] == 4
    assert detail["billing"]["billable_bytes"] == 12800


def test_fragments_sent_once_and_complete_idempotent():
    plan = tiered_exports.create_plan(
        retention_days=8, hot_days=3, requested_days=8,
        points_per_day=100, bytes_per_point=32, compression="none",
        today="2026-11-26", create_task=False,
    )
    seg_id = plan["segments"][0]["segment_id"]
    first = tiered_exports.read_fragments(plan["plan_no"], seg_id)
    assert len(first["sent_fragments"]) == 4
    second = tiered_exports.read_fragments(plan["plan_no"], seg_id)
    assert second["sent_fragments"] == []
    assert len(second["already_sent_fragments"]) == 4
    assert second["resent_count"] == 0

    done1 = tiered_exports.complete_segment(plan["plan_no"], seg_id)
    assert done1["ok"] is True
    done2 = tiered_exports.complete_segment(plan["plan_no"], seg_id)
    assert done2["ok"] is True
    detail = tiered_exports.detail(plan["plan_no"])
    seg = next(s for s in detail["segments"] if s["segment_id"] == seg_id)
    assert seg["state_code"] == "delivered"
    assert seg["status"] == DELIVERED
    # 每个片段只发送过一次
    assert all(f["send_count"] == 1 for f in seg["fragments"])


def test_state_string_equals_chinese_and_english_codes():
    plan = tiered_exports.create_plan(
        retention_days=6, hot_days=2, today="2026-11-26", create_task=False)
    tiered_exports.claim(plan["plan_no"], count=1)
    detail = tiered_exports.detail(plan["plan_no"])
    seg = next(s for s in detail["segments"] if s["state_code"] == "waiting_read")
    assert seg["status"] == "等待读取"
    # 服务层原始 StateValue 同时可与英文码比较
    raw = tiered_exports.get_plan(plan["plan_no"])
    raw_seg = next(s for s in raw["segments"] if str(s["status"]) == WAITING_READ)
    assert raw_seg["state"] == "waiting_read"
    assert raw_seg["state"] == WAITING_READ
