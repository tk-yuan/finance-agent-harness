"""企业档案接口。"""

from __future__ import annotations

from fastapi import APIRouter

from ...schemas.company import CompanyUpdate
from ...services import company_service

router = APIRouter(prefix="/company", tags=["company"])


@router.get("", summary="获取企业档案")
def get_company() -> dict:
    return company_service.ensure_company()


@router.put("", summary="更新企业档案")
def update_company(payload: CompanyUpdate) -> dict:
    return company_service.update_company(**payload.model_dump(exclude_none=True))
