"""冷热分层跨层导出的 HTTP 接口。

为了方便不同调用方，同一组能力暴露了多组路径别名；参数同时支持
query string、JSON body 和表单，并兼容中文参数名。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.services.batch_export import BatchExportService
from app.services.tiered_plan import tiered_exports
from app.services.ts_store import TsStoreService

router = APIRouter(prefix="/api", tags=["冷热分层跨层导出"])

ts_store_service = TsStoreService()
batch_export_service = BatchExportService()

_PARAM_ALIASES: dict[str, tuple[str, ...]] = {
    "retention_days": ("retention_days", "retention", "总保留天数", "总保留", "保留天数", "保留"),
    "hot_days": ("hot_days", "hot_retention_days", "hot", "热层天数", "热层", "冷热分层"),
    "requested_days": ("requested_days", "export_days", "days", "query_days", "span_days",
                       "导出天数", "请求天数", "查询天数", "时间跨度天数", "准备天数"),
    "points_per_day": ("points_per_day", "daily_points", "data_points_per_day",
                       "每日点数", "每天点数", "日点数"),
    "bytes_per_point": ("bytes_per_point", "point_bytes", "每点字节", "点字节数"),
    "compression": ("compression", "compressor", "compression_algorithm", "压缩", "压缩算法", "压缩方式"),
    "compression_ratio": ("compression_ratio", "ratio", "压缩比"),
    "today": ("today", "base_date", "基准日期"),
    "strategy_ref": ("strategy_ref", "strategy_id", "strategy", "策略编号", "策略id", "策略"),
    "point_id": ("point_id", "point", "测点编号", "测点"),
    "device_range": ("device_range", "devices", "设备范围"),
    "task_ref": ("task_ref", "task_id", "task_no", "任务编号", "任务id"),
    "segment_ref": ("segment_ref", "segment_id", "segment", "冷段", "冷段编号", "segmentId"),
    "count": ("count", "limit", "数量", "个数", "几个"),
    "pick": ("pick", "order", "选取方式", "取"),
}

_LIST_KEYS = ("segment_ids", "dates")


async def collect_params(request: Request, **path_params: Any) -> dict[str, Any]:
    raw: dict[str, Any] = dict(request.query_params)
    if request.method in {"POST", "PUT", "PATCH"}:
        content_type = request.headers.get("content-type", "")
        body: Any = {}
        if "application/json" in content_type:
            try:
                body = await request.json()
            except Exception:
                body = {}
        elif "form" in content_type:
            try:
                body = dict(await request.form())
            except Exception:
                body = {}
        if isinstance(body, dict):
            for key, value in body.items():
                if value is not None:
                    raw[key] = value
            nested_values = body.get("values")
            if isinstance(nested_values, dict):
                for key, value in nested_values.items():
                    raw.setdefault(key, value)

    params: dict[str, Any] = {}
    for canonical, aliases in _PARAM_ALIASES.items():
        for alias in aliases:
            if alias in raw and raw[alias] not in (None, ""):
                params[canonical] = raw[alias]
                break

    for key in _LIST_KEYS:
        value = raw.get(key) or raw.get(f"{key}[]")
        if value is None:
            continue
        if isinstance(value, list):
            params[key] = [str(v) for v in value]
        else:
            params[key] = [part.strip() for part in str(value).split(",") if part.strip()]

    # 未在别名表里但可能直接使用的参数也保留
    for key, value in raw.items():
        params.setdefault(key, value)

    params.update({k: v for k, v in path_params.items() if v is not None})

    # 数字字段转换
    for int_key in ("retention_days", "hot_days", "requested_days", "points_per_day",
                    "bytes_per_point", "count"):
        if int_key in params and not isinstance(params[int_key], int):
            from app.services.tiered_plan import parse_days
            params[int_key] = parse_days(params[int_key], 0)
    if "compression_ratio" in params and not isinstance(params["compression_ratio"], (int, float)):
        try:
            params["compression_ratio"] = float(params["compression_ratio"])
        except (TypeError, ValueError):
            params.pop("compression_ratio")
    return params


def _plan_kwargs(params: dict[str, Any]) -> dict[str, Any]:
    keys = ("retention_days", "hot_days", "requested_days", "points_per_day",
            "bytes_per_point", "compression", "compression_ratio", "today",
            "strategy_ref", "point_id", "device_range", "task_ref")
    return {k: params[k] for k in keys if k in params}


# ===========================================================================
# 无状态容量试算 / 预览
# ===========================================================================

async def _capacity(request: Request) -> dict[str, Any]:
    params = await collect_params(request)
    result = ts_store_service.capacity_plan(
        retention_days=params.get("retention_days", 5),
        hot_days=params.get("hot_days", 30),
        points_per_day=params.get("points_per_day", 100),
        bytes_per_point=params.get("bytes_per_point", 32),
        compression=params.get("compression", "none"),
        compression_ratio=params.get("compression_ratio"),
        today=params.get("today"),
        requested_days=params.get("requested_days"),
    )
    return {"ok": True, **result}


@router.api_route("/ts_store/capacity", methods=["GET", "POST"])
async def ts_store_capacity(request: Request):
    return await _capacity(request)


@router.api_route("/ts_store/estimate", methods=["GET", "POST"])
async def ts_store_estimate_alias(request: Request):
    return await _capacity(request)


@router.api_route("/ts_store/storage-plan", methods=["GET", "POST"])
async def ts_store_plan_alias(request: Request):
    return await _capacity(request)


@router.api_route("/tiered-exports/preview", methods=["GET", "POST"])
async def tiered_preview(request: Request):
    return await _capacity(request)


@router.api_route("/tiered-exports/capacity", methods=["GET", "POST"])
async def tiered_capacity(request: Request):
    return await _capacity(request)


# ===========================================================================
# 计划集合
# ===========================================================================

@router.get("/tiered-exports")
async def list_tiered_plans():
    return {"ok": True, "items": tiered_exports.list_plans(), "total": len(tiered_exports.list_plans())}


@router.post("/tiered-exports")
async def create_tiered_export(request: Request):
    params = await collect_params(request)
    plan = tiered_exports.create_plan(**_plan_kwargs(params))
    return {"ok": True, "message": f"分层读取计划已生成：{plan['plan_no']}",
            "plan": tiered_exports.detail(plan["plan_no"]),
            "plan_no": plan["plan_no"], "task_no": plan["task_no"], "task_id": plan["task_id"]}


@router.post("/ts_store/cross-tier-export")
async def ts_store_cross_export(request: Request):
    params = await collect_params(request)
    result = ts_store_service.submit_cross_tier_export(
        params.get("strategy_ref"), **{k: v for k, v in _plan_kwargs(params).items()
                                       if k != "strategy_ref"})
    plan = result["plan"]
    return {**result, "plan": tiered_exports.detail(plan["plan_no"]),
            "plan_no": plan["plan_no"], "task_no": plan["task_no"]}


@router.post("/ts_store/{ref}/cross-tier-export")
async def ts_store_ref_cross_export(request: Request, ref: str):
    params = await collect_params(request, strategy_ref=ref)
    plan = tiered_exports.create_plan(**_plan_kwargs(params))
    return {"ok": True, "message": f"分层读取计划已生成：{plan['plan_no']}",
            "plan": tiered_exports.detail(plan["plan_no"]),
            "plan_no": plan["plan_no"], "task_no": plan["task_no"]}


@router.post("/batch_export/cross-tier-export")
async def batch_cross_export(request: Request):
    params = await collect_params(request)
    plan = tiered_exports.create_plan(**_plan_kwargs(params))
    return {"ok": True, "plan": tiered_exports.detail(plan["plan_no"]),
            "plan_no": plan["plan_no"], "task_no": plan["task_no"]}


@router.post("/batch_export/tiered-export")
async def batch_tiered_export_alias(request: Request):
    return await batch_cross_export(request)


# ===========================================================================
# 单个计划
# ===========================================================================

@router.get("/tiered-exports/{ref}")
async def get_tiered_plan(ref: str):
    detail = tiered_exports.detail(ref)
    if detail is None:
        return {"ok": False, "message": f"分层导出计划 {ref} 不存在"}
    return {"ok": True, "plan": detail, "read_plan": detail}


@router.get("/tiered-exports/{ref}/detail")
async def get_tiered_detail(ref: str):
    return batch_export_service.export_detail(ref)


@router.get("/tiered-exports/{ref}/read-plan")
async def get_tiered_read_plan(ref: str):
    result = batch_export_service.get_read_plan(ref)
    return result


@router.get("/batch_export/{ref}/detail")
async def batch_export_detail(ref: str):
    return batch_export_service.export_detail(ref)


@router.get("/batch_export/{ref}/read-plan")
async def batch_export_read_plan(ref: str):
    return batch_export_service.get_read_plan(ref)


@router.get("/batch_export/{ref}/plan")
async def batch_export_plan_alias(ref: str):
    return batch_export_service.get_read_plan(ref)


@router.post("/batch_export/{ref}/read-plan")
async def batch_export_create_read_plan(request: Request, ref: str):
    params = await collect_params(request, task_ref=ref)
    task = tiered_exports.get_batch_task(ref)
    if task is None:
        return {"ok": False, "message": f"导出任务 {ref} 不存在"}
    # 已有计划则返回已有计划（除非显式要求重建）
    existing = tiered_exports.get_plan(ref)
    if existing is not None and not params.get("rebuild"):
        return {"ok": True, "plan": tiered_exports.detail(existing["plan_no"]),
                "read_plan": tiered_exports.detail(existing["plan_no"])}
    kwargs = _plan_kwargs(params)
    kwargs["task_ref"] = task.get("id")
    kwargs.setdefault("point_id", str(task.get("测点列表", "")).split(",")[0].strip() or None)
    plan = tiered_exports.create_plan(**kwargs)
    return {"ok": True, "plan": tiered_exports.detail(plan["plan_no"]),
            "read_plan": tiered_exports.detail(plan["plan_no"])}


@router.post("/batch_export/{ref}/plan")
async def batch_export_plan_post_alias(request: Request, ref: str):
    return await batch_export_create_read_plan(request, ref)


# ===========================================================================
# 冷段操作（计划级批量领取 + 分段动作）
# ===========================================================================

@router.post("/tiered-exports/{ref}/claim")
async def plan_claim(request: Request, ref: str):
    params = await collect_params(request)
    return tiered_exports.claim(
        ref,
        segment_ids=params.get("segment_ids"),
        dates=params.get("dates"),
        count=params.get("count"),
        pick=str(params.get("pick", "older")),
    )


@router.post("/batch_export/{ref}/claim")
async def batch_plan_claim(request: Request, ref: str):
    return await plan_claim(request, ref)


@router.post("/tiered-exports/{ref}/segments/{seg}/claim")
async def segment_claim(ref: str, seg: str):
    return tiered_exports.claim(ref, segment_ids=[seg])


@router.post("/batch_export/{ref}/segments/{seg}/claim")
async def batch_segment_claim(ref: str, seg: str):
    return tiered_exports.claim(ref, segment_ids=[seg])


@router.post("/tiered-exports/{ref}/segments/{seg}/read")
async def segment_read(ref: str, seg: str):
    return tiered_exports.read_fragments(ref, seg)


@router.post("/batch_export/{ref}/segments/{seg}/read")
async def batch_segment_read(ref: str, seg: str):
    return tiered_exports.read_fragments(ref, seg)


@router.post("/tiered-exports/{ref}/segments/{seg}/confirm")
async def segment_confirm(ref: str, seg: str):
    return tiered_exports.confirm_segment(ref, seg)


@router.post("/batch_export/{ref}/segments/{seg}/confirm")
async def batch_segment_confirm(ref: str, seg: str):
    return tiered_exports.confirm_segment(ref, seg)


@router.post("/tiered-exports/{ref}/segments/{seg}/complete")
async def segment_complete(ref: str, seg: str):
    return tiered_exports.complete_segment(ref, seg)


@router.post("/tiered-exports/{ref}/segments/{seg}/continue")
async def segment_continue(ref: str, seg: str):
    return tiered_exports.complete_segment(ref, seg)


@router.post("/batch_export/{ref}/segments/{seg}/complete")
async def batch_segment_complete(ref: str, seg: str):
    return tiered_exports.complete_segment(ref, seg)


@router.post("/batch_export/{ref}/segments/{seg}/continue")
async def batch_segment_continue(ref: str, seg: str):
    return tiered_exports.complete_segment(ref, seg)


# ===========================================================================
# 保留策略变更 / 冷层清理
# ===========================================================================

@router.post("/tiered-exports/{ref}/retention")
async def plan_retention(request: Request, ref: str):
    params = await collect_params(request)
    return tiered_exports.apply_retention_change(
        ref,
        retention_days=params.get("retention_days"),
        hot_days=params.get("hot_days"),
        strategy_ref=params.get("strategy_ref"),
    )


@router.post("/tiered-exports/{ref}/cleanup")
async def plan_cleanup(request: Request, ref: str):
    params = await collect_params(request)
    return tiered_exports.cleanup_cold_tier(
        ref,
        retention_days=params.get("retention_days"),
        hot_days=params.get("hot_days"),
        strategy_ref=params.get("strategy_ref"),
    )


@router.post("/batch_export/{ref}/retention")
async def batch_retention(request: Request, ref: str):
    return await plan_retention(request, ref)


@router.post("/batch_export/{ref}/cleanup")
async def batch_cleanup(request: Request, ref: str):
    return await plan_cleanup(request, ref)


@router.post("/ts_store/retention")
async def ts_store_retention(request: Request):
    params = await collect_params(request)
    return tiered_exports.apply_retention_change(
        params.get("task_ref") or params.get("plan_ref"),
        retention_days=params.get("retention_days"),
        hot_days=params.get("hot_days"),
        strategy_ref=params.get("strategy_ref"),
    )


@router.post("/ts_store/{ref}/retention")
async def ts_store_ref_retention(request: Request, ref: str):
    params = await collect_params(request, strategy_ref=ref)
    return tiered_exports.apply_retention_change(
        params.get("task_ref") or params.get("plan_ref"),
        retention_days=params.get("retention_days"),
        hot_days=params.get("hot_days"),
        strategy_ref=ref,
    )


@router.post("/ts_store/cleanup-cold")
async def ts_store_cleanup(request: Request):
    params = await collect_params(request)
    return tiered_exports.cleanup_cold_tier(
        params.get("task_ref") or params.get("plan_ref"),
        retention_days=params.get("retention_days"),
        hot_days=params.get("hot_days"),
        strategy_ref=params.get("strategy_ref"),
    )


# ---------------------------------------------------------------------------
# 方法名风格 / 其他常见路径别名
# ---------------------------------------------------------------------------

@router.api_route("/ts_store/estimate_storage", methods=["GET", "POST"])
async def ts_store_estimate_storage_named(request: Request):
    return await _capacity(request)


@router.api_route("/ts_store/estimate-storage", methods=["GET", "POST"])
async def ts_store_estimate_storage_kebab(request: Request):
    return await _capacity(request)


@router.api_route("/batch_export/estimate_from_ts_store", methods=["GET", "POST"])
async def batch_estimate_from_ts_store(request: Request):
    params = await collect_params(request)
    point_id = params.get("point_id") or params.get("strategy_ref") or ""
    days = params.get("requested_days") or params.get("days") or 30
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30
    return batch_export_service.estimate_from_ts_store(str(point_id), days)


@router.post("/batch_export/cross-tier-plan")
async def batch_cross_tier_plan_alias(request: Request):
    return await batch_cross_export(request)


@router.post("/batch_export/read-plan")
async def batch_read_plan_collection(request: Request):
    params = await collect_params(request)
    task_ref = params.get("task_ref")
    if task_ref is not None:
        task = tiered_exports.get_batch_task(task_ref)
        if task is not None:
            existing = tiered_exports.get_plan(task.get("id"))
            if existing is not None and not params.get("rebuild"):
                detail = tiered_exports.detail(existing["plan_no"])
                return {"ok": True, "plan": detail, "read_plan": detail}
            kwargs = _plan_kwargs(params)
            kwargs["task_ref"] = task.get("id")
            plan = tiered_exports.create_plan(**kwargs)
            detail = tiered_exports.detail(plan["plan_no"])
            return {"ok": True, "plan": detail, "read_plan": detail}
    plan = tiered_exports.create_plan(**_plan_kwargs(params))
    detail = tiered_exports.detail(plan["plan_no"])
    return {"ok": True, "plan": detail, "read_plan": detail}


@router.post("/cross/cross-tier-export")
async def cross_tier_export_short_alias(request: Request):
    return await batch_cross_export(request)


@router.api_route("/cross/flow10/export", methods=["GET", "POST"])
async def cross_flow10_export_named(request: Request):
    """与 /api/cross/flow9/export 命名平行的 Flow10 入口（参数/JSON 均可）。"""
    params = await collect_params(request)
    result = cross_service_flow10(**params)
    return result


def cross_service_flow10(**params: Any):
    from app.services.cross_module import cross_service

    return cross_service.submit_tiered_export_from_store(
        strategy_id=params.get("strategy_ref"),
        point_id=params.get("point_id"),
        retention_days=params.get("retention_days"),
        hot_days=params.get("hot_days"),
        export_days=params.get("requested_days") or params.get("export_days") or params.get("days"),
        points_per_day=params.get("points_per_day", 100),
        bytes_per_point=params.get("bytes_per_point", 32),
        compression=params.get("compression", "none"),
        device_range=params.get("device_range", ""),
        today=params.get("today"),
    )
