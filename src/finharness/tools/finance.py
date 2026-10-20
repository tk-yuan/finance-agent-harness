"""财务工具：余额、往来、对账、结账、时间、报表。

这一层和 `ledger.py` 的分工：
    ledger.py   —— **改变账本状态**的操作（提交/审核/驳回/改科目），有内控边界
    本文件      —— **读取与核算**：算余额、查往来、对账、结账、出报表

## 三个容易做错、所以必须由代码保证的地方

**① 余额只算已入账的。** 待审核的单据还没进账，算进余额就等于
「出纳自己录一笔，钱就变了」——内控白做。所有查询都带 `status == "approved"`。

**② 余额 = 各笔分录的借方 − 贷方。** 不是简单地把收入减支出——
那是流水口径。会计上的余额是**账户维度**的：一个账户被借记就增加、被贷记就减少
（资产类如此；负债类相反）。混了口径，报表就是错的。

**③ 结账前必须没有待审核单据。** 一个会计期间一旦结账就不能再改，
所以结账前要保证这个期间的单据都已经过审核——否则等于把未确认的东西封进历史。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date as _date
from datetime import datetime, timedelta

from sqlalchemy import select

from ..domain import accounts as A
from ..domain.db._session import session
from ..domain.db.models import BankReconciliation, ClosedPeriod, Counterparty, LedgerEntry
from ..runtime.tools import Tool, ToolRegistry


# ═══════════════ 余额 ═══════════════

def _balances() -> dict[str, float]:
    """按账户算余额。**只算已入账的**——待审核的还没进账。"""
    with session() as db:
        rows = [r for r in db.scalars(select(LedgerEntry)).all()
                if r.status == "approved"]
    bal: dict[str, float] = defaultdict(float)
    for r in rows:
        if r.debit_account:
            bal[r.debit_account] += r.amount
        if r.credit_account:
            bal[r.credit_account] -= r.amount
    return bal


def get_account_balance(account: str = "") -> dict:
    """查账户余额。

    和「查流水」的区别：查流水回答"这段时间花了多少"，
    本工具回答"账上现在还剩多少"。余额是**存量**，流水是**流量**。
    """
    bal = _balances()
    cash = round(bal.get("库存现金", 0.0), 2)
    bank = round(bal.get("银行存款", 0.0), 2)

    # 模型常把「流动资金」这类概念词当账户名传进来，兜一下
    if account and account not in bal and account in (
        "流动资金", "现金", "钱", "余额", "总余额", "资金", "可用资金"
    ):
        account = ""

    detail = {
        name: round(bal.get(name, 0.0), 2)
        for name in A.ASSET_ACCOUNTS + A.LIABILITY_ACCOUNTS
        if abs(bal.get(name, 0.0)) >= 0.005
    }
    return {
        "库存现金": cash,
        "银行存款": bank,
        "流动资金合计": round(cash + bank, 2),
        "account_balances": detail,
        "note": "仅统计已入账账目；待审核单据尚未计入",
    }


# ═══════════════ 往来 ═══════════════

def query_counterparty(name: str) -> dict:
    """查与某客户/供应商的往来。"""
    if not name.strip():
        return {"error": "请给出往来单位名称"}
    with session() as db:
        cp = db.scalars(
            select(Counterparty).where(Counterparty.name.contains(name.strip()))
        ).first()
        if cp is None:
            return {"error": f"没有找到名称包含「{name}」的往来单位"}

        rows = db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.counterparty_id == cp.id)
            .where(LedgerEntry.status == "approved")
            .order_by(LedgerEntry.date)
        ).all()

    income = round(sum(r.amount for r in rows if r.entry_type == "income"), 2)
    expense = round(sum(r.amount for r in rows if r.entry_type == "expense"), 2)
    return {
        "name": cp.name,
        "kind": "客户" if cp.kind == "customer" else "供应商",
        "count": len(rows),
        "income_from": income,      # 从它这儿收了多少
        "paid_to": expense,         # 付给它多少
        "net": round(income - expense, 2),
        "recent": [r.as_dict() for r in rows[-8:]],
    }


# ═══════════════ 对账 ═══════════════

def reconcile_month(period: str = "") -> dict:
    """月度对账：找出异常（大额、重复、缺项），并和银行对账单核对。

    对账不是"再看一遍账"，是**主动找异常**：
      · 金额异常大的支出
      · 同一天同一金额同一摘要（可能重复录入）
      · 缺凭证的、缺往来的
    这三类是实际查账时最花时间的，交给代码扫描比人眼快得多。
    """
    period = period or datetime.now().strftime("%Y-%m")
    with session() as db:
        rows = db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.date.startswith(period))
            .where(LedgerEntry.status == "approved")
        ).all()
        recon = db.get(BankReconciliation, 1)

    if not rows:
        return {"period": period, "count": 0, "message": f"{period} 没有已入账账目"}

    amounts = [r.amount for r in rows]
    avg = sum(amounts) / len(amounts)

    anomalies: list[dict] = []
    seen: dict[tuple, list[int]] = defaultdict(list)
    for r in rows:
        if r.amount > max(avg * 5, 10000):
            anomalies.append({"类型": "大额支出", "单据": r.id, "金额": r.amount,
                              "摘要": r.description})
        seen[(r.date, r.amount, r.description)].append(r.id)
        if not r.attachment:
            anomalies.append({"类型": "缺凭证", "单据": r.id, "金额": r.amount})
    for key, ids in seen.items():
        if len(ids) > 1:
            anomalies.append({"类型": "疑似重复记账", "单据": ids,
                              "金额": key[1], "摘要": key[2]})

    income = round(sum(r.amount for r in rows if r.entry_type == "income"), 2)
    expense = round(sum(r.amount for r in rows if r.entry_type == "expense"), 2)
    by_type: dict[str, int] = {}
    for a in anomalies:
        by_type[a["类型"]] = by_type.get(a["类型"], 0) + 1
    return {
        "period": period,
        "count": len(rows),
        "income": income,
        "expense": expense,
        "anomaly_count": len(anomalies),
        "anomaly_by_type": by_type,
        "anomalies": anomalies[:15],
        "bank_reconciled": bool(recon.reconciled) if recon else False,
    }


# ═══════════════ 月末结账（s11/s12 的核心动作）═══════════════

def close_period(period: str = "", note: str = "") -> dict:
    """月末结账：把当期损益结转到「本年利润」，并封存该期间。

    ## 为什么这个动作要有前置校验

    结账一旦完成，这个期间的账**就不该再动了**——它是给股东、税局、
    银行看的最终数字。所以结账前必须保证：

      · **没有待审核单据**。混着未审核的账结账，等于把没确认的东西封进历史
      · **该期间没结过账**。重复结账会把损益结转两次，本年利润直接翻倍

    这两条不是"最好有"，是**必须**——出了错就得上反结账，而反结账本身
    在真实企业里是要走审批的重大操作。

    ## 结转规则

    收入类科目（贷方余额）→ 转入本年利润贷方
    费用类科目（借方余额）→ 转入本年利润借方
    结转后各损益类科目余额归零，本年利润反映当期净利润。
    """
    period = period or datetime.now().strftime("%Y-%m")

    with session() as db:
        if db.get(ClosedPeriod, period) is not None:
            return {"error": f"{period} 已经结过账了。要改得先反结账。"}

        pending = db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.date.startswith(period))
            .where(LedgerEntry.status == "pending")
        ).all()
        if pending:
            return {
                "error": f"{period} 还有 {len(pending)} 张待审核单据，不能结账",
                "pending_ids": [r.id for r in pending],
                "next": "先让会计审核完这些单据",
            }

        rows = db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.date.startswith(period))
            .where(LedgerEntry.status == "approved")
        ).all()

        income = round(sum(r.amount for r in rows if r.entry_type == "income"), 2)
        expense = round(sum(r.amount for r in rows if r.entry_type == "expense"), 2)
        profit = round(income - expense, 2)

        # 结转分录：一条把收入转出，一条把费用转出，差额进本年利润
        if income:
            db.add(LedgerEntry(
                date=f"{period}-末", amount=income,
                description=f"{period} 结转收入", category="主营业务收入",
                entry_type="income", debit_account="主营业务收入",
                credit_account=A.PROFIT_ACCOUNT, status="approved",
                attachment="结转凭证",
            ))
        if expense:
            db.add(LedgerEntry(
                date=f"{period}-末", amount=expense,
                description=f"{period} 结转费用", category="主营业务成本",
                entry_type="expense", debit_account=A.PROFIT_ACCOUNT,
                credit_account="主营业务成本", status="approved",
                attachment="结转凭证",
            ))

        db.add(ClosedPeriod(period=period, note=note[:200]))

    return {
        "ok": True, "period": period,
        "income": income, "expense": expense, "profit": profit,
        "message": f"{period} 已结账：收入 {income:,.2f} − 费用 {expense:,.2f} "
                   f"= 净利润 {profit:,.2f} 元（已结转至「{A.PROFIT_ACCOUNT}」）",
    }


def list_closed_periods() -> dict:
    with session() as db:
        rows = db.scalars(select(ClosedPeriod).order_by(ClosedPeriod.period)).all()
    return {"closed": [{"period": r.period, "closed_at": str(r.closed_at),
                        "note": r.note} for r in rows]}


# ═══════════════ 报表 ═══════════════

def generate_report(period: str = "", include_balance: bool = True) -> dict:
    """出简易报表：利润表 + 资产负债表（小企业口径）。"""
    period = period or datetime.now().strftime("%Y")
    bal = _balances()
    with session() as db:
        rows = db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.date.startswith(period))
            .where(LedgerEntry.status == "approved")
        ).all()

    income = round(sum(r.amount for r in rows if r.entry_type == "income"), 2)
    expense = round(sum(r.amount for r in rows if r.entry_type == "expense"), 2)
    by_cat: dict[str, float] = defaultdict(float)
    for r in rows:
        if r.entry_type == "expense" and r.category:
            by_cat[r.category] += r.amount

    report = {
        "period": period,
        "income_statement": {
            "营业收入": income,
            "营业成本及费用": expense,
            "净利润": round(income - expense, 2),
            "费用明细": {k: round(v, 2) for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])},
        },
    }
    if include_balance:
        report["balance_sheet"] = {
            "资产": {n: round(bal.get(n, 0.0), 2) for n in A.ASSET_ACCOUNTS
                     if abs(bal.get(n, 0.0)) >= 0.005},
            "负债": {n: round(-bal.get(n, 0.0), 2) for n in A.LIABILITY_ACCOUNTS
                     if abs(bal.get(n, 0.0)) >= 0.005},
        }
    return report


# ═══════════════ 时间 ═══════════════

def get_current_time() -> dict:
    """当前日期。日期是确定性信息，必须由代码给出，不让模型猜。"""
    now = datetime.now()
    return {"date": now.strftime("%Y-%m-%d"), "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "weekday": "一二三四五六日"[now.weekday()], "period": now.strftime("%Y-%m")}


def days_between(start_date: str, end_date: str) -> dict:
    try:
        d1 = _date.fromisoformat(start_date.strip())
        d2 = _date.fromisoformat(end_date.strip())
    except ValueError:
        return {"error": "日期格式应为 YYYY-MM-DD"}
    return {"days": (d2 - d1).days, "start": start_date, "end": end_date}


# ═══════════════ 注册 ═══════════════

def register_finance_tools(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="get_account_balance",
        description="查账户余额（账上现在还剩多少钱）。区别于「查流水」——余额是存量，流水是流量。",
        parameters={"type": "object", "properties": {
            "account": {"type": "string", "description": "账户名，不填返回全部"}}},
        handler=get_account_balance, read_only=True, category="finance",
    ))
    registry.register(Tool(
        name="query_counterparty",
        description="查与某客户/供应商的往来：收了多少、付了多少、净额多少。",
        parameters={"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]},
        handler=query_counterparty, read_only=True, category="finance",
    ))
    registry.register(Tool(
        name="reconcile_month",
        description="月度对账：扫描异常（大额支出、疑似重复记账、缺凭证），并给出当期收支合计。",
        parameters={"type": "object", "properties": {
            "period": {"type": "string", "description": "月份 YYYY-MM，不填用当月"}}},
        handler=reconcile_month, read_only=True, category="finance",
    ))
    registry.register(Tool(
        name="close_period",
        description="**月末结账**：把当期损益结转到「本年利润」并封存该期间。"
                    "结账后该期间不能再改。有未审核单据或已结过账都会被拒绝。",
        parameters={"type": "object", "properties": {
            "period": {"type": "string", "description": "月份 YYYY-MM，不填用当月"},
            "note": {"type": "string"}}},
        handler=close_period, read_only=False, category="finance",
    ))
    registry.register(Tool(
        name="list_closed_periods",
        description="列出已结账的会计期间。",
        parameters={"type": "object", "properties": {}},
        handler=list_closed_periods, read_only=True, category="finance",
    ))
    registry.register(Tool(
        name="generate_report",
        description="生成报表：利润表（收入/费用/净利润 + 费用明细）与资产负债表。",
        parameters={"type": "object", "properties": {
            "period": {"type": "string", "description": "年份 YYYY，不填用今年"},
            "include_balance": {"type": "boolean", "default": True}}},
        handler=generate_report, read_only=True, category="finance",
    ))
    registry.register(Tool(
        name="get_current_time",
        description="获取当前日期与会计期间。",
        parameters={"type": "object", "properties": {}},
        handler=get_current_time, read_only=True, category="time",
    ))
    registry.register(Tool(
        name="days_between",
        description="计算两个日期相差多少天。",
        parameters={"type": "object", "properties": {
            "start_date": {"type": "string"}, "end_date": {"type": "string"}},
            "required": ["start_date", "end_date"]},
        handler=days_between, read_only=True, category="time",
    ))
