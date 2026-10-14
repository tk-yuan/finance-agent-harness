"""记账工具：出纳侧 + 会计侧。

## 这个文件里最重要的不是功能，是三条**代码级约束**

财务系统的灵魂是内控。内控不能靠提示词说"请不要"，必须写死在代码里：

1. **必须有凭证才能记账**。没发票/回单的支出，税务上不认（白条入账）。
   `submit_voucher` 里校验 `attachment` 是否存在，不存在直接拒绝。

2. **Agent 提交的单据只能进「待审核」**，不能直接入账。
   这是「出纳登记 → 会计审核」两道工序的代码化。
   只有 `post_voucher` 能把状态推到 `approved`，而**它是需要人工审批的高风险工具**。

3. **复式记账的借贷方向由代码决定**，不让模型自由发挥。
   支出 = 借费用科目 / 贷支付方式；收入 = 借收款方式 / 贷收入科目；
   购置固定资产是**资产内部转换**，不进当期费用。
   —— 这几条规则写错一个字，凭证就是错的，而凭证错了报表就全错。

## 为什么把「出纳」和「会计」分开成两组工具

不是分组好看，是**它们对应不同的权限和后果**：

| | 出纳侧 | 会计侧 |
|---|---|---|
| 能做什么 | 登记、撤回自己的单据 | 审核、定科目、驳回 |
| 后果 | 只是草稿 | **一旦入账就进报表，有法律后果** |
| 工具性质 | 普通写操作 | `post_voucher` 走**阻塞式人工审批** |
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from ..config import DATA_DIR
from ..domain import accounts as A
from ..domain.db._session import session
from ..domain.db.models import LedgerEntry
from ..runtime.tools import Tool, ToolRegistry

ATTACH_DIR = DATA_DIR / "attachments"


def _today() -> str:
    """日期是确定性信息，由代码给出，不让模型猜。"""
    return datetime.now().strftime("%Y-%m-%d")


def make_entries(entry_type: str, category: str, payment: str) -> tuple[str, str]:
    """按复式记账规则算借贷方向。

    这是整个财务领域最容易做错、也最不能交给 LLM 的一步——
    借贷方向错了，金额再对，凭证也是废的。
    """
    if entry_type == "expense":
        return category, payment          # 借费用 / 贷支付
    if entry_type == "income":
        return payment, category          # 借收款 / 贷收入
    if entry_type == "asset":
        return "固定资产", payment        # 资产内部转换，不进当期损益
    raise ValueError(f"未知收付方向：{entry_type}")


# ═══════════════ 出纳侧 ═══════════════

def submit_voucher(amount: float, description: str, voucher: str,
                   entry_type: str = "expense", counterparty_name: str = "",
                   date: str = "", payment: str = "银行存款") -> dict:
    """提交一张待审核单据（出纳的活）。

    对应界面上的「🧾 智能出纳」。注意它**不生成借贷分录**——
    科目是会计审核时定的，出纳只登记"钱动了"这个事实。
    """
    if not voucher or not voucher.strip():
        return {"error": "不能记账：没有凭证。记账必须有发票/收据/回单——请先上传凭证再提交。"}
    safe = Path(voucher).name
    if not (ATTACH_DIR / safe).is_file():
        return {"error": f"不能记账：找不到凭证文件「{safe}」。"}
    if amount <= 0:
        return {"error": "金额必须大于 0。"}
    if entry_type not in ("expense", "income", "asset"):
        return {"error": f"收付方向只能是 expense / income / asset，收到的是 {entry_type}。"}

    with session() as db:
        row = LedgerEntry(
            date=date or _today(),
            amount=round(amount, 2),
            description=description.strip()[:100],
            category="",                 # 科目留给会计
            entry_type=entry_type,
            debit_account="", credit_account="",
            attachment=safe, payment=payment,
            status="pending",            # ★ 只进待审核，不直接入账
        )
        db.add(row)
        db.flush()
        rid = row.id

    return {
        "ok": True, "entry_id": rid,
        "status": "pending",
        "message": f"已登记待审核单据 #{rid}：{amount:,.2f} 元（凭证已附）",
        "next": "需会计审核后才能计入报表",
    }


def withdraw_voucher(entry_id: int, reason: str = "") -> dict:
    """撤回**待审核**单据（出纳发现自己录错了）。

    只能撤未审核的——已入账的要走会计的反审核，这是内控边界。
    """
    with session() as db:
        row = db.get(LedgerEntry, entry_id)
        if row is None:
            return {"error": f"单据 #{entry_id} 不存在"}
        if row.status != "pending":
            return {"error": f"单据 #{entry_id} 当前是「{row.status}」，"
                             f"只有待审核的才能撤回。已入账的请找会计走反审核。"}
        db.delete(row)
    return {"ok": True, "withdrawn": entry_id, "reason": reason}


def list_my_entries(status: str = "") -> dict:
    """出纳查自己提过的单据。"""
    with session() as db:
        stmt = select(LedgerEntry).order_by(LedgerEntry.id.desc()).limit(30)
        if status:
            stmt = stmt.where(LedgerEntry.status == status)
        rows = db.scalars(stmt).all()
    return {"count": len(rows), "entries": [r.as_dict() for r in rows]}


# ═══════════════ 会计侧 ═══════════════

def list_pending() -> dict:
    """待审核队列。"""
    with session() as db:
        rows = db.scalars(
            select(LedgerEntry).where(LedgerEntry.status == "pending")
            .order_by(LedgerEntry.id)
        ).all()
    return {"count": len(rows), "pending": [r.as_dict() for r in rows]}


def post_voucher(entry_id: int, category: str, note: str = "") -> dict:
    """**审核入账**（会计的活）——这是全系统风险最高的动作。

    一旦入账，这笔就进报表、有法律后果，而且不能直接删（只能反审核）。

    注意：这个工具在注册时被标记为 `requires_approval=True`，
    所以它**不是**被模型调用就直接执行——会先阻塞、等人工批准。
    这正是「代 AI 记账需要人把关」的机制化表达。
    """
    with session() as db:
        row = db.get(LedgerEntry, entry_id)
        if row is None:
            return {"error": f"单据 #{entry_id} 不存在"}
        if row.status != "pending":
            return {"error": f"单据 #{entry_id} 当前是「{row.status}」，不能重复入账"}
        if category not in A.EXPENSE_ACCOUNTS + A.INCOME_ACCOUNTS + ["固定资产"]:
            return {"error": f"科目「{category}」不在科目表内"}

        debit, credit = make_entries(row.entry_type, category, row.payment)
        row.category = category
        row.debit_account = debit
        row.credit_account = credit
        row.status = "approved"
        row.reviewed_at = datetime.now()

    return {
        "ok": True, "entry_id": entry_id, "status": "approved",
        "voucher": {"借": debit, "贷": credit, "金额": row.amount},
        "message": f"单据 #{entry_id} 已入账：借 {debit} / 贷 {credit} {row.amount:,.2f} 元",
    }


def reject_voucher(entry_id: int, reason: str) -> dict:
    """驳回单据（会计发现出纳录错了）。"""
    if not reason.strip():
        return {"error": "驳回必须写理由——这是内控留痕，不能省。"}
    with session() as db:
        row = db.get(LedgerEntry, entry_id)
        if row is None:
            return {"error": f"单据 #{entry_id} 不存在"}
        if row.status != "pending":
            return {"error": f"单据 #{entry_id} 当前是「{row.status}」，不能驳回"}
        row.status = "rejected"
        row.reject_reason = reason[:200]
        row.reviewed_at = datetime.now()
    return {"ok": True, "entry_id": entry_id, "status": "rejected", "reason": reason}


def recategorize(entry_id: int, category: str) -> dict:
    """改已入账单据的科目（会计的纠错权，会重算借贷分录）。"""
    with session() as db:
        row = db.get(LedgerEntry, entry_id)
        if row is None:
            return {"error": f"单据 #{entry_id} 不存在"}
        if row.status != "approved":
            return {"error": f"单据 #{entry_id} 还没入账，改科目请走审核流程"}
        old = row.category
        debit, credit = make_entries(row.entry_type, category, row.payment)
        row.category, row.debit_account, row.credit_account = category, debit, credit
    return {"ok": True, "entry_id": entry_id, "from": old, "to": category,
            "voucher": {"借": debit, "贷": credit}}


# ═══════════════ 查询（只读） ═══════════════

def query_entries(category: str = "", start_date: str = "", end_date: str = "",
                  entry_type: str = "") -> dict:
    """查账。**只查已入账的**（approved）——待审核的还不算数。"""
    with session() as db:
        stmt = select(LedgerEntry).where(LedgerEntry.status == "approved")
        if category:
            stmt = stmt.where(LedgerEntry.category == category)
        if start_date:
            stmt = stmt.where(LedgerEntry.date >= start_date)
        if end_date:
            stmt = stmt.where(LedgerEntry.date <= end_date)
        if entry_type:
            stmt = stmt.where(LedgerEntry.entry_type == entry_type)
        rows = db.scalars(stmt.order_by(LedgerEntry.date)).all()

    if not rows:
        return {"count": 0, "message": "没有符合条件的已入账账目"}

    income = round(sum(r.amount for r in rows if r.entry_type == "income"), 2)
    expense = round(sum(r.amount for r in rows if r.entry_type == "expense"), 2)
    by_cat: dict[str, float] = {}
    for r in rows:
        if r.entry_type == "expense":
            by_cat[r.category] = round(by_cat.get(r.category, 0) + r.amount, 2)

    return {
        "count": len(rows),
        "income_total": income,
        "expense_total": expense,
        "net": round(income - expense, 2),
        "expense_by_category": dict(sorted(by_cat.items(), key=lambda kv: -kv[1])),
        "recent": [r.as_dict() for r in rows[-10:]],
    }


# ═══════════════ 注册 ═══════════════

def register_ledger_tools(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="submit_voucher",
        description="提交一张待审核的记账单据（出纳的活）。必须先有凭证文件，否则会被拒绝。"
                    "提交后状态是「待审核」，要等会计审核才计进报表。",
        parameters={"type": "object", "properties": {
            "amount": {"type": "number", "description": "金额（正数，元）"},
            "description": {"type": "string", "description": "摘要，如「买办公用品」"},
            "voucher": {"type": "string", "description": "凭证文件名（必须先上传）"},
            "entry_type": {"type": "string", "enum": ["expense", "income", "asset"],
                           "description": "付款/收款/购置资产", "default": "expense"},
            "counterparty_name": {"type": "string", "description": "往来单位名称"},
            "date": {"type": "string", "description": "日期 YYYY-MM-DD，不传用今天"},
            "payment": {"type": "string", "default": "银行存款"},
        }, "required": ["amount", "description", "voucher"]},
        handler=submit_voucher, read_only=False, category="ledger",
    ))
    registry.register(Tool(
        name="withdraw_voucher",
        description="撤回一张**待审核**的单据（出纳发现自己录错了）。已入账的不能撤，要走会计反审核。",
        parameters={"type": "object", "properties": {
            "entry_id": {"type": "integer"}, "reason": {"type": "string"},
        }, "required": ["entry_id"]},
        handler=withdraw_voucher, read_only=False, category="ledger",
    ))
    registry.register(Tool(
        name="list_pending",
        description="查看待审核队列（会计的活）。返回所有 status=pending 的单据。",
        parameters={"type": "object", "properties": {}},
        handler=list_pending, read_only=True, category="ledger",
    ))
    registry.register(Tool(
        name="post_voucher",
        description="**审核入账**（会计的活）。这是有法律后果的高风险动作——"
                    "入账后即计入报表，且不能直接删除。调用本工具会触发人工审批。",
        parameters={"type": "object", "properties": {
            "entry_id": {"type": "integer"},
            "category": {"type": "string", "description": "会计定的科目"},
            "note": {"type": "string", "description": "审核备注"},
        }, "required": ["entry_id", "category"]},
        handler=post_voucher, read_only=False, requires_approval=True, category="ledger",
    ))
    registry.register(Tool(
        name="reject_voucher",
        description="驳回一张待审核单据。必须写理由（内控留痕）。",
        parameters={"type": "object", "properties": {
            "entry_id": {"type": "integer"}, "reason": {"type": "string"},
        }, "required": ["entry_id", "reason"]},
        handler=reject_voucher, read_only=False, category="ledger",
    ))
    registry.register(Tool(
        name="recategorize",
        description="修改已入账单据的科目（会计纠错），会自动重算借贷分录。",
        parameters={"type": "object", "properties": {
            "entry_id": {"type": "integer"}, "category": {"type": "string"},
        }, "required": ["entry_id", "category"]},
        handler=recategorize, read_only=False, category="ledger",
    ))
    registry.register(Tool(
        name="query_entries",
        description="查账。只返回**已入账**的账目，并给出收入/支出合计与按科目汇总。",
        parameters={"type": "object", "properties": {
            "category": {"type": "string"}, "start_date": {"type": "string"},
            "end_date": {"type": "string"},
            "entry_type": {"type": "string", "enum": ["income", "expense", "asset"]},
        }},
        handler=query_entries, read_only=True, category="ledger",
    ))
