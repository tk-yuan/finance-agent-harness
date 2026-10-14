"""财务报表：利润表（损益表）。

老板最关心的不是"账上有多少钱"，而是"这个月赚了多少"——那就是利润表。
之前系统只有资产负债表（余额表），缺了这一张。
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from ..domain.db.models import LedgerEntry
from ..domain.db.session import SessionLocal

# 科目 → 利润表项目 的归类
_REVENUE = ["主营业务收入", "其他业务收入", "营业收入"]
_COST = ["主营业务成本"]
_TAX_SURCHARGE = ["税费"]
_FINANCE = ["银行手续费"]
# 其余费用都算管理费用（简化处理）
_ADMIN_KEYWORDS = ["工资", "社保费", "房租", "水电费", "办公费", "差旅费",
                   "业务招待费", "通讯费", "交通费", "折旧费", "其他"]


def _sum_by_account(rows: list[LedgerEntry], period: str | None) -> dict[str, float]:
    """按科目汇总「影响损益」的金额。

    用 stats_service._operating_type 统一判断，避免各处口径不一致——
    曾经利润表和资产负债表算法不同，一个显示赚 6 万、一个显示亏 74 万。
    """
    from ..services.stats_service import _operating_type

    agg: dict[str, float] = defaultdict(float)
    for r in rows:
        if r.status != "approved":
            continue
        if period and not r.date.startswith(period):
            continue
        if not _operating_type(r):
            continue          # 不影响损益的（资产购置、股东投入、付款转账）跳过
        key = r.category or (r.debit_account or r.credit_account)
        agg[key] += r.amount
    return agg


def income_statement(period: str | None = None) -> dict:
    """利润表。

    Args:
        period: YYYY-MM，不填则统计全部期间。
    """
    with SessionLocal() as db:
        rows = db.scalars(select(LedgerEntry)).all()

    agg = _sum_by_account(rows, period)

    revenue = sum(agg.get(a, 0) for a in _REVENUE)
    cost = sum(agg.get(a, 0) for a in _COST)
    tax = sum(agg.get(a, 0) for a in _TAX_SURCHARGE)
    finance = sum(agg.get(a, 0) for a in _FINANCE)
    admin = sum(agg.get(a, 0) for a in _ADMIN_KEYWORDS)

    gross = revenue - cost                    # 毛利
    operating = gross - admin - tax - finance  # 营业利润
    total_profit = operating                   # 简化：没有营业外收支
    income_tax = round(max(total_profit, 0) * 0.05, 2)   # 小微企业实际税负约 5%
    net = round(total_profit - income_tax, 2)

    def row(name: str, amount: float, *, bold: bool = False,
            indent: bool = False) -> dict:
        return {"name": name, "amount": round(amount, 2),
                "bold": bold, "indent": indent}

    return {
        "period": period or "全部期间",
        "lines": [
            row("一、营业收入", revenue, bold=True),
            row("减：营业成本", cost, indent=True),
            row("　　税金及附加", tax, indent=True),
            row("　　管理费用", admin, indent=True),
            row("　　财务费用", finance, indent=True),
            row("二、营业利润", operating, bold=True),
            row("三、利润总额", total_profit, bold=True),
            row("减：所得税费用", income_tax, indent=True),
            row("四、净利润", net, bold=True),
        ],
        "revenue": round(revenue, 2),
        "cost": round(cost, 2),
        "gross_profit": round(gross, 2),
        "gross_margin": round(gross / revenue * 100, 1) if revenue else 0.0,
        "operating_profit": round(operating, 2),
        "net_profit": net,
        "net_margin": round(net / revenue * 100, 1) if revenue else 0.0,
        "admin": round(admin, 2),
        "tax_surcharge": round(tax, 2),
        "finance": round(finance, 2),
        "income_tax": income_tax,
    }


def available_periods() -> list[str]:
    """有数据的月份列表，供利润表选择期间。"""
    with SessionLocal() as db:
        rows = db.scalars(select(LedgerEntry)).all()
    months = sorted({r.date[:7] for r in rows if r.date and r.status == "approved"})
    return months
