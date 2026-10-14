"""月末核对接口：现金盘点、银行存款余额调节表、往来对账单。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ...services import checkup_service as svc

router = APIRouter(prefix="/checkup", tags=["checkup"])


class CashCheckIn(BaseModel):
    actual_amount: float = Field(..., ge=0, description="实际盘点到的现金")
    date: str = Field(..., min_length=10, max_length=10, description="盘点日期 YYYY-MM-DD")
    note: str = Field(default="", max_length=200)
    checked_by: str = Field(default="", max_length=30)


class AdjustmentItem(BaseModel):
    desc: str = Field(..., max_length=100)
    amount: float = Field(..., gt=0)


class BankReconIn(BaseModel):
    period: str = Field(..., min_length=7, max_length=7, description="YYYY-MM")
    statement_balance: float = Field(..., description="银行对账单余额")
    items_added: list[AdjustmentItem] = Field(default_factory=list,
                                              description="企业已收、银行未收")
    items_subtracted: list[AdjustmentItem] = Field(default_factory=list,
                                                   description="企业已付、银行未付")
    note: str = Field(default="", max_length=200)


# ---------- 现金盘点 ----------

@router.get("/cash/balance", summary="账面库存现金余额")
def cash_balance(date: str | None = Query(default=None, description="截止日期 YYYY-MM-DD")) -> dict:
    return {"book_balance": svc.cash_book_balance(until=date)}


@router.post("/cash", summary="登记现金盘点（自动算盘盈/盘亏）")
def create_cash_check(payload: CashCheckIn) -> dict:
    return svc.create_cash_check(**payload.model_dump())


@router.get("/cash", summary="现金盘点记录")
def list_cash_checks() -> dict:
    return {"items": svc.list_cash_checks()}


# ---------- 银行存款余额调节表 ----------

@router.get("/bank/balance", summary="企业账面银行存款余额")
def bank_balance(date: str | None = Query(default=None)) -> dict:
    return {"book_balance": svc.bank_book_balance(until=date)}


@router.post("/bank", summary="编制银行存款余额调节表")
def create_bank_recon(payload: BankReconIn) -> dict:
    d = payload.model_dump()
    d["items_added"] = [x for x in d["items_added"]]
    d["items_subtracted"] = [x for x in d["items_subtracted"]]
    return svc.create_bank_recon(**d)


@router.get("/bank", summary="历史调节表")
def list_bank_recons() -> dict:
    return {"items": svc.list_bank_recons()}


# ---------- 往来对账单 ----------

@router.get("/statement/{counterparty_id}", summary="生成往来对账单")
def statement(counterparty_id: int,
              start: str | None = Query(default=None),
              end: str | None = Query(default=None)) -> dict:
    d = svc.counterparty_statement(counterparty_id, start, end)
    if not d.get("ok"):
        raise HTTPException(404, d.get("message", "生成失败"))
    return d


@router.get("/statement/{counterparty_id}/text", summary="往来对账单（纯文本，可打印）")
def statement_text(counterparty_id: int,
                   start: str | None = Query(default=None),
                   end: str | None = Query(default=None)) -> PlainTextResponse:
    text = svc.counterparty_statement_text(counterparty_id, start, end)
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")
