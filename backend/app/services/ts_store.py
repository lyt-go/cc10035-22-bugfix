"""时序存储领域服务：时序数据的存储策略、压缩与生命周期管理。

提供存储策略优化、压缩比估算、冷热数据分层计算等真实业务能力。

容量口径（与跨层导出共用 app.services.tiered_plan 中的同一套策略）：
- 日字节 = 每日点数 × 每点字节 / 压缩比；
- 热层实际天数 = min(配置热层天数, 总保留天数)，热层超长时只占实际留存天数；
- 冷层天数 = max(总保留 - 热层实际天数, 0)；
- 热层字节 + 冷层字节 = 总保留字节，冷层不会出现负数。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.tiered_plan import (
    BYTES_PER_POINT,
    POINTS_PER_DAY,
    build_read_plan,
    parse_compression,
    parse_days,
    tiered_exports,
)
from app.store import store

CST = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


class TsStoreService(BaseModuleService):
    MODULE = "ts_store"
    ENTITY = "存储策略"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["策略编号", "测点编号", "存储引擎", "压缩算法", "降采样规则", "保留天数", "分段策略", "冷热分层"]
    REQUIRED_FIELDS = ["策略编号", "测点编号", "存储引擎"]
    STATUS_ORDER = ["草稿", "待验证", "已生效", "已停用"]
    ACTION_RULES = {"提交验证": "待验证", "标记生效": "已生效", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    # ---------- 容量 / 分层计划 ----------

    def capacity_plan(
        self,
        *,
        retention_days: Any = 5,
        hot_days: Any = 30,
        points_per_day: Any = POINTS_PER_DAY,
        data_points_per_day: Any = None,
        bytes_per_point: Any = BYTES_PER_POINT,
        compression: Any = "none",
        compression_ratio: Any = None,
        today: Any = None,
        requested_days: Any = None,
    ) -> dict[str, Any]:
        """无状态容量试算：直接按填写的总保留/热层天数计算。

        ``data_points_per_day`` 是旧参数名，保留兼容。
        """
        if data_points_per_day is not None:
            points_per_day = data_points_per_day
        return build_read_plan(
            retention_days=retention_days,
            hot_days=hot_days,
            points_per_day=points_per_day,
            bytes_per_point=bytes_per_point,
            compression=compression,
            compression_ratio=compression_ratio,
            today=today,
            requested_days=requested_days,
        )

    # 语义别名
    storage_capacity = capacity_plan
    preview_read_plan = capacity_plan
    build_tiered_read_plan = capacity_plan

    def _find_strategy_row(self, point_id: str) -> dict[str, Any] | None:
        """按测点编号/策略编号找策略；优先已生效，其次任意状态。"""
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("测点编号") == point_id and row.get("status") == "已生效":
                return row
        for row in rows:
            if row.get("策略编号") == point_id and row.get("status") == "已生效":
                return row
        for row in rows:
            if row.get("测点编号") == point_id or row.get("策略编号") == point_id:
                return row
        return None

    def estimate_storage(self, point_id: str, data_points_per_day: int = 86400) -> dict[str, Any]:
        """存储空间估算：根据采集间隔和保留天数预估存储需求。

        业务流程：
        1. 查找测点的存储策略
        2. 根据每日点数计算日数据量
        3. 乘以保留天数
        4. 考虑压缩比与冷热分层截断（热层天数不能超过总保留）
        """
        strategy = self._find_strategy_row(point_id)
        if strategy is None:
            return {"ok": False, "message": f"测点 {point_id} 无已生效的存储策略"}

        retention_days = parse_days(strategy.get("保留天数"), 30)
        hot_configured = parse_days(
            strategy.get("热层天数", strategy.get("冷热分层", 30)), 30)

        compressor = strategy.get("压缩算法", "none") or "none"
        _, compression_ratio = parse_compression(compressor, default=2)

        bytes_per_point = parse_days(strategy.get("每点字节"), BYTES_PER_POINT) or BYTES_PER_POINT
        points_per_day = parse_days(strategy.get("每日点数"), data_points_per_day) or data_points_per_day

        plan = build_read_plan(
            retention_days=retention_days,
            hot_days=hot_configured,
            points_per_day=points_per_day,
            bytes_per_point=bytes_per_point,
            compression=compressor,
            compression_ratio=compression_ratio if str(compressor) not in {"none", "无", "无压缩", "不压缩"} else 1,
        )

        raw_bytes = points_per_day * bytes_per_point * retention_days
        compressed_bytes = plan["total_bytes"]

        return {
            "ok": True,
            "point_id": point_id,
            "retention_days": retention_days,
            "hot_days_configured": hot_configured,
            "hot_days": plan["hot_days"],
            "cold_days": plan["cold_days"],
            "compression_algorithm": compressor,
            "compression_ratio": compression_ratio,
            "raw_bytes_estimate": raw_bytes,
            "compressed_bytes_estimate": compressed_bytes,
            "total_storage_bytes": plan["total_bytes"],
            "hot_storage_bytes": plan["hot_storage_bytes"],
            "cold_storage_bytes": plan["cold_storage_bytes"],
            "storage_total_bytes": plan["total_bytes"],
            "bytes_sum_check": plan["bytes_sum_check"],
            "tiers": plan["tiers"],
        }

    # ---------- 从冷热策略表发起跨层导出 ----------

    def submit_cross_tier_export(self, strategy_ref: Any = None, **params: Any) -> dict[str, Any]:
        """从冷热策略表的一行提交跨层导出，返回带实际读取区间的分层计划。"""
        strategy = tiered_exports.find_strategy(strategy_ref, params.get("point_id"))
        merged: dict[str, Any] = dict(params)
        if strategy is not None:
            policy = tiered_exports.policy_from_strategy(strategy, params)
            merged = {**policy, **{k: v for k, v in params.items() if v not in (None, "")}}
        result = tiered_exports.create_plan(strategy_ref=strategy_ref, **merged)
        return {"ok": True, "message": f"跨层导出计划已提交：{result['plan_no']}", "plan": result}

    create_cross_tier_export = submit_cross_tier_export
    prepare_cross_tier_export = submit_cross_tier_export

    def update_retention(self, strategy_ref: Any = None, retention_days: Any = None,
                         hot_days: Any = None, **kwargs: Any) -> dict[str, Any]:
        """修改保留/热层策略，并协调所有在途跨层导出计划。"""
        return tiered_exports.apply_retention_change(
            kwargs.pop("plan_ref", None),
            retention_days=retention_days,
            hot_days=hot_days,
            strategy_ref=strategy_ref,
            **kwargs,
        )

    def update_entry(self, entry_id: int, values: dict[str, Any]):
        """通用更新：保留/热层字段变化时，自动与在途跨层导出计划对账。"""
        entry, updated = super().update_entry(entry_id, values)
        if entry is None:
            return entry, updated
        policy_keys = {"保留天数", "冷热分层", "热层天数", "每日点数", "每点字节", "压缩算法"}
        if policy_keys & set(updated):
            try:
                tiered_exports.apply_retention_change(
                    None,
                    retention_days=entry.get("保留天数"),
                    hot_days=entry.get("热层天数", entry.get("冷热分层")),
                    strategy_ref=entry_id,
                    persist_strategy=False,
                )
            except Exception:  # 对账失败不应让策略保存失败
                pass
        return entry, updated

    def cleanup_cold_tier(self, strategy_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        return tiered_exports.cleanup_cold_tier(
            kwargs.pop("plan_ref", None), strategy_ref=strategy_ref, **kwargs)

    # 让通用动作入口也能触发“跨层导出”（参数可放在 remark 的 JSON 中）
    def run_action(self, entry_id: int, action: str, remark: str | None = None
                   ) -> tuple[dict[str, Any] | None, str]:
        if action in {"跨层导出", "提交跨层导出"}:
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            import json
            remark_params: dict[str, Any] = {}
            raw = remark if isinstance(remark, str) else entry.get("_action_params")
            if isinstance(raw, str) and raw.strip().startswith(("{", "[")):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        remark_params = parsed
                except ValueError:
                    remark_params = {}
            result = self.submit_cross_tier_export(entry_id, **remark_params)
            plan = result["plan"]
            entry["last_plan_no"] = plan["plan_no"]
            entry["last_read_plan"] = tiered_exports.summary(plan)
            return entry, f"已从策略 {entry.get('策略编号')} 提交跨层导出：{plan['plan_no']}"

        entry, message = super().run_action(entry_id, action)
        if action == "标记生效" and entry is not None:
            entry["生效时间"] = _now()
        return entry, message

    def optimize_retention(self, min_free_space_mb: float = 1024) -> dict[str, Any]:
        """保留策略优化：根据预估存储空间调整各测点的保留天数。

        超过空间限制时，按采集间隔降序缩减保留天数。
        """
        rows = store.rows(self.MODULE)
        point_rows = store.rows("ts_point")

        # 构建测点->采集间隔映射
        interval_map = {}
        for pt in point_rows:
            try:
                interval = int(str(pt.get("采集间隔", "10s")).replace("s", ""))
            except (ValueError, TypeError):
                interval = 10
            interval_map[pt.get("测点编号")] = interval

        recommendations = []
        total_estimated_mb = 0
        for strategy in rows:
            if strategy.get("status") != "已生效":
                continue
            pt_id = strategy.get("测点编号")
            interval = interval_map.get(pt_id, 10)
            daily_points = 86400 // interval
            retention = parse_days(strategy.get("保留天数"), 30)

            # 估算存储
            raw_bytes = daily_points * 32 * retention
            total_estimated_mb += raw_bytes / (1024 * 1024)

            # 保留天数越短越好
            recommended_retention = max(7, min(retention, retention * 2 // 3))
            recommendations.append({
                "测点编号": pt_id,
                "当前保留天数": retention,
                "建议保留天数": recommended_retention,
                "每日数据点": daily_points,
            })

        return {
            "total_estimated_mb": round(total_estimated_mb, 2),
            "min_free_space_mb": min_free_space_mb,
            "need_optimize": total_estimated_mb > min_free_space_mb,
            "recommendations": recommendations,
        }
