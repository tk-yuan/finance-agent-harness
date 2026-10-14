"""统计接口：给前端图表和表格提供数据。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ...services import stats_service

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/overview", summary="总览：收入/支出/结余/笔数")
def overview() -> dict:
    return stats_service.overview()


@router.get("/by-category", summary="按科目汇总支出（饼图）")
def by_category() -> dict:
    return stats_service.by_category()


@router.get("/balances", summary="账户余额表（含流动资金与试算平衡）")
def balances() -> dict:
    return stats_service.account_balances()


@router.get("/monthly", summary="按月汇总收支（折线图）")
def monthly() -> dict:
    return stats_service.monthly()


@router.get("/entries", summary="账目明细列表（表格）")
def entries(
    category: str | None = Query(default=None),
    month: str | None = Query(default=None),
    status: str | None = Query(default=None,
                               description="pending / approved / rejected，不填=全部"),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict:
    # 注意：status 必须透传下去。忘了传的话筛选会失效、
    # 返回一堆不相干的账目（曾经踩过这个坑：出纳页"被驳回"显示了 200 笔假数据）。
    return {"items": stats_service.list_entries(
        category=category, month=month, status=status, limit=limit)}


@router.post("/reconcile", summary="执行对账，返回异常报告")
def reconcile() -> dict:
    from ...tools.finance import reconcile_month

    entries = stats_service.list_entries(limit=2000)
    report = reconcile_month.invoke({})
    return {"report": report, "checked": len(entries)}


@router.delete("/entries/{entry_id}", summary="删除一笔账目")
def delete_entry(entry_id: int) -> dict:
    return stats_service.delete_entry(entry_id)
