"""健康检查接口。"""

from __future__ import annotations

from fastapi import APIRouter

from ...config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health", summary="存活探针")
def health() -> dict:
    s = get_settings()
    return {"status": "ok", "app": s.app_name, "model": s.siliconflow_chat_model}
