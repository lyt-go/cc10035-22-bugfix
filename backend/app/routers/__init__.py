"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import cross_module, resource, tiered_export

# tiered_export 含 /api/ts_store/...、/api/batch_export/... 的多段/静态别名，
# 必须注册在 resource 的 /api/{module}/{entry_id} 动态路由之前。
ROUTERS = [cross_module, tiered_export, resource]
