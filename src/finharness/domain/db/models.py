"""账本数据模型：每笔交易一条记录，带借贷双方科目。

复式记账核心：每笔业务同时记"借方"和"贷方"，金额相等、方向相反。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class LedgerEntry(Base):
    """一笔账目。

    状态流转（对应真实的分工制衡）：
        出纳录入 → pending（待审核）
        会计审核 → approved（已入账）
        会计驳回 → rejected（已驳回）

    **只有 approved 的账目才计入报表** —— 这条规则让「出纳管钱、会计管账」
    的职务分离真正生效。
    """

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10))  # 记账日期 YYYY-MM-DD
    amount: Mapped[float] = mapped_column(Float)  # 金额（正数）
    description: Mapped[str] = mapped_column(String(200))  # 摘要，如"打车"
    # 科目由【会计】在审核时指定；出纳录入时为空
    category: Mapped[str] = mapped_column(String(50), default="")
    entry_type: Mapped[str] = mapped_column(String(20), default="expense")  # income / expense / asset
    debit_account: Mapped[str] = mapped_column(String(50), default="")  # 借方科目
    credit_account: Mapped[str] = mapped_column(String(50), default="")  # 贷方科目
    # 往来单位（客户/供应商）—— 规范做法是关联到档案，而不是把名字塞在摘要里
    counterparty_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 凭证附件：真实记账必须有原始凭证（发票/回单），白条入账税务不认
    attachment: Mapped[str] = mapped_column(String(200), default="")
    # 出纳录入时选的收付款方式（会计审核生成分录时用）
    payment: Mapped[str] = mapped_column(String(20), default="银行存款")

    # ---- 审核流转 ----
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    reject_reason: Mapped[str] = mapped_column(String(200), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def as_dict(self) -> dict:
        status_cn = {"pending": "待审核", "approved": "已入账", "rejected": "已驳回"}
        return {
            "id": self.id,
            "date": self.date,
            "amount": self.amount,
            "description": self.description,
            "category": self.category,
            "entry_type": self.entry_type,
            "debit_account": self.debit_account,
            "credit_account": self.credit_account,
            "counterparty_id": self.counterparty_id,
            "attachment": self.attachment,
            "payment": self.payment,
            "status": self.status,
            "status_cn": status_cn.get(self.status, self.status),
            "reject_reason": self.reject_reason,
        }


class Counterparty(Base):
    """往来单位档案：客户 / 供应商。

    为什么单独建表，而不是把名字写在摘要里：
    - 摘要里的名字会写错、会不统一（"恒信商贸" vs "恒信公司"）
    - 建了档案才能统计「这个客户累计欠多少」「欠了多久（账龄）」
    - 记账时从下拉选，比手打更准
    """

    __tablename__ = "counterparties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(10), default="customer")  # customer / supplier
    contact: Mapped[str] = mapped_column(String(50), default="")   # 联系人
    phone: Mapped[str] = mapped_column(String(30), default="")
    note: Mapped[str] = mapped_column(String(200), default="")

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "kind_cn": "客户" if self.kind == "customer" else "供应商",
            "contact": self.contact,
            "phone": self.phone,
            "note": self.note,
        }


class CashCheck(Base):
    """库存现金盘点记录。

    出纳的基本功：每天/每月实际点钞票，和账面比。对不上就是盘盈（长款）或盘亏（短款），
    必须查明原因——短款甚至要出纳自己赔。这是"账实相符"的核心动作。
    """

    __tablename__ = "cash_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10))            # 盘点日期
    book_amount: Mapped[float] = mapped_column(Float)        # 账面应有现金
    actual_amount: Mapped[float] = mapped_column(Float)      # 实际盘点数
    difference: Mapped[float] = mapped_column(Float)         # 差额 = 实盘 − 账面
    result: Mapped[str] = mapped_column(String(20))          # 相符 / 盘盈 / 盘亏
    note: Mapped[str] = mapped_column(String(200), default="")
    checked_by: Mapped[str] = mapped_column(String(30), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "date": self.date,
            "book_amount": round(self.book_amount, 2),
            "actual_amount": round(self.actual_amount, 2),
            "difference": round(self.difference, 2),
            "result": self.result,
            "note": self.note,
            "checked_by": self.checked_by,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
        }


class BankReconciliation(Base):
    """银行存款余额调节表。

    银行对账单余额 和企业账面余额 通常对不上，因为有「未达账项」：
      企业已记、银行未记 / 银行已记、企业未记
    通过加减这些未达账项，两边调成一致，才能确认银行账没错。
    这是会计每月必做的一张表。
    """

    __tablename__ = "bank_reconciliations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(7))           # YYYY-MM
    statement_balance: Mapped[float] = mapped_column(Float)  # 银行对账单余额
    book_balance: Mapped[float] = mapped_column(Float)       # 企业账面余额
    # 未达账项明细，JSON 字符串存 [{"desc": "...", "amount": 5000}, ...]
    items_added: Mapped[str] = mapped_column(String(2000), default="[]")
    items_subtracted: Mapped[str] = mapped_column(String(2000), default="[]")
    reconciled: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def as_dict(self) -> dict:
        import json

        def load(s: str) -> list:
            try:
                return json.loads(s or "[]")
            except Exception:
                return []

        added, sub = load(self.items_added), load(self.items_subtracted)
        adjusted = self.statement_balance + sum(x["amount"] for x in added) \
            - sum(x["amount"] for x in sub)
        return {
            "id": self.id,
            "period": self.period,
            "statement_balance": round(self.statement_balance, 2),
            "book_balance": round(self.book_balance, 2),
            "items_added": added,
            "items_subtracted": sub,
            "adjusted_balance": round(adjusted, 2),
            "difference": round(adjusted - self.book_balance, 2),
            "reconciled": self.reconciled,
            "note": self.note,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
        }


class Company(Base):
    """企业档案：这家公司是干什么的。

    刻意只保留「业务描述」层面的信息，不含任何工商登记类字段
    （统一社会信用代码、法人、注册地址、员工人数等）——本项目用的是仿真数据，
    写上那些字段会像真实企业，造成误导。
    """

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100))
    industry: Mapped[str] = mapped_column(String(50), default="")  # 所属行业
    main_business: Mapped[str] = mapped_column(String(300), default="")  # 主营业务
    description: Mapped[str] = mapped_column(String(1000), default="")  # 业务说明

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "industry": self.industry,
            "main_business": self.main_business,
            "description": self.description,
        }


class ClosedPeriod(Base):
    """已结账的会计期间。

    月末结账后，该期间不能再改账——这是会计的基本纪律：
    账结了就是"封账"，要改就得反结账。
    """

    __tablename__ = "closed_periods"

    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # YYYY-MM
    closed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    note: Mapped[str] = mapped_column(String(200), default="")


class Conversation(Base):
    """一个会话（一次对话）。"""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(100), default="新对话")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M"),
        }


class Message(Base):
    """会话里的一条消息。存下来，前端才能看到历史记录。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(32), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user / assistant
    content: Mapped[str] = mapped_column(String(8000))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at.strftime("%H:%M"),
        }
