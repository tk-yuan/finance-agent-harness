"""会计期末工作与报表接口：月末结账、利润表。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ...services import closing_service, report_service

router = APIRouter(prefix="/closing", tags=["closing"])


@router.get("/periods", summary="有数据的会计期间（供选择）")
def periods() -> dict:
    return {
        "available": report_service.available_periods(),
        "closed": closing_service.list_closed(),
    }


@router.get("/preview", summary="预览月末结账会生成哪些分录")
def preview(period: str = Query(..., description="YYYY-MM")) -> dict:
    return closing_service.preview(period)


@router.post("", summary="执行月末结账（计提折旧 + 结转损益 + 封账）")
def close(period: str = Query(..., description="YYYY-MM"),
          force: bool = Query(default=False, description="有未审核单据时是否强制结账")) -> dict:
    result = closing_service.close(period, force=force)
    if not result.get("ok"):
        raise HTTPException(400, result.get("message", "结账失败"))
    return result


@router.get("/income-statement", summary="利润表")
def income_statement(period: str | None = Query(default=None, description="YYYY-MM，不填=全部期间")) -> dict:
    return report_service.income_statement(period)
