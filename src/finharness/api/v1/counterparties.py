"""往来单位（客户/供应商）档案接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...services import counterparty_service as svc

router = APIRouter(prefix="/counterparties", tags=["counterparties"])


class CounterpartyIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    kind: str = Field(default="customer", description="customer / supplier")
    contact: str = Field(default="", max_length=50)
    phone: str = Field(default="", max_length=30)
    note: str = Field(default="", max_length=200)


@router.get("", summary="往来单位列表（含往来统计）")
def list_counterparties() -> dict:
    return {"items": svc.list_with_stats()}


@router.post("/sync", summary="从历史账目自动建立往来单位档案")
def sync() -> dict:
    return svc.sync_from_entries()


@router.get("/{counterparty_id}", summary="某个单位的往来明细")
def detail(counterparty_id: int) -> dict:
    d = svc.get_detail(counterparty_id)
    if not d.get("ok"):
        raise HTTPException(404, d.get("message", "不存在"))
    return d


@router.post("", summary="新建往来单位")
def create(payload: CounterpartyIn) -> dict:
    r = svc.create(**payload.model_dump())
    if not r["ok"]:
        raise HTTPException(400, r["message"])
    return r


@router.put("/{counterparty_id}", summary="修改往来单位")
def update(counterparty_id: int, payload: CounterpartyIn) -> dict:
    r = svc.update(counterparty_id, **payload.model_dump())
    if not r["ok"]:
        raise HTTPException(404, r["message"])
    return r


@router.delete("/{counterparty_id}", summary="删除往来单位（有账目则不允许）")
def delete(counterparty_id: int) -> dict:
    r = svc.delete(counterparty_id)
    if not r["ok"]:
        raise HTTPException(400, r["message"])
    return r
