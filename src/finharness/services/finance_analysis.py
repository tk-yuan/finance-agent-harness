"""财务分析指标计算（确定性部分）。

设计原则：**指标由代码算，解读交给大模型**。
让 LLM 自己算毛利率是不可靠的——它会算错，而且错得不易察觉。
所以这里把数字全部算准，大模型只负责"这些数字说明什么"。

按财务分析的经典四维度组织：
    盈利能力 —— 赚不赚钱
    偿债能力 —— 还得出债吗
    营运能力 —— 资产转得快不快
    风险排查 —— 有没有雷
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from ..domain.accounts import EXPENSE_ACCOUNTS, INCOME_ACCOUNTS
from ..domain.db.models import Counterparty, LedgerEntry
from ..domain.db.session import SessionLocal


def _rows() -> list[LedgerEntry]:
    with SessionLocal() as db:
        return [r for r in db.scalars(select(LedgerEntry)).all()
                if r.status == "approved"]


def _operating_type(r: LedgerEntry) -> str:
    from ..services.stats_service import _operating_type as f
    return f(r)


def _sum(rows, *, account: str | None = None, kind: str | None = None,
         debit_side: bool = True) -> float:
    """按科目或损益类型汇总。"""
    total = 0.0
    for r in rows:
        if kind and _operating_type(r) != kind:
            continue
        if account:
            side = r.debit_account if debit_side else r.credit_account
            if side != account:
                continue
        total += r.amount
    return round(total, 2)


# ============ ① 盈利能力 ============

def profitability(rows: list[LedgerEntry] | None = None) -> dict:
    rows = rows if rows is not None else _rows()
    revenue = _sum(rows, kind="income")
    cost = _sum(rows, account="主营业务成本")
    expense = _sum(rows, kind="expense")
    gross = round(revenue - cost, 2)
    net = round(revenue - expense, 2)

    return {
        "营业收入": revenue,
        "营业成本": cost,
        "毛利": gross,
        "净利润": net,
        "毛利率": round(gross / revenue * 100, 1) if revenue else 0.0,
        "净利率": round(net / revenue * 100, 1) if revenue else 0.0,
        "费用率": round((expense - cost) / revenue * 100, 1) if revenue else 0.0,
    }


# ============ ② 偿债能力 ============

def solvency(rows: list[LedgerEntry] | None = None) -> dict:
    rows = rows if rows is not None else _rows()

    # 从账户余额算
    bal: dict[str, float] = defaultdict(float)
    for r in rows:
        bal[r.debit_account] += r.amount
        bal[r.credit_account] -= r.amount

    资产 = sum(v for k, v in bal.items() if k in
              ("库存现金", "银行存款", "应收账款", "库存商品", "固定资产")
              )
    资产 += bal.get("累计折旧", 0.0)          # 累计折旧是资产备抵（负数）
    负债 = -(sum(v for k, v in bal.items()
                 if k in ("应付账款", "应付职工薪酬", "应交税费")))
    流动资产 = bal.get("库存现金", 0) + bal.get("银行存款", 0) + bal.get("应收账款", 0)
    流动负债 = 负债

    return {
        "资产总额": round(资产, 2),
        "负债总额": round(负债, 2),
        "流动资产": round(流动资产, 2),
        "流动负债": round(流动负债, 2),
        "资产负债率": round(负债 / 资产 * 100, 1) if 资产 else 0.0,
        "流动比率": round(流动资产 / 流动负债, 2) if 流动负债 else 0.0,
        "流动资金": round(bal.get("库存现金", 0) + bal.get("银行存款", 0), 2),
    }


# ============ ③ 营运能力 ============

def efficiency(rows: list[LedgerEntry] | None = None) -> dict:
    rows = rows if rows is not None else _rows()
    revenue = _sum(rows, kind="income")
    cost = _sum(rows, account="主营业务成本")

    # 期末应收/应付余额
    bal: dict[str, float] = defaultdict(float)
    for r in rows:
        bal[r.debit_account] += r.amount
        bal[r.credit_account] -= r.amount
    应收 = bal.get("应收账款", 0.0)
    应付 = -bal.get("应付账款", 0.0)

    # 周转率 = 收入(成本) / 期末余额。严格应该用平均余额，这里简化。
    应收周转 = round(revenue / 应收, 2) if 应收 else 0.0
    应付周转 = round(cost / 应付, 2) if 应付 else 0.0

    # 赊销占比
    赊销 = sum(r.amount for r in rows
               if r.debit_account == "应收账款" and _operating_type(r) == "income")
    现结 = sum(r.amount for r in rows
               if r.debit_account == "银行存款" and _operating_type(r) == "income")
    总销售 = 赊销 + 现结

    return {
        "应收账款余额": round(应收, 2),
        "应付账款余额": round(应付, 2),
        "应收账款周转率": 应收周转,
        "应付账款周转率": 应付周转,
        "赊销占比": round(赊销 / 总销售 * 100, 1) if 总销售 else 0.0,
        "现结占比": round(现结 / 总销售 * 100, 1) if 总销售 else 0.0,
    }


# 单笔超过此金额视为大额异常（原先定义在 agents/reconcile.py，那个模块是 LangGraph 的，已弃用）
LARGE_AMOUNT = 150000.0

# ============ ④ 风险排查 ============

def risk(rows: list[LedgerEntry] | None = None) -> dict:

    rows = rows if rows is not None else _rows()

    big = [r for r in rows if _operating_type(r) == "expense" and r.amount > LARGE_AMOUNT]
    vague = [r for r in rows if len((r.description or "").strip()) < 2]

    # 客户集中度：前 3 大客户的销售额占比
    with SessionLocal() as db:
        cps = {c.id: c for c in db.scalars(select(Counterparty)).all()}
    by_cp: dict[str, float] = defaultdict(float)
    for r in rows:
        if r.counterparty_id and _operating_type(r) == "income":
            by_cp[cps[r.counterparty_id].name if r.counterparty_id in cps else "?"] += r.amount
    total_sales = sum(by_cp.values()) or 1
    top3 = sorted(by_cp.items(), key=lambda x: -x[1])[:3]
    concentration = round(sum(v for _, v in top3) / total_sales * 100, 1)

    # 客户欠款（应收）集中在谁身上
    top_debtors = sorted(
        ((name, amt) for name, amt in by_cp.items()), key=lambda x: -x[1]
    )[:3]

    return {
        "大额支出笔数": len(big),
        "大额支出金额": round(sum(r.amount for r in big), 2),
        "大额支出明细": [
            {"date": r.date, "desc": r.description, "amount": r.amount} for r in big[:5]
        ],
        "摘要缺失笔数": len(vague),
        "客户集中度": concentration,
        "前三大客户": [{"name": n, "amount": round(a, 2)} for n, a in top3],
        "涉及客户数": len(by_cp),
    }


def all_metrics() -> dict:
    """一次算完四个维度的指标。"""
    rows = _rows()
    return {
        "profitability": profitability(rows),
        "solvency": solvency(rows),
        "efficiency": efficiency(rows),
        "risk": risk(rows),
        "entry_count": len(rows),
    }
