"""统计服务：给前端图表提供汇总数据。"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from ..domain.db.models import LedgerEntry
from ..domain.db.session import SessionLocal


def _approved(rows: list[LedgerEntry]) -> list[LedgerEntry]:
    """只保留【已入账】的账目。

    这是「出纳管钱、会计管账」分离的关键：出纳录进来的是 pending（待审核），
    只有会计审核通过（approved）的才计入报表。否则出纳录错了直接就影响报表。
    """
    return [r for r in rows if r.status == "approved"]


def _operating_type(row: LedgerEntry) -> str:
    """判断一笔账是否影响损益（利润），返回 "income" / "expense" / ""。

    **规则：看分录的哪一侧是损益科目。**

       借 工资(费用)  贷 应付职工薪酬   → 借方是费用科目 → expense ✓
       借 应付职工薪酬 贷 银行存款       → 两侧都不是损益科目 → "" ✓（只是付钱）
       借 银行存款    贷 主营业务收入   → 贷方是收入科目 → income ✓
       借 固定资产    贷 银行存款       → "" ✓（资产购置）
       借 银行存款    贷 实收资本       → "" ✓（股东投入，是权益）

    为什么不能简单按 category 判断（踩过两次坑）：
      1. 「股东投入」若按 entry_type 算，会被当成收入 → 利润虚增 100 万
      2. 「发放工资」的 category 也是"工资"，按 category 算会
         和「计提工资」重复 → 费用虚增 81.6 万
    月末结转分录（entry_type=closing）必须排除，它们只是把余额转到本年利润。
    """
    from ..domain.accounts import EXPENSE_ACCOUNTS, INCOME_ACCOUNTS

    if row.entry_type == "closing":
        return ""
    # 借方是费用/成本科目 → 这笔是费用
    if row.debit_account in EXPENSE_ACCOUNTS:
        return "expense"
    # 贷方是收入科目 → 这笔是收入
    if row.credit_account in INCOME_ACCOUNTS:
        return "income"
    return ""


def overview(company_id: int | None = None) -> dict:
    """总览：总收入、总支出、结余、笔数（只统计已入账的经营收支）。"""
    with SessionLocal() as db:
        rows = _approved(db.scalars(select(LedgerEntry)).all())

    income = sum(r.amount for r in rows if _operating_type(r) == "income")
    expense = sum(r.amount for r in rows if _operating_type(r) == "expense")
    return {
        "total_entries": len(rows),
        "total_income": round(income, 2),
        "total_expense": round(expense, 2),
        "net": round(income - expense, 2),
    }


def account_balances() -> dict:
    """账户余额表（试算平衡表）。

    会计原理：每个账户的余额 = 各笔分录对该账户的借方合计 − 贷方合计。
    在这个约定下：资产/费用类余额为正，负债/权益/收入类余额为负。
    展示时会把负债/权益/收入转成正数（更符合直觉）。

    返回按会计要素分组的余额、「流动资金」，以及配平检查。
    """
    from ..domain.accounts import (
        ASSET_ACCOUNTS, EQUITY_ACCOUNTS, EXPENSE_ACCOUNTS, INCOME_ACCOUNTS,
        LIABILITY_ACCOUNTS,
    )

    with SessionLocal() as db:
        rows = _approved(db.scalars(select(LedgerEntry)).all())

    bal: dict[str, float] = defaultdict(float)
    for r in rows:
        bal[r.debit_account] += r.amount
        bal[r.credit_account] -= r.amount

    def _group(names: list[str], *, credit_side: bool = False) -> list[dict]:
        """credit_side=True 表示这类科目是贷方余额，展示时取反成正数。"""
        out = []
        for name in names:
            v = bal.get(name, 0.0)
            if abs(v) > 0.005:
                out.append({"account": name, "balance": round(-v if credit_side else v, 2)})
        return out

    assets = _group(ASSET_ACCOUNTS)
    liabilities = _group(LIABILITY_ACCOUNTS, credit_side=True)
    # 「本年利润」不从这里取——月末结账会往这个科目记结转分录，
    # 只取一部分期间的话（比如只结了 8 月），账上余额就只是 8 月的，
    # 其余月份的利润会丢，导致资产 ≠ 负债+权益。所以统一在下面用
    # 「全部期间的收入 − 费用」算，口径始终一致。
    equity = _group([a for a in EQUITY_ACCOUNTS if a != "本年利润"],
                    credit_side=True)
    revenue = _group(INCOME_ACCOUNTS, credit_side=True)
    expenses = _group(EXPENSE_ACCOUNTS)

    # 本年利润 = 经营性收入 − 经营性支出（按科目类型判断，见 _operating_type）
    total_revenue = sum(r.amount for r in rows if _operating_type(r) == "income")
    total_expense = sum(r.amount for r in rows if _operating_type(r) == "expense")
    profit = round(total_revenue - total_expense, 2)
    if abs(profit) > 0.005:
        equity = [*equity, {"account": "本年利润", "balance": profit}]

    # 试算平衡：借方余额合计 = 贷方余额合计
    total_debit = sum(v for v in bal.values() if v > 0)
    total_credit = sum(-v for v in bal.values() if v < 0)

    # 流动资金 = 手头现金 + 银行活期
    working_capital = round(bal.get("库存现金", 0.0) + bal.get("银行存款", 0.0), 2)

    # 配平检查：资产 = 负债 + 所有者权益
    total_assets = round(sum(a["balance"] for a in assets), 2)
    total_liab_equity = round(
        sum(a["balance"] for a in liabilities) + sum(a["balance"] for a in equity), 2
    )

    return {
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "revenue": revenue,
        "expenses": expenses,
        "working_capital": working_capital,
        "total_assets": total_assets,
        "total_liabilities_equity": total_liab_equity,
        "balance_sheet_ok": abs(total_assets - total_liab_equity) < 0.01,
        "trial_balance": {
            "debit": round(total_debit, 2),
            "credit": round(total_credit, 2),
            "balanced": abs(total_debit - total_credit) < 0.01,
        },
    }


def by_category() -> dict:
    """按科目汇总支出（饼图数据）。"""
    with SessionLocal() as db:
        rows = _approved(db.scalars(select(LedgerEntry)).all())

    agg: dict[str, float] = defaultdict(float)
    for r in rows:
        if _operating_type(r) == "expense":  # 只统计影响损益的费用
            agg[r.category or r.debit_account] += r.amount

    items = [
        {"name": k, "value": round(v, 2)}
        for k, v in sorted(agg.items(), key=lambda x: -x[1])
    ]
    return {"items": items}


def monthly() -> dict:
    """按月汇总收入/支出（折线图数据）。

    只统计 income / expense——股东投入(equity)、账户间转账(transfer)、
    购置资产(asset) 都不属于经营收支，混进来会让图表的数字失真。
    """
    with SessionLocal() as db:
        rows = _approved(db.scalars(select(LedgerEntry)).all())

    agg: dict[str, dict[str, float]] = defaultdict(lambda: {"income": 0.0, "expense": 0.0})
    for r in rows:
        kind = _operating_type(r)
        if not kind:
            continue
        month = r.date[:7]  # YYYY-MM
        agg[month][kind] += r.amount

    months = sorted(agg.keys())
    return {
        "months": months,
        "income": [round(agg[m]["income"], 2) for m in months],
        "expense": [round(agg[m]["expense"], 2) for m in months],
    }


def list_entries(category: str | None = None, month: str | None = None,
                 status: str | None = None, limit: int = 200) -> list[dict]:
    """账目明细列表（表格数据）。

    和报表不同，这里**默认显示全部状态**（含待审核），
    因为出纳需要看到自己刚录的单子；会计也需要看到待审核队列。
    """
    with SessionLocal() as db:
        stmt = select(LedgerEntry)
        if category:
            stmt = stmt.where(LedgerEntry.category == category)
        if month:
            stmt = stmt.where(LedgerEntry.date.like(f"{month}%"))
        if status:
            stmt = stmt.where(LedgerEntry.status == status)
        rows = db.scalars(stmt.order_by(LedgerEntry.date.desc()).limit(limit)).all()
    return [r.as_dict() for r in rows]


def delete_entry(entry_id: int) -> dict:
    """删除一笔账目（连同它的凭证文件）。

    误记、重复记账在真实工作中很常见，所以必须能删。
    """
    from pathlib import Path

    with SessionLocal() as db:
        row = db.get(LedgerEntry, entry_id)
        if row is None:
            return {"ok": False, "message": f"找不到编号 {entry_id} 的账目"}
        info = f"{row.date} {row.description} {row.amount:.2f}元"

        # 一并删掉凭证文件，避免留下没人认领的孤儿文件
        if row.attachment:
            safe_name = Path(row.attachment).name
            try:
                (Path("data/attachments") / safe_name).unlink(missing_ok=True)
            except OSError:
                pass  # 文件删不掉不影响删账目

        db.delete(row)
        db.commit()
    return {"ok": True, "message": f"已删除：{info}"}
