"""用户自助门户 API（/api/portal，T52）：Auth / Keys / Stats / Traces / Grants。

一般用户登录后的自助平面：会话与管理面同体系（角色约束放行全部启用用户），
资源范围强制归属当前用户。授权/用户/模型等管理动作仍在 /api/admin/*。
"""

from fastapi import APIRouter

from app.portal.auth import router as auth_router
from app.portal.grants import router as grants_router
from app.portal.keys import router as keys_router
from app.portal.stats import router as stats_router
from app.portal.traces import router as traces_router

router = APIRouter(prefix="/api/portal")
router.include_router(auth_router)
router.include_router(keys_router)
router.include_router(stats_router)
router.include_router(traces_router)
router.include_router(grants_router)
