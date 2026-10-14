"""智能会计：审核出纳交来的单据，确定科目、生成记账凭证。

分工（《会计法》不相容职务分离）：
    出纳登记"钱怎么动的、票在哪" → 会计决定"记到哪个科目、怎么分录"。
    这样管钱的不管账、管账的不管钱，互相牵制。

审核通过（approve）后，账目状态变为 approved，才计入报表。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...domain.accounts import EXPENSE_ACCOUNTS, INCOME_ACCOUNTS
from ...domain.db.models import Counterparty, LedgerEntry
from ...domain.db.session import SessionLocal
from ...services import stats_service

router = APIRouter(prefix="/accounting", tags=["accounting"])

# 会计可选的科目：费用类 + 资产类（购置资产）+ 收入类
ASSET_SIDE_ACCOUNTS = ["固定资产", "库存商品"]


class ReviewIn(BaseModel):
    """会计审核入参。"""

    category: str = Field(..., min_length=1, max_length=50, description="会计确定的科目")
    note: str = Field(default="", max_length=200, description="审核备注（可选）")


class RejectIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=200, description="驳回原因")


@router.get("/vouchers", summary="已入账的记账凭证列表（含借贷分录）")
def vouchers(period: str | None = None, limit: int = 200) -> dict:
    """会计的记账凭证。

    这是会计最核心的产物——每笔业务的分录：借什么、贷什么。
    出纳录单时看不到借贷（他不该管），会计审核后就形成了凭证。
    """
    with SessionLocal() as db:
        stmt = select(LedgerEntry).where(LedgerEntry.status == "approved")
        if period:
            stmt = stmt.where(LedgerEntry.date.like(f"{period}%"))
        rows = db.scalars(stmt.order_by(LedgerEntry.date.desc()).limit(limit)).all()
        cps = {c.id: c.name for c in db.scalars(select(Counterparty)).all()}

    items = []
    for i, r in enumerate(rows, 1):
        d = r.as_dict()
        d["counterparty_name"] = cps.get(r.counterparty_id, "") if r.counterparty_id else ""
        items.append(d)
    return {"items": items, "count": len(items)}


@router.put("/{entry_id}/recategorize", summary="会计修改已入账凭证的科目（反审核）")
def recategorize(entry_id: int, payload: ReviewIn) -> dict:
    """会计发现科目记错了，直接改。

    真实系统里叫「反审核」——改完会重新生成分录。这里简化成直接改。
    """
    with SessionLocal() as db:
        row = db.get(LedgerEntry, entry_id)
        if row is None:
            raise HTTPException(404, f"找不到编号 {entry_id} 的凭证")
        if row.status != "approved":
            raise HTTPException(400, "只有已入账的凭证需要改科目；待审核的直接审核即可")

        category = payload.category.strip()
        allowed = set(EXPENSE_ACCOUNTS) | set(INCOME_ACCOUNTS) | set(ASSET_SIDE_ACCOUNTS)
        if category not in allowed:
            raise HTTPException(400, f"科目「{category}」不在可选范围内")

        # 只改「业务科目」那一侧，**必须保留原来的收付款账户**。
        # 踩过的坑：老数据的 payment 字段是后加的、默认"银行存款"，
        # 如果直接用它重算，会把原本走"库存现金"的账悄悄改成"银行存款"——
        # 金额没变但账户变了，属于静默的数据损坏。
        if row.entry_type == "income":
            debit = row.debit_account or row.payment      # 收款侧：借方是收钱账户
            credit = category
        else:
            debit = category
            credit = row.credit_account or row.payment    # 付款侧：贷方是付钱账户

        row.category = category
        row.debit_account = debit
        row.credit_account = credit
        row.reviewed_at = datetime.now()
        db.commit()
        db.refresh(row)

    return {"ok": True, "entry": row.as_dict(),
            "message": f"已改为：借:{debit} 贷:{credit}（{category}）"}


@router.post("/approve-suggested", summary="按建议科目批量通过")
def approve_suggested() -> dict:
    """把待审核单据里「建议科目明确」的一次性通过。

    真实会计也不是每笔都从零判断——日常报销这类有明确规则的，
    看一眼确认就行。有疑问的（建议是"其他"）留给会计逐笔处理。
    """
    from ...domain.accounts import EXPENSE_ACCOUNTS

    with SessionLocal() as db:
        rows = db.scalars(
            select(LedgerEntry).where(LedgerEntry.status == "pending")
        ).all()

        done, skipped = 0, 0
        for row in rows:
            cat = _suggest_category(row.description, row.entry_type)
            # 建议是"其他"说明没把握，交给会计自己判断
            if cat == "其他" and row.entry_type == "expense":
                skipped += 1
                continue
            if cat not in set(EXPENSE_ACCOUNTS) | set(INCOME_ACCOUNTS) | set(ASSET_SIDE_ACCOUNTS):
                skipped += 1
                continue

            pay_acct = row.payment or (row.credit_account if row.entry_type == "expense"
                                       else row.debit_account)
            if row.entry_type == "income":
                debit, credit = pay_acct, cat
            else:
                debit, credit = cat, pay_acct

            row.category = cat
            row.debit_account = debit
            row.credit_account = credit
            row.status = "approved"
            row.reviewed_at = datetime.now()
            done += 1
        db.commit()

    return {"ok": True, "approved": done, "skipped": skipped,
            "message": f"已按建议通过 {done} 笔"
                       + (f"，{skipped} 笔建议不明确已保留待会计处理" if skipped else "")}


@router.get("/options", summary="会计可选的科目")
def options() -> dict:
    return {
        "expense_accounts": EXPENSE_ACCOUNTS,
        "income_accounts": INCOME_ACCOUNTS,
        "asset_accounts": ASSET_SIDE_ACCOUNTS,
    }


def _suggest_category(description: str, entry_type: str) -> str:
    """根据摘要猜一个科目，给会计做参考（会计可以改）。

    两级判断：
      1. 关键词规则（快、免费、覆盖大部分日常支出）
      2. 命中不了就返回"其他"，让会计自己定
    会计只需要在下拉里核对一下，不用每次从头翻找。
    """
    from ...services.bill_importer import categorize

    desc = description or ""
    if entry_type == "income":
        # 收款基本都是主营业务收入（少数是其他业务收入）
        return "主营业务收入"

    guess = categorize(desc)
    if guess != "其他":
        return guess
    # 兜底规则：再试一轮更宽的关键词
    extra = [
        ("工资", ["工资", "薪酬", "薪金"]),
        ("社保费", ["社保", "公积金", "五险"]),
        ("折旧费", ["折旧"]),
        ("税费", ["税", "增值税", "所得税", "附加"]),
        ("房租", ["租金", "房租", "物业"]),
        ("水电费", ["水费", "电费", "燃气"]),
        ("通讯费", ["话费", "宽带", "流量", "快递", "邮费"]),
        ("银行手续费", ["手续费", "年费"]),
        ("办公费", ["办公", "文具", "打印", "耗材", "域名", "软件"]),
        ("业务招待费", ["招待", "宴请", "客户餐"]),
        ("差旅费", ["出差", "住宿", "机票", "高铁"]),
        ("交通费", ["打车", "出租", "地铁", "公交", "加油", "停车"]),
    ]
    for cat, kws in extra:
        if any(k in desc for k in kws):
            return cat
    return "其他"


@router.get("/pending", summary="待审核单据列表")
def pending(limit: int = 200) -> dict:
    """待审核队列。带往来单位名 + 建议科目，会计核对时更省事。"""
    with SessionLocal() as db:
        rows = db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.status == "pending")
            .order_by(LedgerEntry.date.desc())
            .limit(limit)
        ).all()
        cps = {c.id: c.name for c in db.scalars(select(Counterparty)).all()}

    items = []
    for r in rows:
        d = r.as_dict()
        d["counterparty_name"] = cps.get(r.counterparty_id, "") if r.counterparty_id else ""
        d["suggested_category"] = _suggest_category(r.description, r.entry_type)
        items.append(d)
    return {"items": items}


@router.get("/stats", summary="审核情况概览")
def review_stats() -> dict:
    with SessionLocal() as db:
        rows = db.scalars(select(LedgerEntry)).all()
    counts = {"pending": 0, "approved": 0, "rejected": 0}
    pending_amount = 0.0
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status == "pending":
            pending_amount += r.amount
    return {
        "counts": counts,
        "pending_amount": round(pending_amount, 2),
    }


@router.post("/{entry_id}/approve", summary="审核通过：确定科目并生成记账凭证")
def approve(entry_id: int, payload: ReviewIn) -> dict:
    """会计审核通过。

    关键：**科目由会计在这里确定**，借贷方向由代码按规则生成——
    不做让模型自由发挥，保证分录永远合规。
    """
    with SessionLocal() as db:
        row = db.get(LedgerEntry, entry_id)
        if row is None:
            raise HTTPException(404, f"找不到编号 {entry_id} 的单据")
        if row.status != "pending":
            raise HTTPException(400, f"该单据已是「{row.status}」状态，不能重复审核")

        category = payload.category.strip()
        allowed = set(EXPENSE_ACCOUNTS) | set(INCOME_ACCOUNTS) | set(ASSET_SIDE_ACCOUNTS)
        if category not in allowed:
            raise HTTPException(400, f"科目「{category}」不在可选范围内")

        # 按收付方向生成借贷分录（规则写死，保证不会做反）
        # 付款账户优先用出纳登记时选的 payment；老数据没这个字段就用原来的贷方账户
        pay_acct = row.payment or (row.credit_account if row.entry_type == "expense"
                                   else row.debit_account)
        if row.entry_type == "income":
            debit, credit = pay_acct, category      # 收款：借 银行/现金，贷 收入
        else:
            debit, credit = category, pay_acct      # 付款：借 费用/资产，贷 银行/现金

        row.category = category
        row.debit_account = debit
        row.credit_account = credit
        row.status = "approved"
        row.reviewed_at = datetime.now()
        row.reject_reason = ""
        db.commit()
        db.refresh(row)
        result = row.as_dict()

    return {
        "ok": True,
        "entry": result,
        "message": f"已入账：借:{debit} 贷:{credit}（{category}）",
    }


@router.post("/{entry_id}/reject", summary="审核驳回：退回出纳修改")
def reject(entry_id: int, payload: RejectIn) -> dict:
    with SessionLocal() as db:
        row = db.get(LedgerEntry, entry_id)
        if row is None:
            raise HTTPException(404, f"找不到编号 {entry_id} 的单据")
        if row.status != "pending":
            raise HTTPException(400, f"该单据已是「{row.status}」状态，不能重复审核")

        row.status = "rejected"
        row.reject_reason = payload.reason.strip()[:200]
        row.reviewed_at = datetime.now()
        db.commit()

    return {"ok": True, "message": f"已驳回：{payload.reason}"}


@router.post("/{entry_id}/restore", summary="把驳回的单据退回待审核")
def restore(entry_id: int) -> dict:
    """出纳补正材料后，单据退回待审核队列。"""
    with SessionLocal() as db:
        row = db.get(LedgerEntry, entry_id)
        if row is None:
            raise HTTPException(404, f"找不到编号 {entry_id} 的单据")
        if row.status != "rejected":
            raise HTTPException(400, "只有「已驳回」的单据才能退回待审核")
        row.status = "pending"
        row.reject_reason = ""
        db.commit()
    return {"ok": True, "message": "已退回待审核"}


@router.get("/detail/{entry_id}", summary="查看单据详情（含往来的单位名）")
def detail(entry_id: int) -> dict:
    with SessionLocal() as db:
        row = db.get(LedgerEntry, entry_id)
        if row is None:
            raise HTTPException(404, "找不到该单据")
        d = row.as_dict()
        if row.counterparty_id:
            cp = db.get(Counterparty, row.counterparty_id)
            if cp:
                d["counterparty_name"] = cp.name
                d["counterparty_kind"] = cp.as_dict()["kind_cn"]
    return d
