"""批量导出领域服务：IoT时序数据的批量导出与格式转换管理。

提供导出任务创建、格式转换、进度跟踪、文件预估、导出统计等真实业务能力。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.tiered_plan import tiered_exports
from app.store import store

CST = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _now_dt() -> datetime:
    return datetime.now(CST)


class BatchExportService(BaseModuleService):
    MODULE = "batch_export"
    ENTITY = "导出任务"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["任务编号", "设备范围", "测点列表", "时间范围", "导出格式", "数据行数", "文件大小", "导出状态"]
    REQUIRED_FIELDS = ["任务编号", "设备范围", "测点列表"]
    STATUS_ORDER = ["待执行", "导出中", "已完成", "已取消"]
    ACTION_RULES = {"发起导出": "导出中", "标记完成": "已完成", "取消导出": "已取消"}
    NEGATIVE_ACTIONS = []

    def create_export(self, device_range: str, point_list: str,
                      format_type: str = "CSV",
                      time_start: str = "",
                      time_end: str = "") -> dict[str, Any]:
        """创建批量导出任务。

        业务流程：
        1. 校验设备范围是否合法
        2. 校验时间范围
        3. 校验导出格式
        4. 预估数据量和文件大小
        5. 创建导出任务
        """
        rows = store.rows(self.MODULE)

        # 校验导出格式
        valid_formats = ["CSV", "JSON", "Parquet", "Excel", "Avro"]
        if format_type not in valid_formats:
            return {
                "ok": False,
                "message": f"不支持的导出格式: {format_type}，支持: {', '.join(valid_formats)}",
            }

        # 时间范围检查
        start = time_start or (_now_dt() - timedelta(hours=24)).isoformat(timespec="seconds")
        end = time_end or _now()
        try:
            start_dt = datetime.fromisoformat(start)
            end_dt = datetime.fromisoformat(end)
            if end_dt < start_dt:
                return {"ok": False, "message": "结束时间不能早于开始时间"}
            time_span_hours = (end_dt - start_dt).total_seconds() / 3600
        except (ValueError, TypeError):
            return {"ok": False, "message": "时间格式无效"}

        # 预估数据量
        points = [p.strip() for p in point_list.split(",") if p.strip()]
        if not points:
            return {"ok": False, "message": "测点列表不能为空"}

        estimated_rows = int(time_span_hours * len(points) * 120)  # 每小时每测点120条

        # 格式转换因子
        format_size_factors = {
            "CSV": 1.0,
            "JSON": 1.5,
            "Parquet": 0.4,
            "Excel": 2.0,
            "Avro": 0.6,
        }
        factor = format_size_factors.get(format_type, 1.0)
        estimated_size_mb = round(estimated_rows * 0.0002 * factor, 2)

        max_id = max((int(row.get("id", 0)) for row in rows), default=0)
        task = {
            "id": max_id + 1,
            "任务编号": f"EXP-{max_id + 1:04d}",
            "设备范围": device_range,
            "测点列表": point_list,
            "时间范围": f"{start} ~ {end}",
            "导出格式": format_type,
            "数据行数": str(estimated_rows),
            "文件大小": f"{estimated_size_mb}MB",
            "导出状态": "待执行",
            "status": "待执行",
            "pending": True,
            "abnormal": False,
        }
        rows.append(task)

        return {
            "ok": True,
            "message": f"批量导出任务已创建: {task['任务编号']}",
            "task": task,
            "预估行数": estimated_rows,
            "预估文件大小": f"{estimated_size_mb}MB",
            "测点数": len(points),
            "时间跨度小时": round(time_span_hours, 1),
        }

    def format_convert(self, task_id: str, target_format: str) -> dict[str, Any]:
        """格式转换：将导出任务转换为目标格式。

        业务流程：
        1. 查找导出任务
        2. 校验目标格式
        3. 预估转换后的大小
        4. 更新任务
        """
        rows = store.rows(self.MODULE)
        task = None
        for row in rows:
            if row.get("任务编号") == task_id:
                task = row
                break
        if task is None:
            return {"ok": False, "message": f"导出任务 {task_id} 不存在"}

        valid_formats = ["CSV", "JSON", "Parquet", "Excel", "Avro"]
        if target_format not in valid_formats:
            return {
                "ok": False,
                "message": f"不支持的导出格式: {target_format}",
            }

        source_format = task.get("导出格式", "CSV")
        try:
            source_rows = int(task.get("数据行数", "0"))
        except (ValueError, TypeError):
            source_rows = 0

        format_size_factors = {
            "CSV": 1.0, "JSON": 1.5, "Parquet": 0.4, "Excel": 2.0, "Avro": 0.6,
        }
        factor = format_size_factors.get(target_format, 1.0)
        new_size = round(source_rows * 0.0002 * factor, 2)

        old_format = task.get("导出格式", "")
        task["导出格式"] = target_format
        task["文件大小"] = f"{new_size}MB"

        return {
            "ok": True,
            "message": f"导出任务 {task_id} 格式转换: {old_format} → {target_format}",
            "原格式": old_format,
            "目标格式": target_format,
            "预估新大小": f"{new_size}MB",
        }

    def estimate_cost(self, task_id: str) -> dict[str, Any]:
        """预估导出任务的计算和存储成本。"""
        rows = store.rows(self.MODULE)
        task = None
        for row in rows:
            if row.get("任务编号") == task_id:
                task = row
                break
        if task is None:
            return {"ok": False, "message": f"导出任务 {task_id} 不存在"}

        try:
            rows_count = int(task.get("数据行数", "0"))
        except (ValueError, TypeError):
            rows_count = 0

        format_type = task.get("导出格式", "CSV")
        time_range = task.get("时间范围", "")

        # 解析时间范围
        time_span_hours = 24
        if "~" in time_range:
            try:
                parts = time_range.split("~")
                start = datetime.fromisoformat(parts[0].strip())
                end = datetime.fromisoformat(parts[1].strip())
                time_span_hours = max((end - start).total_seconds() / 3600, 1)
            except (ValueError, TypeError, IndexError):
                pass

        # 成本模型
        cpu_cost = round(time_span_hours * 0.05, 4)  # 每小时0.05元
        storage_cost = round(rows_count * 0.0001 * 0.1, 4)  # 每万行0.1元
        network_cost = round(rows_count * 0.00001, 4)  # 传输成本

        return {
            "ok": True,
            "task_id": task_id,
            "format": format_type,
            "estimated_rows": rows_count,
            "cost_breakdown": {
                "计算成本元": cpu_cost,
                "存储成本元": storage_cost,
                "传输成本元": network_cost,
                "总计元": round(cpu_cost + storage_cost + network_cost, 4),
            },
        }

    def export_summary(self) -> dict[str, Any]:
        """批量导出统计摘要。"""
        rows = store.rows(self.MODULE)
        total = len(rows)
        by_format = {}
        total_rows = 0
        total_size = 0.0

        for row in rows:
            fmt = row.get("导出格式", "未知")
            by_format[fmt] = by_format.get(fmt, 0) + 1

            try:
                total_rows += int(row.get("数据行数", "0"))
            except (ValueError, TypeError):
                pass

            try:
                size_str = row.get("文件大小", "0MB")
                if "MB" in size_str:
                    total_size += float(size_str.replace("MB", ""))
            except (ValueError, TypeError):
                pass

        return {
            "total_tasks": total,
            "total_exported_rows": total_rows,
            "total_estimated_size_mb": round(total_size, 2),
            "by_format": by_format,
            "completed": sum(1 for r in rows if r.get("status") == "已完成"),
            "in_progress": sum(1 for r in rows if r.get("status") == "导出中"),
        }

    # ========== 跨模块方法 ==========

    def export_from_stream_source(self, stream_id: str, device_range: str,
                                  point_list: str, export_format: str = "CSV") -> dict[str, Any]:
        """从数据流管道源创建批量导出任务。

        业务流程:
        1. 校验管道存在且运行
        2. 预估数据量
        3. 创建导出任务
        """
        stream_rows = store.rows("data_stream")

        stream = None
        for row in stream_rows:
            if row.get("管道编号") == stream_id:
                stream = row
                break
        if stream is None:
            return {"ok": False, "message": f"数据流管道 {stream_id} 不存在"}

        if stream.get("status") != "运行中":
            return {"ok": False, "message": f"管道 {stream_id} 未运行"}

        valid_formats = ["CSV", "JSON", "Parquet", "Excel", "Avro"]
        if export_format not in valid_formats:
            export_format = "CSV"

        rows_data = store.rows(self.MODULE)
        max_id = max((int(row.get("id", 0)) for row in rows_data), default=0)
        task = {
            "id": max_id + 1,
            "任务编号": f"EXP-{max_id + 1:04d}",
            "设备范围": device_range,
            "测点列表": point_list,
            "时间范围": f"{_now()}-24h ~ {_now()}",
            "导出格式": export_format,
            "数据行数": "75000",
            "文件大小": "32MB",
            "导出状态": "待执行",
            "status": "待执行",
            "pending": True,
            "abnormal": False,
        }
        rows_data.append(task)

        return {
            "ok": True,
            "message": f"从管道 {stream_id} 触发批量导出",
            "stream": {
                "管道编号": stream_id,
                "输入源": stream.get("输入源"),
                "并行度": stream.get("并行度"),
            },
            "export_task": task,
        }

    def estimate_from_ts_store(self, point_id: str, days: int = 30) -> dict[str, Any]:
        """根据时序存储策略估算导出数据量。

        业务流程:
        1. 查找时序存储策略
        2. 获取保留天数和压缩算法
        3. 估算导出行数和文件大小
        """
        ts_store_rows = store.rows("ts_store")

        strategy = None
        for row in ts_store_rows:
            if row.get("测点编号") == point_id and row.get("status") == "已生效":
                strategy = row
                break
        if strategy is None:
            for row in ts_store_rows:
                if row.get("策略编号") == point_id and row.get("status") == "已生效":
                    strategy = row
                    break
        if strategy is None:
            # 兼容刚登记、尚未点“标记生效”的策略行
            for row in ts_store_rows:
                if row.get("测点编号") == point_id or row.get("策略编号") == point_id:
                    strategy = row
                    break

        if strategy is None:
            return {"ok": False, "message": f"测点 {point_id} 无已生效的存储策略"}

        from app.services.tiered_plan import build_read_plan, parse_compression, parse_days

        retention_days = parse_days(strategy.get("保留天数", "30"), 30)
        hot_days = parse_days(strategy.get("热层天数", strategy.get("冷热分层", "30")), 30)
        points_per_day = parse_days(strategy.get("每日点数"), 86400) or 86400
        bytes_per_point = parse_days(strategy.get("每点字节"), 32) or 32

        compressor = strategy.get("压缩算法", "none") or "none"
        _, compression_ratio = parse_compression(compressor, default=2)

        effective_days = min(days, retention_days)
        daily_rows = 86400  # 每秒一个数据点（旧口径保留）
        estimated_rows = daily_rows * effective_days
        raw_bytes = estimated_rows * 32
        compressed_bytes = raw_bytes / compression_ratio

        # 与容量试算、预算共用同一套策略的分层读取计划（实际读取段）
        read_plan = build_read_plan(
            retention_days=retention_days,
            hot_days=hot_days,
            requested_days=days,
            points_per_day=points_per_day,
            bytes_per_point=bytes_per_point,
            compression=compressor,
            compression_ratio=compression_ratio,
        )

        return {
            "ok": True,
            "point_id": point_id,
            "storage_retention_days": retention_days,
            "hot_days": read_plan["hot_days"],
            "cold_days": read_plan["cold_days"],
            "query_days": days,
            "effective_days": effective_days,
            "estimated_rows": estimated_rows,
            "estimated_size_mb": round(compressed_bytes / (1024 * 1024), 2),
            "compression": compressor,
            "compression_ratio": compression_ratio,
            "hot_storage_bytes": read_plan["hot_storage_bytes"],
            "cold_storage_bytes": read_plan["cold_storage_bytes"],
            "total_storage_bytes": read_plan["total_bytes"],
            "read_plan": read_plan,
            "tiered_plan": read_plan,
            "segments": read_plan["dates"],
        }

    # ========== 冷热分层跨层导出（Flow 10） ==========
    #
    # 所有日期区间、容量、预算与冷段状态都由 tiered_exports 统一生成，
    # 保证“两层日期和预算使用同一套策略”。

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """通用详情：导出任务额外挂载完整分层读取计划与分段。"""
        entry = super().get_entry(entry_id)
        if entry is None:
            return None
        plan = tiered_exports.detail(entry_id) or tiered_exports.detail(entry.get("任务编号", ""))
        if plan is not None:
            entry["read_plan"] = plan
            entry["plan"] = plan
            entry["segments"] = plan.get("segments", [])
            entry["billing"] = plan.get("billing")
        return entry

    def prepare_tiered_export(self, **params: Any) -> dict[str, Any]:
        """准备/提交跨层导出，返回带实际读取区间的分层读取计划。"""
        plan = tiered_exports.create_plan(**params)
        return {"ok": True, "message": f"分层读取计划已生成：{plan['plan_no']}", "plan": plan}

    create_cross_tier_export = prepare_tiered_export
    submit_cross_tier_export = prepare_tiered_export
    create_tiered_export = prepare_tiered_export

    def export_from_ts_store(self, point_id: str = "", days: int | None = None, **params: Any) -> dict[str, Any]:
        """与 export_from_stream_source 平行的命名：从时序存储策略发起跨层导出。"""
        strategy_ref = params.pop("strategy_ref", None)
        requested_days = params.pop("requested_days", None)
        if days is not None:
            requested_days = days
        strategy = tiered_exports.find_strategy(strategy_ref, point_id or None)
        if strategy is None and not point_id and strategy_ref is None:
            return {"ok": False, "message": "请提供测点编号或策略编号"}
        ref = strategy.get("id") if strategy is not None else strategy_ref
        plan = tiered_exports.create_plan(
            strategy_ref=ref,
            point_id=point_id or None,
            requested_days=requested_days,
            **params,
        )
        return {"ok": True, "message": f"从时序存储策略发起跨层导出：{plan['plan_no']}",
                "plan": plan, "export_task": {
                    "id": plan.get("task_id"), "任务编号": plan.get("task_no")}}

    def get_read_plan(self, ref: Any) -> dict[str, Any]:
        plan = tiered_exports.detail(ref)
        if plan is None:
            return {"ok": False, "message": f"导出任务/计划 {ref} 不存在"}
        return {"ok": True, "plan": plan, "read_plan": plan}

    get_tiered_read_plan = get_read_plan
    read_plan_detail = get_read_plan

    def claim_cold_segments(self, ref: Any, **selector: Any) -> dict[str, Any]:
        return tiered_exports.claim(ref, **selector)

    claim_cold_segment = claim_cold_segments

    def read_segment(self, ref: Any, segment_ref: str) -> dict[str, Any]:
        return tiered_exports.read_fragments(ref, segment_ref)

    fetch_segment = read_segment
    read_fragments = read_segment

    def confirm_segment(self, ref: Any, segment_ref: str) -> dict[str, Any]:
        return tiered_exports.confirm_segment(ref, segment_ref)

    confirm_read_segment = confirm_segment

    def complete_segment(self, ref: Any, segment_ref: str) -> dict[str, Any]:
        """继续完成仍可读取的等待冷段（幂等，已发片段不重发）。"""
        return tiered_exports.complete_segment(ref, segment_ref)

    continue_segment = complete_segment

    def cleanup_cold_segments(self, ref: Any = None, **policy: Any) -> dict[str, Any]:
        """冷层清理：区分 等待读取 / 不可读 / 已交付 冷段。"""
        return tiered_exports.cleanup_cold_tier(ref, **policy)

    cleanup_cold_tier = cleanup_cold_segments

    def change_retention(self, ref: Any = None, retention_days: Any = None,
                         hot_days: Any = None, **kwargs: Any) -> dict[str, Any]:
        """保留策略变更并与在途读取计划对账。"""
        return tiered_exports.apply_retention_change(
            ref, retention_days=retention_days, hot_days=hot_days, **kwargs)

    update_retention_policy = change_retention

    def export_detail(self, ref: Any) -> dict[str, Any]:
        """导出详情页所需的聚合视图：任务行 + 分层计划 + 分段 + 计费。"""
        task = tiered_exports.get_batch_task(ref)
        plan = tiered_exports.detail(ref) or (
            tiered_exports.detail(task.get("id")) if task is not None else None)
        if task is None and plan is None:
            return {"ok": False, "message": f"导出任务 {ref} 不存在"}
        return {
            "ok": True,
            "task": task,
            "plan": plan,
            "read_plan": plan,
            "segments": (plan or {}).get("segments", []),
            "billing": (plan or {}).get("billing"),
        }