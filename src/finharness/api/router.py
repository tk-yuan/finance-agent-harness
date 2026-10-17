"""路由汇总。新增接口只改这里。"""

from fastapi import APIRouter

from ..api.v1 import (
    accounting,
    analysis,
    audit,
    chat,
    checkup,
    closing,
    company,
    conversations,
    counterparties,
    entries,
    export,
    stats,
)

api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(accounting.router)
api_v1.include_router(analysis.router)
api_v1.include_router(audit.router)
api_v1.include_router(chat.router)
api_v1.include_router(checkup.router)
api_v1.include_router(closing.router)
api_v1.include_router(company.router)
api_v1.include_router(conversations.router)
api_v1.include_router(counterparties.router)
api_v1.include_router(entries.router)
api_v1.include_router(export.router)
api_v1.include_router(stats.router)

__all__ = ["api_v1"]
