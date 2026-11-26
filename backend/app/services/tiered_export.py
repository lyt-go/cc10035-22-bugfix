"""跨层导出领域服务的兼容导入路径。

核心实现位于 ``app.services.tiered_plan``；这里重新导出常用类型与单例，
方便不同命名习惯的调用方与测试导入。
"""
from __future__ import annotations

from app.services.tiered_plan import (  # noqa: F401
    BYTES_PER_POINT,
    POINTS_PER_DAY,
    DELIVERED,
    PENDING,
    UNREADABLE,
    WAITING_READ,
    TieredExportService,
    build_read_plan,
    display_status,
    normalize_policy,
    parse_compression,
    parse_days,
    state_code,
    state_value,
    tiered_exports,
)

# 常见命名别名
TieredExportService = TieredExportService
service = tiered_exports


def get_service() -> TieredExportService:
    return tiered_exports


__all__ = [
    "BYTES_PER_POINT",
    "POINTS_PER_DAY",
    "DELIVERED",
    "PENDING",
    "UNREADABLE",
    "WAITING_READ",
    "TieredExportService",
    "build_read_plan",
    "display_status",
    "normalize_policy",
    "parse_compression",
    "parse_days",
    "state_code",
    "state_value",
    "tiered_exports",
    "service",
    "get_service",
]
