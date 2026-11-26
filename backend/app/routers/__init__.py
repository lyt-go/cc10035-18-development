"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import cross_module, resource, sampling_credential

# sampling_credential 是固定前缀路由，必须排在 resource 的 /api/{module} 动态路由之前。
ROUTERS = [cross_module, sampling_credential, resource]
