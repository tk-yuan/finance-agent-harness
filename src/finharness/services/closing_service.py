"""月末结账：会计的核心动作。

月末要干三件事，做完这个月的账才算"闭合"：
    ① 计提折旧 —— 固定资产按月折旧，不计利润就虚高
    ② 结转损益 —— 把收入、费用科目结转到「本年利润」
    ③ 结账封账 —— 本月账目封存，不能再改

为什么必须结转损益：
    平时利润是"算"出来的（收入减费用）；结转之后利润是"记"进账里的，
    收入费用科目归零，下个月重新开始累计。这才是真实会计的做法。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from ..domain.accounts import (
    DEPRECIATION_SALVAGE_RATE,
    DEPRECIATION_YEARS,
    EXPENSE_ACCOUNTS,
    INCOME_ACCOUNTS,
    PROFIT_ACCOUNT,
)
from ..domain.db.models import ClosedPeriod, LedgerEntry
from ..domain.db.session import SessionLocal


def list_closed() -> list[dict]:
    """已结账的期间列表。"""
    with SessionLocal() as db:
        rows = db.scalars(select(ClosedPeriod).order_by(ClosedPeriod.period.desc())).all()
    return [{"period": r.period, "closed_at": r.closed_at.strftime("%Y-%m-%d %H:%M")}
            for r in rows]


def is_closed(period: str) -> bool:
    with SessionLocal() as db:
        return db.get(ClosedPeriod, period) is not None


def _period_rows(db, period: str) -> list[LedgerEntry]:
    """取某期间【已入账】的账目。"""
    return [r for r in db.scalars(
        select(LedgerEntry).where(LedgerEntry.date.like(f"{period}%"))
    ).all() if r.status == "approved"]


def _depreciation(period: str) -> tuple[float, float]:
    """算本月应计提的折旧。

    直线法：月折旧 = 原值 × (1 − 残值率) ÷ (年限 × 12)
    只在同一个期间计提一次（已计提过就不再提）。
    """
    with SessionLocal() as db:
        rows = _period_rows(db, period)
        # 已经提过折旧就不再提
        already = any(r.description.startswith("计提折旧") for r in rows)
        if already:
            return 0.0, 0.0

        # 固定资产原值 = 固定资产科目的借方累计（截至本月末）
        all_rows = [r for r in db.scalars(select(LedgerEntry)).all()
                    if r.status == "approved" and r.date <= f"{period}-31"]
    original = sum(r.amount for r in all_rows if r.debit_account == "固定资产")
    if original <= 0:
        return 0.0, 0.0

    monthly = original * (1 - DEPRECIATION_SALVAGE_RATE) / (DEPRECIATION_YEARS * 12)
    return round(original, 2), round(monthly, 2)


def preview(period: str) -> dict:
    """预览结账会产生哪些分录——先看清楚再执行。"""
    if is_closed(period):
        return {"ok": False, "message": f"{period} 已经结过账了"}

    with SessionLocal() as db:
        rows = _period_rows(db, period)

    pending = _count_pending(period)
    income_total = sum(r.amount for r in rows if r.entry_type == "income")
    expense_total = sum(r.amount for r in rows if r.entry_type == "expense")
    original, monthly = _depreciation(period)

    entries = []
    if monthly > 0:
        entries.append({
            "type": "折旧",
            "text": f"借:折旧费 {monthly:,.2f}　贷:累计折旧 {monthly:,.2f}",
            "note": f"固定资产原值 {original:,.2f}，"
                    f"按 {DEPRECIATION_YEARS} 年直线法（残值率 {DEPRECIATION_SALVAGE_RATE:.0%}）计提",
        })
    if income_total or expense_total:
        entries.append({
            "type": "结转损益",
            "text": f"收入 {income_total:,.2f} 与费用 {expense_total:,.2f} 结转到「{PROFIT_ACCOUNT}」",
            "note": f"结转后本月利润 = {income_total - expense_total:,.2f} 元",
        })

    return {
        "ok": True,
        "period": period,
        "pending_count": pending,
        "income_total": round(income_total, 2),
        "expense_total": round(expense_total, 2),
        "profit": round(income_total - expense_total, 2),
        "depreciation": monthly,
        "plans": entries,
        "warn": "本月还有待审核单据，建议先审核完再结账" if pending else "",
    }


def _count_pending(period: str) -> int:
    with SessionLocal() as db:
        rows = db.scalars(
            select(LedgerEntry).where(LedgerEntry.date.like(f"{period}%"))
        ).all()
    return len([r for r in rows if r.status == "pending"])


def close(period: str, force: bool = False) -> dict:
    """执行月末结账。

    force=True 时即使还有待审核单据也结账（不推荐，但要给会计选择权）。
    """
    if is_closed(period):
        return {"ok": False, "message": f"{period} 已经结过账了"}

    pending = _count_pending(period)
    if pending and not force:
        return {"ok": False,
                "message": f"本月还有 {pending} 笔待审核单据，请先审核完再结账"}

    created = []
    end_day = f"{period}-28"   # 结账分录统一记在月末

    with SessionLocal() as db:
        # ---- ① 计提折旧 ----
        original, monthly = _depreciation(period)
        if monthly > 0:
            db.add(LedgerEntry(
                date=end_day, amount=monthly, description="计提本月折旧",
                category="折旧费", entry_type="expense",
                debit_account="折旧费", credit_account="累计折旧",
                payment="", status="approved", reviewed_at=datetime.now(),
            ))
            created.append(f"计提折旧 {monthly:,.2f} 元")

        # ---- ② 结转损益 ----
        rows = _period_rows(db, period)
        # 按科目汇总。**按科目类型判断收支，不按 entry_type**——
        # 否则「股东投入」这类权益科目（出纳记为收款）会被当成收入结转到利润里。
        from ..services.stats_service import _operating_type

        income_by: dict[str, float] = {}
        expense_by: dict[str, float] = {}
        for r in rows:
            if not r.category:
                continue
            kind = _operating_type(r)
            if kind == "income":
                income_by[r.category] = income_by.get(r.category, 0) + r.amount
            elif kind == "expense":
                expense_by[r.category] = expense_by.get(r.category, 0) + r.amount

        # 收入 → 本年利润：借 收入科目，贷 本年利润
        for acct, amt in income_by.items():
            if acct not in INCOME_ACCOUNTS or amt <= 0:
                continue
            db.add(LedgerEntry(
                date=end_day, amount=round(amt, 2),
                description=f"期末结转-{acct}", category=acct,
                entry_type="closing",
                debit_account=acct, credit_account=PROFIT_ACCOUNT,
                payment="", status="approved", reviewed_at=datetime.now(),
            ))
        # 费用 → 本年利润：借 本年利润，贷 费用科目
        for acct, amt in expense_by.items():
            if amt <= 0:
                continue
            db.add(LedgerEntry(
                date=end_day, amount=round(amt, 2),
                description=f"期末结转-{acct}", category=acct,
                entry_type="closing",
                debit_account=PROFIT_ACCOUNT, credit_account=acct,
                payment="", status="approved", reviewed_at=datetime.now(),
            ))
        if income_by or expense_by:
            profit = sum(income_by.values()) - sum(expense_by.values())
            created.append(f"结转损益，本月利润 {profit:,.2f} 元")

        # ---- ③ 标记已结账 ----
        db.add(ClosedPeriod(period=period, note="月末结账"))

        # 结转分录里的折旧费也要结平（它是在结账过程中产生的）
        db.commit()

    return {"ok": True, "period": period, "created": created,
            "message": f"{period} 结账完成：" + "；".join(created)}
