"""管理 API（/api/admin）：Provider / Model / Pipeline / Trace / Playground / Settings / Stats。"""

from fastapi import APIRouter

from app.admin.meta import router as meta_router
from app.admin.models import router as models_router
from app.admin.pipelines import router as pipelines_router
from app.admin.playground import router as playground_router
from app.admin.providers import router as providers_router
from app.admin.settings import router as settings_router
from app.admin.stats import router as stats_router
from app.admin.traces import router as traces_router

router = APIRouter(prefix="/api/admin")
router.include_router(providers_router)
router.include_router(models_router)
router.include_router(pipelines_router)
router.include_router(traces_router)
router.include_router(playground_router)
router.include_router(settings_router)
router.include_router(meta_router)
router.include_router(stats_router)
