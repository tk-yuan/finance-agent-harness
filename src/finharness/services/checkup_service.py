"""月末核对：现金盘点、银行存款余额调节表、往来对账单。

会计月末核对账的正式说法叫「四相符」：
    账证相符（账簿 = 凭证）
    账账相符（总账 = 明细账；会计账 = 出纳日记账）
    账实相符（账上 = 实际）← 本模块主要解决这个
    账表相符（报表 = 账簿）

「账实相符」要解决的是：**账上记的钱，和现实里实际有多少，对不对得上**。
现金要真去点，银行要对账单，往来要对方确认——系统算不了，但它能帮你
把差额算清楚、把调节表和对账单准备好。
"""

from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy import select

from ..domain.db.models import BankReconciliation, CashCheck, Counterparty, LedgerEntry
from ..domain.db.session import SessionLocal


# ============ 通用：取已入账账目 ============

def _approved_rows() -> list[LedgerEntry]:
    with SessionLocal() as db:
        return [r for r in db.scalars(select(LedgerEntry)).all()
                if r.status == "approved"]


def _account_balance(account: str, until: str | None = None) -> float:
    """算某科目的余额（借方为正，贷方为负）。until 用于取截止某日的余额。"""
    total = 0.0
    for r in _approved_rows():
        if until and r.date > until:
            continue
        if r.debit_account == account:
            total += r.amount
        if r.credit_account == account:
            total -= r.amount
    return round(total, 2)


# ============ ① 库存现金盘点 ============

def cash_book_balance(until: str | None = None) -> float:
    """账面库存现金余额。"""
    return _account_balance("库存现金", until)


def create_cash_check(actual_amount: float, date: str, note: str = "",
                      checked_by: str = "") -> dict:
    """登记一次现金盘点。

    差额 = 实盘 − 账面：
        正数 = 盘盈（长款，钱比账上多）
        负数 = 盘亏（短款，钱比账上少）← 要查明原因，通常要出纳赔偿
    """
    book = cash_book_balance(until=date)
    diff = round(actual_amount - book, 2)
    if abs(diff) < 0.005:
        result = "相符"
    elif diff > 0:
        result = "盘盈"
    else:
        result = "盘亏"

    with SessionLocal() as db:
        row = CashCheck(
            date=date, book_amount=book, actual_amount=round(actual_amount, 2),
            difference=diff, result=result, note=note.strip()[:200],
            checked_by=checked_by.strip()[:30],
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.as_dict()


def list_cash_checks(limit: int = 50) -> list[dict]:
    with SessionLocal() as db:
        rows = db.scalars(
            select(CashCheck).order_by(CashCheck.id.desc()).limit(limit)
        ).all()
    return [r.as_dict() for r in rows]


# ============ ② 银行存款余额调节表 ============

def bank_book_balance(until: str | None = None) -> float:
    """企业账面银行存款余额。"""
    return _account_balance("银行存款", until)


def create_bank_recon(period: str, statement_balance: float,
                      items_added: list[dict] | None = None,
                      items_subtracted: list[dict] | None = None,
                      note: str = "") -> dict:
    """编制银行存款余额调节表。

    调节逻辑：
        银行对账单余额
        ＋ 企业已收、银行未收（比如客户转账在路上）
        － 企业已付、银行未付（比如开出的支票还没兑付）
        ─────────────────────────────
        ＝ 调节后余额   ← 应与企业账面余额一致

    一致 → 说明银行账没问题；不一致 → 还有别的未达账项没找出来。
    """
    import calendar

    y, m = period.split("-")
    last_day = f"{period}-{calendar.monthrange(int(y), int(m))[1]:02d}"
    book = bank_book_balance(until=last_day)

    added = items_added or []
    sub = items_subtracted or []
    adjusted = round(statement_balance + sum(x["amount"] for x in added)
                     - sum(x["amount"] for x in sub), 2)
    reconciled = abs(adjusted - book) < 0.01

    with SessionLocal() as db:
        row = BankReconciliation(
            period=period,
            statement_balance=round(statement_balance, 2),
            book_balance=book,
            items_added=json.dumps(added, ensure_ascii=False),
            items_subtracted=json.dumps(sub, ensure_ascii=False),
            reconciled=reconciled,
            note=note.strip()[:200],
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.as_dict()


def list_bank_recons(limit: int = 24) -> list[dict]:
    with SessionLocal() as db:
        rows = db.scalars(
            select(BankReconciliation).order_by(BankReconciliation.period.desc()).limit(limit)
        ).all()
    return [r.as_dict() for r in rows]


# ============ ③ 往来对账单 ============

def counterparty_statement(counterparty_id: int,
                           start: str | None = None,
                           end: str | None = None) -> dict:
    """生成某个往来单位的对账单。

    对账单是给客户/供应商核对的凭证："截至 X 月末，你欠我 XX 元，如有出入请指出。"

    说明（诚实标注）：本系统的「收回销售货款」是按整笔记录的、没有逐笔核销到具体客户，
    所以期末应收余额是**按本期赊销估算**的，不是精确的账龄结果。
    """
    with SessionLocal() as db:
        cp = db.get(Counterparty, counterparty_id)
        if cp is None:
            return {"ok": False, "message": f"找不到编号 {counterparty_id} 的往来单位"}
        rows = [r for r in db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.counterparty_id == counterparty_id,
                   LedgerEntry.status == "approved")
            .order_by(LedgerEntry.date)
        ).all()]
        info = cp.as_dict()

    # 期初余额：start 之前的累计挂账
    opening = 0.0
    if start:
        opening = round(sum(r.amount for r in rows
                            if r.date < start and _is_credit_side(r, cp.kind)), 2)

    period_rows = [r for r in rows
                   if (not start or r.date >= start) and (not end or r.date <= end)]

    credit = round(sum(r.amount for r in period_rows if _is_credit_side(r, cp.kind)), 2)
    cash = round(sum(r.amount for r in period_rows
                     if not _is_credit_side(r, cp.kind)), 2)

    closing = round(opening + credit, 2)

    return {
        "ok": True,
        "counterparty": info,
        "start": start or "（全部）",
        "end": end or "（全部）",
        "opening_balance": opening,
        "period_credit": credit,      # 本期挂账（赊销/赊购）
        "period_cash": cash,          # 本期现结
        "closing_balance": closing,   # 期末应收/应付（估算）
        "entries": [r.as_dict() for r in period_rows],
        "note": "期末余额按「期初 + 本期赊销/赊购」估算；"
                "本系统尚未做逐笔核销，实际以双方对账为准。",
    }


def _is_credit_side(r: LedgerEntry, kind: str) -> bool:
    """判断这笔是否形成「挂账」（客户→应收，供应商→应付）。"""
    if kind == "customer":
        return r.debit_account == "应收账款"
    return r.credit_account == "应付账款"


def counterparty_statement_text(counterparty_id: int,
                                start: str | None = None,
                                end: str | None = None) -> str:
    """把对账单渲染成纯文本，方便打印/发给对方。"""
    d = counterparty_statement(counterparty_id, start, end)
    if not d.get("ok"):
        return d.get("message", "生成失败")

    cp = d["counterparty"]
    role = "客户" if cp["kind"] == "customer" else "供应商"
    kind_cn = "应收" if cp["kind"] == "customer" else "应付"

    lines = [
        "=" * 46,
        f"　对 账 单（{role}）",
        "=" * 46,
        f"单位名称：{cp['name']}",
        f"对账期间：{d['start']} ~ {d['end']}",
        "-" * 46,
        f"期初{kind_cn}余额：{d['opening_balance']:>16,.2f}",
        f"本期新增（赊{'销' if cp['kind'] == 'customer' else '购'}）：{d['period_credit']:>14,.2f}",
        f"本期现结：{d['period_cash']:>22,.2f}",
        "-" * 46,
        f"期末{kind_cn}余额：{d['closing_balance']:>16,.2f}",
        "=" * 46,
        "",
        "本期交易明细：",
    ]
    for r in d["entries"]:
        lines.append(f"  {r['date']}  {r['description'][:26]:28s} {r['amount']:>12,.2f}")
    lines += ["", f"注：{d['note']}", "", "　　　　　　　　　　请核对后盖章回传"]
    return "\n".join(lines)
